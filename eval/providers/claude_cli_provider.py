"""Promptfoo provider: runs Claude Code CLI on the same coding tasks.

Baseline for A/B comparison against our agent.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def call_api(prompt, options, context):
    """Promptfoo entry point."""
    return asyncio.run(_run_cli(prompt, options, context))


async def _run_cli(prompt, options, context):
    variables = context.get("vars", {})
    fixture = variables.get("fixture", "")
    task = variables.get("task", prompt)

    workspace = tempfile.mkdtemp(prefix="eval-cli-")
    try:
        _setup_workspace(workspace, fixture, variables)

        # Initialize git
        for cmd in [
            ["git", "init"],
            ["git", "add", "-A"],
        ]:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=workspace,
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

        # Run Claude Code CLI
        proc = await asyncio.create_subprocess_exec(
            "claude", "--print", "--dangerously-skip-permissions", task,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {"output": json.dumps({"error": "Claude CLI timed out after 300s"})}

        cli_output = (stdout or b"").decode()

        # Capture diff — uncommitted + committed
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        diff_out, _ = await proc.communicate()

        # Also check committed changes
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "--oneline", "-1", "--format=%H", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        head_hash, _ = await proc.communicate()

        committed_diff = b""
        if head_hash.strip():
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "HEAD~1..HEAD", cwd=workspace,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            committed_diff, _ = await proc.communicate()

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

        full_diff = (diff_out or b"").decode() + (committed_diff or b"").decode()
        output = {
            "cli_output": cli_output[:5000],
            "diff": full_diff[:5000],
            "files": modified_files,
        }

        return {"output": json.dumps(output, indent=2)}

    except Exception as e:
        return {"output": json.dumps({"error": str(e)}), "error": str(e)}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _setup_workspace(workspace, fixture, variables):
    """Populate workspace from fixture directory or inline files."""
    fixtures_dir = os.path.join(PROJECT_ROOT, "eval", "fixtures", fixture)

    if fixture and os.path.isdir(fixtures_dir):
        for item in os.listdir(fixtures_dir):
            src = os.path.join(fixtures_dir, item)
            dst = os.path.join(workspace, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
    elif "files" in variables:
        files = variables["files"]
        if isinstance(files, str):
            files = json.loads(files)
        for path, content in files.items():
            full_path = os.path.join(workspace, path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)
