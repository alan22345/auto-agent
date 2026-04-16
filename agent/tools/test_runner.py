"""Structured test runner tool — detects framework, runs tests, parses results.

Unlike bash, this tool:
1. Auto-detects the test framework from the project
2. Runs tests and captures structured pass/fail output
3. Returns a concise summary instead of raw output
4. Can run specific test files or the full suite
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from agent.tools.base import Tool, ToolContext, ToolResult


class TestRunnerTool(Tool):
    name = "test_runner"
    description = (
        "Run tests and get structured results. Auto-detects the test framework "
        "(pytest, jest, go test, cargo test, etc). Returns pass/fail summary with "
        "failure details. Use this instead of bash for running tests."
    )
    parameters = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Specific test file or directory to run (relative to workspace). "
                    "Leave empty to run the full test suite."
                ),
            },
            "framework": {
                "type": "string",
                "description": (
                    "Override auto-detection. One of: pytest, jest, mocha, go, cargo, rspec."
                ),
            },
        },
        "required": [],
    }
    is_readonly = False  # Running tests can have side effects

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.readonly:
            return ToolResult(output="Error: test runner disabled in readonly mode.", is_error=True)

        target = arguments.get("target", "")
        framework = arguments.get("framework") or _detect_framework(context.workspace)

        if not framework:
            return ToolResult(
                output="Could not detect test framework. Specify 'framework' parameter or use bash tool directly.",
                is_error=True,
            )

        cmd = _build_command(framework, target)
        if not cmd:
            return ToolResult(
                output=f"Unknown test framework: {framework}",
                is_error=True,
            )

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=context.workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "FORCE_COLOR": "0", "NO_COLOR": "1"},
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            raw_output = (stdout or b"").decode(errors="replace")
            exit_code = proc.returncode or 0
        except asyncio.TimeoutError:
            return ToolResult(
                output="Test run timed out after 120 seconds.",
                is_error=True,
            )
        except Exception as e:
            return ToolResult(output=f"Error running tests: {e}", is_error=True)

        # Parse and summarize
        summary = _parse_output(framework, raw_output, exit_code)

        # Include relevant failure output, truncated
        if exit_code != 0:
            # Show the last portion of output which usually has the failures
            failure_output = raw_output[-3000:] if len(raw_output) > 3000 else raw_output
            summary += f"\n\n--- Test Output (last 3000 chars) ---\n{failure_output}"

        return ToolResult(
            output=summary,
            token_estimate=len(summary) // 4,
        )


def _detect_framework(workspace: str) -> str | None:
    """Auto-detect test framework from project files."""
    # Python
    if os.path.isfile(os.path.join(workspace, "pyproject.toml")):
        return "pytest"
    if os.path.isfile(os.path.join(workspace, "setup.py")):
        return "pytest"
    if os.path.isfile(os.path.join(workspace, "pytest.ini")):
        return "pytest"
    if os.path.isfile(os.path.join(workspace, "setup.cfg")):
        return "pytest"

    # JavaScript/TypeScript
    pkg_json = os.path.join(workspace, "package.json")
    if os.path.isfile(pkg_json):
        try:
            import json
            with open(pkg_json) as f:
                pkg = json.load(f)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "jest" in deps:
                return "jest"
            if "mocha" in deps:
                return "mocha"
            if "vitest" in deps:
                return "vitest"
            # Default to npm test if scripts.test exists
            if "test" in pkg.get("scripts", {}):
                return "npm"
        except Exception:
            pass

    # Go
    if os.path.isfile(os.path.join(workspace, "go.mod")):
        return "go"

    # Rust
    if os.path.isfile(os.path.join(workspace, "Cargo.toml")):
        return "cargo"

    # Ruby
    if os.path.isfile(os.path.join(workspace, "Gemfile")):
        return "rspec"

    # Fallback: look for test files
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".venv", "venv", "__pycache__"}]
        for f in files:
            if f.startswith("test_") and f.endswith(".py"):
                return "pytest"
            if f.endswith(".test.js") or f.endswith(".spec.js"):
                return "jest"
        break  # Only check top 2 levels

    return None


def _build_command(framework: str, target: str) -> str | None:
    """Build the test command for the given framework."""
    commands = {
        "pytest": f"python -m pytest {target} -v --tb=short --no-header -q" if target else "python -m pytest -v --tb=short --no-header -q",
        "jest": f"npx jest {target} --no-color" if target else "npx jest --no-color",
        "vitest": f"npx vitest run {target} --reporter=verbose" if target else "npx vitest run --reporter=verbose",
        "mocha": f"npx mocha {target}" if target else "npx mocha",
        "npm": "npm test",
        "go": f"go test {target} -v" if target else "go test ./... -v",
        "cargo": f"cargo test {target} -- --nocapture" if target else "cargo test -- --nocapture",
        "rspec": f"bundle exec rspec {target}" if target else "bundle exec rspec",
    }
    return commands.get(framework)


def _parse_output(framework: str, output: str, exit_code: int) -> str:
    """Parse test output into a structured summary."""
    status = "PASSED" if exit_code == 0 else "FAILED"

    # Try to extract counts
    passed = failed = errors = skipped = 0

    if framework == "pytest":
        # pytest: "5 passed, 2 failed, 1 error in 1.23s"
        m = re.search(r"(\d+) passed", output)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+) failed", output)
        if m:
            failed = int(m.group(1))
        m = re.search(r"(\d+) error", output)
        if m:
            errors = int(m.group(1))
        m = re.search(r"(\d+) skipped", output)
        if m:
            skipped = int(m.group(1))

    elif framework in ("jest", "vitest"):
        # jest: "Tests: 2 failed, 5 passed, 7 total"
        m = re.search(r"Tests:\s*(.*?)total", output, re.IGNORECASE)
        if m:
            counts_str = m.group(1)
            pm = re.search(r"(\d+)\s*passed", counts_str)
            fm = re.search(r"(\d+)\s*failed", counts_str)
            if pm:
                passed = int(pm.group(1))
            if fm:
                failed = int(fm.group(1))

    elif framework == "go":
        passed = output.count("--- PASS:")
        failed = output.count("--- FAIL:")

    total = passed + failed + errors + skipped

    parts = [f"Test result: {status} (exit code {exit_code})"]
    if total > 0:
        parts.append(f"  Passed: {passed}, Failed: {failed}, Errors: {errors}, Skipped: {skipped}")
    parts.append(f"  Framework: {framework}")

    return "\n".join(parts)
