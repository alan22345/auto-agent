# [ADR-010] Subprocess execution as a single seam

## Status

Accepted

## Context

Ten async-subprocess call sites across the agent layer each hand-rolled
the same pattern: build argv, call `asyncio.create_subprocess_exec` (or
`_shell`), wrap `proc.communicate()` in `asyncio.wait_for(...)`, decode
bytes, kill on timeout, and assemble stdout/stderr/exit-code into either
a `ToolResult` or a `RuntimeError`. The duplication had — predictably —
grown three independent divergence bugs:

1. **`agent/lifecycle/review.py::find_existing_pr_url`** ran
   `gh pr list` with no timeout at all. A network stall on the GitHub
   API would block the agent loop indefinitely, and a single hung
   request would wedge the entire task lifecycle until manual
   intervention.
2. **`agent/workspace.py::_run_git`** had no timeout *and* did not set
   `GIT_TERMINAL_PROMPT=0`. A clone or fetch against a remote that
   demanded credentials would block on a TTY that did not exist.
3. **`agent/tools/git.py`** and **`agent/tools/test_runner.py`** caught
   `asyncio.TimeoutError` but skipped the `proc.kill()` step that
   `agent/tools/bash.py` had — leaking the child process. Meanwhile
   `agent/lifecycle/deploy.py::_try_local_deploy` caught the wrong
   exception class (`TimeoutError` instead of `asyncio.TimeoutError`),
   subtly broken across Python versions.

This is the same anti-pattern flagged in *DEEPENING.md*: an invariant
copied to N call sites is an invariant that won't actually be
maintained, and indeed three of ten copies had drifted from the
correct behaviour.

The interface every caller actually wanted was a single line:

```python
result = await sh.run(argv, cwd=..., timeout=..., env=...)
```

— with stdout, stderr, returncode, and a `timed_out` boolean returned
in a small dataclass.

## Decision

Introduce `agent/sh.py` as the single subprocess seam for the agent
layer. It exports:

- `RunResult(stdout, stderr, returncode, timed_out, argv)` — frozen
  dataclass with a `failed` predicate (`timed_out or returncode != 0`).
- `async def run(argv, *, cwd, timeout, env=None, stderr_to_stdout=False, max_output=None) -> RunResult` —
  argv form via `create_subprocess_exec`.
- `async def run_shell(command, *, cwd, timeout, env=None, ...) -> RunResult` —
  shell form via `create_subprocess_shell` for the bash tool and the
  test_runner.

The seam owns five invariants in one place:

1. **Env merge**: `os.environ` ∪ `{"GIT_TERMINAL_PROMPT": "0"}` ∪
   caller-supplied env (caller wins on conflict). No accidentally
   prompting `git`/`gh` invocation can ever block on a missing TTY.
2. **Timeout-with-kill**: on `asyncio.TimeoutError`, the process is
   killed and drained via `communicate()` (with a 2s drain cap so the
   cleanup path can't itself hang), and the result carries
   `timed_out=True`.
3. **Decode-with-replace**: stdout/stderr decoded with
   `errors="replace"` so non-UTF-8 output never raises.
4. **Optional `max_output` truncation** with the labelled-tail format
   the bash tool already used (`... (truncated, N total chars)`).
5. **Optional `stderr_to_stdout`** stream redirection for test_runner.

`bash`, `git`, `test_runner`, `_run_git` (workspace), `find_existing_pr_url`,
`create_pr`, the two `git checkout` blocks, the deploy script /
`make deploy-dev` block, the `gh pr create` block in `harness.py`, and
the three `_get_head_sha` / `_get_changed_files` / `_run_git` blocks in
`agent/context/system.py` all collapse to argv-building + result
formatting around `sh.run` / `sh.run_shell`. `RunResult` is a small
domain-free dataclass; `ToolResult` is composed from it where tools
need the LLM-facing format.

The seam's tests (`tests/test_sh.py`) exercise actual subprocesses
(`python3 -c "..."`) for the success / non-zero-exit / timeout-with-kill
/ env-merge / decode-replace / shell-mode / truncation / stderr-merge
paths — no `unittest.mock.patch("asyncio.create_subprocess_exec")`.
The wall-clock check in `test_run_kills_on_timeout` is the proof the
kill path actually fired.

## Consequences

**Wins.**

- The `gh pr list` no-timeout hang in `find_existing_pr_url` is fixed
  for free — the seam's 20s timeout returns `RunResult(timed_out=True,
  failed=True)`, and the function's existing `if result.failed: return
  None` guard naturally falls through to `gh pr create`. A regression
  test (`test_returns_none_on_timeout`) pins this.
- `_run_git` in workspace inherits the 60s timeout and
  `GIT_TERMINAL_PROMPT=0` it was missing. No more credential-prompt
  hangs in fresh containers.
- The two missing-kill leaks (`tools/git.py`, `tools/test_runner.py`)
  and the `TimeoutError`-vs-`asyncio.TimeoutError` mismatch in
  `lifecycle/deploy.py` all disappear because the seam owns the kill
  invariant.
- Tests for the seam exercise actual `/bin/echo`-style commands once;
  per-tool tests can now inject a fake `RunResult` (see
  `tests/test_create_pr_idempotent.py`, `tests/test_tools_subprocess_seam.py`)
  rather than reaching for `unittest.mock.patch("asyncio.create_subprocess_exec")`.

**Trade-offs.**

- One module-level entry point per subprocess flavour (`run` / `run_shell`).
  Two functions instead of one was deliberate — `subprocess_exec` and
  `subprocess_shell` accept different argument shapes (`argv` vs.
  `command`), and a single `shell=True` keyword would obscure that.
- `RunResult.returncode` is `Optional[int]` only because the kernel may
  not have reaped a killed process by the time we return. Callers
  branch on `failed` / `timed_out`, not `returncode`.

**Alternatives rejected.**

- *Make it a Protocol with two adapters (real + in-memory).* Tempting
  given the precedent set by ADR-007 (Publisher) — but there is exactly
  one production subprocess implementation, and tests already get the
  swap point they need by patching the module-level `sh.run` /
  `sh.run_shell` references inside each call site. "One adapter is
  hypothetical, two is real" cuts the other way here: the deletion test
  alone justifies the seam (removing `sh.py` re-spreads the timeout-kill
  invariant to ten call sites and re-introduces three independent
  divergence bugs).
- *Fold `agent/llm/claude_cli.py::_invoke_cli_once` into `sh.py` too.*
  Out of scope. That call sits behind the `LLMProvider` seam (ADR-006)
  and owns its own session-collision recovery (`_run_cli`'s rotate-
  fresh-UUID-and-retry-once contract). Its concern is "make one Claude
  CLI request"; mixing in a generic subprocess utility would muddy two
  distinct deep modules. A future refactor can deepen it separately if
  ever justified, but bundling now would weaken both seams.
- *`eval/providers/*` subprocess calls.* Out of scope — `eval/` is the
  promptfoo benchmark harness, not part of the agent runtime layer.
