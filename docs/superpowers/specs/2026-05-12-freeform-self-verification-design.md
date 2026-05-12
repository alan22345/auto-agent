# Freeform Self-Verification — Design Spec

**Date:** 2026-05-12
**Status:** Approved (pending user review of this written spec)
**Scope:** Sub-project C of the 3-spec overhaul. Sub-project A (`bigger-po-market-research`) has shipped. Sub-project B (architect/builder/reviewer trio) is queued behind this.
**Predecessor brief:** `docs/superpowers/specs/2026-05-12-freeform-self-verification-brief.md`.

## Problem

In freeform mode, the agent writes code, commits it, opens a PR, and that is the loop. It can run `pytest` via `test_runner` and arbitrary commands via `bash`, but it has no way to:

- Start the project's dev server and see whether the app boots.
- Check whether its diff actually addresses what the task asked for.
- Visit a route and observe what renders.
- Loop back when the answer is "no."

The core gap is two-sided. **Functional** — the agent ships code without ever observing the running system or confirming the diff addresses the task. **Quality** — code review (`agent/lifecycle/review.py`) catches code-quality issues today, but doesn't see the rendered UI, so visually-broken pages still ship.

The fix splits the work cleanly across the two existing post-coding phases. **Verify** answers *"does it run and does it address the original ask?"* — boot the dev server, then check the diff against the task description. **Review** answers *"is the work good?"* — the existing code review, extended to drive a headless browser through declared routes and judge the rendered screenshots. Failures at either phase loop back into coding with structured context; second failure blocks the task.

## Out of scope (deferred)

- Golden-image comparison. No baseline images on a fresh feature; defer until usage data demands it.
- E2E user flows (click X, then assert Y). Only static route renders for this cut.
- User-declared structured assertions via `FreeformConfig`. Possible Spec D.
- Concurrency safeguards beyond OS-allocated ephemeral ports. `MAX_CONCURRENT_TASKS=2` keeps load light.
- Sandboxing the dev server beyond subprocess + process group. Same trust model as the existing `bash` tool.
- Sub-project B's reviewer agent. C is the horizontal capability B will consume; B is its own spec.

## Architecture

The post-coding flow becomes:

```
CODING → VERIFYING → AWAITING_CI → AWAITING_REVIEW → DONE
                ↘ CODING (fail, retry)         ↘ CODING (fail, retry)
                ↘ BLOCKED (fail, 2nd cycle)    ↘ BLOCKED (fail, 2nd cycle)
```

### Verify (new phase, between CODING and AWAITING_CI)

Two sub-checks, scoped to "does it work and does it match the ask":

1. **Boot check** (always-on when a run command resolves). Start the dev server in the workspace, wait for the TCP port, hold 5 seconds watching for crash. Catches import errors, missing env wiring, "binds then dies" failures.
2. **Intent check** (always runs when there is a diff). One agent invocation receives the task description, the diff summary, and the contents of changed non-code artifacts (markdown, JSON, etc.). It judges whether the work actually addresses the original task: missing requirements, off-topic changes, partial implementations. Emits `OK` or `NOT-OK: <reason>`.

Pass → transition `VERIFYING → AWAITING_CI` and open the PR. Fail → `VERIFYING → CODING` with failure context (cycle 1) or `VERIFYING → BLOCKED` with `block_reason="verify_failed"` (cycle 2).

### Review (existing phase, extended)

Two sub-checks, scoped to quality:

1. **Code review** (existing). The code-quality dimension behaves as today; the reviewer prompt is extended only to *also* judge visual output when screenshots are present (see UI check below).
2. **UI check** (new, conditional on `task.affected_routes` non-empty). Boot the dev server fresh in the review workspace, drive Playwright through each declared route, capture screenshots, and feed them into the same reviewer invocation as the code diff. One combined verdict, two dimensions.

Pass → `AWAITING_REVIEW → DONE`. Fail → `AWAITING_REVIEW → CODING` (existing behaviour, now extended to fire on UI failures too). Second consecutive review failure → `BLOCKED` with `block_reason="review_failed"`.

### Why split this way

Verify's two checks need cheap signals available at coding-completion time (server boots, diff matches task). Review's two checks need richer signals (code-quality reasoning, rendered UI). They fail for different reasons — a verify failure means the work is wrong; a review failure means the work is right but the output is poor. Distinct retry contexts produce more useful coding-turn prompts than a single mashed-together gate would.

The dev server is booted twice — once in verify (held 5 s, killed) and once in review (held during the Playwright probe, killed). Total cost: ~30 s extra per task. Acceptable for a personal-project freeform cadence.

## When each check runs

| Sub-check | Phase | Runs when |
|---|---|---|
| Boot check | Verify | A run command resolves (sniffable manifest or `FreeformConfig.run_command`). |
| Intent check | Verify | Always (any non-empty diff). |
| Code review | Review | Always (existing). |
| UI check | Review | `task.affected_routes` is non-empty. |

Detection of "run command available" uses the same priority everywhere: `FreeformConfig.run_command` → `package.json` `scripts.dev` → `Procfile` `web:` → `pyproject.toml [tool.auto-agent].run`.

**Edge cases:**
- No run command sniffable, empty `affected_routes` → boot check and UI check skipped silently. Verify still runs (intent check). Review still runs (code review). Pure CLI/library/docs tasks proceed exactly as today, with the intent check as a new safety net.
- No run command sniffable, non-empty `affected_routes` → publish `verify_skipped_no_runner` event in verify; publish `review_skipped_no_runner` event in review. Both phases proceed without the dev-server-dependent sub-check. Operational signal, not a task failure.

## Data model

### New table: `verify_attempts`

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `task_id` | int FK → `tasks.id`, indexed | |
| `cycle` | smallint | 1 or 2. |
| `status` | text | `pass` / `fail` / `error`. |
| `boot_check` | text nullable | `pass` / `fail` / `skipped`. Null when phase never reached this sub-check. |
| `intent_check` | text nullable | `pass` / `fail`. Null when verify failed before this sub-check ran. |
| `intent_judgment` | text nullable | Agent's `OK`/`NOT-OK` verdict with reasoning. |
| `failure_reason` | text nullable | `boot_timeout` / `early_exit` / `intent_not_addressed` / `phase_timeout` / `internal_error`. |
| `log_tail` | text nullable | Last 50 lines of dev server stdout/stderr; populated when boot failed. |
| `started_at`, `finished_at` | timestamptz | |

### New table: `review_attempts`

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `task_id` | int FK → `tasks.id`, indexed | |
| `cycle` | smallint | 1 or 2. |
| `status` | text | `pass` / `fail` / `error`. |
| `code_review_verdict` | text nullable | Existing code-review output, persisted instead of only sent as PR comment. |
| `ui_check` | text nullable | `pass` / `fail` / `skipped`. Null when review-phase failed before UI check ran. |
| `routes_probed` | jsonb nullable | `[{path, method, http_status, screenshot_path}, ...]` when UI check ran. |
| `ui_judgment` | text nullable | Agent's reasoning when UI portion failed (or "OK" with notes). |
| `failure_reason` | text nullable | `code_review_rejected` / `route_error` / `ui_judgment_not_ok` / `boot_timeout` / `phase_timeout` / `internal_error`. |
| `log_tail` | text nullable | |
| `started_at`, `finished_at` | timestamptz | |

`Task` gains `verify_attempts` and `review_attempts` relationships.

### `Task` — additions

| column | type | notes |
|---|---|---|
| `affected_routes` | jsonb default `'[]'` | `[{method, path, label}, ...]` populated by the planner. Empty list = non-UI task. |

### `TaskStatus` enum — addition

Add `VERIFYING = "verifying"`. Postgres enum migration uses `ALTER TYPE ... ADD VALUE`. Existing `AWAITING_REVIEW` is reused for the review phase; no new state needed there.

### `FreeformConfig` — additions

| column | type | notes |
|---|---|---|
| `run_command` | text nullable | Optional explicit override when manifest sniffing fails. |

## Components

### `agent/tools/dev_server.py` (new)

Public surfaces:

- **`sniff_run_command(workspace_path) -> str | None`** — pure function. Sniffing priority: `FreeformConfig.run_command` → `package.json` `scripts.dev` → `Procfile` `web:` → `pyproject.toml [tool.auto-agent].run`. Returns `None` if nothing resolves. Used by both `verify.py` and `review.py` to decide whether dev-server-dependent sub-checks can run.
- **`start_dev_server(workspace_path) -> DevServerHandle`** — internal helper, not an agent tool. Allocates ephemeral port via `socket.bind(('', 0))`, exports `PORT=<n>`, spawns via `asyncio.create_subprocess_exec(..., preexec_fn=os.setsid)` so the child is in a new process group (kill cascades catch npm → node → next). Returns `DevServerHandle(pid, pgid, port, log_path, started_at)`. Async background task drains the subprocess stdout into a workspace-scoped tempfile.
- **`wait_for_port(port, timeout=60)`** — polls TCP `connect()` every 250 ms; raises `BootTimeout(log_tail)` on timeout.
- **`hold(server, seconds=5)`** — polls `server.process.returncode` every 500 ms for the configured duration; raises `EarlyExit(log_tail)` if the process exits during the hold. Used by the verify boot check.
- **`tail_dev_server_log(lines=50) -> str`** — agent-callable tool (registered via `with_browser=True` flag). Lets the agent diagnose boot/runtime errors when verify or review reports a failure.

### `agent/tools/browse_url.py` (new)

Agent-callable tool. Single call:

- **Input:** `url` (required), `wait_for` selector (optional, default `body`), `viewport` (optional, default `{width: 1280, height: 800}`).
- **Action:** Playwright headless Chromium → `page.goto(url, wait_until='networkidle')` → `page.wait_for_selector(wait_for, timeout=15s)` → `page.screenshot(full_page=True)`.
- **Output:** `tool_result` with three blocks — HTTP status text, rendered text content (~5000-char cap), screenshot as Anthropic `image` content block.

Per-call timeout 30 s. Used inside review.py's UI check (the phase drives it deterministically), and exposed to the review agent itself so it can re-probe routes if it wants a closer look.

### `agent/lifecycle/verify.py` (new)

Mirrors `agent/lifecycle/review.py` shape. Two sub-checks, run in order.

```python
async def handle_verify(task_id: int) -> None:
    task = await get_task(task_id)
    workspace = await ensure_workspace(task)
    cycle = await next_cycle_number(task_id, table="verify_attempts")  # 1 or 2
    attempt = await create_verify_attempt(task_id, cycle)

    try:
        # Sub-check 1: boot (when a run command resolves)
        run_cmd = dev_server.sniff_run_command(workspace)
        if run_cmd:
            async with dev_server.start_dev_server(workspace) as server:
                await dev_server.wait_for_port(server.port, timeout=60)
                await dev_server.hold(server, seconds=5)
                attempt.boot_check = "pass"
        else:
            attempt.boot_check = "skipped"

        # Sub-check 2: intent (always, on any non-empty diff)
        diff = await git.diff_summary(workspace)
        verdict = await agent_intent_check(task, diff, workspace)
        attempt.intent_check = "pass" if verdict.ok else "fail"
        attempt.intent_judgment = verdict.reasoning
        if not verdict.ok:
            return await fail_cycle(attempt, "intent_not_addressed", None, task, cycle)

        return await pass_cycle(attempt, task)
    except dev_server.BootTimeout as e:
        attempt.boot_check = "fail"
        return await fail_cycle(attempt, "boot_timeout", e.log_tail, task, cycle)
    except dev_server.EarlyExit as e:
        attempt.boot_check = "fail"
        return await fail_cycle(attempt, "early_exit", e.log_tail, task, cycle)
    except asyncio.TimeoutError:
        return await fail_cycle(attempt, "phase_timeout", None, task, cycle)
```

`agent_intent_check` is a single `create_agent(..., readonly=True)` invocation with a focused prompt (see `prompts.py` changes). Token budget: 15 turns. The agent has `file_read`, `grep`, `glob` but no browser tools — intent is judged from text artifacts, not rendered UI.

`pass_cycle` opens the PR (via the extracted `_open_pr_and_advance` helper in `coding.py`) and transitions `VERIFYING → AWAITING_CI`. `fail_cycle` transitions `VERIFYING → CODING` with failure context (cycle 1) or `VERIFYING → BLOCKED` (cycle 2). Total per-cycle wall-time wrapped in `asyncio.wait_for(..., timeout=120)`.

### `agent/lifecycle/review.py` (modified)

Two sub-checks now, with the UI check inserted before the final verdict.

- **Setup:** if `task.affected_routes` is non-empty AND a run command resolves, start the dev server and Playwright probe each route deterministically. Persist `routes_probed` to the `review_attempts` row. Boot failures and route ≥400s short-circuit to `fail_cycle` with `failure_reason="boot_timeout"` or `"route_error"`.
- **Review agent invocation:** the existing review prompt is extended to take the probe results (HTTP statuses + screenshots as `image` blocks) when present. The prompt asks the agent to deliver one combined verdict covering code quality AND visual correctness. The agent has `file_read`, `grep`, `glob`, `browse_url`, `tail_dev_server_log` — it can re-probe routes if needed.
- **Verdict:** structured `OK` / `NOT-OK` with per-dimension reasoning. NOT-OK on either dimension fails the cycle (`failure_reason="code_review_rejected"` or `"ui_judgment_not_ok"`).
- **Retry:** two cycles, same shape as verify. After two fails → `BLOCKED` with `block_reason="review_failed"`.
- **Cleanup:** dev server lifetime scoped to the phase (`try/finally` + process-group kill).

The existing review.py is restructured so its today's "review agent invocation + transition" code is split into "deterministic UI probe (new) → review agent invocation (extended) → transition (existing)."

### `agent/lifecycle/coding.py` (modified)

`_finish_coding` today commits, pushes, opens a PR, and transitions `CODING → AWAITING_CI`. Refactor:

- Extract `_open_pr_and_advance(task)` — push branch, open PR, transition to `AWAITING_CI`. Pure code move.
- `_finish_coding` becomes: commit + push (no PR yet), transition `CODING → VERIFYING`, dispatch `handle_verify`.
- `pass_cycle` in `verify.py` calls `_open_pr_and_advance(task)`.

There is no "skip verify" path — verify always runs (the intent sub-check runs unconditionally). If both verify sub-checks happen to skip (no run command, no diff — which shouldn't occur post-coding), pass through harmlessly.

### `agent/lifecycle/factory.py` (modified)

`create_agent` gains a `with_browser: bool = False` parameter. When `True`, the tool registry adds `browse_url` and `tail_dev_server_log`. Review-phase agents use `with_browser=True`. Verify and other phases stay `with_browser=False`.

### `agent/tools/__init__.py` (modified)

`create_default_registry` gains `with_browser: bool = False`. When `True`, registers `BrowseUrlTool` and `TailDevServerLogTool`.

### `agent/prompts.py` (modified)

- `PLANNING_PROMPT` gets one new required field in its output schema: `affected_routes: list[AffectedRoute]`. Instructions added: "When your change affects user-visible routes, list each one with method, path, and a short label. If purely backend/CLI/library, leave empty."
- New constant `VERIFY_INTENT_PROMPT` — the intent-check prompt. Receives task description, diff summary, optional affected-file contents. Asks: "Does this work address the task as stated? Flag missing requirements, off-topic changes, partial implementations. Output OK or NOT-OK with specific reasons."
- Existing `REVIEW_PROMPT` (wherever it lives) extended to address visual output when screenshots are present, with a structured per-dimension verdict.

### `shared/models.py` (modified)

- New `VerifyAttempt` ORM model.
- New `ReviewAttempt` ORM model.
- `Task.affected_routes` column (jsonb default `[]`).
- `Task.verify_attempts` and `Task.review_attempts` relationships.
- `TaskStatus.VERIFYING` enum value.

### `shared/types.py` (modified)

- `AffectedRoute = {method: Literal['GET','POST','PUT','PATCH','DELETE'], path: str, label: str}`.
- `IntentVerdict = {ok: bool, reasoning: str}`.
- `RouteProbeResult = {path, method, http_status: int | None, screenshot_path: str | None, error: str | None}`.
- `VerifyResult`, `ReviewResult` Pydantic models for API responses.

### `shared/config.py` (modified)

`FreeformConfig.run_command: str | None = None`. Documented in the FreeformConfig docstring.

### `shared/events.py` (modified)

New event builders: `verify_started`, `verify_passed`, `verify_failed`, `verify_skipped_no_runner`, `review_ui_check_started`, `review_skipped_no_runner`. Registered in `agent/lifecycle/_orchestrator_api.py`.

### `orchestrator/state_machine.py` (modified)

Add `TaskStatus.VERIFYING` to the transitions table:
- `CODING → VERIFYING` (new).
- `VERIFYING → AWAITING_CI` (pass).
- `VERIFYING → CODING` (fail, retry).
- `VERIFYING → BLOCKED` (fail, exhausted).

Existing `AWAITING_REVIEW → CODING` transition picks up the new "UI check failed" trigger; no schema change needed.

### `orchestrator/router.py` (modified)

Two new endpoints:
- `GET /api/tasks/:id/verify-attempts` — list `VerifyAttempt` rows for the task.
- `GET /api/tasks/:id/review-attempts` — list `ReviewAttempt` rows for the task.

Screenshots live in `var/verify-screenshots/<task-id>/<phase>/<cycle>/` (the orchestrator copies them out of the workspace before cleanup). Served as static files at `GET /api/tasks/:id/<phase>/<cycle>/screenshots/:filename`.

### `web-next/` (modified)

Two new components, one mount point:

1. **`web-next/components/task/VerifyAttempts.tsx`** — renders the per-cycle verify history: status badge, boot-check status, intent-check verdict + reasoning, log tail (collapsed).
2. **`web-next/components/task/ReviewAttempts.tsx`** — renders the per-cycle review history: status badge, code-review verdict, UI check status, routes probed with HTTP status, screenshot thumbnails (click to enlarge), agent's combined reasoning.
3. **`web-next/app/(app)/tasks/[id]/page.tsx`** — mounts both components on the task detail page, in flow order (verify above review).

Each component pulls from its endpoint via a hook in `web-next/hooks/` (`useVerifyAttempts`, `useReviewAttempts`).

## Data flow

1. Planning phase produces a plan and sets `task.affected_routes` (may be empty).
2. Coding phase writes code, commits, pushes the branch.
3. `_finish_coding` transitions `CODING → VERIFYING`, dispatches `handle_verify`.
4. `handle_verify`:
   - Boot check (when run command resolves): start server, wait for port, hold 5 s.
   - Intent check (always): agent judges diff vs task description.
   - Pass → `_open_pr_and_advance` → `AWAITING_CI`. Fail cycle 1 → `CODING`. Fail cycle 2 → `BLOCKED`.
5. CI runs externally; on green → `AWAITING_REVIEW`, dispatch `handle_review`.
6. `handle_review`:
   - UI check (when `affected_routes` non-empty AND run command resolves): boot server, Playwright probe, screenshots.
   - Code-review agent invocation: extended prompt receives diff + screenshots when present; emits combined verdict.
   - Pass → `DONE`. Fail cycle 1 → `CODING`. Fail cycle 2 → `BLOCKED`.

## Error handling

### Verify

| Failure | Behavior |
|---|---|
| Dev server fails to boot (port timeout) | `failure_reason="boot_timeout"`, log tail attached. Coding turn sees the log tail and the boot command. |
| Server binds port but exits within 5 s hold | `failure_reason="early_exit"`. |
| Intent agent says NOT-OK | `failure_reason="intent_not_addressed"`, agent reasoning passed to next coding turn. |
| No run command sniffable, empty `affected_routes` | Boot check skipped; intent check still runs. No failure. |
| No run command sniffable, non-empty `affected_routes` | Publish `verify_skipped_no_runner`, boot check skipped, intent check runs. No failure (operational signal). |
| Phase exceeds 120 s | `failure_reason="phase_timeout"`. |

### Review

| Failure | Behavior |
|---|---|
| Dev server boot fails during UI-check setup | `failure_reason="boot_timeout"`. |
| Route returns 4xx/5xx | `failure_reason="route_error"`, status + log tail in the next coding turn. |
| Playwright navigation timeout on a single route | Recorded as `http_status=null, error="navigation_timeout"` in `routes_probed`; counts as route failure. |
| Code-review verdict NOT-OK on code-quality dimension | `failure_reason="code_review_rejected"`. Existing behaviour. |
| Code-review verdict NOT-OK on UI dimension | `failure_reason="ui_judgment_not_ok"`. |
| No run command sniffable, non-empty `affected_routes` | Publish `review_skipped_no_runner`, UI check skipped, code review still runs. |
| Server orphan cleanup | `os.killpg(pgid, SIGTERM)` then `SIGKILL` after 2 s. `ProcessLookupError` logged and swallowed. |

## Testing

Following `tests/test_po_with_market_research.py` as the pattern.

### Unit tests

- **`tests/test_dev_server.py`** — `sniff_run_command` priority, `wait_for_port` success/timeout, `hold` early-exit detection, process-group kill catches descendants.
- **`tests/test_browse_url.py`** — Playwright mocked; `image` block in `tool_result`; text capped; timeout returns text-only.
- **`tests/test_verify_phase.py`**
  - `test_boot_pass_intent_pass`: run command resolves, diff matches task → both pass → `VERIFYING → AWAITING_CI`.
  - `test_boot_only_no_runner`: no run command, diff matches → boot skipped, intent pass → `VERIFYING → AWAITING_CI`. (Covers research-doc task.)
  - `test_boot_fail_early_exit`: server binds then exits → `early_exit`, transition `VERIFYING → CODING`.
  - `test_intent_fail`: diff doesn't address task → `intent_not_addressed`, transition `VERIFYING → CODING`.
  - `test_second_failure_blocks`: cycle=2 fails → `VERIFYING → BLOCKED`.
- **`tests/test_review_phase_ui_check.py`**
  - `test_ui_check_skipped_no_routes`: `affected_routes=[]` → code review runs alone (existing path).
  - `test_ui_check_runs_and_passes`: routes return 200, agent verdict OK → `AWAITING_REVIEW → DONE`.
  - `test_ui_check_route_error`: route returns 500 → `route_error`, transition `AWAITING_REVIEW → CODING`.
  - `test_ui_check_judgment_not_ok`: routes 200 but verdict NOT-OK on UI dimension → `ui_judgment_not_ok`.
  - `test_code_review_rejects_independent_of_ui`: routes pass but code review rejects → `code_review_rejected`. Regression for the existing review behaviour.

### Regression tests (load-bearing)

Three tests guard the "no PR ships broken or off-target code" invariant:

- **`tests/test_no_pr_on_failed_boot.py`** — verify boot layer.
  - Fixture: workspace with `"dev": "node -e 'process.exit(1)'"`. Plan has `affected_routes=[]` (pure backend change).
  - Assert: no PR created, task ends in `BLOCKED` after 2 cycles, PR-creation code path never reached.

- **`tests/test_no_pr_on_intent_mismatch.py`** — verify intent layer.
  - Fixture: task asks "add a dark-mode toggle to settings"; agent's diff only adds a markdown comment. Stubbed intent agent returns NOT-OK with "no dark-mode toggle found in diff."
  - Assert: no PR created, task ends in `BLOCKED` after 2 cycles.

- **`tests/test_no_done_on_failed_ui_review.py`** — review UI layer.
  - Fixture: server boots, route `/broken` returns 500. Plan has `affected_routes=[{...path:"/broken"}]`. Code review verdict OK on code quality, NOT-OK on UI.
  - Assert: task does not reach `DONE`, ends in `BLOCKED` after 2 review cycles. (CI passes en route; the gate is review.)

### Integration test

- **`tests/test_verify_review_e2e_smoke.py`** (slow, `@pytest.mark.slow`)
  - Real Playwright + tiny `python -m http.server` fixture.
  - Full path: verify boots, intent agent stubbed OK; CI stubbed green; review UI check probes, agent stubbed OK → `DONE`.
  - Optional in CI; included in nightly.

### Not tested here

- Real Next.js startup — too slow for unit tests; covered by the smoke test with a tiny fixture.
- Intent-check *quality* — eval territory; follow-up adds intent-aware case to agent eval.
- `web-next` rendering — manual smoke after deploy.

## Migrations

One Alembic migration:

- `ALTER TYPE task_status ADD VALUE 'verifying'`.
- Add `affected_routes jsonb default '[]'::jsonb not null` to `tasks`.
- Add `run_command text` to `freeform_configs`.
- Create `verify_attempts` table.
- Create `review_attempts` table.
- Indexes: `(task_id, cycle)` unique on both new tables.

All additions backwards-compatible.

## Acceptance criteria

1. Tasks cannot reach `AWAITING_CI` without a passing `VerifyAttempt`. The intent check runs on every task; the boot check runs whenever a run command resolves. Regression tests `test_no_pr_on_failed_boot` and `test_no_pr_on_intent_mismatch` enforce this.
2. Tasks cannot reach `DONE` without a passing `ReviewAttempt`. The code-review sub-check runs on every task; the UI sub-check runs whenever `task.affected_routes` is non-empty. Regression test `test_no_done_on_failed_ui_review` enforces this.
3. Verify and review each run at most 2 cycles per task. After 2 fails at either gate, the task is in `BLOCKED` with `block_reason="verify_failed"` or `"review_failed"`.
4. `browse_url` tool returns an `image` content block that the agent's vision-capable model can reason over.
5. Dev server processes are killed on phase exit, including descendants (npm → node → next). No orphans observed in a 50-cycle soak test across both phases.
6. `web-next` task detail page renders per-cycle verify and review attempts with boot/intent status, code-review verdict, UI check status, screenshots (when present), and reasoning.
7. The full existing test suite (`tests/`) still passes. `ruff check .` clean.

## Open questions / follow-ups (not blocking implementation)

- Should the intent check have a way to escape — e.g., agent can emit "INTENT_REVISION_NEEDED" and surface the mismatch as a clarification to the user rather than a failure cycle? Likely yes once usage data shows how often intent fails on ambiguous tasks. Defer.
- Should `run_command` graduate from `FreeformConfig` to a `.auto-agent/run.sh` repo-side contract once we've seen which projects need overrides? Defer.
- Should `affected_routes` accept request bodies/headers for POST/PATCH probes in review's UI check? Defer — static GET renders are the dominant case.
- Eval task cases for "verify catches intent mismatch" and "review catches visually-broken UI" — follow-ups after this spec ships.
- Sub-project B's reviewer agent consumes `browse_url` and `tail_dev_server_log` directly via tool registry composition. No changes needed here to enable that.
