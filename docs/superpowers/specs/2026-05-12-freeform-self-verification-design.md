# Freeform Self-Verification — Design Spec

**Date:** 2026-05-12
**Status:** Approved (pending user review of this written spec)
**Scope:** Sub-project C of the 3-spec overhaul. Sub-project A (`bigger-po-market-research`) has shipped. Sub-project B (architect/builder/reviewer trio) is queued behind this.
**Predecessor brief:** `docs/superpowers/specs/2026-05-12-freeform-self-verification-brief.md`.

## Problem

In freeform mode, the agent writes code, commits it, opens a PR, and that is the loop. It can run `pytest` via `test_runner` and arbitrary commands via `bash`, but it has no way to:

- Start the project's dev server and see whether the app boots.
- Visit a route and observe what renders.
- Capture a screenshot of a UI change it just made.
- Loop back when the page is broken: "this looks wrong — let me fix it."

The core gap: **the agent ships code without ever observing the running system it just modified.** For UI work especially, "the tests pass" is not the same as "the feature actually works." `test_runner` covers test correctness; nothing today covers feature correctness.

The fix is a new lifecycle phase between coding and PR creation that boots the dev server, drives a headless browser through the routes the agent declared as affected, and asks the agent to self-judge the rendered screenshots. Failure loops back into coding once; second failure blocks the task for human review.

## Out of scope (deferred)

- Golden-image comparison. No baseline images to compare against on a fresh feature; defer until usage data tells us we need it.
- E2E user flows (click X, then assert Y). Only static route renders for this cut.
- User-declared structured assertions (e.g. "page contains text X after clicking Y") via `FreeformConfig`. Possible Spec D.
- Concurrency safeguards beyond OS-allocated ephemeral ports. `MAX_CONCURRENT_TASKS=2` keeps the load light.
- Sandboxing the dev server beyond subprocess + process group. Same trust model as the existing `bash` tool.
- Non-UI freeform tasks (CLI tools, libraries, pure backend changes). These skip the phase entirely; see "When verify runs" below.
- Sub-project B's reviewer agent. C is the horizontal capability B will consume; B is its own spec.

## Architecture

A new `VERIFYING` task status sits between `CODING` and `AWAITING_CI`. After coding finishes its work and is about to transition to `AWAITING_CI` (PR opened, CI awaited), it instead transitions to `VERIFYING` when the plan declares any affected routes. The verify phase boots the dev server in the workspace, drives Playwright through each declared route, captures screenshots, and asks the agent to judge the output. On pass, transition to `AWAITING_CI` (proceed to PR). On fail, transition back to `CODING` with structured failure context. Second consecutive fail → `BLOCKED` with `block_reason="verify_failed"`.

```
CODING
  └─ agent writes code, commits, ready to open PR
       ├─ task.affected_routes empty? → AWAITING_CI    (skip verify; non-UI task)
       └─ task.affected_routes non-empty? → VERIFYING

VERIFYING (handle_verify)
  ├─ start dev server in workspace (ephemeral port, subprocess + new process group)
  ├─ wait_for_port(port, timeout=60s)
  ├─ for each route in task.affected_routes:
  │     result = agent.call_tool("browse_url", f"http://localhost:{port}{route.path}")
  │     persist VerifyAttempt.routes_probed[i] = {path, status, screenshot_path}
  ├─ if any status >= 400:
  │     fail cycle with log_tail + failure_reason="route_error"
  ├─ else:
  │     ask agent to judge screenshots → OK | NOT-OK + reasoning
  ├─ on PASS  → kill server, transition VERIFYING → AWAITING_CI (open PR)
  ├─ on FAIL with cycle == 1 → kill server, transition VERIFYING → CODING with failure context
  └─ on FAIL with cycle == 2 → kill server, transition VERIFYING → BLOCKED (block_reason="verify_failed")
```

Total verify time per cycle capped at 120 s; second cycle also gets 120 s. Server lifetime is scoped to the phase via `try/finally` + process-group kill (`os.killpg(pgid, SIGTERM)` then `SIGKILL` after a grace period).

## When verify runs

Verify is gated on `task.affected_routes` being non-empty. The planner declares this — see "Planner contract change" below. If empty (planner judged the task non-UI), the coding phase skips verify and goes straight to `AWAITING_CI` as it does today.

This is the only gate. No repo-tree sniffing, no `package.json` introspection, no per-repo `FreeformConfig` toggle. The planner is the agent that already reasons about whether the task touches UI; it tells us by listing routes or not.

## Data model

### New table: `verify_attempts`

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `task_id` | int FK → `tasks.id`, indexed | |
| `cycle` | smallint | 1 or 2. |
| `status` | text | `pass` / `fail` / `skipped` / `error`. |
| `routes_probed` | jsonb | `[{path, method, http_status, screenshot_path, viewport}, ...]` — screenshot_path is workspace-relative. |
| `agent_judgment` | text nullable | The agent's OK/NOT-OK verdict + reasoning. Null when the cycle failed before judgment (HTTP error, boot failure). |
| `failure_reason` | text nullable | `boot_timeout` / `route_error` / `judgment_not_ok` / `phase_timeout` / `internal_error`. |
| `log_tail` | text nullable | Last 50 lines of dev server stdout/stderr. Always populated on failure; null on pass. |
| `started_at` | timestamptz | |
| `finished_at` | timestamptz | |

`Task` gains `verify_attempts = relationship(...)`.

### `Task` — additions

| column | type | notes |
|---|---|---|
| `affected_routes` | jsonb default `'[]'` | `[{method, path, label}, ...]` populated by the planner. Empty list = non-UI task. |

That is the only Task addition. `block_reason` already exists for the `BLOCKED` status path.

### `TaskStatus` enum — addition

Add `VERIFYING = "verifying"`. Postgres enum migration uses `ALTER TYPE ... ADD VALUE`.

## Components

### `agent/tools/dev_server.py` (new)

Two surfaces, one file:

**`start_dev_server(workspace_path) -> DevServerHandle`** — **internal helper, not a tool.** Called by the verify phase, not the agent.

- Sniffs run command in priority order:
  1. `FreeformConfig.run_command` (new column, optional override).
  2. `package.json` `scripts.dev` → `npm run dev`.
  3. `Procfile` `web:` entry.
  4. `pyproject.toml` `[tool.auto-agent].run` (new convention).
- Allocates ephemeral port via `socket.socket(AF_INET, SOCK_STREAM); s.bind(('', 0)); port = s.getsockname()[1]; s.close()`. Exports `PORT=<n>` env into the subprocess for frameworks that respect it (Next.js does).
- Spawns via `asyncio.create_subprocess_exec(*shlex.split(cmd), preexec_fn=os.setsid, stdout=PIPE, stderr=STDOUT, cwd=workspace_path, env=...)`. The `setsid` puts the child in a new process group so kill cascades catch npm → node → next.
- Returns `DevServerHandle(pid, pgid, port, log_path, started_at)`. Log path is a workspace-scoped tempfile that an async background task drains the subprocess's stdout into.

**`tail_dev_server_log(lines=50) -> str`** — **agent-callable tool.** Registered via `create_default_registry(..., with_browser=True)`. Returns the last N lines of the current dev server's log. Used by the agent when verify reports a boot or runtime error and the agent wants to diagnose. Reads from the workspace-scoped log file, so it works even if the server has since been killed.

Companion internal helper `wait_for_port(port, timeout=60)` polls TCP `connect()` every 250 ms. Returns when the port accepts a connection; raises `BootTimeout` otherwise.

### `agent/tools/browse_url.py` (new)

Agent-callable tool. Single call:

- **Input:** `url` (required, absolute or relative — verify phase passes `http://localhost:{port}{path}`), `wait_for` selector (optional, for SPA hydration; default `body`), `viewport` (optional, default `{width: 1280, height: 800}`).
- **Action:** Playwright headless Chromium, `page.goto(url, wait_until='networkidle')`, `page.wait_for_selector(wait_for, timeout=15s)`, `page.screenshot(full_page=True)`.
- **Output:** `tool_result` with three content blocks:
  1. `text` block: HTTP status + final URL (after redirects).
  2. `text` block: rendered text content (HTML → readable text via existing `fetch_url`-style markdownification, capped at ~5000 chars).
  3. `image` block: PNG screenshot, base64-encoded, served as Anthropic content type `image`.
- Per-call timeout 30 s; on timeout returns a `text` block with the timeout reason and no image.

Playwright runs inside a single shared `BrowserContext` per verify cycle to amortise startup (~500 ms vs ~2 s per call). Cleanup at phase exit.

### `agent/lifecycle/verify.py` (new)

Mirrors `agent/lifecycle/review.py` shape. Two sub-steps inside the phase:

**Sub-step 1: deterministic probe** (no agent). The phase itself drives Playwright through each declared route, recording HTTP status, response time, and screenshot path. This is fast (~2 s per route after Playwright warmup) and produces baseline data that doesn't need LLM judgment.

**Sub-step 2: single agent judgment.** One `create_agent(..., with_browser=True)` invocation receives the task description, the diff summary, the probe results (routes + statuses + screenshots as `image` content blocks), and is asked to emit `OK` or `NOT-OK: <reason>`. The agent also has access to `browse_url` and `tail_dev_server_log` so it can dig deeper before deciding — e.g., re-screenshot at a different viewport, or check the log for a non-fatal error. Token budget: 20 turns max for the judgment call.

```python
async def handle_verify(task_id: int) -> None:
    task = await get_task(task_id)
    workspace = await ensure_workspace(task)
    routes = task.affected_routes or []

    cycle = await next_cycle_number(task_id)         # 1 or 2
    attempt = await create_verify_attempt(task_id, cycle)

    try:
        async with dev_server.start_dev_server(workspace) as server:
            await dev_server.wait_for_port(server.port, timeout=60)

            # Sub-step 1: deterministic probe
            async with playwright_context() as browser:
                results = [await probe_route(browser, server.port, r) for r in routes]
            attempt.routes_probed = results

            if any(r["http_status"] is None or r["http_status"] >= 400 for r in results):
                return await fail_cycle(attempt, "route_error", server.log_tail(), task, cycle)

            # Sub-step 2: single agent judgment
            verdict = await agent_judge_screenshots(task, results, server)
            attempt.agent_judgment = verdict.reasoning
            if verdict.ok:
                return await pass_cycle(attempt, task)
            return await fail_cycle(attempt, "judgment_not_ok", server.log_tail(), task, cycle)
    except dev_server.BootTimeout as e:
        return await fail_cycle(attempt, "boot_timeout", e.log_tail, task, cycle)
    except asyncio.TimeoutError:
        return await fail_cycle(attempt, "phase_timeout", server.log_tail() if 'server' in locals() else None, task, cycle)
```

Total per-cycle wall-time wrapped in `asyncio.wait_for(..., timeout=120)`. `pass_cycle` opens the PR (see `_finish_coding` change below) and transitions `VERIFYING → AWAITING_CI`. `fail_cycle` transitions `VERIFYING → CODING` with the failure context formatted as the next coding-turn prompt (cycle 1) or `VERIFYING → BLOCKED` with `block_reason="verify_failed"` (cycle 2). The judgment agent has the workspace as readonly — it observes, does not write code.

### `agent/lifecycle/coding.py` (modified)

`_finish_coding` today commits the work, pushes the branch, opens a PR, and transitions `CODING → AWAITING_CI`. We split it:

- Extract a private helper `_open_pr_and_advance(task)` that handles "push branch, open PR, transition to `AWAITING_CI`". This is the existing PR-creation code, now factored out.
- `_finish_coding` becomes a router:
  - If `task.affected_routes == []` → call `_open_pr_and_advance(task)` (current behaviour, no change observable).
  - Else → commit + push branch (no PR yet), transition `CODING → VERIFYING`, dispatch `handle_verify`.
- `pass_cycle` in `verify.py` calls `_open_pr_and_advance(task)` once verify succeeds.

Failing verify never produces a PR — this is the load-bearing behaviour the regression test guards.

If verify loops back (`VERIFYING → CODING`), the next coding turn receives the structured failure context (failed routes, response bodies, screenshots, log tail, agent's previous judgment) as the next user message in the conversation. The branch is unchanged; the agent commits the fix on top.

### `agent/lifecycle/factory.py` (modified)

`create_agent` gains a `with_browser: bool = False` parameter. When `True`, the tool registry adds `browse_url` and `tail_dev_server_log`. Verify-phase agents use `with_browser=True`. Planning/coding agents are unchanged.

### `agent/tools/__init__.py` (modified)

`create_default_registry` gains `with_browser: bool = False`. When `True`, registers `BrowseUrlTool` and `TailDevServerLogTool`.

### `agent/prompts.py` (modified)

`PLANNING_PROMPT` (or wherever the planner's output schema lives) gets one new required field in the plan-output JSON: `affected_routes: list[AffectedRoute]`. Instruction added:

> When your change affects user-visible routes, list each one in `affected_routes` with method, path, and a short label. If the change is purely backend, CLI, or library code with no rendered UI, leave the list empty — verification will be skipped.

A new prompt constant `VERIFY_JUDGMENT_PROMPT` lives alongside the planning prompt. It receives the task description, the diff summary, the affected routes, and the screenshots, and asks for `OK` or `NOT-OK` + reasoning. Strict output format so we can parse the verdict deterministically.

### `shared/models.py` (modified)

- New `VerifyAttempt` ORM model.
- `Task.affected_routes` column (jsonb default `[]`).
- `Task.verify_attempts` relationship.
- `TaskStatus.VERIFYING` enum value.

### `shared/types.py` (modified)

- `AffectedRoute = {method: Literal['GET','POST','PUT','PATCH','DELETE'], path: str, label: str}`.
- `VerifyStatus = Literal['pass','fail','skipped','error']`.
- `VerifyResult` Pydantic model mirroring the ORM row, for API responses.

### `shared/config.py` (modified)

- `FreeformConfig.run_command: str | None = None` — optional explicit override for sniffing failure cases. Documented in the existing FreeformConfig docstring.

### `shared/events.py` (modified)

New event builders: `verify_started`, `verify_passed`, `verify_failed`. Registered in `agent/lifecycle/_orchestrator_api.py`.

### `orchestrator/state_machine.py` (modified)

Add `TaskStatus.VERIFYING` to the transitions table:
- `CODING → VERIFYING` (new).
- `VERIFYING → AWAITING_CI` (pass).
- `VERIFYING → CODING` (fail, retry).
- `VERIFYING → BLOCKED` (fail, exhausted).

### `orchestrator/router.py` (modified)

One new endpoint: `GET /api/tasks/:id/verify-attempts` → returns the list of `VerifyAttempt` rows for the task as JSON. Used by `web-next` to render the verify section on task detail.

Screenshots are served as static files from a workspace-scoped path: `GET /api/tasks/:id/verify-attempts/:cycle/screenshots/:filename` → streams the PNG from disk. No upload, no S3 — files live in the workspace until cleanup, copied to `var/verify-screenshots/<task-id>/<cycle>/` for durability across workspace cleanup. The orchestrator owns this directory.

### `web-next/` (modified)

One new component, one extension:

1. **`web-next/components/task/VerifyAttempts.tsx`** — renders the per-cycle verify history on the task detail page: status badge, routes probed with HTTP status, screenshot thumbnails (click to enlarge), the agent's judgment text, log tail (collapsed). Pulls from the new endpoint via a `useVerifyAttempts(taskId)` hook in `web-next/hooks/`.
2. **`web-next/app/(app)/tasks/[id]/page.tsx`** — mounts `<VerifyAttempts taskId={id} />` between the existing review section and the PR link.

No changes to the suggestions page or PO views. No legacy `web/` changes.

## Data flow

1. Planning phase produces a plan and now also sets `task.affected_routes` (may be empty).
2. Coding phase writes code, commits, ready to ship.
3. `_finish_coding`:
   - If `task.affected_routes == []` → transition `CODING → AWAITING_CI`, open PR (current behaviour).
   - Else → transition `CODING → VERIFYING`, dispatch `handle_verify`.
4. `handle_verify` runs (see component above). Server boots, routes probed, screenshots taken, agent judges.
5. On pass: transition `VERIFYING → AWAITING_CI`, open PR. Existing CI / review flow continues.
6. On fail cycle 1: persist `VerifyAttempt` with failure context, transition `VERIFYING → CODING`, post failure context to the task message stream. Agent's next coding turn sees the failures and screenshots.
7. On fail cycle 2: persist `VerifyAttempt`, transition `VERIFYING → BLOCKED` with `block_reason="verify_failed"`. User sees the failures in `web-next`.

## Error handling

| Failure | Behavior |
|---|---|
| Dev server fails to boot (port timeout) | `failure_reason="boot_timeout"`, log tail attached, fail cycle. Coding turn sees the log tail and the boot command. |
| Run command can't be sniffed and no `FreeformConfig.run_command` set | `failure_reason="boot_timeout"` with explicit "no run command found" log. Coding turn is told to either fix the project's `package.json` `dev` script or surface a clarification to the user. |
| Route returns 4xx/5xx | `failure_reason="route_error"`, per-route status + response body + log tail. Coding turn sees the failing routes. |
| Playwright timeout on a single route | Recorded as `http_status=null`, `error="navigation_timeout"` in `routes_probed`. Counts as a route failure. |
| Agent judges screenshots NOT-OK | `failure_reason="judgment_not_ok"` + reasoning. Coding turn sees the screenshots + reasoning. |
| Verify phase exceeds 120 s | `asyncio.TimeoutError` → kill server, `failure_reason="phase_timeout"`. |
| Server crashes mid-probe | Detected via `process.returncode is not None`; remaining routes recorded as `error="server_dead"`. Fail cycle. |
| Cleanup: dev server orphans | `try/finally` calls `os.killpg(pgid, SIGTERM)` then `SIGKILL` after 2 s grace. If `killpg` raises `ProcessLookupError`, log and continue. |
| Playwright not installed in deployment | Verify phase logs `verify_setup_error` event, fails cycle, blocks the task with a setup-actionable message. CI ensures Playwright + Chromium are in the runtime image. |

## Testing

Following the patterns in `tests/test_po_with_market_research.py` (Spec A's regression test pattern).

### Unit tests

- **`tests/test_dev_server.py`**
  - `test_sniff_npm_dev`: fixture with `package.json` containing `scripts.dev` → returns `npm run dev`.
  - `test_sniff_procfile`: fixture with `Procfile` → returns the `web:` command.
  - `test_sniff_override`: when `FreeformConfig.run_command` is set, it wins over sniffing.
  - `test_sniff_failure`: no recognised manifest → returns `None`, verify phase reports `boot_timeout` with "no run command found".
  - `test_wait_for_port_success`: stub a listening socket, assert `wait_for_port` returns.
  - `test_wait_for_port_timeout`: nothing listens, assert `BootTimeout` raised after timeout.
  - `test_process_group_kill`: spawn a child process that itself forks; assert all descendants are killed by `os.killpg`.

- **`tests/test_browse_url.py`**
  - `test_returns_image_block`: Playwright fully mocked; assert the `tool_result` contains exactly one `image` block with PNG content type.
  - `test_text_capped`: page with 50k chars of content → text block capped at ~5000 chars.
  - `test_timeout_returns_text_only`: simulate Playwright timeout → no image block, text block with timeout message.

- **`tests/test_verify_phase.py`** (the big one)
  - `test_skip_when_no_routes`: `task.affected_routes=[]` → verify is not invoked, transition goes `CODING → AWAITING_CI` directly. (Test on `_finish_coding`, not on `handle_verify`.)
  - `test_pass_path`: fixture workspace with a working `index.html` served by a stub server; agent judgment stubbed to OK → transition `VERIFYING → AWAITING_CI`, `VerifyAttempt.status="pass"`.
  - `test_route_error_loops_back`: route returns 500 → `VerifyAttempt.status="fail"`, `failure_reason="route_error"`, transition `VERIFYING → CODING`.
  - `test_judgment_not_ok_loops_back`: routes 200 but stubbed agent says NOT-OK → fail with `failure_reason="judgment_not_ok"`.
  - `test_second_failure_blocks`: cycle=2, fail → transition `VERIFYING → BLOCKED`, `block_reason="verify_failed"`.

### Regression test (load-bearing)

- **`tests/test_no_pr_on_failed_verify.py`**
  - Fixture: a workspace with an intentionally broken `package.json` `dev` script (e.g., `"dev": "node -e 'process.exit(1)'"`). Plan has `affected_routes=[{method:"GET",path:"/",label:"home"}]`.
  - Run the coding-finish flow end-to-end with stubs that always say "ready to ship."
  - Assert: no PR is created, task ends in `BLOCKED` (after 2 cycles), and the existing PR-creation code path is never reached.
  - This test is the gating reason we cannot regress to "ships visually-broken code anyway." It is the spec's equivalent of Spec A's `test_po_drops_ungrounded_suggestions`.

### Integration tests

- **`tests/test_verify_e2e_smoke.py`** (slow, marked `@pytest.mark.slow`)
  - Real Playwright + a tiny `python -m http.server` fixture serving a real HTML page.
  - Asserts the full path: dev server boots, browse_url returns an image block, verify passes.
  - Optional in CI (skipped in the fast pre-commit run, included in nightly).

### Not tested here

- Real Next.js boot — covered by the e2e smoke test against a tiny fixture; full framework startup is too slow for unit tests.
- Verification *quality* — eval territory (`eval/`); a follow-up task adds a verify-aware case to the agent eval after this ships.
- `web-next` rendering correctness — manual smoke on the task detail page after deployment.

## Migrations

One Alembic migration:

- `ALTER TYPE task_status ADD VALUE 'verifying'`.
- Add `affected_routes jsonb default '[]'::jsonb not null` to `tasks`.
- Add `run_command text` to `freeform_configs`.
- Create `verify_attempts` table per the data model.
- Indexes: `(task_id, cycle)` unique on `verify_attempts`.

All additions backwards-compatible: defaulted `affected_routes`, nullable `run_command`, new table.

## Acceptance criteria

1. Tasks whose plan declares `affected_routes` cannot reach `AWAITING_CI` without a passing `VerifyAttempt`. The regression test `test_no_pr_on_failed_verify` enforces this.
2. Tasks whose plan declares empty `affected_routes` proceed `CODING → AWAITING_CI` exactly as today; verify is never invoked.
3. The verify phase runs at most 2 cycles per task. After 2 fails, the task is in `BLOCKED` with `block_reason="verify_failed"`.
4. `browse_url` tool returns an `image` content block that the agent's vision-capable model can reason over.
5. Dev server processes are killed on phase exit, including descendants (npm → node → next). No orphans observed in a soak test over 50 verify cycles.
6. `web-next` task detail page renders per-cycle verify attempts with screenshots, routes probed, and the agent's judgment.
7. The full existing test suite (`tests/`) still passes. `ruff check .` clean.

## Open questions / follow-ups (not blocking implementation)

- Should `run_command` be promoted from `FreeformConfig` to a `.auto-agent/run.sh` repo-side contract once we've seen which projects need overrides? Defer the call until usage data exists.
- Should `affected_routes` accept request bodies / headers for POST/PATCH probes? For now GET-only is the dominant case; POST routes typically don't have visual output and would just go through HTTP probing without screenshot judgment. Revisit if a task surfaces the need.
- Eval task case for "verify catches a visually-broken UI" — added in a follow-up after this spec ships.
- Sub-project B's reviewer agent will consume `browse_url` and `tail_dev_server_log` directly. No changes needed here to enable that — it's just tool registry composition.
