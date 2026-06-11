"""Paired readout for the code-graph A/B eval (ADR-025).

Usage:
    cd eval && promptfoo eval -c promptfooconfig-graph.yaml --no-cache \
        -o graph_eval_results.json
    ../.venv/bin/python3 analyze_graph_eval.py graph_eval_results.json

Prints per-task and per-category (NAV / TRAP / CONTROL) comparisons of
score, tokens, file reads, graph usage, and wall-clock between the
graph-on and graph-off arms.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict

ARM_ON = "agent-graph-on"
ARM_OFF = "agent-graph-off"


def load_rows(path: str) -> list[dict]:
    """Flatten a promptfoo results file into per-run dicts."""
    with open(path) as f:
        data = json.load(f)
    raw = data.get("results", data)
    if isinstance(raw, dict):
        raw = raw.get("results", [])

    rows = []
    for item in raw:
        provider = item.get("provider", {})
        arm = provider.get("label") or provider.get("id") or "?"
        description = (
            item.get("testCase", {}).get("description")
            or item.get("description")
            or (item.get("vars", {}) or {}).get("task", "?")[:40]
        )
        score = item.get("score")
        if score is None:
            score = (item.get("gradingResult") or {}).get("score", 0.0)

        metrics = {}
        output = (item.get("response") or {}).get("output")
        if isinstance(output, str):
            try:
                metrics = json.loads(output)
            except ValueError:
                metrics = {}
        elif isinstance(output, dict):
            metrics = output

        tokens = metrics.get("tokens", {}) or {}
        rows.append(
            {
                "arm": arm,
                "task": description,
                "score": float(score or 0.0),
                "success": bool(item.get("success")),
                "tokens_in": int(tokens.get("input") or 0),
                "tokens_out": int(tokens.get("output") or 0),
                "total_reads": int(metrics.get("total_reads") or 0),
                "tool_calls": int(metrics.get("tool_calls") or 0),
                "graph_calls": int(metrics.get("graph_calls") or 0),
                "graph_ops": metrics.get("graph_ops") or {},
                "elapsed": float(metrics.get("elapsed_seconds") or 0.0),
                "error": metrics.get("error"),
            }
        )
    return rows


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def aggregate(rows: list[dict]) -> dict:
    """(task, arm) -> averaged metrics across repeats."""
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["task"], row["arm"])].append(row)
    return {
        key: {
            "n": len(group),
            "score": mean([g["score"] for g in group]),
            "tokens": mean([g["tokens_in"] + g["tokens_out"] for g in group]),
            "reads": mean([g["total_reads"] for g in group]),
            "tool_calls": mean([g["tool_calls"] for g in group]),
            "graph_calls": mean([g["graph_calls"] for g in group]),
            "elapsed": mean([g["elapsed"] for g in group]),
            "errors": sum(1 for g in group if g["error"]),
        }
        for key, group in grouped.items()
    }


def category(task: str) -> str:
    for prefix in ("NAV", "TRAP", "CONTROL"):
        if task.startswith(prefix):
            return prefix
    return "OTHER"


def pct_delta(on: float, off: float) -> str:
    if off == 0:
        return "n/a"
    return f"{(on - off) / off * +100:+.0f}%"


def main(path: str) -> None:
    rows = load_rows(path)
    if not rows:
        print(f"No result rows found in {path}")
        return
    agg = aggregate(rows)
    tasks = sorted({task for task, _ in agg})

    header = (
        f"{'task':42} {'score on/off':>14} {'tokens on/off':>20} "
        f"{'reads on/off':>13} {'graph':>5} {'sec on/off':>14}"
    )
    print(header)
    print("-" * len(header))
    for task in tasks:
        on = agg.get((task, ARM_ON))
        off = agg.get((task, ARM_OFF))
        if not on or not off:
            print(f"{task[:42]:42} (missing one arm)")
            continue
        print(
            f"{task[:42]:42}"
            f" {on['score']:.2f}/{off['score']:.2f}      "
            f" {on['tokens']:>8.0f}/{off['tokens']:<8.0f}"
            f" {on['reads']:>5.1f}/{off['reads']:<5.1f}"
            f" {on['graph_calls']:>5.1f}"
            f" {on['elapsed']:>6.0f}/{off['elapsed']:<6.0f}"
        )

    print()
    for cat in ("NAV", "TRAP", "CONTROL"):
        cat_tasks = [t for t in tasks if category(t) == cat]
        if not cat_tasks:
            continue
        on_vals = [agg[(t, ARM_ON)] for t in cat_tasks if (t, ARM_ON) in agg]
        off_vals = [agg[(t, ARM_OFF)] for t in cat_tasks if (t, ARM_OFF) in agg]
        if not on_vals or not off_vals:
            continue
        score_on, score_off = (
            mean([v["score"] for v in on_vals]),
            mean([v["score"] for v in off_vals]),
        )
        tok_on, tok_off = (
            mean([v["tokens"] for v in on_vals]),
            mean([v["tokens"] for v in off_vals]),
        )
        reads_on, reads_off = (
            mean([v["reads"] for v in on_vals]),
            mean([v["reads"] for v in off_vals]),
        )
        graph_on = mean([v["graph_calls"] for v in on_vals])
        print(
            f"{cat:8} score {score_on:.2f} vs {score_off:.2f}"
            f" | tokens {tok_on:.0f} vs {tok_off:.0f} ({pct_delta(tok_on, tok_off)})"
            f" | reads {reads_on:.1f} vs {reads_off:.1f} ({pct_delta(reads_on, reads_off)})"
            f" | graph calls (on arm): {graph_on:.1f}"
        )

    on_calls = [r["graph_calls"] for r in rows if r["arm"] == ARM_ON]
    if on_calls and mean(on_calls) == 0:
        print(
            "\nWARNING: graph-on arm never called query_repo_graph — the "
            "comparison measured the nudge, not the graph."
        )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "graph_eval_results.json")
