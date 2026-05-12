# Freeform Self-Verification — Design Spec

**Date:** 2026-05-12
**Status:** Approved (pending user review of this written spec)
**Scope:** Sub-project C of the 3-spec overhaul. Sub-project A (`bigger-po-market-research`) has shipped. Sub-project B (architect/builder/reviewer trio) is queued behind this.
**Predecessor brief:** `docs/superpowers/specs/2026-05-12-freeform-self-verification-brief.md`.

## Problem

In freeform mode, the agent writes code, commits it, opens a PR, and that is the loop. It can run `pytest` via `test_runner` and arbitrary commands via `bash`, but it has no way to:

- Start the project's dev server and see whether the app boots.
- Take a screenshot of a route and observe what renders.
- Check whether its diff actually addresses what the task asked for.
- Iterate on UI work by looking at the rendered output.
- Loop back when any of the above answers is "no."

The gap is three-sided. **Functional** — the agent ships code without observing the running system or confirming the diff matches the task. **Quality** — code review catches code-quality issues today, but doesn't see the rendered UI. **Authoring** — for a freeform task like *"improve the UI, make it more realistic,"* the coding agent has no way to look at what it's producing as it produces it.

The fix introduces three pieces:

1. A new **verify** phase between CODING and AWAITING_CI, scoped to *"does it run and does it address the original ask?"*
2. An extended **review** phase, scoped to *"is the work good — code and UI?"*
3. A reusable **visual-capture tool** (`browse_url`) that agents in all three lifecycle phases (coding, verify, review) can call when guidance directs them to. Phases boot the dev server; agents drive the inspection.

## Out of scope (deferred)

- Golden-image comparison. No baseline images on a fresh feature; defer until usage data demands it.
- E2E user flows (click X, then assert Y). Only static route renders for this cut — the agent navigates via tool calls, not interaction scripts.
- User-declared structured assertions via `FreeformConfig`. Possible Spec D.
- Concurrency safeguards beyond OS-allocated ephemeral ports. `MAX_CONCURRENT_TASKS=2` keeps load light.
- Sandboxing the dev server beyond subprocess + process group. Same trust model as the existing `bash` tool.
- Sub-project B's reviewer agent. C is the horizontal capability B will consume; B is its own spec.
- Cross-phase server reuse / pooling. Each phase boots its own; if boot cost becomes a bottleneck, optimize later.

## Architecture

The post-coding flow becomes:

```
CODING ──► VERIFYING ──► AWAITING_CI ──► AWAITING_REVIEW ──► DONE
   ▲          │                              │
   │          ▼ fail (cycle 1)               ▼ fail (cycle 1)
   └──────  CODING  ◄──────────────────  CODING
              │                              │
              ▼ fail (cycle 2)               ▼ fail (cycle 2)
           BLOCKED                        BLOCKED
```

Both `VERIFYING` and `AWAITING_REVIEW` retry into `CODING` on first failure (existing pattern for review; new for verify). Second consecutive failure at either gate blocks the task for human review. Each gate has its own 2-cycle budget; the budgets are independent (a verify retry doesn't burn a review retry).

### Coding (existing phase, extended)

When `task.affected_routes` is non-empty AND a run command resolves, the coding phase auto-starts the dev server in the workspace before the agent loop begins, exposing the port to the agent via the system prompt:

> A dev server is running at `http://localhost:{port}`. The affected routes for this task are: `{routes}`. Use the `browse_url` tool to inspect rendered output as you work. Use `tail_dev_server_log` if you need to debug the server.

The agent now has `browse_url` and `tail_dev_server_log` alongside its usual edit tools. It can screenshot → edit → re-screenshot to iterate on visual work. The server is killed on phase exit (success, failure, or retry from a gate).

For tasks with empty `affected_routes` or no resolvable run command, coding behaves exactly as today — no server, no browser tools.

### Verify (new phase, between CODING and AWAITING_CI)

Two sub-checks, scoped to "does it work and does it match the ask":

1. **Boot check** (always-on when a run command resolves). Deterministic. Phase starts the dev server, waits for the TCP port, holds 5 seconds watching for crash. Catches import errors, missing env wiring, "binds then dies" failures.
2. **Intent check** (always runs when there is a diff). Single agent invocation. The phase passes task description + diff summary + the dev server's URL (still running from the boot check) + `browse_url` + `tail_dev_server_log` to the intent agent. Prompt gives concrete guidance:

   > Task description: {...}. Diff summary: {...}. Affected routes: {...}. Did this work address the task as stated? If the task describes visual behaviour or UI changes, screenshot the affected routes via `browse_url` to confirm the rendered output matches the description. Flag missing requirements, off-topic changes, partial implementations. Output `OK` or `NOT-OK: <reason>`.

Pass → transition `VERIFYING → AWAITING_CI` (PR opens). Fail → `VERIFYING → CODING` with failure context (cycle 1) or `VERIFYING → BLOCKED` (cycle 2). The dev server is killed on phase exit either way.

### Review (existing phase, extended)

Two sub-checks, scoped to quality, run inside a single reviewer agent invocation:

1. **Code review** (existing). Code-quality dimension behaves as today.
2. **UI check** (new, conditional on `task.affected_routes` non-empty AND run command resolves). Phase boots the dev server; reviewer agent gets `browse_url` + `tail_dev_server_log` and concrete guidance:

   > For each route in `affected_routes`, call `browse_url(http://localhost:{port}/{path})`. Judge the rendered output against the diff and the task description. Combine this judgment with your code-quality review into one verdict.

The reviewer emits a single structured verdict covering both dimensions. NOT-OK on either dimension fails the cycle. Pass → `AWAITING_REVIEW → DONE`. Fail → `AWAITING_REVIEW → CODING` (cycle 1) or `AWAITING_REVIEW → BLOCKED` (cycle 2).

### Why phases boot the server but agents drive the inspection

The phase owns server lifecycle (start, kill, log capture) because the phase has the cleanup discipline — `try/finally` + process-group kill — that an agent loop doesn't. The agent owns visual inspection because the agent knows what to look for: which routes matter most, which viewport sizes to try, whether to re-screenshot after a tool result revealed something unexpected. Splitting it this way keeps the deterministic infrastructure deterministic and gives the LLM the agency it needs.

### Dev server boot count per task

Each phase that uses the server boots its own. Worst case (all three phases active, no retries): 3 boots per task — coding, verify, review. With one retry per gate, up to 5. For a Next.js app that's roughly 30–60 s of extra wall time per task. Acceptable for personal-project freeform cadence; revisit if it becomes a bottleneck.

## When each check runs

| Phase | Sub-check | Runs when |
|---|---|---|
| Coding | Dev server (in background) | `affected_routes` non-empty AND run command resolves |
| Verify | Boot check | Run command resolves |
| Verify | Intent check | Always (any non-empty diff) |
| Review | Code review | Always (existing) |
| Review | UI check | `affected_routes` non-empty AND run command resolves |

Run command sniffing priority everywhere: `FreeformConfig.run_command` → `package.json` `scripts.dev` → `Procfile` `web:` → `pyproject.toml [tool.auto-agent].run`.

**Edge cases:**
- No run command sniffable, empty `affected_routes`: coding stays serverless, verify runs intent-check only, review runs code-review only. Pure CLI/library/docs flow.
- No run command sniffable, non-empty `affected_routes`: planner declared visual scope but the project can't be served. Publish `verify_skipped_no_runner` / `review_skipped_no_runner` events; coding agent works without browser tools; verify intent-check runs without the URL hint; review UI-check is skipped. Operational signal, not a failure.

## Data model

### New table: `verify_attempts`

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `task_id` | int FK → `tasks.id`, indexed | |
| `cycle` | smallint | 1 or 2. |
| `status` | text | `pass` / `fail` / `error`. |
| `boot_check` | text nullable | `pass` / `fail` / `skipped`. |
| `intent_check` | text nullable | `pass` / `fail`. |
| `intent_judgment` | text nullable | Agent's verdict + reasoning. |
| `tool_calls` | jsonb nullable | The intent agent's `browse_url` + `tail_dev_server_log` calls and results (URLs visited, screenshot paths, log snippets). Audit trail. |
| `failure_reason` | text nullable | `boot_timeout` / `early_exit` / `intent_not_addressed` / `phase_timeout` / `internal_error`. |
| `log_tail` | text nullable | Last 50 lines of dev server stdout/stderr when boot or runtime failure. |
| `started_at`, `finished_at` | timestamptz | |

### New table: `review_attempts`

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `task_id` | int FK → `tasks.id`, indexed | |
| `cycle` | smallint | 1 or 2. |
| `status` | text | `pass` / `fail` / `error`. |
| `code_review_verdict` | text nullable | Code-quality dimension's verdict + reasoning. |
| `ui_check` | text nullable | `pass` / `fail` / `skipped`. |
| `ui_judgment` | text nullable | UI dimension's verdict + reasoning when UI check ran. |
| `tool_calls` | jsonb nullable | Reviewer's `browse_url` + `tail_dev_server_log` calls. Audit trail (includes screenshot paths). |
| `failure_reason` | text nullable | `code_review_rejected` / `ui_judgment_not_ok` / `boot_timeout` / `phase_timeout` / `internal_error`. |
| `log_tail` | text nullable | |
| `started_at`, `finished_at` | timestamptz | |

`Task` gains `verify_attempts` and `review_attempts` relationships.

### `Task` — additions

| column | type | notes |
|---|---|---|
| `affected_routes` | jsonb default `'[]'` | `[{method, path, label}, ...]` populated by the planner. Drives whether coding starts a dev server, and whether the review UI check runs. |

### `TaskStatus` enum — addition

Add `VERIFYING = "verifying"`. Postgres enum migration uses `ALTER TYPE ... ADD VALUE`. Existing `AWAITING_REVIEW` is reused.

### `FreeformConfig` — additions

| column | type | notes |
|---|---|---|
| `run_command` | text nullable | Optional override when manifest sniffing fails. |

## Components

### `agent/tools/dev_server.py` (new)

- **`sniff_run_command(workspace_path) -> str | None`** — pure helper. Priority: `FreeformConfig.run_command` → `package.json` `scripts.dev` → `Procfile` `web:` → `pyproject.toml [tool.auto-agent].run`. Returns `None` if nothing resolves.
- **`start_dev_server(workspace_path) -> DevServerHandle`** — internal helper, **not** an agent tool. Allocates ephemeral port via `socket.bind(('', 0))`, exports `PORT=<n>`, spawns via `asyncio.create_subprocess_exec(..., preexec_fn=os.setsid)` (new process group). Returns `DevServerHandle(pid, pgid, port, log_path, started_at)`.
- **`wait_for_port(port, timeout=60)`** — polls TCP `connect()` every 250 ms; raises `BootTimeout(log_tail)` on timeout.
- **`hold(server, seconds=5)`** — polls `server.process.returncode`; raises `EarlyExit(log_tail)` if the process exits during the hold. Used by verify's boot check.
- **`tail_dev_server_log(lines=50) -> str`** — agent-callable tool. Returns the last N lines of the current dev server's log. Available in coding/verify/review when `with_browser=True`.

Cleanup contract: every `start_dev_server` call must be paired with a kill (`os.killpg(pgid, SIGTERM)` then `SIGKILL` after 2 s grace). Implemented via async context manager.

### `agent/tools/browse_url.py` (new)

**Agent-callable visual-capture tool.** This is the single screenshot mechanism for the entire system.

- **Input:** `url` (required), `wait_for` selector (optional, default `body`), `viewport` (optional, default `{width: 1280, height: 800}`).
- **Action:** Playwright headless Chromium → `page.goto(url, wait_until='networkidle')` → `wait_for_selector(wait_for, timeout=15s)` → `page.screenshot(full_page=True)`.
- **Output:** `tool_result` with three blocks — HTTP status text, rendered text content (~5000-char cap), screenshot as Anthropic `image` content block.
- **Timeout:** 30 s per call.

The tool itself doesn't care which phase invoked it. Phases inject prompt guidance telling the agent when to use it.

### `agent/lifecycle/coding.py` (modified)

Three changes:

1. **Pre-loop: start dev server when applicable.** At the start of `handle_coding`, if `task.affected_routes` is non-empty AND `sniff_run_command(workspace)` returns non-None, call `start_dev_server` and stash the handle in the coding session state. If boot times out, log + publish `coding_server_boot_failed`, and proceed without a server (agent works without `browse_url`).
2. **System prompt augmentation.** When a server is running, append:
   > A dev server is running at `http://localhost:{port}`. Affected routes for this task: `{routes}`. Use `browse_url` to inspect rendered output as you make changes; use `tail_dev_server_log` if you need server logs. Hot reload is active for most frameworks — re-screenshot after edits to see results.
3. **Post-loop cleanup.** `try/finally` around the agent loop kills the server on exit (success, failure, or retry from a gate).
4. **`_finish_coding` refactor.** Extract `_open_pr_and_advance(task)` from existing PR-opening code. `_finish_coding` becomes: commit + push branch, transition `CODING → VERIFYING`, dispatch `handle_verify`. `pass_cycle` in `verify.py` calls `_open_pr_and_advance(task)`.

### `agent/lifecycle/verify.py` (new)

Mirrors `agent/lifecycle/review.py` shape.

```python
async def handle_verify(task_id: int) -> None:
    task = await get_task(task_id)
    workspace = await ensure_workspace(task)
    cycle = await next_cycle_number(task_id, "verify_attempts")  # 1 or 2
    attempt = await create_verify_attempt(task_id, cycle)

    run_cmd = dev_server.sniff_run_command(workspace)
    server = None
    try:
        if run_cmd:
            server = await dev_server.start_dev_server(workspace).__aenter__()
            try:
                await dev_server.wait_for_port(server.port, timeout=60)
                await dev_server.hold(server, seconds=5)
                attempt.boot_check = "pass"
            except dev_server.BootTimeout as e:
                attempt.boot_check = "fail"
                return await fail_cycle(attempt, "boot_timeout", e.log_tail, task, cycle)
            except dev_server.EarlyExit as e:
                attempt.boot_check = "fail"
                return await fail_cycle(attempt, "early_exit", e.log_tail, task, cycle)
        else:
            attempt.boot_check = "skipped"

        # Intent check (always)
        diff = await git.diff_summary(workspace)
        verdict = await agent_intent_check(task, diff, server, workspace)
        attempt.intent_check = "pass" if verdict.ok else "fail"
        attempt.intent_judgment = verdict.reasoning
        attempt.tool_calls = verdict.tool_calls
        if not verdict.ok:
            return await fail_cycle(attempt, "intent_not_addressed", None, task, cycle)
        return await pass_cycle(attempt, task)
    except asyncio.TimeoutError:
        return await fail_cycle(attempt, "phase_timeout", None, task, cycle)
    finally:
        if server is not None:
            await dev_server.kill_server(server)
```

`agent_intent_check` is a single `create_agent(..., readonly=True, with_browser=(server is not None))` invocation. Token budget: 15 turns. The agent receives the task description, diff summary, and (when a server is running) the URL + affected routes + concrete prompt guidance to screenshot when the task describes visual behaviour. The agent's `tool_calls` (browse_url URLs hit, screenshots captured) are persisted to the attempt for the audit trail.

`pass_cycle` calls `_open_pr_and_advance(task)`. `fail_cycle` transitions `VERIFYING → CODING` with failure context (cycle 1) or `VERIFYING → BLOCKED` with `block_reason="verify_failed"` (cycle 2). Total per-cycle wall-time wrapped in `asyncio.wait_for(..., timeout=120)`.

### `agent/lifecycle/review.py` (modified)

Three changes:

1. **Pre-agent setup.** If `task.affected_routes` is non-empty AND a run command resolves, boot a fresh dev server before invoking the reviewer agent. Otherwise no server; UI check is skipped.
2. **Reviewer prompt extension.** Append the UI-check guidance (server URL, affected routes, instruction to screenshot each) to the existing code-review prompt. The reviewer's output schema becomes:
   ```
   {
     "code_review": {"verdict": "OK"|"NOT-OK", "reasoning": "..."},
     "ui_check": {"verdict": "OK"|"NOT-OK"|"SKIPPED", "reasoning": "..."}
   }
   ```
3. **Verdict handling.** NOT-OK on `code_review` → `failure_reason="code_review_rejected"`. NOT-OK on `ui_check` → `failure_reason="ui_judgment_not_ok"`. Either fails the cycle. Both OK → pass.

Cleanup: server killed on phase exit. After two consecutive review failures (any combination of code/UI) → `BLOCKED` with `block_reason="review_failed"`.

### `agent/lifecycle/factory.py` (modified)

`create_agent` gains `with_browser: bool = False`. When `True`, the tool registry includes `browse_url` and `tail_dev_server_log`. Used by:
- Coding phase, when the coding-phase dev server is running.
- Verify phase intent-check, when the verify-phase dev server is running.
- Review phase, when the review-phase dev server is running.

### `agent/tools/__init__.py` (modified)

`create_default_registry` gains `with_browser: bool = False`. When `True`, registers `BrowseUrlTool` and `TailDevServerLogTool`.

### `agent/prompts.py` (modified)

- `PLANNING_PROMPT` adds the `affected_routes: list[AffectedRoute]` output field. Instruction: "List user-visible routes this change affects with method, path, and short label. If purely backend/CLI/library, leave empty."
- Coding system-prompt builder appends the server-available block when applicable (see `coding.py` change above).
- New constant `VERIFY_INTENT_PROMPT` — task description, diff summary, optional URL + routes, instruction to use `browse_url` when the task is visual, output `OK` or `NOT-OK: <reason>`.
- `REVIEW_PROMPT` extended to the two-dimensional output schema (see `review.py` change above).

### `shared/models.py` (modified)

- New `VerifyAttempt` ORM model.
- New `ReviewAttempt` ORM model.
- `Task.affected_routes` column (jsonb default `[]`).
- `Task.verify_attempts` and `Task.review_attempts` relationships.
- `TaskStatus.VERIFYING` enum value.

### `shared/types.py` (modified)

- `AffectedRoute = {method: Literal['GET','POST','PUT','PATCH','DELETE'], path: str, label: str}`.
- `IntentVerdict = {ok: bool, reasoning: str, tool_calls: list[dict]}`.
- `ReviewVerdict = {code_review: {verdict, reasoning}, ui_check: {verdict, reasoning}}`.
- `VerifyResult`, `ReviewResult` Pydantic models for API responses.

### `shared/config.py` (modified)

`FreeformConfig.run_command: str | None = None`.

### `shared/events.py` (modified)

New events: `verify_started`, `verify_passed`, `verify_failed`, `verify_skipped_no_runner`, `coding_server_boot_failed`, `review_ui_check_started`, `review_skipped_no_runner`. Registered in `agent/lifecycle/_orchestrator_api.py`.

### `orchestrator/state_machine.py` (modified)

Add `TaskStatus.VERIFYING`:
- `CODING → VERIFYING` (new).
- `VERIFYING → AWAITING_CI` (pass).
- `VERIFYING → CODING` (fail, retry).
- `VERIFYING → BLOCKED` (fail, exhausted).

Existing `AWAITING_REVIEW → CODING` and `AWAITING_REVIEW → DONE` transitions pick up the UI-check verdict — no schema change.

### `orchestrator/router.py` (modified)

Two new endpoints:
- `GET /api/tasks/:id/verify-attempts` — list `VerifyAttempt` rows.
- `GET /api/tasks/:id/review-attempts` — list `ReviewAttempt` rows.

Screenshots live in `var/verify-screenshots/<task-id>/<phase>/<cycle>/`. The orchestrator copies them out of the workspace before workspace cleanup. Served as static files at `GET /api/tasks/:id/<phase>/<cycle>/screenshots/:filename`.

### `web-next/` (modified)

Two new components, both mounted on the task detail page in flow order:

1. **`web-next/components/task/VerifyAttempts.tsx`** — per-cycle: status badge, boot-check status, intent-check verdict + reasoning, screenshots from the intent agent's tool calls (if any), log tail (collapsed).
2. **`web-next/components/task/ReviewAttempts.tsx`** — per-cycle: status badge, code-review verdict, UI-check verdict, screenshots from the reviewer's tool calls (click to enlarge), combined reasoning.

Hooks `useVerifyAttempts(taskId)`, `useReviewAttempts(taskId)` in `web-next/hooks/`.

## Data flow

1. Planning sets `task.affected_routes`.
2. Coding phase:
   - If `affected_routes` + run command → start dev server, agent works with `browse_url` available.
   - Else → serverless coding (today's behaviour).
   - On completion: kill server, commit + push, transition `CODING → VERIFYING`.
3. Verify phase:
   - Boot check (when run command resolves): own dev server, 5 s hold.
   - Intent check (always): agent with `browse_url` (when server is running) judges diff vs task. Records tool calls.
   - Pass → `_open_pr_and_advance` → `AWAITING_CI`. Fail cycle 1 → `CODING`. Fail cycle 2 → `BLOCKED`.
4. CI external; on green → `AWAITING_REVIEW`.
5. Review phase:
   - UI-check setup (when `affected_routes` + run command): own dev server.
   - Reviewer agent: code review + UI check in one call, with `browse_url` available when server is running.
   - Pass → `DONE`. Fail cycle 1 → `CODING`. Fail cycle 2 → `BLOCKED`.

## Error handling

### Coding

| Failure | Behavior |
|---|---|
| Dev server fails to boot in coding pre-loop | Publish `coding_server_boot_failed`, proceed without server. Agent continues without `browse_url`; task can still complete (no visual feedback). |
| Agent uses `browse_url` and gets HTTP error | Returned in tool result; agent reacts. No phase-level failure. |
| Server orphan on phase exit | `try/finally` + `os.killpg(pgid, SIGTERM)` then `SIGKILL` after 2 s grace. |

### Verify

| Failure | Behavior |
|---|---|
| Boot timeout | `failure_reason="boot_timeout"`. |
| Early exit during 5 s hold | `failure_reason="early_exit"`. |
| Intent agent NOT-OK | `failure_reason="intent_not_addressed"`. |
| Phase exceeds 120 s | `failure_reason="phase_timeout"`. |
| No run command + non-empty routes | `verify_skipped_no_runner` event, boot skipped, intent runs without URL hint. No failure. |

### Review

| Failure | Behavior |
|---|---|
| Boot timeout during UI-check setup | `failure_reason="boot_timeout"`. |
| Reviewer NOT-OK on code dimension | `failure_reason="code_review_rejected"`. |
| Reviewer NOT-OK on UI dimension | `failure_reason="ui_judgment_not_ok"`. |
| Reviewer's `browse_url` call returns error / 5xx | Surfaced in tool result; reviewer decides (typically NOT-OK on UI dimension). |
| No run command + non-empty routes | `review_skipped_no_runner` event, UI check skipped, code review still runs. |

## Testing

### Unit tests

- **`tests/test_dev_server.py`** — `sniff_run_command` priority across manifests, `wait_for_port` success/timeout, `hold` early-exit detection, process-group kill catches descendants.
- **`tests/test_browse_url.py`** — Playwright mocked: `image` block in `tool_result`; text capped; per-call timeout returns text-only result.
- **`tests/test_coding_server_lifecycle.py`** — coding phase with `affected_routes` non-empty boots server; coding phase with empty routes does not; server killed on phase exit (success and failure paths); `coding_server_boot_failed` published when boot times out and agent continues without browser tools.
- **`tests/test_verify_phase.py`**
  - `test_boot_pass_intent_pass`: full happy path → `VERIFYING → AWAITING_CI`.
  - `test_no_runner_intent_only`: research-doc task (no run command) → boot `skipped`, intent runs, pass.
  - `test_boot_fail_early_exit`: server binds then dies → `early_exit`, `VERIFYING → CODING`.
  - `test_intent_fail`: stubbed intent agent NOT-OK → `intent_not_addressed`, `VERIFYING → CODING`.
  - `test_intent_uses_browse_url`: stubbed agent calls `browse_url`; assert tool call recorded on attempt and screenshot path persisted.
  - `test_second_cycle_blocks`: cycle=2 fails → `VERIFYING → BLOCKED`.
- **`tests/test_review_phase_ui_check.py`**
  - `test_ui_skipped_no_routes`: empty `affected_routes` → code review only path.
  - `test_ui_runs_and_passes`: stubbed reviewer screenshots + verdict OK → `AWAITING_REVIEW → DONE`.
  - `test_ui_judgment_not_ok`: stubbed reviewer NOT-OK on UI dimension → `ui_judgment_not_ok`, `AWAITING_REVIEW → CODING`.
  - `test_code_rejected_independent_of_ui`: code dimension NOT-OK, UI OK → `code_review_rejected`. Regression for existing review behaviour.

### Regression tests (load-bearing)

Three tests guard "no broken or off-target work ships":

- **`tests/test_no_pr_on_failed_boot.py`** — verify boot layer.
  - Broken `"dev"` script, `affected_routes=[]` → no PR, BLOCKED after 2 verify cycles.
- **`tests/test_no_pr_on_intent_mismatch.py`** — verify intent layer.
  - Task asks for dark-mode toggle; diff only adds a comment; stubbed intent agent NOT-OK → no PR, BLOCKED after 2 verify cycles.
- **`tests/test_no_done_on_failed_ui_review.py`** — review UI layer.
  - `affected_routes=[{path:"/broken"}]`, route returns 500, stubbed reviewer NOT-OK on UI → task never reaches DONE, ends in BLOCKED after 2 review cycles.

### Integration test

- **`tests/test_verify_review_e2e_smoke.py`** (`@pytest.mark.slow`) — real Playwright + tiny `python -m http.server` fixture. Full path: coding boots, verify passes, CI stub green, review UI check passes → DONE. Nightly-only in CI.

### Not tested here

- Real Next.js startup — covered by the smoke test with a tiny fixture.
- Intent-check *quality* — eval territory.
- `web-next` rendering — manual smoke after deploy.

## Migrations

One Alembic migration:

- `ALTER TYPE task_status ADD VALUE 'verifying'`.
- Add `affected_routes jsonb default '[]'::jsonb not null` to `tasks`.
- Add `run_command text` to `freeform_configs`.
- Create `verify_attempts` and `review_attempts` tables with `(task_id, cycle)` unique indexes.

Backwards-compatible.

## Acceptance criteria

1. Tasks cannot reach `AWAITING_CI` without a passing `VerifyAttempt`. Intent check runs every task; boot check runs whenever a run command resolves. Regression tests `test_no_pr_on_failed_boot` and `test_no_pr_on_intent_mismatch` enforce this.
2. Tasks cannot reach `DONE` without a passing `ReviewAttempt`. Code review runs every task; UI check runs whenever `affected_routes` is non-empty AND a run command resolves. Regression test `test_no_done_on_failed_ui_review` enforces this.
3. Coding agent has `browse_url` and `tail_dev_server_log` available when `affected_routes` is non-empty AND a run command resolves. Coding-phase dev server is killed on phase exit in all paths.
4. Verify and review each retry at most twice. Second failure at either gate transitions the task to `BLOCKED` (`block_reason="verify_failed"` or `"review_failed"`).
5. `browse_url` returns an `image` content block the vision-capable model can reason over. Tool is the single screenshot mechanism for all three phases.
6. Dev server processes are killed on phase exit, including descendants (npm → node → next). No orphans observed in a 50-cycle soak test.
7. `web-next` task detail page renders both verify and review attempts with statuses, screenshots from tool-call audit trails, and reasoning.
8. Full existing test suite still passes. `ruff check .` clean.

## Open questions / follow-ups (not blocking implementation)

- Should the dev server be reused across coding → verify → review for the same task (state-isolation aside)? Boot count of 3+ may add up; revisit if it becomes a bottleneck.
- Should intent check escape to a clarification ("I think the task is ambiguous") rather than only `OK`/`NOT-OK`? Probably yes once we see how often intent fails on ambiguous tasks; defer.
- Should `affected_routes` accept request bodies/headers for POST/PATCH probes in review UI check? Defer — static GETs dominate.
- Should `run_command` graduate to a `.auto-agent/run.sh` repo-side contract? Defer until usage data shows which projects need overrides.
- Eval cases for "verify catches intent mismatch" and "review catches visually-broken UI" — follow-ups after spec ships.
- Sub-project B's reviewer agent consumes `browse_url` and `tail_dev_server_log` via the same `with_browser=True` registry composition. No additional changes needed here.
