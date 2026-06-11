# [ADR-023] Code graph as the agent's navigation substrate

> **Summary:** Close the graph's navigation loop (search_symbols in, get_symbol_source out), bridge it to the claude_cli path via a per-task stdio MCP server, and make "search before writing a new helper" an explicit convention — so agents stop paying grep + whole-file-read context costs and stop duplicating existing methods.

## Status

Accepted

Supplements [ADR-016] (the code graph itself — unchanged and still binding).

## Context

ADR-016 shipped the graph and a `query_repo_graph` tool, but in practice agents kept grepping and reading whole files. Three structural reasons, found while comparing our graph against Serena (oraios' LSP-backed MCP toolkit):

1. **No entry point.** Every op took a `node_id` the agent could only learn by reading files first — the graph could answer "who calls X?" but not "where is X?". The nudge even said "use grep to find candidate symbols", conceding the first step to the expensive path.
2. **No exit point.** A relationship query still ended in a whole-file `file_read` to see any code. The reads the graph was meant to save were still happening.
3. **Invisible in prod.** With `LLM_PROVIDER=claude_cli` the passthrough hands the task to `claude --print`, which only sees MCP servers — in-process Python tools don't exist on that path. The graph was unreachable exactly where most tasks run.

Separately, the repo owner's second goal for the graph — reducing method duplication — had only retrospective support (`clones` reports duplication after it shipped); nothing pushed the agent to check for an existing equivalent *before* writing a new helper.

## Decision

Four pieces, all additive to ADR-016:

### 1. `search_symbols` op — name → node ids

`{query, kind?, area?, limit?=20}` over the stored blob. Case-insensitive, ranked: exact label match, label prefix, label substring, then id substring (catches path-style queries). Returns nodes with the standard existence flags. This replaces grep as the first step of navigation and is the primitive that makes the dedup convention actionable.

### 2. `get_symbol_source` op — node id → just that symbol's code

`{node_id, context_lines?=0}` reads the node's recorded line range from the analyser workspace. The window read (traversal guard, 500-line / 50 KiB caps) is extracted from the `/graph/code` preview endpoint into `agent/graph_analyzer/source_window.py` — one owner, two consumers. A missing file returns an explicit "graph may be stale" error rather than wrong content.

### 3. Per-task stdio MCP server for the CLI path

`agent/mcp/code_graph_server.py` wraps the same `QueryRepoGraphTool` behind one MCP tool. `build_mcp_servers(settings, repo_id=...)` adds it to the per-task `--mcp-config` (targets `{"cli"}` only — native keeps the in-process tool), pinned via `CODE_GRAPH_REPO_ID`, with `DATABASE_URL` forwarded so the subprocess resolves the right Postgres regardless of cwd. This follows the team-memory precedent exactly and was the Serena lesson: distribution-via-MCP is what makes a code-intelligence layer usable by any agent.

### 4. Dedup as a stated convention

The system-prompt nudge (and the MCP tool description) now instruct: before writing a new helper or utility, `search_symbols` for an existing equivalent — duplicating one is a defect. Cheap to state, actionable now that the search op exists.

## Consequences

### What becomes easier

- The full navigation loop — name → node → relationships → source — runs inside the graph, costing tens of lines of context instead of grep output plus whole files.
- The graph works on the prod claude_cli path for the first time, through the identical query code.
- Duplication prevention moves from retrospective (`clones` findings, health loop) to write-time (search-first convention).

### What becomes harder / risks

- **Stale graph, navigation edition.** An agent that trusts `get_symbol_source` on a drifted graph reads outdated code. Mitigated by the existing staleness envelope and the explicit stale-file error; genuinely fixed only by refresh-on-push (still deferred, see below).
- One more spawned subprocess per CLI task (the stdio server); it's idle unless called.
- The MCP server needs DB reachability from wherever the CLI runs — true today (same container), an assumption to revisit if the CLI ever runs remotely.

### Deferred (intentionally not in this change)

- **Auto-refresh on push** — prerequisite for agents *relying* on the graph rather than double-checking; separate decision with webhook/debounce design.
- **Task-start graph-slice injection** (ADR-016 already defers this) — revisit once `search_symbols` usage gives signal on what a useful slice is.
- **Reviewer-side write-time clone check on diffs** — complements the convention; belongs with the health-loop work.
- **HTTP query endpoint for external agents** (e.g. a laptop Cursor session) — needs an auth story first; the stdio server covers all in-container consumers.
