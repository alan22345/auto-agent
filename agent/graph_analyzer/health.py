"""Per-file and repo-level maintainability scoring (ADR-016 quality layer §6).

Maintainability Index formula (0..100, clamped):
    MI = 100 - complexity_density * 30 - dead_code_ratio * 20 - fan_out_penalty

Where:
    complexity_density  = sum(cyclomatic) / file_loc  (0 if loc == 0)
    dead_code_ratio     = dead_symbols_in_file / total_fn_class_nodes_in_file,
                         capped at 1.0; any unused_file finding for the file
                         forces ratio to 1.0 (whole file considered dead).
    fan_out_penalty     = min(20.0, 2.0 * cross_area_outgoing_edges)

Bands:
    good     — MI >= 70
    moderate — MI >= 40 (and < 70)
    poor     — MI <  40

``crap`` (untested-complexity risk, change-risk anti-patterns) is deferred:
it requires per-function coverage data that the graph does not yet ingest.
It is always None.

Fan-out uses cross-area edges: edges whose source node lives in the file and
whose target node lives in a different area. Module:/unresolved targets that
do not resolve to a known node are ignored.
"""

from __future__ import annotations

from shared.types import FileHealth, Node, RepoGraphBlob, RepoHealth


def compute_health(
    blob: RepoGraphBlob,
    file_loc: dict[str, int],
    file_cyclomatic_total: dict[str, int],
) -> tuple[list[FileHealth], RepoHealth]:
    """Compute per-file FileHealth records and a RepoHealth summary.

    Pure function — no I/O.

    Parameters
    ----------
    blob:
        The fully-assembled analysis blob (nodes, edges, dead_code, clones,
        cycles, hotspots already populated).
    file_loc:
        Mapping of relative file path → line count (from count_loc).
    file_cyclomatic_total:
        Mapping of relative file path → sum of cyclomatic complexity over all
        function nodes in that file.

    Returns
    -------
    (file_health, repo_health)
        ``file_health`` sorted by file path ascending.
        ``repo_health`` is the LOC-weighted summary.
    """
    # ------------------------------------------------------------------
    # 1. Group function+class nodes by file.
    # ------------------------------------------------------------------
    nodes_by_file: dict[str, list[Node]] = {}
    for n in blob.nodes:
        if n.kind in {"function", "class"} and n.file is not None:
            nodes_by_file.setdefault(n.file, []).append(n)

    if not nodes_by_file:
        repo_health = RepoHealth(
            score=100.0,
            clone_count=len(blob.clones),
            cycle_count=len(blob.cycles),
            dead_count=len(blob.dead_code),
            hotspot_count=len(blob.hotspots),
        )
        return [], repo_health

    # ------------------------------------------------------------------
    # 2. Build node-area lookup (for fan-out penalty).
    # ------------------------------------------------------------------
    node_area: dict[str, str] = {n.id: n.area for n in blob.nodes}

    # ------------------------------------------------------------------
    # 3. Build file → set of dead-symbol counts (unused_export).
    # ------------------------------------------------------------------
    # Count unused_export findings per file.
    unused_export_per_file: dict[str, int] = {}
    unused_files: set[str] = set()
    for dc in blob.dead_code:
        if dc.kind == "unused_export" and dc.file is not None:
            unused_export_per_file[dc.file] = unused_export_per_file.get(dc.file, 0) + 1
        elif dc.kind == "unused_file":
            # target looks like "file:path/to/file.py"
            path = dc.target
            if path.startswith("file:"):
                path = path[len("file:") :]
            unused_files.add(path)
            # Also check dc.file if set
            if dc.file is not None:
                unused_files.add(dc.file)

    # ------------------------------------------------------------------
    # 5. Build file → cross-area outgoing edge count.
    # ------------------------------------------------------------------
    # Build source-node to file mapping.
    node_file: dict[str, str] = {n.id: n.file for n in blob.nodes if n.file is not None}

    cross_area_out_per_file: dict[str, int] = {}
    for e in blob.edges:
        src_file = node_file.get(e.source)
        if src_file is None:
            continue
        src_area = node_area.get(e.source)
        tgt_area = node_area.get(e.target)
        if src_area is None or tgt_area is None:
            continue
        if src_area != tgt_area:
            cross_area_out_per_file[src_file] = cross_area_out_per_file.get(src_file, 0) + 1

    # ------------------------------------------------------------------
    # 6. Compute FileHealth for each file with function/class nodes.
    # ------------------------------------------------------------------
    file_health_list: list[FileHealth] = []

    for file, fn_class_nodes in nodes_by_file.items():
        loc = file_loc.get(file, 0)
        total_nodes = len(fn_class_nodes)

        # complexity_density
        cyc_total = file_cyclomatic_total.get(file, 0)
        complexity_density = cyc_total / loc if loc > 0 else 0.0

        # dead_code_ratio
        if file in unused_files:
            dead_code_ratio = 1.0
        else:
            dead_exports = unused_export_per_file.get(file, 0)
            dead_code_ratio = min(1.0, dead_exports / total_nodes) if total_nodes > 0 else 0.0

        # fan_out_penalty
        cross_area_out = cross_area_out_per_file.get(file, 0)
        fan_out_penalty = min(20.0, 2.0 * cross_area_out)

        # maintainability_index
        mi = max(
            0.0,
            min(
                100.0, 100.0 - complexity_density * 30.0 - dead_code_ratio * 20.0 - fan_out_penalty
            ),
        )

        # band
        if mi >= 70.0:
            band: str = "good"
        elif mi >= 40.0:
            band = "moderate"
        else:
            band = "poor"

        file_health_list.append(
            FileHealth(
                file=file,
                maintainability_index=mi,
                band=band,  # type: ignore[arg-type]
                crap=None,
            )
        )

    # Sort deterministically by file path.
    file_health_list.sort(key=lambda fh: fh.file)

    # ------------------------------------------------------------------
    # 7. Compute RepoHealth (LOC-weighted mean).
    # ------------------------------------------------------------------
    total_weight = sum(file_loc.get(fh.file, 0) for fh in file_health_list)
    if total_weight > 0:
        weighted_sum = sum(
            fh.maintainability_index * file_loc.get(fh.file, 0) for fh in file_health_list
        )
        repo_score = weighted_sum / total_weight
    elif file_health_list:
        repo_score = sum(fh.maintainability_index for fh in file_health_list) / len(
            file_health_list
        )
    else:
        repo_score = 100.0

    repo_health = RepoHealth(
        score=repo_score,
        clone_count=len(blob.clones),
        cycle_count=len(blob.cycles),
        dead_count=len(blob.dead_code),
        hotspot_count=len(blob.hotspots),
    )

    return file_health_list, repo_health
