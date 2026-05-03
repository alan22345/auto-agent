"""Skill tool — loads structured skills into the agent's context.

Skills are .md files with YAML frontmatter that give the agent specific
workflows for grilling, architecture, TDD, debugging, etc. When invoked,
the full skill content is returned so the agent can follow the instructions.

Skills are discovered at import time from two roots, in priority order:

1. ``skills/engineering/`` — locally vendored skills (e.g. mattpocock's
   engineering skills); see ``skills/engineering/README.md``.
2. ``superpowers/skills/`` — the obra/superpowers submodule.

On a name collision the first-seen entry wins, so engineering takes
precedence over superpowers.
"""

from __future__ import annotations

import os
import re
from typing import Any

from agent.tools.base import Tool, ToolContext, ToolResult

# Resolve skill directories relative to project root, in priority order.
# First match wins on name collision.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKILL_DIRS: list[str] = [
    os.path.join(_PROJECT_ROOT, "skills", "engineering"),
    os.path.join(_PROJECT_ROOT, "superpowers", "skills"),
]


def _discover_skills() -> dict[str, dict[str, str]]:
    """Scan all skill roots and return {name: {description, path, dir}}.

    Iteration order follows ``_SKILL_DIRS``; the first directory containing
    a skill of a given name wins on collision.
    """
    skills: dict[str, dict[str, str]] = {}

    for root in _SKILL_DIRS:
        if not os.path.isdir(root):
            continue

        for entry in sorted(os.listdir(root)):
            skill_dir = os.path.join(root, entry)
            skill_file = os.path.join(skill_dir, "SKILL.md")
            if not os.path.isfile(skill_file):
                continue

            try:
                with open(skill_file, "r") as f:
                    content = f.read(500)  # Only read frontmatter

                # Parse YAML frontmatter
                fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
                if not fm_match:
                    continue

                name = entry
                description = ""
                for line in fm_match.group(1).splitlines():
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip().strip('"\'')
                    elif line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip('"\'')

                if name in skills:
                    # Earlier root wins; skip the duplicate silently.
                    continue

                skills[name] = {
                    "description": description,
                    "path": skill_file,
                    "dir": skill_dir,
                }
            except Exception:
                continue

    return skills


# Discover skills once at import time
AVAILABLE_SKILLS = _discover_skills()


def _build_skill_list() -> str:
    """Build a formatted list of available skills for the tool description."""
    if not AVAILABLE_SKILLS:
        return "No skills available."
    lines = []
    for name, info in AVAILABLE_SKILLS.items():
        lines.append(f"  - {name}: {info['description']}")
    return "\n".join(lines)


class SkillTool(Tool):
    name = "skill"
    description = (
        "Load a structured skill to guide your approach. Skills provide structured "
        "workflows for grilling the user, architecture review, TDD, debugging, and more. "
        "Call this BEFORE starting work that matches a skill's trigger.\n\n"
        "Available skills:\n" + _build_skill_list()
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the skill to load.",
                "enum": list(AVAILABLE_SKILLS.keys()),
            },
        },
        "required": ["name"],
    }
    is_readonly = True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        skill_name = arguments.get("name", "")
        skill = AVAILABLE_SKILLS.get(skill_name)

        if not skill:
            available = ", ".join(AVAILABLE_SKILLS.keys())
            return ToolResult(
                output=f"Unknown skill '{skill_name}'. Available: {available}",
                is_error=True,
            )

        # Read the full SKILL.md
        try:
            with open(skill["path"], "r") as f:
                content = f.read()
        except Exception as e:
            return ToolResult(output=f"Error reading skill: {e}", is_error=True)

        # Also load any referenced files in the same directory (e.g., templates,
        # reviewer prompts) — but only .md files and only the first few
        supplementary = []
        skill_dir = skill["dir"]
        for fname in sorted(os.listdir(skill_dir)):
            if fname == "SKILL.md" or not fname.endswith(".md"):
                continue
            fpath = os.path.join(skill_dir, fname)
            try:
                with open(fpath, "r") as f:
                    supplementary.append(f"## {fname}\n\n{f.read()}")
            except Exception:
                continue
            if len(supplementary) >= 3:  # Cap supplementary files
                break

        output = content
        if supplementary:
            output += "\n\n---\n\n# Supplementary Files\n\n" + "\n\n---\n\n".join(supplementary)

        return ToolResult(
            output=output,
            token_estimate=len(output) // 4,
        )
