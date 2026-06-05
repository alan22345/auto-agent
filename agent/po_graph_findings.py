"""Code-graph findings summarizer for the Product Owner prompt.

Converts a :class:`shared.types.RepoGraphBlob` into a markdown 'Code Graph
Findings' section that the PO agent can use as high-signal seed input when
proposing refactor tasks.

## Churn-gating rationale

The churn gate is the key quality filter here: the code graph records every
complex or duplicated symbol it finds, regardless of whether that code is
actively touched.  Proposing refactors for complex-but-frozen code (high
cyclomatic, but never committed to) wastes engineering cycles — a function
that hasn't changed in two years is risky to rewrite and unlikely to be a
practical bottleneck.

We therefore restrict complex-function and clone findings to files that are
ALSO hotspots (score >= hotspot_min_score).  Hotspot score = churn x
complexity_density, so a file only qualifies if it is BOTH actively changing
AND structurally complex.  Import cycles and dead code are NOT churn-gated:
cycles are a correctness/build concern, and dead code is a cleanliness
concern — neither benefits from the churn filter.

Only functions in hotspot_files, and clone groups with at least one instance
in hotspot_files, are surfaced.  Everything else is silently omitted.
"""

from __future__ import annotations

import logging

from shared.types import RepoGraphBlob

log = logging.getLogger(__name__)


def summarize_graph_findings(
    blob: RepoGraphBlob,
    *,
    hotspot_min_score: float = 50.0,
) -> str:
    """Render a markdown 'Code Graph Findings' section for the PO prompt.

    Produces ranked, churn-gated, evidence-cited findings so the PO agent
    can propose specific refactor tasks grounded in the code graph rather
    than guessing.

    Returns an empty string if there are no notable findings (all sections
    empty), so callers can safely check ``if graph_findings`` before adding
    it to the prompt.

    The output is deterministic: given the same blob the function always
    returns the same string.
    """
    sections: list[str] = []

    # --- Hotspots -----------------------------------------------------------
    qualifying_hotspots = [h for h in blob.hotspots if h.score >= hotspot_min_score]
    top_hotspots = qualifying_hotspots[:10]
    hotspot_files: set[str] = {h.file for h in top_hotspots}

    if top_hotspots:
        lines = ["### Hotspots (churn x complexity - actively changing AND complex)\n"]
        for h in top_hotspots:
            lines.append(
                f"- `{h.file}` — score {h.score:.1f}, complexity_density"
                f" {h.complexity_density:.3f}, trend: {h.trend}"
            )
        sections.append("\n".join(lines))

    # --- Complex functions in hotspots (CHURN-GATED) ------------------------
    complex_in_hotspots = [
        n
        for n in blob.nodes
        if n.kind == "function"
        and n.file in hotspot_files
        and (
            (n.cyclomatic is not None and n.cyclomatic >= 20)
            or (n.cognitive is not None and n.cognitive >= 15)
        )
    ]
    # Sort deterministically: cyclomatic desc, then node id asc
    complex_in_hotspots.sort(key=lambda n: (-(n.cyclomatic or 0), -(n.cognitive or 0), n.id))

    if complex_in_hotspots:
        lines = ["### Complex functions in hotspot files (churn-gated — high refactor ROI)\n"]
        for n in complex_in_hotspots:
            cyc = f"cyclomatic={n.cyclomatic}" if n.cyclomatic is not None else ""
            cog = f"cognitive={n.cognitive}" if n.cognitive is not None else ""
            metrics = ", ".join(filter(None, [cyc, cog]))
            loc = (
                f"{n.file}:{n.line_start}-{n.line_end}"
                if n.line_start is not None and n.line_end is not None
                else n.file or "(unknown file)"
            )
            lines.append(f"- `{n.id}` ({metrics}) @ {loc}")
        sections.append("\n".join(lines))

    # --- Code clones (CHURN-GATED + significant) ----------------------------
    significant_clones = [
        g
        for g in blob.clones
        if (len(g.instances) >= 3 or g.token_len >= 80)
        and any(inst.file in hotspot_files for inst in g.instances)
    ]
    # Sort deterministically: token_len desc, then id asc
    significant_clones.sort(key=lambda g: (-g.token_len, g.id))

    if significant_clones:
        lines = [
            "### Code clones in hotspot files (churn-gated + significant — extract or deduplicate)\n"
        ]
        for g in significant_clones:
            instance_lines = "; ".join(
                f"`{inst.file}`:{inst.line_start}-{inst.line_end}" for inst in g.instances
            )
            lines.append(
                f"- Clone group `{g.id}` — {g.token_len} tokens,"
                f" {len(g.instances)} instances: {instance_lines}"
            )
        sections.append("\n".join(lines))

    # --- Import cycles (always — correctness) --------------------------------
    if blob.cycles:
        lines = ["### Import cycles (always reported — correctness / build risk)\n"]
        for c in blob.cycles:
            members_str = " → ".join(c.members)
            if c.closing_edges:
                edge = c.closing_edges[0]
                closing = f" (closing edge: `{edge.file}`:{edge.line})"
            else:
                closing = ""
            lines.append(f"- Cycle `{c.id}` [{c.kind}]: {members_str}{closing}")
        sections.append("\n".join(lines))

    # --- Dead code (always — cleanliness / correctness) ----------------------
    if blob.dead_code:
        # Group by kind for readability
        by_kind: dict[str, list] = {}
        for finding in blob.dead_code:
            by_kind.setdefault(finding.kind, []).append(finding)

        lines = ["### Dead code (always reported — cleanliness / correctness)\n"]
        for kind in sorted(by_kind):
            lines.append(f"**{kind}:**")
            for f in by_kind[kind]:
                file_note = f" (`{f.file}`)" if f.file else ""
                lines.append(f"  - `{f.target}`{file_note} — {f.reason}")
        sections.append("\n".join(lines))

    # --- Health summary ------------------------------------------------------
    if blob.health is not None or blob.file_health:
        health_lines = ["### Repo health summary\n"]
        if blob.health is not None:
            health_lines.append(f"- Overall maintainability score: **{blob.health.score:.1f}/100**")
            health_lines.append(
                f"  - Hotspots: {blob.health.hotspot_count}, clones: {blob.health.clone_count}, cycles: {blob.health.cycle_count}, dead: {blob.health.dead_count}"
            )
        poor_files = [fh for fh in blob.file_health if fh.band == "poor"]
        if poor_files:
            health_lines.append(
                f"- **{len(poor_files)} file(s) in 'poor' health band** (maintainability < 40):"
            )
            for fh in sorted(poor_files, key=lambda x: x.maintainability_index):
                health_lines.append(f"  - `{fh.file}` — index {fh.maintainability_index:.1f}")
        sections.append("\n".join(health_lines))

    if not sections:
        return ""

    parts = [
        "## Code Graph Findings\n"
        "_Machine-derived, evidence-backed findings from the static code graph."
        " These are ranked by impact and churn-gated where applicable._\n"
    ]
    parts.extend(sections)
    parts.append(
        "\n**PO directive:** Propose SPECIFIC, evidence-cited refactor tasks from "
        'the findings above. Examples: "Extract duplicated block `<group-id>` '
        '(N tokens, M instances)…", "Split `<node-id>` — cyclomatic N '
        '(churn-hotspot)", "Break import cycle `<members>`". '
        "PRIORITIZE findings in hotspot-resident files — they deliver the most "
        "refactor ROI because the code is actively changing. Only propose tasks "
        "that serve the stated goal."
    )
    return "\n\n".join(parts)


async def load_latest_graph_blob(repo_id: int) -> RepoGraphBlob | None:
    """Load and parse the latest stored RepoGraphBlob for repo_id.

    Returns None if no graph exists, if the config row is absent, or if
    parsing fails. Never raises — a missing graph must not break PO analysis.
    """
    try:
        from agent.tools.query_repo_graph import _load_graph

        cfg, graph_row = await _load_graph(repo_id)
        if cfg is None or graph_row is None:
            return None
        return RepoGraphBlob.model_validate(graph_row.graph_json)
    except Exception:
        log.warning(
            "load_latest_graph_blob failed for repo_id=%s",
            repo_id,
            exc_info=True,
        )
        return None
