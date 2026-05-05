# Multi-tenant Claude credentials + 5-worker concurrency

**Date:** 2026-05-05
**Status:** Approved (design); awaiting implementation plan

## Problem

Auto-agent currently runs the Claude Code CLI as a subprocess that inherits the
container's single `~/.claude/` credential store. Onboarding a new user means
SSHing into the VM and running `claude login`. The whole team shares one
subscription; tasks queued by user A consume user B's quota. Concurrency is
also capped at 1 complex + 1 simple task globally.

We want each teammate to bring their own Claude subscription, authenticate
through the web UI, and have their tasks run under their own credentials with
no spill-over. We also want at least 5 concurrent CLI workers.

## Goals

1. Each user authenticates their own Claude CLI session through the web UI
   (no SSH, no credential pasting).
2. A task queued by user X always runs with user X's credentials. No path
   exists for one user's task to use another user's `.claude/` directory.
3. The orchestrator runs up to 5 CLI workers concurrently, FIFO across users.
4. The system detects when a user's token has expired and prompts them to
   reconnect; queued tasks pause until they do.

## Non-goals

- Multi-tenancy for the Bedrock or native-Anthropic API providers. Those
  remain shared/global; only the CLI provider (which is what backs the
  subscription flow) becomes per-user.
- Rate-limiting, billing, or per-user quotas beyond "you must be paired."
- Replacing the existing JWT auth. We add to it, not replace it.

## Architecture

Three changes, scoped narrowly:

1. **Per-user credential vault on disk:** `/data/users/<user_id>/.claude/`
   (mode 0700, owned by the app user). The CLI's normal token-refresh
   behavior writes back into this directory in place; we never read or copy
   the token contents ourselves.
2. **In-UI pairing flow** that drives the `claude` CLI in a PTY
   (pseudo-terminal), scrapes the login URL out of stdout, forwards the
   user's pasted one-time code back into stdin, and lets the CLI itself
   write the credentials into the user's vault.
3. **5-slot global FIFO worker pool.** Tasks dispatch with
   `HOME=/data/users/<task.created_by_user_id>` so the CLI subprocess sees
   only that user's vault.

## Components

### 1. Pairing flow (UI ↔ CLI in a PTY)

A "PTY" is a pseudo-terminal — an OS primitive that lets a parent process
pretend to be a human at a keyboard for a child process. We need it because
the `claude` login command checks whether stdin/stdout are an interactive
terminal and refuses to run otherwise. We'll use `ptyprocess` (or `pexpect`)
in Python rather than raw `pty.fork`.

**New page in `web-next`:** Settings → Connect Claude.

**Endpoints (FastAPI, `orchestrator/router.py`):**

- `POST /api/claude/pair/start` (auth required) — spawns
  `claude setup-token` (or the equivalent `claude login` invocation; the
  exact command is verified during implementation against the installed
  CLI version) inside a PTY with `HOME=/data/users/<user_id>`. Returns a
  `pairing_id`. The backend keeps the PTY in an in-process registry keyed
  by `pairing_id`, with a 5-minute TTL and forced cleanup on expiry.
- `WS /ws/claude/pair/<pairing_id>` — streams the PTY's stdout to the
  browser line by line. The frontend extracts the `https://claude.ai/...`
  URL with a regex and renders an "Open this link" button plus a code-paste
  input.
- `POST /api/claude/pair/code` `{pairing_id, code}` — writes the code plus
  newline to the PTY's stdin.
- On PTY exit code 0, the backend verifies
  `/data/users/<user_id>/.claude/.credentials.json` exists, sets
  `users.claude_auth_status = 'paired'` and `users.claude_paired_at = now()`,
  emits an event over the existing Redis/WS bus so the UI advances to
  "Connected", and any of that user's `blocked_on_auth` tasks are
  auto-requeued. On non-zero exit, the captured stderr is returned to the
  UI.

**Re-pair** simply overwrites the user's `.claude/` directory.
**Disconnect** `rm -rf`s the directory and sets
`claude_auth_status = 'never_paired'`.

### 2. Auth-state detection + re-prompt

**New column on `users`:** `claude_auth_status` enum (`paired`, `expired`,
`never_paired`), default `never_paired`. We also keep
`claude_paired_at TIMESTAMP NULL` as an audit timestamp, but
`claude_auth_status` is the value that gates dispatch.

**At task dispatch time**, before spawning the worker, the orchestrator
probes the user's credentials by running a minimal CLI invocation
(e.g. `claude --print --dangerously-skip-permissions "ping"`) with that
user's `HOME` and a short timeout. If it exits non-zero with an
auth-related stderr signature (`unauthorized`, `expired`, `please log in`,
or similar — the exact patterns are determined empirically during
implementation), the orchestrator:

1. Sets `claude_auth_status = 'expired'` for that user.
2. Moves the task to a new `blocked_on_auth` state (not `failed` — it
   resumes once the user re-pairs).
3. Emits a real-time event so the UI shows a banner:
   **"Your Claude session expired. Reconnect to resume your queued tasks."**
   with a link to Settings → Connect Claude.

**On successful re-pair**, the pairing endpoint flips
`claude_auth_status = 'paired'` and emits an event that requeues any of
that user's `blocked_on_auth` tasks back to `queued`.

**At submit time**, the orchestrator rejects new task submissions from
users whose `claude_auth_status != 'paired'` with HTTP 400 and the same
"Connect your Claude account" UI banner.

A lightweight background probe (every ~6h) over all `paired` users to
proactively downgrade expired ones early is a nice-to-have, not required
for v1. The dispatch-time check is the source of truth.

### 3. Worker pool (5-slot global FIFO)

- `shared/config.py`: replace `max_concurrent_simple` and
  `max_concurrent_complex` with a single
  `max_concurrent_workers: int = 5`. (The legacy fields can stay as
  deprecated aliases during migration if anything else reads them, then be
  deleted; verified during implementation.)
- `orchestrator/queue.py::can_start_now` becomes
  `active_count() < settings.max_concurrent_workers`. FIFO across all
  users — first eligible task gets the next free slot, regardless of who
  owns it.
- Task dispatch reads `task.created_by_user_id`, derives the vault path
  `/data/users/<id>`, and threads it through `agent/main.py` →
  `ClaudeCLIProvider`.
- `agent/llm/claude_cli.py::ClaudeCLIProvider.__init__` gains a
  `home_dir: str | None` argument. `_invoke_cli_once` passes
  `env={**os.environ, "HOME": home_dir}` to `create_subprocess_exec`
  whenever `home_dir` is set. No other env changes — the workspace `cwd`
  is still the per-task git clone in `agent/workspace.py`.

This is the isolation boundary: the CLI subprocess only ever reads from
the `HOME` we pass it, and `HOME` is derived from the task's owner. There
is no shared in-process state between concurrent workers that could leak
credentials across them.

## Data model

```sql
ALTER TABLE users
  ADD COLUMN claude_auth_status TEXT NOT NULL DEFAULT 'never_paired'
    CHECK (claude_auth_status IN ('paired', 'expired', 'never_paired')),
  ADD COLUMN claude_paired_at TIMESTAMP NULL;
```

Tasks gain a new value in their existing status enum:
`blocked_on_auth` (in addition to whatever values exist today; verified
in `shared/models.py` and `orchestrator/state_machine.py` during
implementation).

No tokens are stored in the database. Credentials live only in
`/data/users/<user_id>/.claude/` on the VM disk.

## Security

- Vault directories are created mode 0700, files mode 0600, owned by the
  app user.
- The vault path is derived deterministically from `user_id`; users have
  no path-traversal vector because the user_id comes from the verified
  JWT, not from a request body.
- The PTY's stdout is streamed to the WebSocket but never persisted to
  logs or DB. The login URL itself is not sensitive (it's the same URL
  Anthropic shows anyone running `claude login`), but the one-time code
  the user pastes IS sensitive — it's only ever in transit and is forwarded
  directly into the PTY's stdin without being logged.
- The contents of `.credentials.json` are never read or logged by our
  code. We only check the file's existence to confirm pairing succeeded.
- `/data` should already be on the encrypted persistent disk on the VM.
  If it isn't, that's a one-line VM-config change and is a prerequisite
  for shipping this.
- Bedrock and native-Anthropic providers retain their current shared
  configuration; this design does not change their security posture.

## Testing

- **Unit:** `ClaudeCLIProvider` honors `home_dir` — mock
  `create_subprocess_exec` and assert `env["HOME"]` matches the expected
  per-user vault path.
- **Unit:** queue admits up to 5 concurrent tasks and rejects task
  submissions from users whose `claude_auth_status != 'paired'`.
- **Unit:** dispatch-time auth probe correctly classifies stderr
  signatures into `paired` vs `expired`, and `expired` transitions move
  the task to `blocked_on_auth` rather than `failed`.
- **Unit:** successful re-pair emits the requeue event and moves
  `blocked_on_auth` tasks back to `queued`.
- **Integration:** pairing endpoint with a fake `claude` binary (a small
  shell script that prints a URL, reads a code from stdin, writes a fake
  credentials file, and exits 0). Verifies the full PTY round-trip
  without hitting Anthropic.
- **Manual:** one end-to-end pairing flow against the real CLI on the VM,
  by a real user, before declaring the feature done.

## Open questions resolved during brainstorming

- Onboarding flow: in-app OAuth (option A), not credential paste or API
  keys.
- Storage: per-user `HOME` on the VM disk (option A), not encrypted-in-DB.
- Concurrency fairness: global FIFO across all users, no per-user cap.
- Auth detection: dispatch-time probe with re-prompt, plus optional
  background sweep.

## Out of scope (explicit)

- Per-user UI theming, billing, or quotas.
- Migrating Bedrock/native-Anthropic providers to per-user.
- Replacing the existing JWT auth or user-management UI.
- Sharing tasks between users / handoffs.
