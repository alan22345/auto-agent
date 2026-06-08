"""Per-file maintainability + repo-level composite health scoring
(ADR-016 quality layer §6).

Per-file Maintainability Index (0..100, clamped):
    MI = 100 - complexity_density * 30 - dead_code_ratio * 20 - fan_out_penalty
    complexity_density  = sum(cyclomatic) / file_loc  (0 if loc == 0)
    dead_code_ratio     = dead_symbols_in_file / total_fn_class_nodes_in_file,
                         capped at 1.0; any unused_file finding forces 1.0.
    fan_out_penalty     = min(20.0, 2.0 * cross_area_outgoing_edges)
    Bands: good >= 70, moderate >= 40, poor < 40.

Repo health is a COMPOSITE — the weighted mean of five sub-scores, each
0..100 (higher = better). Unlike the old score (which was just the
maintainability mean and ignored duplication/cycles/dead-volume), the
composite moves with every dimension, and each sub-score is surfaced so the
number is interpretable.

    maintainability  LOC-weighted mean of per-file MI
    duplication      100 * (1 - cloned_LOC / total_LOC)
    dead_code        100 * (1 - real_dead / (fn_class_nodes + files))
                     real_dead EXCLUDES test-only findings (reasons in
                     _TEST_ONLY_REASONS) — those are used, just by tests.
    cycles           100 * (1 - nodes_in_cycles / fn_class_nodes)
    coupling         100 * (1 - cross_area_edges / total_edges * _K_COUPLING)

    score = 0.30*maintainability + 0.25*dead_code + 0.20*duplication
          + 0.15*coupling + 0.10*cycles

All ratios are clamped to [0, 1]; every denominator is guarded (0 → no
penalty). Weights, _K_COUPLING, and the MI coefficients are explicit,
heuristic, and tunable — not empirically validated.

``crap`` (untested-complexity risk) is deferred: it needs per-function
coverage the graph does not ingest. Always None.
"""

from __future__ import annotations

from shared.types import FileHealth, Node, RepoGraphBlob, RepoHealth

# Composite weights — sum to 1.0. Heuristic and tunable.
_W_MAINTAINABILITY = 0.30
_W_DEAD_CODE = 0.25
_W_DUPLICATION = 0.20
_W_COUPLING = 0.15
_W_CYCLES = 0.10

# Coupling penalty scale: cross-area edge fraction * this, clamped to 1.0.
_K_COUPLING = 3.0

# Dead-code finding reasons that are NOT real dead code (used by tests).
_TEST_ONLY_REASONS = frozenset(
    {"referenced only by tests", "imported only by tests"}
)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


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

    # ------------------------------------------------------------------
    # 8. Per-dimension sub-scores + composite (0..100, higher = better).
    # ------------------------------------------------------------------
    maintainability = repo_score
    fn_class_total = sum(len(v) for v in nodes_by_file.values())
    file_count = sum(1 for n in blob.nodes if n.kind == "file")

    # Duplication — fraction of LOC covered by clone instances.
    total_loc = sum(file_loc.values())
    cloned_loc = sum(
        max(0, inst.line_end - inst.line_start + 1)
        for cg in blob.clones
        for inst in cg.instances
    )
    duplication = (
        100.0 * (1.0 - _clamp01(cloned_loc / total_loc)) if total_loc > 0 else 100.0
    )

    # Dead code — real (non-test-only) findings over symbols + files.
    real_dead = sum(1 for dc in blob.dead_code if dc.reason not in _TEST_ONLY_REASONS)
    dead_denom = fn_class_total + file_count
    dead_code_score = (
        100.0 * (1.0 - _clamp01(real_dead / dead_denom)) if dead_denom > 0 else 100.0
    )

    # Cycles — fraction of symbols tangled in a dependency cycle.
    cycle_members: set[str] = set()
    for cyc in blob.cycles:
        cycle_members.update(cyc.members)
    cycles_score = (
        100.0 * (1.0 - _clamp01(len(cycle_members) / fn_class_total))
        if fn_class_total > 0
        else 100.0
    )

    # Coupling — fraction of edges crossing area boundaries, scaled.
    cross_area_edges = sum(
        1
        for e in blob.edges
        if (sa := node_area.get(e.source)) is not None
        and (ta := node_area.get(e.target)) is not None
        and sa != ta
    )
    total_edges = len(blob.edges)
    coupling = (
        100.0 * (1.0 - _clamp01(cross_area_edges / total_edges * _K_COUPLING))
        if total_edges > 0
        else 100.0
    )

    composite = (
        _W_MAINTAINABILITY * maintainability
        + _W_DEAD_CODE * dead_code_score
        + _W_DUPLICATION * duplication
        + _W_COUPLING * coupling
        + _W_CYCLES * cycles_score
    )

    repo_health = RepoHealth(
        score=composite,
        clone_count=len(blob.clones),
        cycle_count=len(blob.cycles),
        dead_count=len(blob.dead_code),
        hotspot_count=len(blob.hotspots),
        maintainability=maintainability,
        duplication=duplication,
        dead_code=dead_code_score,
        cycles=cycles_score,
        coupling=coupling,
    )

    return file_health_list, repo_health
