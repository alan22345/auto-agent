"""Promptfoo provider: runs our model-agnostic agent on a coding task.

Sets up a temp workspace with fixture files, runs the AgentLoop,
evaluates the resulting workspace state.
"""

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import time

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
_PROVIDERS_DIR = os.path.dirname(os.path.abspath(__file__))
if _PROVIDERS_DIR not in sys.path:
    sys.path.insert(0, _PROVIDERS_DIR)

from dotenv import load_dotenv


def _collect_tool_metrics(messages):
    """Per-run tool telemetry from the agent transcript.

    Returns ``(tool_distribution, read_paths, graph_ops)`` where
    graph_ops counts query_repo_graph calls per op — the "did the
    treatment actually happen" signal for the graph A/B (ADR-025).
    """
    tool_distribution = {}
    read_paths = []
    graph_ops = {}
    for msg in messages:
        if msg.role != "assistant" or not msg.tool_calls:
            continue
        for tc in msg.tool_calls:
            tool_distribution[tc.name] = tool_distribution.get(tc.name, 0) + 1
            if tc.name == "file_read":
                path = tc.arguments.get("file_path", "")
                if path:
                    read_paths.append(path)
            elif tc.name == "query_repo_graph":
                op = tc.arguments.get("op", "unknown")
                graph_ops[op] = graph_ops.get(op, 0) + 1
    return tool_distribution, read_paths, graph_ops


def call_api(prompt, options, context):
    """Promptfoo entry point. Returns the agent's output + workspace diff."""
    # Load .env so shared.config picks up LLM settings. Done at call time, not
    # import time, so unit tests that import helpers from this module
    # (e.g. tests/test_eval_gitignore.py) don't accidentally inject
    # DATABASE_URL into the pytest process env, which made skipif-on-DB tests
    # un-skip mid-suite and produced ~50 flaky "DB pollution" failures.
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    return asyncio.run(_run_agent(prompt, options, context))


async def _run_agent(prompt, options, context):
    from _git_helpers import capture_committed_diff, init_git_workspace

    variables = context.get("vars", {})
    fixture = variables.get("fixture", "")
    task = variables.get("task", prompt)
    provider_config = (options or {}).get("config", {}) or {}
    graph_arm = bool(provider_config.get("graph", False))
    provider_override = provider_config.get("provider_override")

    # Create temp workspace from fixture
    workspace = tempfile.mkdtemp(prefix="eval-agent-")
    try:
        _setup_workspace(workspace, fixture, variables)

        # Write a .gitignore so npm install / pip install etc. don't pollute the diff.
        # Must be done BEFORE `git add -A` so these paths are never tracked.
        _write_gitignore(workspace)

        # Initialize git so tools work
        await init_git_workspace(workspace)

        # Graph arm (ADR-025): build an AST-only graph over the fixture
        # and patch the DB seams so query_repo_graph + the nudge work
        # without Postgres. The off arm is the status quo (repo_id=None).
        graph_context = contextlib.nullcontext()
        repo_id = None
        graph_nodes = graph_edges = 0
        if graph_arm:
            from graph_support import EVAL_REPO_ID, build_graph_blob, graph_enabled

            blob, head_sha = await build_graph_blob(workspace)
            graph_context = graph_enabled(workspace, blob, head_sha)
            repo_id = EVAL_REPO_ID
            graph_nodes, graph_edges = len(blob.nodes), len(blob.edges)

        # Run the agent
        from agent.llm import get_provider
        from agent.tools import create_default_registry
        from agent.context import ContextManager
        from agent.loop import AgentLoop

        provider = get_provider(provider_override=provider_override)
        tools = create_default_registry(readonly=False)
        ctx = ContextManager(workspace, provider)
        agent = AgentLoop(
            provider=provider, tools=tools, context_manager=ctx,
            max_turns=30, workspace=workspace, repo_id=repo_id,
        )
        t0 = time.monotonic()
        try:
            with graph_context:
                result = await agent.run(task)
        finally:
            tool_distribution, read_paths, graph_ops = _collect_tool_metrics(result.messages)
            # Close the async HTTP client before the event loop shuts down,
            # otherwise httpx raises "Event loop is closed" during GC cleanup.
            if hasattr(provider, '_client'):
                try:
                    inner = provider._client
                    if hasattr(inner, '_client') and hasattr(inner._client, 'aclose'):
                        await inner._client.aclose()
                    elif hasattr(inner, 'close'):
                        inner.close()
                except Exception:
                    pass
        elapsed_s = round(time.monotonic() - t0, 1)

        # Stage all changes (respecting .gitignore) so untracked new files
        # show up in `git diff --cached`. This captures agent writes even
        # when the agent forgot to `git add`.
        proc = await asyncio.create_subprocess_exec(
            "git", "add", "-A", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        # Capture the diff — check both uncommitted and committed changes
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        unstaged_diff, _ = await proc.communicate()

        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--cached", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        staged_diff, _ = await proc.communicate()

        # Also check committed changes (agent may have committed)
        committed_diff = await capture_committed_diff(workspace)

        # Identify which files actually changed (from git)
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--name-only", "HEAD", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        changed_unstaged, _ = await proc.communicate()

        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--name-only", "--cached", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        changed_staged, _ = await proc.communicate()

        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--name-only", "HEAD~1..HEAD", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        changed_committed, _ = await proc.communicate()

        # Also include untracked files (newly created)
        proc = await asyncio.create_subprocess_exec(
            "git", "ls-files", "--others", "--exclude-standard", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        untracked, _ = await proc.communicate()

        # Noise directories to skip (node_modules etc create thousands of untracked files)
        _SKIP_PATH_PARTS = frozenset({
            "node_modules", "__pycache__", ".venv", "venv",
            ".mypy_cache", ".ruff_cache", ".pytest_cache", ".tox",
            "dist", "build", ".next", ".nuxt", "coverage",
            ".git",
        })

        changed_names = set()
        for raw in [changed_unstaged, changed_staged, changed_committed, untracked]:
            for line in (raw or b"").decode().strip().splitlines():
                path = line.strip()
                if not path:
                    continue
                # Skip if any path segment is in the noise list
                parts = path.split("/")
                if any(p in _SKIP_PATH_PARTS for p in parts):
                    continue
                changed_names.add(path)

        # Only read files that changed — cap each at 5000 chars
        _MAX_FILE_CHARS = 5000
        _MAX_TOTAL_FILES = 50  # Safety cap on number of files included
        modified_files = {}
        for rel in sorted(changed_names)[:_MAX_TOTAL_FILES]:
            fp = os.path.join(workspace, rel)
            try:
                with open(fp, "r") as fh:
                    content = fh.read()
                modified_files[rel] = content[:_MAX_FILE_CHARS]
            except Exception:
                pass

        diff_text = (
            (unstaged_diff or b"").decode()
            + (staged_diff or b"").decode()
            + (committed_diff or b"").decode()
        )

        # Count unique file reads vs total reads (high repeat = stuck re-reading)
        unique_reads = len(set(read_paths))
        total_reads = len(read_paths)

        output = {
            "agent_output": result.output[:3000],  # Cap agent prose output
            "tool_calls": result.tool_calls_made,
            "tool_distribution": tool_distribution,
            "unique_files_read": unique_reads,
            "total_reads": total_reads,
            "graph_arm": graph_arm,
            "graph_calls": sum(graph_ops.values()),
            "graph_ops": graph_ops,
            "graph_nodes": graph_nodes,
            "graph_edges": graph_edges,
            "elapsed_seconds": elapsed_s,
            "tokens": {
                "input": result.tokens_used.input_tokens,
                "output": result.tokens_used.output_tokens,
            },
            "diff": diff_text[:5000],
            "files": modified_files,
        }

        # Hard cap on total output size — prevents token overflow in grader.
        # If over 500KB, progressively trim files until under the limit.
        _MAX_OUTPUT_BYTES = 500_000
        serialized = json.dumps(output)
        if len(serialized) > _MAX_OUTPUT_BYTES:
            # Drop files one by one (largest first) until under the limit
            while output["files"] and len(json.dumps(output)) > _MAX_OUTPUT_BYTES:
                largest = max(output["files"], key=lambda k: len(output["files"][k]))
                del output["files"][largest]
            # If still too big, truncate diff
            if len(json.dumps(output)) > _MAX_OUTPUT_BYTES:
                output["diff"] = output["diff"][:2000]
            output["_truncated"] = True

        return {
            "output": json.dumps(output, indent=2),
            "tokenUsage": {
                "total": result.tokens_used.total,
                "prompt": result.tokens_used.input_tokens,
                "completion": result.tokens_used.output_tokens,
            },
        }

    except Exception as e:
        return {"output": json.dumps({"error": str(e)}), "error": str(e)}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


_GITIGNORE_CONTENT = """\
# Noise files — agent may produce these via npm/pip install, test runs, etc.
node_modules/
__pycache__/
.venv/
venv/
.mypy_cache/
.ruff_cache/
.pytest_cache/
.tox/
dist/
build/
.next/
.nuxt/
coverage/
*.pyc
*.pyo
*.log
package-lock.json
yarn.lock
poetry.lock
Pipfile.lock
.DS_Store
"""


def _write_gitignore(workspace):
    """Write a .gitignore that excludes common noise files.

    Without this, agents that run `npm install` or `pip install` pollute the
    workspace with thousands of files that bloat the diff/files output and
    crash the LLM grader with token overflow.
    """
    gitignore_path = os.path.join(workspace, ".gitignore")
    if not os.path.exists(gitignore_path):
        with open(gitignore_path, "w") as f:
            f.write(_GITIGNORE_CONTENT)


def _setup_workspace(workspace, fixture, variables):
    """Populate workspace from fixture directory or inline files."""
    fixtures_dir = os.path.join(PROJECT_ROOT, "eval", "fixtures", fixture)

    if fixture and os.path.isdir(fixtures_dir):
        # Copy fixture files
        for item in os.listdir(fixtures_dir):
            src = os.path.join(fixtures_dir, item)
            dst = os.path.join(workspace, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
    elif "files" in variables:
        # Create files from inline definitions
        files = variables["files"]
        if isinstance(files, str):
            files = json.loads(files)
        for path, content in files.items():
            full_path = os.path.join(workspace, path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)
