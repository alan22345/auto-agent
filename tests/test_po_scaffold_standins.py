"""ADR-018 Stage 3 — PO standin extensions for the scaffold flow.

Covers:

1. The three new ``agent.po_agent`` functions
   (``po_answer_intent_grill``, ``po_approve_root_adr``,
   ``po_approve_domain_adr``) — each writes the canonical verdict file
   at the right path, with valid JSON, and never escapes.

2. The scaffold-flow ``request_po_*`` wiring helpers exposed by
   ``intent_grill``, ``root_adr_approval``, and ``domain_adr_approval``
   — each delegates to the matching PO function and returns the verdict
   path the orchestrator will read.

3. Skill smoke tests — every new ``skills/auto-agent/<name>/SKILL.md``
   file exists, has the required ``name:`` frontmatter, and ships a
   ``<what-to-do>`` block. Matches the canonical-skill shape per
   ``skills/auto-agent/submit-grill-exit/SKILL.md``.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    task_id: int = 42,
    description: str = "Build a TODO app for small families.",
    title: str = "Scaffold a TODO app",
    freeform_mode: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        title=title,
        description=description,
        repo_id=None,
        repo=None,
        freeform_mode=freeform_mode,
        source_id="",
        mode_override="freeform",
    )


@pytest.fixture
def workspace() -> str:
    """Per-test workspace dir under which ``.auto-agent/`` artefacts land."""

    with tempfile.TemporaryDirectory() as root:
        (Path(root) / ".auto-agent").mkdir(parents=True, exist_ok=True)
        yield root


# ---------------------------------------------------------------------------
# 1. po_answer_intent_grill — writes intent_grill_answer.json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_po_answer_intent_grill_writes_answer_file_with_partial_intent(
    workspace: str,
) -> None:
    """When a partial intent.md exists, the PO uses it as grounding context
    via a one-shot agent and writes a JSON answer file with citations."""

    from agent.lifecycle.workspace_paths import INTENT_GRILL_ANSWER_PATH, INTENT_PATH
    from agent.po_agent import po_answer_intent_grill

    # Seed a partial intent.md so the PO has grounding context.
    (Path(workspace) / INTENT_PATH).write_text(
        "# Intent\n\n## What the user wants\nA dead-simple TODO app.\n"
    )

    # Stub the one-shot agent so we don't actually hit the LLM.
    fake_agent = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                output='```json\n{"answer": "Yes — keep it minimal, one user only."}\n```'
            )
        )
    )
    with patch("agent.po_agent.create_agent", return_value=fake_agent):
        await po_answer_intent_grill(
            _make_task(),
            "Do we need multi-user support?",
            workspace,
        )

    path = Path(workspace) / INTENT_GRILL_ANSWER_PATH
    assert path.is_file(), "intent_grill_answer.json was not written"
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == "1"
    assert payload["source"] == "po_standin"
    assert "answer" in payload
    assert "keep it minimal" in payload["answer"]
    assert "task.description" in payload["cited_context"]
    assert "intent.md(partial)" in payload["cited_context"]
    assert payload["fallback_reasons"] == []


@pytest.mark.asyncio
async def test_po_answer_intent_grill_falls_back_when_no_context(workspace: str) -> None:
    """No description, no partial intent → heuristic default + fallback marker."""

    from agent.lifecycle.workspace_paths import INTENT_GRILL_ANSWER_PATH
    from agent.po_agent import po_answer_intent_grill

    task = _make_task(description="")
    await po_answer_intent_grill(task, "Anything?", workspace)

    payload = json.loads((Path(workspace) / INTENT_GRILL_ANSWER_PATH).read_text())
    assert payload["fallback_reasons"]
    assert any("intent_grill" in r for r in payload["fallback_reasons"])
    # Never escapes — the file is still written with a usable answer.
    assert "Default answer" in payload["answer"]


@pytest.mark.asyncio
async def test_po_answer_intent_grill_falls_back_on_unparseable_llm_output(
    workspace: str,
) -> None:
    """LLM returns garbage → heuristic fallback, no crash, file still written."""

    from agent.lifecycle.workspace_paths import INTENT_GRILL_ANSWER_PATH
    from agent.po_agent import po_answer_intent_grill

    fake_agent = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(output="not json at all"))
    )
    with patch("agent.po_agent.create_agent", return_value=fake_agent):
        await po_answer_intent_grill(_make_task(), "Q?", workspace)

    payload = json.loads((Path(workspace) / INTENT_GRILL_ANSWER_PATH).read_text())
    assert any("unparseable_output" in r for r in payload["fallback_reasons"])
    assert "Default answer" in payload["answer"]


# ---------------------------------------------------------------------------
# 1b. po_answer_domain_grill — writes domain_grill_answers/<slug>.json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_po_answer_domain_grill_writes_per_slug_answer_with_root_adr_context(
    workspace: str,
) -> None:
    """Per-domain grill answer is grounded in the root ADR + intent.md."""

    from agent.lifecycle.workspace_paths import (
        INTENT_PATH,
        ROOT_ADR_PATH,
        domain_grill_answer_path,
    )
    from agent.po_agent import po_answer_domain_grill

    (Path(workspace) / INTENT_PATH).write_text("# Intent\n\n## What the user wants\nA tool.\n")
    adrs_dir = Path(workspace) / ".auto-agent" / "adrs"
    adrs_dir.mkdir(parents=True, exist_ok=True)
    (Path(workspace) / ROOT_ADR_PATH).write_text(
        "# 000 — System ADR\n\n## Vision\nA TODO app.\n"
    )

    fake_agent = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                output='```json\n{"answer": "Yes — store sessions in the auth domain only."}\n```'
            )
        )
    )
    with patch("agent.po_agent.create_agent", return_value=fake_agent):
        await po_answer_domain_grill(
            _make_task(),
            "Where do sessions live?",
            "auth",
            workspace,
        )

    rel = domain_grill_answer_path("auth")
    path = Path(workspace) / rel
    assert path.is_file(), f"{rel} was not written"
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == "1"
    assert payload["domain_slug"] == "auth"
    assert payload["source"] == "po_standin"
    assert "sessions" in payload["answer"].lower()
    assert "task.description" in payload["cited_context"]
    assert "000-system.md" in payload["cited_context"]
    assert payload["fallback_reasons"] == []


@pytest.mark.asyncio
async def test_po_answer_domain_grill_falls_back_without_context(workspace: str) -> None:
    """No description, no intent, no root ADR → heuristic default."""

    from agent.lifecycle.workspace_paths import domain_grill_answer_path
    from agent.po_agent import po_answer_domain_grill

    task = _make_task(description="")
    await po_answer_domain_grill(task, "any?", "billing", workspace)

    payload = json.loads((Path(workspace) / domain_grill_answer_path("billing")).read_text())
    assert payload["fallback_reasons"]
    assert any("domain_grill" in r for r in payload["fallback_reasons"])
    assert payload["domain_slug"] == "billing"
    assert "Default answer" in payload["answer"]


@pytest.mark.asyncio
async def test_po_answer_domain_grill_falls_back_on_unparseable_output(workspace: str) -> None:
    """LLM returns garbage → heuristic fallback, no crash, file still written."""

    from agent.lifecycle.workspace_paths import domain_grill_answer_path
    from agent.po_agent import po_answer_domain_grill

    fake_agent = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(output="not json"))
    )
    with patch("agent.po_agent.create_agent", return_value=fake_agent):
        await po_answer_domain_grill(_make_task(), "Q?", "auth", workspace)

    payload = json.loads(
        (Path(workspace) / domain_grill_answer_path("auth")).read_text()
    )
    assert any("unparseable_output" in r for r in payload["fallback_reasons"])
    assert "Default answer" in payload["answer"]


# ---------------------------------------------------------------------------
# 2. po_approve_root_adr — writes root_adr_approval.json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_po_approve_root_adr_writes_approval_file(workspace: str) -> None:
    """Non-empty ADR + task description → approved verdict, no fallback."""

    from agent.lifecycle.workspace_paths import ROOT_ADR_APPROVAL_PATH
    from agent.po_agent import po_approve_root_adr

    await po_approve_root_adr(_make_task(), "# System ADR\n\n## Vision\nA TODO app.\n", workspace)

    path = Path(workspace) / ROOT_ADR_APPROVAL_PATH
    assert path.is_file(), "root_adr_approval.json was not written"
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == "1"
    assert payload["verdict"] == "approved"
    assert payload["source"] == "po_standin"
    assert payload["fallback_reasons"] == []
    assert payload["comments"]  # rationale present


@pytest.mark.asyncio
async def test_po_approve_root_adr_empty_adr_returns_revise(workspace: str) -> None:
    """Empty ADR → revise verdict with explanatory comment."""

    from agent.lifecycle.workspace_paths import ROOT_ADR_APPROVAL_PATH
    from agent.po_agent import po_approve_root_adr

    await po_approve_root_adr(_make_task(), "", workspace)

    payload = json.loads((Path(workspace) / ROOT_ADR_APPROVAL_PATH).read_text())
    assert payload["verdict"] == "revise"
    assert "empty_adr" in payload["fallback_reasons"][0]


@pytest.mark.asyncio
async def test_po_approve_root_adr_no_description_logs_fallback(workspace: str) -> None:
    """No task description → still approves (heuristic), marker logged."""

    from agent.lifecycle.workspace_paths import ROOT_ADR_APPROVAL_PATH
    from agent.po_agent import po_approve_root_adr

    task = _make_task(description="")
    await po_approve_root_adr(task, "# Root ADR\n\nbody\n", workspace)

    payload = json.loads((Path(workspace) / ROOT_ADR_APPROVAL_PATH).read_text())
    assert payload["verdict"] == "approved"
    assert any("no_task_description" in r for r in payload["fallback_reasons"])


# ---------------------------------------------------------------------------
# 3. po_approve_domain_adr — writes domain_adr_approvals/<slug>.json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_po_approve_domain_adr_writes_per_slug_file(workspace: str) -> None:
    """Per-slug verdict file lands at the expected path."""

    from agent.lifecycle.workspace_paths import domain_adr_approval_path
    from agent.po_agent import po_approve_domain_adr

    await po_approve_domain_adr(
        _make_task(),
        "# Auth ADR\n\nbody\n",
        domain_slug="auth",
        workspace_root=workspace,
    )

    rel = domain_adr_approval_path("auth")
    path = Path(workspace) / rel
    assert path.is_file(), f"{rel} was not written"
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == "1"
    assert payload["slug"] == "auth"
    assert payload["verdict"] == "approved"
    assert payload["source"] == "po_standin"


@pytest.mark.asyncio
async def test_po_approve_domain_adr_empty_body_revises(workspace: str) -> None:
    from agent.lifecycle.workspace_paths import domain_adr_approval_path
    from agent.po_agent import po_approve_domain_adr

    await po_approve_domain_adr(
        _make_task(),
        "",
        domain_slug="billing",
        workspace_root=workspace,
    )

    payload = json.loads((Path(workspace) / domain_adr_approval_path("billing")).read_text())
    assert payload["verdict"] == "revise"
    assert payload["slug"] == "billing"


# ---------------------------------------------------------------------------
# 4. Wiring helpers — request_po_* delegate to po_* and return path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_po_intent_answer_calls_po_function(workspace: str) -> None:
    """``intent_grill.request_po_intent_answer`` delegates to po_answer_intent_grill."""

    from agent.lifecycle.scaffold import intent_grill as ig_mod

    captured: dict = {}

    async def fake_po_answer(task, question, ws):
        captured["task_id"] = task.id
        captured["question"] = question
        captured["workspace"] = ws
        # Write a stub so the wiring helper can return its path.
        from agent.lifecycle.workspace_paths import INTENT_GRILL_ANSWER_PATH

        out = Path(ws) / INTENT_GRILL_ANSWER_PATH
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("{}")

    with (
        patch.object(
            ig_mod,
            "prepare_scaffold_workspace",
            new=AsyncMock(return_value=workspace),
        ),
        patch("agent.po_agent.po_answer_intent_grill", new=fake_po_answer),
    ):
        path = await ig_mod.request_po_intent_answer(_make_task(), "what features?")

    assert captured == {
        "task_id": 42,
        "question": "what features?",
        "workspace": workspace,
    }
    assert path.endswith(".auto-agent/intent_grill_answer.json")


@pytest.mark.asyncio
async def test_request_po_verdict_writes_root_approval(workspace: str) -> None:
    """``root_adr_approval.request_po_verdict`` reads the ADR and writes
    the verdict via the PO standin."""

    from agent.lifecycle.scaffold import root_adr_approval as rad_mod
    from agent.lifecycle.workspace_paths import ROOT_ADR_APPROVAL_PATH, ROOT_ADR_PATH

    adr_abs = Path(workspace) / ROOT_ADR_PATH
    adr_abs.parent.mkdir(parents=True, exist_ok=True)
    adr_abs.write_text("# System ADR\n\n## Vision\nA TODO app for parents.\n")

    with patch.object(
        rad_mod,
        "prepare_scaffold_workspace",
        new=AsyncMock(return_value=workspace),
    ):
        verdict_path = await rad_mod.request_po_verdict(_make_task())

    assert verdict_path.endswith(ROOT_ADR_APPROVAL_PATH)
    payload = json.loads((Path(workspace) / ROOT_ADR_APPROVAL_PATH).read_text())
    assert payload["verdict"] in {"approved", "revise", "rejected"}
    assert payload["source"] == "po_standin"


@pytest.mark.asyncio
async def test_request_po_verdicts_iterates_all_domains(workspace: str) -> None:
    """``domain_adr_approval.request_po_verdicts`` writes one verdict per
    parsed domain in the root ADR."""

    from agent.lifecycle.scaffold import domain_adr_approval as dad_mod
    from agent.lifecycle.workspace_paths import (
        ROOT_ADR_PATH,
        domain_adr_approval_path,
        domain_adr_path,
    )

    # Seed root ADR with two domains.
    root_md = """\
# System ADR

## Vision
A small thing.

```yaml
domains:
  - name: Auth
    slug: auth
    scope_summary: Login + session.
  - name: Billing
    slug: billing
    scope_summary: Plans + invoices.
```
"""
    adr_root = Path(workspace) / ROOT_ADR_PATH
    adr_root.parent.mkdir(parents=True, exist_ok=True)
    adr_root.write_text(root_md)
    # Seed per-domain ADR bodies so the verdicts get a non-empty body
    # and land as approved.
    (Path(workspace) / domain_adr_path(1, "auth")).write_text("# Auth\n\nbody\n")
    (Path(workspace) / domain_adr_path(2, "billing")).write_text("# Billing\n\nbody\n")

    with patch.object(
        dad_mod,
        "prepare_scaffold_workspace",
        new=AsyncMock(return_value=workspace),
    ):
        written = await dad_mod.request_po_verdicts(_make_task())

    assert len(written) == 2
    for slug in ("auth", "billing"):
        p = Path(workspace) / domain_adr_approval_path(slug)
        assert p.is_file()
        payload = json.loads(p.read_text())
        assert payload["slug"] == slug
        assert payload["verdict"] == "approved"


@pytest.mark.asyncio
async def test_request_po_verdicts_returns_empty_when_no_root_adr(workspace: str) -> None:
    """No root ADR → empty list, no crash."""

    from agent.lifecycle.scaffold import domain_adr_approval as dad_mod

    with patch.object(
        dad_mod,
        "prepare_scaffold_workspace",
        new=AsyncMock(return_value=workspace),
    ):
        written = await dad_mod.request_po_verdicts(_make_task())

    assert written == []


# ---------------------------------------------------------------------------
# 5. Skill smoke tests — every new SKILL.md exists with frontmatter + body
# ---------------------------------------------------------------------------


_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills" / "auto-agent"

_NEW_SCAFFOLD_SKILLS: tuple[str, ...] = (
    "submit-intent-summary",
    "submit-root-adr",
    "submit-domain-adr",
    "submit-root-adr-approval",
    "submit-domain-adr-approval",
    "submit-scaffold-final-verification",
    # ADR-018 Stage 8 — per-domain grill round skills.
    "submit-domain-grill-question",
    "submit-domain-grill-summary",
)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Minimal YAML-frontmatter parser — keeps the smoke test stdlib-only.

    The skill files follow the canonical shape (``---\\nname: ...\\n
    description: ...\\n---``) so a key-by-line parser is sufficient.
    """

    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


@pytest.mark.parametrize("skill_name", _NEW_SCAFFOLD_SKILLS)
def test_scaffold_skill_file_exists_and_has_required_shape(skill_name: str) -> None:
    """Every new scaffold-flow SKILL.md ships frontmatter + body.

    Pins the contract Stage 3 adds:
      * file exists at ``skills/auto-agent/<name>/SKILL.md``;
      * frontmatter ``name:`` matches the directory name (the agent
        loop registers skills by directory);
      * frontmatter ``description:`` is non-empty (Claude Code uses
        this to decide when to invoke the skill);
      * the body contains a ``<what-to-do>`` block (the canonical
        instruction-to-the-agent shape).
    """

    path = _SKILLS_DIR / skill_name / "SKILL.md"
    assert path.is_file(), f"missing skill file: {path}"

    text = path.read_text()
    frontmatter = _parse_frontmatter(text)
    assert frontmatter.get("name") == skill_name, (
        f"frontmatter name must match dir name; got {frontmatter.get('name')!r}"
    )
    assert frontmatter.get("description"), "frontmatter description must be non-empty"

    assert "<what-to-do>" in text, "skill body must contain a <what-to-do> block"
    assert "</what-to-do>" in text, "skill body must close the <what-to-do> block"
