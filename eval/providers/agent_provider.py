"""Promptfoo provider: runs our model-agnostic agent on a coding task.

Sets up a temp workspace with fixture files, runs the AgentLoop,
evaluates the resulting workspace state.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)


def call_api(prompt, options, context):
    """Promptfoo entry point. Returns the agent's output + workspace diff."""
    return asyncio.run(_run_agent(prompt, options, context))


async def _run_agent(prompt, options, context):
    variables = context.get("vars", {})
    fixture = variables.get("fixture", "")
    task = variables.get("task", prompt)

    # Create temp workspace from fixture
    workspace = tempfile.mkdtemp(prefix="eval-agent-")
    try:
        _setup_workspace(workspace, fixture, variables)

        # Initialize git so tools work
        proc = await asyncio.create_subprocess_exec(
            "git", "init", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            "git", "add", "-A", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            "git", "commit", "-m", "initial", "--allow-empty",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_AUTHOR_NAME": "eval", "GIT_AUTHOR_EMAIL": "eval@test",
                 "GIT_COMMITTER_NAME": "eval", "GIT_COMMITTER_EMAIL": "eval@test"},
        )
        await proc.communicate()

        # Run the agent
        from agent.llm import get_provider
        from agent.tools import create_default_registry
        from agent.context import ContextManager
        from agent.loop import AgentLoop

        provider = get_provider()
        tools = create_default_registry(readonly=False)
        ctx = ContextManager(workspace, provider)
        agent = AgentLoop(
            provider=provider, tools=tools, context_manager=ctx,
            max_turns=30, workspace=workspace,
        )
        result = await agent.run(task)

        # Capture the diff
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--cached", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        staged_diff, _ = await proc.communicate()

        proc = await asyncio.create_subprocess_exec(
            "git", "diff", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        unstaged_diff, _ = await proc.communicate()

        # Read modified files
        modified_files = {}
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [d for d in dirs if d != ".git"]
            for f in files:
                fp = os.path.join(root, f)
                rel = os.path.relpath(fp, workspace)
                try:
                    with open(fp, "r") as fh:
                        modified_files[rel] = fh.read()
                except Exception:
                    pass

        diff_text = (staged_diff or b"").decode() + (unstaged_diff or b"").decode()

        output = {
            "agent_output": result.output,
            "tool_calls": result.tool_calls_made,
            "tokens": {
                "input": result.tokens_used.input_tokens,
                "output": result.tokens_used.output_tokens,
            },
            "diff": diff_text[:5000],
            "files": modified_files,
        }

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
