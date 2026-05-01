# MCP Memory Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Claude Code CLI native tool access to the org's graph memory via an MCP server, plus a slash command for post-task reflection.

**Architecture:** A single-file MCP server (`agent/mcp_memory_server.py`) using FastMCP that reuses the existing SQLAlchemy queries from `agent/tools/memory_read.py` and `agent/tools/memory_write.py`. Configured in `.claude/settings.local.json` so Claude Code auto-spawns it. A slash command in `.claude/commands/memory-reflect.md` replaces the old `MEMORY_REFLECTION_PROMPT`.

**Tech Stack:** `mcp[cli]` (FastMCP), SQLAlchemy async, existing PostgreSQL database

---

### Task 1: Create the MCP memory server

**Files:**
- Create: `agent/mcp_memory_server.py`
- Test: `tests/test_mcp_memory_server.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_memory_server.py
"""Tests for MCP memory server tool functions."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestMCPMemorySearch:
    @pytest.mark.asyncio
    async def test_search_returns_formatted_nodes(self):
        """memory_search should return formatted node text."""
        from agent.mcp_memory_server import memory_search

        mock_node = MagicMock()
        mock_node.id = uuid.uuid4()
        mock_node.name = "auth-pattern"
        mock_node.node_type = "decision"
        mock_node.content = "Use JWT for auth"
        mock_node.outgoing_edges = []
        mock_node.incoming_edges = []

        with patch("agent.mcp_memory_server._search_nodes", new_callable=AsyncMock, return_value=[mock_node]):
            result = await memory_search("auth")

        assert "auth-pattern" in result
        assert "Use JWT for auth" in result

    @pytest.mark.asyncio
    async def test_search_returns_message_when_empty(self):
        from agent.mcp_memory_server import memory_search

        with patch("agent.mcp_memory_server._search_nodes", new_callable=AsyncMock, return_value=[]):
            result = await memory_search("nonexistent")

        assert "no" in result.lower() or "No" in result


class TestMCPMemoryListRoots:
    @pytest.mark.asyncio
    async def test_list_roots_returns_node_names(self):
        from agent.mcp_memory_server import memory_list_roots

        mock_node = MagicMock()
        mock_node.id = uuid.uuid4()
        mock_node.name = "project-config"
        mock_node.node_type = "project"

        with patch("agent.mcp_memory_server._list_root_nodes", new_callable=AsyncMock, return_value=[mock_node]):
            result = await memory_list_roots()

        assert "project-config" in result

    @pytest.mark.asyncio
    async def test_list_roots_empty_graph(self):
        from agent.mcp_memory_server import memory_list_roots

        with patch("agent.mcp_memory_server._list_root_nodes", new_callable=AsyncMock, return_value=[]):
            result = await memory_list_roots()

        assert "empty" in result.lower()


class TestMCPMemoryCreateNode:
    @pytest.mark.asyncio
    async def test_create_node_returns_confirmation(self):
        from agent.mcp_memory_server import memory_create_node

        fake_id = uuid.uuid4()

        async def mock_create(name, node_type, content, task_id):
            node = MagicMock()
            node.id = fake_id
            node.name = name
            return node

        with patch("agent.mcp_memory_server._create_node_db", new_callable=AsyncMock, side_effect=mock_create):
            result = await memory_create_node(
                name="test-decision",
                node_type="decision",
                content="We chose X over Y",
            )

        assert "test-decision" in result
        assert str(fake_id) in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_mcp_memory_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.mcp_memory_server'`

- [ ] **Step 3: Write the MCP server**

```python
# agent/mcp_memory_server.py
"""MCP server exposing graph memory tools to Claude Code CLI.

Run via: python -m agent.mcp_memory_server
Configured in .claude/settings.local.json as an MCP server.
"""

from __future__ import annotations

import json
import uuid as _uuid

from mcp.server.fastmcp import FastMCP

# Re-use existing DB query functions from the agent tools
from agent.tools.memory_read import (
    _get_decision_chain,
    _list_root_nodes,
    _search_nodes,
    _traverse_node,
)
from shared.database import async_session
from shared.models import MemoryEdge, MemoryNode

mcp = FastMCP(
    "auto-agent-memory",
    instructions=(
        "Graph memory for the auto-agent team. Use memory_search to find existing "
        "knowledge before creating new nodes. Use memory_create_node to record "
        "decisions, capabilities, and preferences learned during tasks."
    ),
)


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_search(query: str, limit: int = 10) -> str:
    """Search the team knowledge graph by keyword. Returns matching nodes with their edges."""
    nodes = await _search_nodes(query, limit)
    if not nodes:
        return f"No memory nodes found matching '{query}'"
    parts = []
    for node in nodes:
        edges_str = ""
        for e in node.outgoing_edges:
            edges_str += f"\n  --[{e.relation}]--> {e.target_id}"
        for e in node.incoming_edges:
            edges_str += f"\n  <--[{e.relation}]-- {e.source_id}"
        parts.append(
            f"[{node.node_type}] {node.name} (id: {node.id})\n"
            f"  Content: {node.content}{edges_str}"
        )
    return "\n\n".join(parts)


@mcp.tool()
async def memory_traverse(node_id: str, depth: int = 2) -> str:
    """Traverse the graph from a node, following edges up to N levels deep."""
    result = await _traverse_node(_uuid.UUID(node_id), depth)
    return json.dumps(result, indent=2)


@mcp.tool()
async def memory_get_decision_chain(node_id: str) -> str:
    """Follow the evolved-from chain for a decision node, from newest to oldest."""
    chain = await _get_decision_chain(_uuid.UUID(node_id))
    if not chain:
        return "No decision chain found for this node"
    parts = []
    for i, node in enumerate(chain):
        prefix = "CURRENT" if i == 0 else f"v{len(chain) - i}"
        parts.append(f"[{prefix}] {node['name']} ({node['created_at']})\n  {node['content']}")
    return "\n\n".join(parts)


@mcp.tool()
async def memory_list_roots() -> str:
    """List all top-level nodes in the knowledge graph (nodes with no incoming edges)."""
    nodes = await _list_root_nodes()
    if not nodes:
        return "No root nodes in memory. The graph is empty."
    parts = [f"[{n.node_type}] {n.name} (id: {n.id})" for n in nodes]
    return "Root nodes:\n" + "\n".join(parts)


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


async def _create_node_db(
    name: str, node_type: str, content: str, task_id: int | None,
) -> MemoryNode:
    """Create a memory node in the database."""
    node = MemoryNode(name=name, node_type=node_type, content=content, created_by_task_id=task_id)
    async with async_session() as session:
        session.add(node)
        await session.commit()
        await session.refresh(node)
        return node


@mcp.tool()
async def memory_create_node(
    name: str,
    node_type: str,
    content: str,
    task_id: int | None = None,
) -> str:
    """Create a new knowledge node. Types: project, preference, decision, capability."""
    node = await _create_node_db(name, node_type, content, task_id)
    return f"Created node '{name}' (type: {node_type}, id: {node.id})"


@mcp.tool()
async def memory_create_edge(
    source_id: str,
    target_id: str,
    relation: str,
) -> str:
    """Link two nodes with a labeled edge (e.g., 'evolved-from', 'has-preference')."""
    edge = MemoryEdge(
        source_id=_uuid.UUID(source_id),
        target_id=_uuid.UUID(target_id),
        relation=relation,
    )
    async with async_session() as session:
        session.add(edge)
        await session.commit()
    return f"Created edge: {source_id} --[{relation}]--> {target_id}"


@mcp.tool()
async def memory_append_decision(
    node_id: str,
    content: str,
    task_id: int | None = None,
) -> str:
    """Append a new version to a decision chain, preserving the history via evolved-from edge."""
    from sqlalchemy import select as _select

    uid = _uuid.UUID(node_id)
    async with async_session() as session:
        result = await session.execute(_select(MemoryNode).where(MemoryNode.id == uid))
        parent = result.scalar_one_or_none()
        if not parent:
            return f"Error: node {node_id} not found"

        new_node = MemoryNode(
            name=parent.name,
            node_type="decision",
            content=content,
            created_by_task_id=task_id,
        )
        session.add(new_node)
        await session.flush()

        edge = MemoryEdge(source_id=new_node.id, target_id=uid, relation="evolved-from")
        session.add(edge)
        await session.commit()

        return f"Appended decision to '{parent.name}' chain (new id: {new_node.id})"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_mcp_memory_server.py -v`
Expected: PASS

- [ ] **Step 5: Run lint**

Run: `.venv/bin/ruff check agent/mcp_memory_server.py tests/test_mcp_memory_server.py`

- [ ] **Step 6: Commit**

```bash
git add agent/mcp_memory_server.py tests/test_mcp_memory_server.py
git commit -m "feat: add MCP memory server for Claude Code CLI graph memory access"
```

---

### Task 2: Configure MCP server in Claude Code settings

**Files:**
- Modify: `.claude/settings.local.json`

- [ ] **Step 1: Update settings.local.json**

Replace the contents of `.claude/settings.local.json` with:

```json
{
  "permissions": {
    "allow": [
      "Bash(.venv/bin/python -m py_compile claude_runner/workspace.py)",
      "Bash(.venv/bin/python -m py_compile claude_runner/prompts.py)",
      "Bash(.venv/bin/python -m py_compile claude_runner/main.py)",
      "Bash(.venv/bin/python -m py_compile integrations/slack/main.py)",
      "Bash(.venv/bin/python -m py_compile integrations/linear/main.py)",
      "Bash(.venv/bin/python -m py_compile integrations/whatsapp/main.py)",
      "Bash(curl -s http://localhost:2020/api/tasks)",
      "Bash(python3 -m json.tool)",
      "Bash(az account:*)",
      "WebFetch(domain:api.github.com)",
      "WebFetch(domain:raw.githubusercontent.com)",
      "WebFetch(domain:github.com)",
      "Bash(.venv/bin/python3:*)",
      "Bash(.venv/bin/ruff check:*)"
    ]
  },
  "mcpServers": {
    "graph-memory": {
      "command": ".venv/bin/python3",
      "args": ["-m", "agent.mcp_memory_server"]
    }
  }
}
```

- [ ] **Step 2: Verify the MCP server starts**

Run: `echo '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}},"id":1}' | .venv/bin/python3 -m agent.mcp_memory_server 2>/dev/null | head -1`

Expected: JSON response containing `"result"` with server capabilities.

- [ ] **Step 3: Commit**

```bash
git add .claude/settings.local.json
git commit -m "feat: configure graph-memory MCP server in Claude Code settings"
```

---

### Task 3: Create memory-reflect slash command

**Files:**
- Create: `.claude/commands/memory-reflect.md`

- [ ] **Step 1: Create the commands directory and slash command**

```markdown
# Memory Reflection

Reflect on what was learned during the task you just completed.

## Instructions

1. Use the `memory_search` tool to check if related knowledge already exists in the graph.
2. Consider what was learned:
   - Were any architectural or tooling **decisions** made? (e.g., chose library X over Y, adopted pattern Z)
   - Were any new **capabilities** created? (e.g., this project now produces/exposes X)
   - Were any existing team **preferences** applied or discovered? (e.g., the team prefers X approach)
3. For each item worth recording:
   - Search first to avoid duplicates
   - Use `memory_create_node` to record new knowledge (types: decision, capability, preference, project)
   - Use `memory_create_edge` to link related nodes
   - Use `memory_append_decision` if updating an existing decision (preserves history)

If nothing notable was learned, say so and stop. Don't create noise in the graph.

Keep node names descriptive and consistent with existing graph vocabulary.
```

- [ ] **Step 2: Commit**

```bash
git add .claude/commands/memory-reflect.md
git commit -m "feat: add /memory-reflect slash command for post-task knowledge capture"
```

---

### Task 4: Skip intent extraction for CLI provider

**Files:**
- Modify: `agent/main.py` (the intent extraction call in `handle_coding`, around line 726)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_intent_extraction.py`:

```python
class TestIntentExtractionSkipsForCLI:
    @pytest.mark.asyncio
    async def test_extract_intent_skips_for_cli_provider(self):
        """extract_intent should return empty dict when provider is claude_cli."""
        from agent.main import extract_intent

        with patch("agent.main.get_provider") as mock_get:
            mock_get.side_effect = Exception("Should not be called")
            with patch("agent.main.settings") as mock_settings:
                mock_settings.llm_provider = "claude_cli"
                result = await extract_intent("Fix bug", "Something broken")

        assert result == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_intent_extraction.py::TestIntentExtractionSkipsForCLI -v`
Expected: FAIL — extract_intent doesn't check the provider yet.

- [ ] **Step 3: Add early return to extract_intent**

In `agent/main.py`, at the top of the `extract_intent` function body, add:

```python
    # Intent extraction is redundant when using Claude CLI — it understands
    # the task natively. Only useful for API providers where we control the loop.
    if settings.llm_provider == "claude_cli":
        return {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_intent_extraction.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add agent/main.py tests/test_intent_extraction.py
git commit -m "feat: skip intent extraction when using Claude CLI provider"
```

---

### Task 5: Run full test suite and lint

**Files:**
- No new files

- [ ] **Step 1: Run full test suite**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: All tests pass.

- [ ] **Step 2: Run lint on changed files**

Run: `.venv/bin/ruff check agent/mcp_memory_server.py tests/test_mcp_memory_server.py .claude/commands/`

- [ ] **Step 3: Fix any issues**

- [ ] **Step 4: Final commit if needed**

```bash
git add -u
git commit -m "fix: address lint/test issues from MCP memory server"
```
