# Code graph A/B — run 1 results (2026-06-11)

Setup: `promptfooconfig-graph.yaml`, 8 tasks × 2 arms × 2 repeats = 32 runs,
Sonnet 4.6 on Bedrock, graph arm = AST-only graph over the fixture +
nudge + live `query_repo_graph` (`repo_id` threaded — see the loop.py fix
in this branch; the nudge had never fired on the native path before it).

Fixture: `graph-bench` (21-file order service). Rerun:

    cd eval
    PROMPTFOO_PYTHON=$PWD/../.venv/bin/python3 promptfoo eval \
        -c promptfooconfig-graph.yaml --no-cache --env-file ../.env \
        --repeat 2 -o graph_eval_results.json
    ../.venv/bin/python3 analyze_graph_eval.py graph_eval_results.json

Cost of this run: ~6.1M tokens, ~8 minutes wall-clock.

## Pre-registered read → outcomes

| Criterion | Outcome |
|---|---|
| Score non-inferior (within 0.05) | **MET** — graph arm ≥ off arm on every task |
| Tokens ≥20% lower on NAV tasks | **NOT MET** — +15% on NAV (only NAV-3 dropped, −26%) |
| Dedup-trap pass rate better | **Directional** — 5/6 clean vs 4/6, but the wins came on runs with 0 graph calls |
| graph_calls > 0 on the on-arm | **MET** — mean 2.0 on NAV; but 0 on TRAP-1/TRAP-3 |

## Numbers

| Category | Score on/off | Tokens on/off | Reads on/off | Graph calls |
|---|---|---|---|---|
| NAV (4 tasks) | 1.00 / 0.95 | 187k / 162k (+15%) | 3.4 / 3.6 | 2.0 |
| TRAP (3 tasks) | 0.95 / 0.90 | 202k / 205k (−1%) | 4.2 / 5.3 | 0.8 |
| CONTROL (1 task) | 1.00 / 1.00 | 213k / 198k (+8%) | 2.0 / 3.0 | 1.0 |

Standout: **NAV-1** (change `compute_total` signature, update all 5 callers)
— graph arm 1.00 vs 0.80; the off arm missed call sites in one repeat.
That is exactly the failure mode the graph was built to prevent.

## Interpretation (honest)

1. **Correctness gain is real and in the predicted direction** on
   multi-caller changes. No task got worse with the graph.
2. **The token claim fails at this repo size.** A 21-file fixture fits
   in a handful of reads, so the nudge + 16-op tool schema + graph
   responses cost more than they save. The +15% NAV overhead is the
   price of the prompt surface, not of graph queries.
3. **Usage is thin** — the model averages 2 graph calls and skipped the
   graph entirely on two trap tasks. The dedup edge (5/6 vs 4/6) can't
   be attributed to the graph yet.

## What would change the verdict

- **Scale**: rerun the same harness against a large fixture (hundreds of
  files — e.g. a snapshot of auto-agent itself) where grep+read genuinely
  explodes. That's the decisive test for the token claim; this run only
  decides the small-repo case (graph not worth its prompt overhead there).
- **Gating**: if large-repo results also show overhead, the cheap fix is
  to inject the nudge/tool only above a repo-size threshold.
- **Usage**: if graph_calls stay near 0, strengthen the nudge or auto-inject
  a task-relevant slice (ADR-016's deferred idea) before re-testing.
