# Pluggable MCP support for auto-agent

**Date:** 2026-06-02
**Status:** Approved (design)

## Goal

Let the auto-agent reach external MCP servers, provider-agnostically and via a
single config-driven list. First two servers:

- **ergodic-ui** — HTTP MCP design system (`init`, `list_components`,
  `get_component`). Useful for any `web-next/` UI work so components are built
  from the shared, token-styled system instead of hand-rolled.
- **team-memory** — already reached **in-process** by the native tool loop
  (`agent/tools/recall_memory.py` → `team_memory.graph.GraphEngine` over the
  shared Postgres). It is a **stdio** MCP server. Native mode keeps its
  in-process tools (faster, no duplicate); the **claude_cli pass-through** mode
  — which can't see in-process Python tools — gets team-memory via the CLI
  `--mcp-config`, closing its current no-memory gap.

## Acceptance criteria

1. `build_mcp_servers(settings)` returns ergodic-ui (targets native+cli, only
   when its token is set) and team-memory (targets cli-only, only when the DB
   URL is set); missing-secret entries are skipped and logged.
2. Native (bedrock/anthropic) coding agents expose `mcp__ergodic-ui__*` tools;
   calling one round-trips to the HTTP server and returns its result.
3. claude_cli invocations include `--mcp-config <file> --strict-mcp-config`
   carrying ergodic-ui (http) + team-memory (stdio); omitted when MCP is
   disabled or no cli-target servers resolve.
4. A down/unreachable server logs a warning and is skipped — it never crashes
   or blocks a task.
5. Readonly/review agents get **no** MCP tools (design-system tools are useless
   there and would bloat the catalogue).
6. `mcp_enabled = False` disables everything (both paths).
7. New unit tests pass; `ruff check .` clean; full suite green.

## Components

### `shared/config.py`
- `mcp_enabled: bool = True` — master switch.
- `ergodic_ui_mcp_url: str = "https://ergodic-ui-mcp.fly.dev/mcp"`.
- `ergodic_ui_mcp_token: str = ""` — from env (`.env`); required to enable ergodic-ui.
- Reuse existing `team_memory_database_url`.

### `agent/mcp/servers.py` — single source of truth
- `McpServerSpec` (pydantic/dataclass): `name`, `transport` (`"http"|"stdio"`),
  `targets: frozenset[str]` (`{"native","cli"}`), and transport fields:
  http → `url`, `headers`; stdio → `command`, `args`, `env`.
  - `to_cli_entry()` → the Claude Code `mcpServers[name]` dict shape.
- `build_mcp_servers(settings) -> list[McpServerSpec]`:
  - ergodic-ui: http, `targets={"native","cli"}`, header
    `Authorization: Bearer <token>` — included iff token set.
  - team-memory: stdio, `targets={"cli"}`,
    `command = shutil.which("team-memory") or sys.executable`,
    `args = ["serve"]` (or `["-m","team_memory","serve"]` for the
    interpreter fallback), `env = {"TEAM_MEMORY_DATABASE_URL": <db url>}` —
    included iff DB URL set. (Resolves to the auto-agent's *own* installed
    package, not a host path; verified during implementation.)
  - Returns `[]` when `mcp_enabled` is False.

### `agent/mcp/client.py` — native HTTP client
- `McpHttpClient(url, headers, timeout)` over the official `mcp` SDK
  (`streamablehttp_client` + `ClientSession`):
  - `async list_tools() -> list[McpToolDef]`
  - `async call_tool(name, args) -> str` (concatenate returned content blocks).
- Opens a session per operation (infrequent design-system reads; avoids
  managing a long-lived connection across the agent lifecycle). All failures
  raise `McpUnavailable`, which callers degrade on.

### `agent/mcp/tool_adapter.py` — registry bridge
- `McpTool(Tool)`: `name = f"mcp__{server}__{tool}"`, `description` +
  `parameters` from the remote `inputSchema`, `is_readonly = True`,
  `execute()` → `client.call_tool(...)`, mapping result → `ToolResult`
  (`is_error=True` on `McpUnavailable`/tool error).
- `async register_mcp_tools(registry, native_specs) -> int`:
  - For each http spec targeting `native`: discover tools, wrap, register.
  - **Per-process discovery cache** keyed by `(name,url)` so repeated tasks pay
    no network cost; a down server logs a warning and is skipped.
  - Idempotent (registry.register overwrites by name).

### Wire-up — `agent/loop.py`
Near the existing claude_cli setup (`loop.py:234-239`):
- **Pass-through provider:** also call `self._provider.set_mcp_servers(cli_specs)`
  where `cli_specs = [s for s in build_mcp_servers(settings) if "cli" in s.targets]`.
- **Native provider:** in the async run path, once, if `self._with_mcp` and the
  provider is not passthrough, `await register_mcp_tools(self._tools, native_specs)`
  where `native_specs = [s for s in build_mcp_servers(settings) if "native" in s.targets and s.transport == "http"]`.

`AgentLoop` gains `with_mcp: bool = True`; the factory passes
`with_mcp = not readonly` (criterion 5).

### Wire-up — `agent/llm/claude_cli.py`
- `set_mcp_servers(specs)` stores cli specs.
- In `_invoke_cli_once`, if specs present, write
  `{"mcpServers": {name: spec.to_cli_entry()}}` to a `NamedTemporaryFile`
  (cleaned up after the call) and append
  `["--mcp-config", path, "--strict-mcp-config"]` to `cmd`. `--strict-mcp-config`
  makes behaviour independent of the container HOME; existing
  `--dangerously-skip-permissions` auto-approves the tools.

## Error handling
`mcp_enabled` kill-switch. Discovery + call failures degrade (warn + skip /
`is_error` result); never crash a task. A malformed/missing temp file path is
guarded so the CLI still runs without MCP rather than failing.

## Tests (TDD)
- `tests/test_mcp_servers.py` — `build_mcp_servers`: right servers/targets,
  skips on missing secrets, empty when disabled; `to_cli_entry` shapes.
- `tests/test_mcp_tool_adapter.py` — `McpTool.execute` result + error mapping;
  `register_mcp_tools` registers from a fake client and skips gracefully on
  failure; discovery cache hit.
- `tests/test_claude_cli_mcp.py` — `--mcp-config` JSON contains both servers in
  correct shape; flags absent when disabled / no cli specs.

## Out of scope
- A JSON `MCP_SERVERS` env var for arbitrary additional servers (the
  `build_mcp_servers` function is the extension point for now).
- Replacing the in-process team-memory tools in native mode.
