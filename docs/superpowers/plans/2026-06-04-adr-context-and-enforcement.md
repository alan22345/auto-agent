# ADR Context & Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ADRs reach the coding agent as living context and stay current, then enforce at review time that a change which overwrites an ADR's decision retires that ADR in the same change.

**Architecture:** A pure `agent/context/adr_index.py` parses `docs/decisions/*.md` into typed metadata and renders a **status-aware** active index (Superseded/Deprecated omitted). The index is injected into the system prompt for API-provider runs and surfaced to passthrough (`claude_cli`) runs via a vendored `adr-awareness` skill that points at the committed `INDEX.md`. `record_decision` regenerates `INDEX.md` and supports superseding a prior ADR in the same write. Enforcement is two-tier, mirroring the existing no-defer gate: a deterministic `check_adr_consistency(diff)` primitive in `verify_primitives.py` (retire-in-same-commit consistency) plus injection of the active index into reviewer prompts so the LLM flags un-retired contradictions.

**Tech Stack:** Python 3.12, pytest, existing `agent/context` + `agent/lifecycle/verify_primitives.py` + `agent/tools/record_decision.py` seams. No new dependencies.

---

## File Structure

- **Create** `agent/context/adr_index.py` — pure ADR parsing + index rendering + active-set queries. One responsibility: turn `docs/decisions/` into typed metadata and an index string.
- **Create** `tests/test_adr_index.py` — unit tests for parsing, status classification, status-aware index, consistency gate inputs.
- **Modify** `docs/decisions/000-template.md` — add a `> **Summary:**` slot under the title.
- **Modify** `docs/decisions/*.md` (22 files) — backfill one `> **Summary:**` line under each H1.
- **Create** `docs/decisions/INDEX.md` — generated active index (committed artifact for humans + passthrough skill).
- **Modify** `agent/context/system.py` — inject the active ADR index as a prompt section.
- **Create** `skills/engineering/adr-awareness/SKILL.md` — passthrough bridge pointing at `INDEX.md` + retire discipline.
- **Modify** `agent/tools/record_decision.py` — `[ADR-NNN]` title prefix, emit `> **Summary:**`, regenerate `INDEX.md`, optional `supersedes` param.
- **Modify** `agent/lifecycle/verify_primitives.py` — add `check_adr_consistency(diff, adr_dir)`.
- **Modify** `agent/lifecycle/pr_reviewer.py` — run the consistency backstop + inject active index into the review prompt.
- **Modify** `tests/` — `test_record_decision.py`, `test_pr_reviewer.py`, `test_adr_consistency.py`.

---

## Phase 1 — ADR index core

### Task 1: `AdrMeta` + `parse_adr`

**Files:**
- Create: `agent/context/adr_index.py`
- Test: `tests/test_adr_index.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adr_index.py
import textwrap
from pathlib import Path

from agent.context.adr_index import parse_adr, status_kind


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body).lstrip("\n"))
    return p


def test_parse_adr_extracts_number_title_status_summary(tmp_path):
    p = _write(tmp_path, "005-workspace-path-tool-seam.md", """
        # [ADR-005] Workspace path resolution as a single tool seam

        > **Summary:** All file tools resolve paths through one seam.

        ## Status

        Accepted

        ## Decision

        Deepen ToolContext.
    """)
    meta = parse_adr(str(p))
    assert meta.number == 5
    assert meta.title == "Workspace path resolution as a single tool seam"
    assert meta.summary == "All file tools resolve paths through one seam."
    assert status_kind(meta.status) == "accepted"
    assert meta.superseded_by is None


def test_parse_adr_reads_superseded_status(tmp_path):
    p = _write(tmp_path, "013-trio-subagents.md", """
        # [ADR-013] Trio drives its backlog via subagents

        ## Status

        Superseded by [ADR-015] — reshaped by the heavy reviewer.

        ## Context

        old.
    """)
    meta = parse_adr(str(p))
    assert status_kind(meta.status) == "superseded"
    assert meta.superseded_by == 15
    assert meta.summary is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_adr_index.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.context.adr_index'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/context/adr_index.py
"""Pure ADR parsing + active-index rendering for docs/decisions/.

No I/O beyond reading the ADR files it is handed. Used by the system-prompt
builder (live, status-aware injection), record_decision (INDEX regeneration),
and the review consistency gate.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_NUM_RE = re.compile(r"^(\d{3})-")
_H1_RE = re.compile(r"^#\s+(?:\[ADR-\d+\]\s+)?(.*\S)\s*$")
_SUMMARY_RE = re.compile(r"^>\s*\*\*Summary:\*\*\s*(.*\S)\s*$")
_SUPERSEDED_BY_RE = re.compile(r"superseded\s+by\s+\[?ADR-(\d+)\]?", re.IGNORECASE)


@dataclass(frozen=True)
class AdrMeta:
    number: int
    path: str
    title: str
    status: str                       # full first non-empty line under ## Status
    summary: str | None = None
    superseded_by: int | None = None


def status_kind(status: str) -> str:
    """Classify a status line by its leading keyword."""
    s = status.strip().lower()
    if s.startswith("superseded"):
        return "superseded"
    if s.startswith("deprecated"):
        return "deprecated"
    if s.startswith("accepted"):
        return "accepted"
    if s.startswith("proposed"):
        return "proposed"
    return "unknown"


def parse_adr(path: str) -> AdrMeta:
    name = os.path.basename(path)
    m = _NUM_RE.match(name)
    number = int(m.group(1)) if m else -1

    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    title = name
    summary: str | None = None
    status = ""
    in_status = False
    for i, line in enumerate(lines):
        h1 = _H1_RE.match(line)
        if h1 and title == name:
            title = h1.group(1)
            continue
        sm = _SUMMARY_RE.match(line)
        if sm and summary is None:
            summary = sm.group(1)
            continue
        if line.strip().lower() == "## status":
            in_status = True
            continue
        if in_status:
            if line.strip().startswith("## "):
                in_status = False
            elif line.strip():
                status = line.strip()
                in_status = False

    superseded_by = None
    sb = _SUPERSEDED_BY_RE.search(status)
    if sb:
        superseded_by = int(sb.group(1))

    return AdrMeta(
        number=number, path=path, title=title,
        status=status, summary=summary, superseded_by=superseded_by,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_adr_index.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add agent/context/adr_index.py tests/test_adr_index.py
git commit -m "feat(adr): parse ADR markdown into typed metadata"
```

---

### Task 2: `active_adrs` + `build_index` (status-aware)

**Files:**
- Modify: `agent/context/adr_index.py`
- Test: `tests/test_adr_index.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_adr_index.py
from agent.context.adr_index import active_adrs, build_index


def test_build_index_is_status_aware(tmp_path):
    d = tmp_path / "decisions"
    d.mkdir()
    (d / "000-template.md").write_text("# [ADR-NNN] Title\n")  # ignored
    _write(d, "005-seam.md",
           "# [ADR-005] Path seam\n\n> **Summary:** One resolver.\n\n## Status\n\nAccepted\n")
    _write(d, "013-old.md",
           "# [ADR-013] Old trio\n\n## Status\n\nSuperseded by [ADR-015]\n")
    _write(d, "015-flow.md",
           "# [ADR-015] Task flow\n\n> **Summary:** Three flows.\n\n## Status\n\nAccepted\n")

    active = active_adrs(str(d))
    nums = [m.number for m in active]
    assert nums == [5, 15]            # 000 skipped, 013 (superseded) omitted, sorted

    index = build_index(str(d))
    assert "ADR-005" in index and "One resolver." in index
    assert "ADR-015" in index and "Three flows." in index
    assert "ADR-013" not in index    # superseded never appears in the active index
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_adr_index.py::test_build_index_is_status_aware -v`
Expected: FAIL — `ImportError: cannot import name 'active_adrs'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to agent/context/adr_index.py
_ADR_FILE_RE = re.compile(r"^\d{3}-.*\.md$")
_ACTIVE = {"accepted", "proposed"}


def _adr_paths(adr_dir: str) -> list[str]:
    if not os.path.isdir(adr_dir):
        return []
    out = []
    for name in sorted(os.listdir(adr_dir)):
        if _ADR_FILE_RE.match(name) and not name.startswith("000-"):
            out.append(os.path.join(adr_dir, name))
    return out


def active_adrs(adr_dir: str) -> list[AdrMeta]:
    """Accepted/Proposed ADRs, sorted by number. Superseded/Deprecated omitted."""
    metas = [parse_adr(p) for p in _adr_paths(adr_dir)]
    active = [m for m in metas if status_kind(m.status) in _ACTIVE]
    return sorted(active, key=lambda m: m.number)


def build_index(adr_dir: str) -> str:
    """Render the active ADR index as markdown. Deterministic, no I/O side effects."""
    lines = [
        "# Architecture Decision Index",
        "",
        "_Active decisions only. Superseded/Deprecated ADRs are intentionally "
        "omitted — read the file's `## Status` before treating any ADR as binding._",
        "",
    ]
    for m in active_adrs(adr_dir):
        summary = m.summary or "(no summary)"
        lines.append(f"- ADR-{m.number:03d} {m.title} — {summary}")
    lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_adr_index.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add agent/context/adr_index.py tests/test_adr_index.py
git commit -m "feat(adr): status-aware active index rendering"
```

---

### Task 3: Backfill summaries + template slot + generate INDEX.md

**Files:**
- Modify: `docs/decisions/000-template.md`
- Modify: `docs/decisions/*.md` (22 files — insert one line under the H1)
- Create: `docs/decisions/INDEX.md`

- [ ] **Step 1: Add the Summary slot to the template**

Edit `docs/decisions/000-template.md` so it reads:

```markdown
# [ADR-NNN] Title

> **Summary:** One sentence — what was decided and why it matters.

## Status

Proposed | Accepted | Deprecated | Superseded by [ADR-NNN]

## Context

What is the issue or decision we need to make? What constraints exist?

## Decision

What did we decide to do?

## Consequences

What are the trade-offs? What becomes easier or harder as a result?
```

- [ ] **Step 2: Insert one `> **Summary:**` line directly under the H1 of each ADR**

Use exactly these lines (approved 2026-06-04). Insert as a blockquote line + blank line between the existing `# [ADR-NNN] …` header and the `## Status` section:

```
001  Bootstrap the harness — CLAUDE.md, ruff lint, pre-commit hooks, docs/decisions/, and entropy.yml idle checks — so agents and humans share enforced guardrails.
002  Memory-tab read+write rides the existing WebSocket contract behind one shared/memory_io.py seam; fact deletion is a soft delete that preserves the audit trail.
003  Vendor the Pocock engineering skills into skills/engineering/, bake the architecture lens into the base prompt, and grill BEFORE planning via a persisted intake_qa round-trip.
004  Run `alembic upgrade head` automatically at FastAPI startup (before create_all), wrapped so a bad migration logs and still boots on the prior consistent schema.
005  All file tools resolve paths through one ToolContext.resolve() seam owning the sandbox invariant — kills five divergent copies and the /work→/workshop escape bug.
006  agent/llm/anthropic_mapper.py is the single owner of Message↔Anthropic-API translation; Bedrock and Anthropic providers shrink to thin transport+auth adapters.
007  shared/events.py is the single publish seam (Publisher protocol + Redis/in-memory adapters); call sites just publish() with no Redis knowledge.
008  Apply the deletion test — delete claude_runner/ entirely and reroute its one live path through the LLMProvider seam.
009  Split the monolithic agent/main.py into per-phase modules under agent/lifecycle/, each owning one phase's handler, dispatched through the EventBus.
010  agent/llm/structured.py is the single owner of "LLM text reply → dict" (parse_json_response + complete_json with bounded retry); callers pick the fallback policy.
011  Typed event taxonomy in shared/events.py — StrEnum types + one factory per event so a payload typo fails at the producer, wire string unchanged.
012  Reviewer-approved (LGTM) freeform PRs auto-merge when CI is green and conflict-free; dirty PRs trigger the conflict resolver. Non-freeform still needs human review.
013  Trio drives its backlog with the subagent tool inside the parent's slot — no child Task rows, no per-item Redis round-trips; the architect holds cross-item context.
014  Split the trio decision contract — heavy model reasons in prose, a cheap Bedrock-Haiku extractor turns prose into the structured JSON envelope, independent of LLM_PROVIDER.
015  Three flows (simple/complex/complex_large) on one classifier with conditional grill, a single design.md approval artefact, heavy per-item review, and four-layer no-defer enforcement.
016  Opt-in hierarchical, function-level, citation-validated code graph — one canonical store served to humans as a visualisation and to the agent through a typed query tool.
017  Add a long-lived AWAITING_REVIEW phase + ITERATING sub-state for post-PR feedback; PR_CREATED becomes a one-shot transit event, not a resting status.
018  Scaffold = four-phase pipeline (intent grill → root architect → per-domain architects → per-domain trios) with a project-level verification gate after all domain trios finish.
019  Per-repo encrypted secret vault — architect declares a required-secrets manifest, a hard gate blocks the build until they're populated, injected into the project runtime at boot.
020  The architect is the scope guardian — design.md is the contract; out-of-scope gaps are escalated, not silently dispatched. Baked into every architect system prompt.
021  agent/sh.py is the single subprocess seam — owns env-merge (GIT_TERMINAL_PROMPT=0), timeout-with-kill, and output capping for every agent shell-out.
022  shared/task_channel.py is the per-task Redis seam (TaskChannel protocol: guidance/heartbeat/stream verbs), mirroring the Publisher shape.
```

For each file, the result must look like:

```markdown
# [ADR-005] Workspace path resolution as a single tool seam

> **Summary:** All file tools resolve paths through one ToolContext.resolve() seam owning the sandbox invariant — kills five divergent copies and the /work→/workshop escape bug.

## Status
```

- [ ] **Step 3: Generate INDEX.md and verify it is status-aware**

Run:
```bash
.venv/bin/python3 -c "from agent.context.adr_index import build_index; open('docs/decisions/INDEX.md','w').write(build_index('docs/decisions'))"
```
Then verify ADR-013 (superseded) is absent and a summary is present:
```bash
grep -c "ADR-013" docs/decisions/INDEX.md   # expect 0
grep -c "ADR-005" docs/decisions/INDEX.md   # expect 1
```
Expected: first prints `0`, second prints `1`.

- [ ] **Step 4: Commit**

```bash
git add docs/decisions/
git commit -m "docs(adr): backfill summaries, add template slot, generate INDEX.md"
```

---

## Phase 2 — System-prompt injection (API providers)

### Task 4: Inject the active index into `SystemPromptBuilder.build`

**Files:**
- Modify: `agent/context/system.py` (after the repo-summary append, before memory_context — around line 282)
- Test: `tests/test_system_prompt_adr.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_system_prompt_adr.py
import textwrap
import pytest

from agent.context.system import SystemPromptBuilder


@pytest.mark.asyncio
async def test_build_injects_active_adr_index(tmp_path):
    d = tmp_path / "docs" / "decisions"
    d.mkdir(parents=True)
    (d / "005-seam.md").write_text(
        "# [ADR-005] Path seam\n\n> **Summary:** One resolver.\n\n## Status\n\nAccepted\n")
    (d / "013-old.md").write_text(
        "# [ADR-013] Old\n\n## Status\n\nSuperseded by [ADR-015]\n")

    builder = SystemPromptBuilder()
    prompt = await builder.build(workspace=str(tmp_path))

    assert "Architecture Decisions" in prompt
    assert "ADR-005" in prompt and "One resolver." in prompt
    assert "ADR-013" not in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_system_prompt_adr.py -q`
Expected: FAIL — assertion error, "Architecture Decisions" not in prompt.

- [ ] **Step 3: Write minimal implementation**

Add near the top of `agent/context/system.py`:
```python
from agent.context.adr_index import active_adrs, build_index
```

Insert in `build()` right after the `repo_summary` block (after line ~282, before the `if memory_context:` block):
```python
        # Active ADR index (status-aware). Built live from the workspace so
        # superseded/deprecated decisions never reach the agent. Cheap (reads
        # a handful of small markdown files); failures are logged and skipped.
        try:
            adr_dir = os.path.join(workspace, "docs", "decisions")
            if active_adrs(adr_dir):
                parts.append(
                    "## Architecture Decisions (active)\n"
                    "Binding decisions for this repo. Before changing code in an "
                    "area below, read that ADR's file; if your change overturns "
                    "one, retire it (set its `## Status` to Superseded) in the "
                    "same change.\n\n" + build_index(adr_dir)
                )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("adr_index_failed", error=str(e))
```

Confirm `import os` and `logger` already exist at the top of `system.py` (they do).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_system_prompt_adr.py -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add agent/context/system.py tests/test_system_prompt_adr.py
git commit -m "feat(adr): inject active ADR index into system prompt"
```

---

## Phase 3 — Passthrough bridge skill

### Task 5: Vendor the `adr-awareness` skill

**Files:**
- Create: `skills/engineering/adr-awareness/SKILL.md`

- [ ] **Step 1: Write the skill** (adapted from the personal `managing-adrs` skill, pointed at the repo's `docs/decisions/INDEX.md`)

```markdown
---
name: adr-awareness
description: Use when about to write, change, or commit code in this repo, before overwriting or replacing existing functionality, and before opening a PR. Also when two ADRs contradict each other or an ADR describes behavior the code no longer has.
---

# ADR Awareness

This repo records Architecture Decision Records in `docs/decisions/`. They are binding context AND a liability when stale. Two duties on every code change:

## Duty 1 — Consult before you change
1. Read `docs/decisions/INDEX.md` — the active decisions, one line each.
2. Read the full body of any ADR whose summary touches your change.
3. Only `Accepted` / `Proposed` ADRs are binding. INDEX.md already omits Superseded/Deprecated; if you open a file directly, check its `## Status`.
4. Follow the binding ADR, or state your deviation explicitly and record a new ADR.

## Duty 2 — Retire when you overwrite
When your change replaces or removes functionality an ADR describes, retire that ADR **in the same commit**:
1. Edit only its `## Status`: `Superseded by [ADR-NNN] — <one line>` (or `Deprecated — <one line>` if nothing replaces it).
2. Keep the file — never delete it. The rationale is the history.
3. Regenerate the index: `.venv/bin/python3 -c "from agent.context.adr_index import build_index; open('docs/decisions/INDEX.md','w').write(build_index('docs/decisions'))"`
4. Commit the retirement with the code change, not separately.

## Red flags — STOP
- "I'll update the ADR later / a teammate owns docs." → The contradiction ships in the gap. Retire it here, or STOP and escalate.
- "I'll just delete the old ADR." → No. Soft-retire (set Status), keep the file.
- "This ADR looks outdated, I'll ignore it." → If its Status is `Accepted`, it is binding until you retire it.
```

- [ ] **Step 2: Verify discovery wiring**

Confirm the engineering-skills loader already discovers `skills/engineering/*/SKILL.md` (ADR-003 §1 extended `agent/tools/skill.py` to scan `skills/engineering/` first):
```bash
grep -n "skills/engineering" agent/tools/skill.py
```
Expected: at least one match (the discovery path). If absent, add `skills/engineering` to the skill search roots in `agent/tools/skill.py`.

- [ ] **Step 3: Commit**

```bash
git add skills/engineering/adr-awareness/
git commit -m "feat(adr): adr-awareness skill bridges passthrough coder to the index"
```

---

## Phase 4 — record_decision lifecycle

### Task 6: `[ADR-NNN]` prefix + Summary emission + INDEX regeneration

**Files:**
- Modify: `agent/tools/record_decision.py`
- Test: `tests/test_record_decision.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_record_decision.py
import os
import pytest

from agent.tools.base import ToolContext
from agent.tools.record_decision import RecordDecisionTool


@pytest.mark.asyncio
async def test_record_decision_writes_prefixed_title_summary_and_index(tmp_path):
    d = tmp_path / "docs" / "decisions"
    d.mkdir(parents=True)
    (d / "000-template.md").write_text("# [ADR-NNN] Title\n\n## Status\n\nProposed\n")
    (d / "005-existing.md").write_text(
        "# [ADR-005] Existing\n\n> **Summary:** Keeps things tidy.\n\n## Status\n\nAccepted\n")

    tool = RecordDecisionTool()
    ctx = ToolContext(workspace=str(tmp_path))
    res = await tool.execute(
        {"title": "New caching layer", "context": "c", "decision": "d",
         "consequences": "x", "summary": "Cache responses at the edge."},
        ctx,
    )
    assert not res.is_error
    new = (d / "006-new-caching-layer.md").read_text()
    assert new.startswith("# [ADR-006] New caching layer")
    assert "> **Summary:** Cache responses at the edge." in new

    index = (d / "INDEX.md").read_text()
    assert "ADR-006" in index and "Cache responses at the edge." in index
    assert "ADR-005" in index
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_record_decision.py -q`
Expected: FAIL — title lacks `[ADR-006]` prefix / no INDEX.md written.

- [ ] **Step 3: Write minimal implementation**

In `agent/tools/record_decision.py`:

Add `summary` and `supersedes` to `parameters["properties"]` and keep them optional:
```python
            "summary": {"type": "string", "description": "One-line gist for the ADR index."},
            "supersedes": {"type": "integer", "description": "ADR number this decision retires, if any."},
```

Replace `_render` so the title is prefixed and the summary is emitted:
```python
def _render(n: int, title: str, summary: str, context: str, decision: str, consequences: str) -> str:
    summary_line = f"> **Summary:** {summary}\n\n" if summary else ""
    return (
        f"# [ADR-{n:03d}] {title}\n\n"
        f"{summary_line}"
        "## Status\n\nAccepted\n\n"
        f"## Context\n\n{context}\n\n"
        f"## Decision\n\n{decision}\n\n"
        f"## Consequences\n\n{consequences}\n"
    )
```

In `execute`, after computing `n`/`path`, replace the body render + write with:
```python
        body = _render(
            n,
            title=arguments["title"],
            summary=arguments.get("summary", ""),
            context=arguments["context"],
            decision=arguments["decision"],
            consequences=arguments["consequences"],
        )
        with open(path, "w") as f:
            f.write(body)

        # Optional supersession: retire the named prior ADR in the same write.
        superseded_note = ""
        supersedes = arguments.get("supersedes")
        if supersedes is not None:
            from agent.tools.adr_supersede import retire_adr  # Task 7
            ok = retire_adr(decisions_dir, int(supersedes), by_number=n)
            superseded_note = (
                f" (superseded ADR-{int(supersedes):03d})" if ok
                else f" (WARNING: ADR-{int(supersedes):03d} not found to supersede)"
            )

        # Regenerate the active index so it never drifts.
        from agent.context.adr_index import build_index
        with open(os.path.join(decisions_dir, "INDEX.md"), "w") as f:
            f.write(build_index(decisions_dir))

        return ToolResult(
            output=f"Wrote ADR: docs/decisions/{filename}{superseded_note}",
            token_estimate=20,
        )
```

Remove the now-unused `template`/`_render(template, …)` call path (the prose-template branch is replaced by the canonical render above; keep reading the template only to assert it exists).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_record_decision.py -q`
Expected: PASS — but note this test does not exercise `supersedes`; that lands in Task 7.

- [ ] **Step 5: Commit**

```bash
git add agent/tools/record_decision.py tests/test_record_decision.py
git commit -m "feat(adr): record_decision emits prefixed title + summary + regenerates INDEX"
```

---

### Task 7: `retire_adr` helper (soft-retire in place)

**Files:**
- Create: `agent/tools/adr_supersede.py`
- Test: `tests/test_adr_supersede.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adr_supersede.py
from agent.tools.adr_supersede import retire_adr
from agent.context.adr_index import parse_adr, status_kind


def test_retire_adr_flips_status_and_keeps_file(tmp_path):
    p = tmp_path / "005-existing.md"
    p.write_text("# [ADR-005] Existing\n\n## Status\n\nAccepted\n\n## Context\n\nc\n")

    ok = retire_adr(str(tmp_path), 5, by_number=21)
    assert ok is True
    assert p.exists()                       # never deleted

    meta = parse_adr(str(p))
    assert status_kind(meta.status) == "superseded"
    assert meta.superseded_by == 21


def test_retire_adr_returns_false_when_missing(tmp_path):
    assert retire_adr(str(tmp_path), 99, by_number=21) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_adr_supersede.py -q`
Expected: FAIL — `ModuleNotFoundError: agent.tools.adr_supersede`.

- [ ] **Step 3: Write minimal implementation**

```python
# agent/tools/adr_supersede.py
"""Soft-retire an ADR in place: flip its Status, keep the file."""
from __future__ import annotations

import os
import re

_NUM_RE = re.compile(r"^(\d{3})-")
_STATUS_HDR = "## Status"


def _find_adr(adr_dir: str, number: int) -> str | None:
    if not os.path.isdir(adr_dir):
        return None
    for name in os.listdir(adr_dir):
        m = _NUM_RE.match(name)
        if m and int(m.group(1)) == number and name.endswith(".md"):
            return os.path.join(adr_dir, name)
    return None


def retire_adr(adr_dir: str, number: int, *, by_number: int) -> bool:
    """Set ADR ``number``'s status to 'Superseded by [ADR-by_number]'.

    Replaces the first non-empty line under '## Status'. Returns False if the
    ADR file is not found. The file is never deleted.
    """
    path = _find_adr(adr_dir, number)
    if path is None:
        return False

    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    out: list[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        out.append(lines[i])
        if lines[i].strip() == _STATUS_HDR and not replaced:
            i += 1
            # copy blank lines after the header
            while i < len(lines) and not lines[i].strip():
                out.append(lines[i])
                i += 1
            # replace the first status value line
            if i < len(lines):
                out.append(f"Superseded by [ADR-{by_number:03d}]")
                i += 1
            replaced = True
            continue
        i += 1

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return replaced
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_adr_supersede.py tests/test_record_decision.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add agent/tools/adr_supersede.py tests/test_adr_supersede.py
git commit -m "feat(adr): retire_adr soft-retires an ADR in place"
```

---

## Phase 5 — Enforcement gate

### Task 8: Deterministic `check_adr_consistency`

**Files:**
- Modify: `agent/lifecycle/verify_primitives.py` (add near `grep_diff_for_stubs`, ~line 935)
- Test: `tests/test_adr_consistency.py`

The deterministic teeth (no `governs` metadata needed): if a change introduces a **new ADR** that declares `Supersedes [ADR-X]`, then ADR-X's `## Status` in the working tree must be Superseded/Deprecated. This enforces "retire in the same change." Also flags a `Superseded by [ADR-Y]` pointing at a non-existent Y.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adr_consistency.py
from agent.lifecycle.verify_primitives import check_adr_consistency


def _seed(tmp_path):
    d = tmp_path / "docs" / "decisions"
    d.mkdir(parents=True)
    return d


def test_new_adr_supersedes_but_old_left_accepted_is_flagged(tmp_path):
    d = _seed(tmp_path)
    (d / "005-old.md").write_text("# [ADR-005] Old\n\n## Status\n\nAccepted\n")
    (d / "023-new.md").write_text(
        "# [ADR-023] New\n\n> **Summary:** Replaces old.\n\n## Status\n\nAccepted\n\n"
        "## Decision\n\nSupersedes [ADR-005].\n")
    diff = "diff --git a/docs/decisions/023-new.md b/docs/decisions/023-new.md\n+Supersedes [ADR-005].\n"

    result = check_adr_consistency(diff, str(d))
    assert not result.ok
    assert any("005" in v for v in result.violations)


def test_new_adr_supersedes_and_old_retired_is_ok(tmp_path):
    d = _seed(tmp_path)
    (d / "005-old.md").write_text("# [ADR-005] Old\n\n## Status\n\nSuperseded by [ADR-023]\n")
    (d / "023-new.md").write_text(
        "# [ADR-023] New\n\n## Status\n\nAccepted\n\n## Decision\n\nSupersedes [ADR-005].\n")
    diff = "diff --git a/docs/decisions/023-new.md b/docs/decisions/023-new.md\n+Supersedes [ADR-005].\n"

    result = check_adr_consistency(diff, str(d))
    assert result.ok
    assert result.violations == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_adr_consistency.py -q`
Expected: FAIL — `ImportError: cannot import name 'check_adr_consistency'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to agent/lifecycle/verify_primitives.py
import re as _re
from dataclasses import dataclass as _dataclass, field as _field

from agent.context.adr_index import _adr_paths, parse_adr, status_kind

_DIFF_ADR_FILE_RE = _re.compile(r"^\+\+\+ b/docs/decisions/(\d{3})-.*\.md$", _re.MULTILINE)
_ADDED_SUPERSEDES_RE = _re.compile(r"^\+.*supersedes\s+\[?ADR-(\d+)\]?", _re.IGNORECASE | _re.MULTILINE)


@_dataclass
class AdrConsistencyResult:
    ok: bool
    violations: list[str] = _field(default_factory=list)


def check_adr_consistency(diff: str, adr_dir: str) -> AdrConsistencyResult:
    """Deterministic ADR retire-in-same-change gate.

    Flags when the diff introduces a 'Supersedes [ADR-X]' claim but ADR-X is
    still Accepted/Proposed in the working tree, and when a 'Superseded by
    [ADR-Y]' points at a Y that does not exist. No governs metadata required.
    """
    violations: list[str] = []

    # current state of every ADR in the tree, keyed by number
    by_num = {}
    for p in _adr_paths(adr_dir):
        m = parse_adr(p)
        by_num[m.number] = m

    # every "Supersedes [ADR-X]" added in this diff must leave X retired
    for sx in _ADDED_SUPERSEDES_RE.finditer(diff):
        x = int(sx.group(1))
        target = by_num.get(x)
        if target is None:
            violations.append(f"diff claims to supersede ADR-{x:03d}, which does not exist")
        elif status_kind(target.status) in {"accepted", "proposed"}:
            violations.append(
                f"diff supersedes ADR-{x:03d} but it is still '{target.status}' — "
                f"retire it (set its ## Status to Superseded) in this change"
            )

    # any 'Superseded by [ADR-Y]' in the tree must point at an existing Y
    for m in by_num.values():
        if m.superseded_by is not None and m.superseded_by not in by_num:
            violations.append(
                f"ADR-{m.number:03d} is 'Superseded by [ADR-{m.superseded_by:03d}]' "
                f"but ADR-{m.superseded_by:03d} does not exist"
            )

    return AdrConsistencyResult(ok=not violations, violations=violations)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_adr_consistency.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add agent/lifecycle/verify_primitives.py tests/test_adr_consistency.py
git commit -m "feat(adr): deterministic retire-in-same-change consistency gate"
```

---

### Task 9: Wire the consistency gate + index into the PR reviewer

**Files:**
- Modify: `agent/lifecycle/pr_reviewer.py` (the `_load_pr_diff` → `grep_diff_for_stubs` block, ~line 264, and the `_ARTEFACT_REVIEW_PROMPT.format(...)` at ~line 297)
- Test: `tests/test_pr_reviewer.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_pr_reviewer.py
import pytest
from agent.lifecycle import pr_reviewer as prr


@pytest.mark.asyncio
async def test_pr_review_flags_unretired_supersession(monkeypatch, tmp_path):
    d = tmp_path / "docs" / "decisions"
    d.mkdir(parents=True)
    (d / "005-old.md").write_text("# [ADR-005] Old\n\n## Status\n\nAccepted\n")
    (d / "023-new.md").write_text("# [ADR-023] New\n\n## Status\n\nAccepted\n\n## Decision\n\nSupersedes [ADR-005].\n")

    diff = "+++ b/docs/decisions/023-new.md\n+Supersedes [ADR-005].\n"
    monkeypatch.setattr(prr, "_load_pr_diff", _async_return(diff))

    class _Task:
        title = "add adr"
        description = ""
        base_branch = "main"

    result = await prr.run_pr_artefact_review(_Task(), str(tmp_path))
    assert result.verdict == "changes_requested"
    assert any("005" in (c.get("comment", "")) for c in result.comments)


def _async_return(value):
    async def _f(*a, **k):
        return value
    return _f
```

(Adjust `run_pr_artefact_review` to the real function name found at `pr_reviewer.py:~250` if it differs.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_pr_reviewer.py -k unretired -q`
Expected: FAIL — verdict is not `changes_requested` (gate not wired).

- [ ] **Step 3: Write minimal implementation**

In `pr_reviewer.py`, import the gate:
```python
from agent.lifecycle.verify_primitives import check_adr_consistency
```

Immediately after the `grep_diff_for_stubs` blocking-stub block returns (after line ~295), add a second deterministic backstop:
```python
    # ADR retire-in-same-change backstop (deterministic). Runs before the LLM.
    adr_dir = os.path.join(workspace_root, "docs", "decisions")
    adr_check = check_adr_consistency(diff, adr_dir)
    if not adr_check.ok:
        result = PRReviewResult(
            verdict="changes_requested",
            comments=[{"path": "docs/decisions/", "line": 0, "comment": v}
                      for v in adr_check.violations],
            summary=f"ADR consistency: {len(adr_check.violations)} issue(s); LLM review skipped.",
        )
        _write_pr_review_json(workspace_root, result)
        log.info("pr_review.adr.consistency_backstop", count=len(adr_check.violations))
        return result
```

Then inject the active index into the LLM prompt. Extend `_ARTEFACT_REVIEW_PROMPT` with an `{adr_index}` slot and a verdict instruction:
```
ADR check: the active architecture decisions for this repo are below. If this
diff contradicts an Accepted decision and does NOT retire that ADR (set its
## Status to Superseded) in the same diff, return changes_requested and name
the ADR.

{adr_index}
```
And pass it in the `.format(...)` call (~line 297):
```python
    from agent.context.adr_index import build_index
    prompt = _ARTEFACT_REVIEW_PROMPT.format(
        task_title=getattr(task, "title", "") or "",
        task_description=getattr(task, "description", "") or "",
        adr_index=build_index(adr_dir),
        # ...existing kwargs unchanged...
    )
```

Confirm `import os` exists at the top of `pr_reviewer.py` (add if missing).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_pr_reviewer.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/lifecycle/pr_reviewer.py tests/test_pr_reviewer.py
git commit -m "feat(adr): PR reviewer enforces retire-in-same-change + judges contradictions"
```

---

## Phase 6 — Verify & document

### Task 10: Full suite, lint, CLAUDE.md note

- [ ] **Step 1: Run the full unit suite**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: all green (note: the branch has pre-existing unrelated failures; confirm no NEW failures vs baseline).

- [ ] **Step 2: Lint**

Run: `.venv/bin/python3 -m ruff check agent/context/adr_index.py agent/tools/record_decision.py agent/tools/adr_supersede.py agent/lifecycle/verify_primitives.py agent/lifecycle/pr_reviewer.py`
Expected: no new errors in these files.

- [ ] **Step 3: Document the seam in CLAUDE.md**

Under "agent/ module layout → context/", add:
```
│   ├─ adr_index.py        # Parse docs/decisions/ → typed meta + status-aware active index
```
Under "Critical invariants", add:
```
5. **Superseded/Deprecated ADRs never enter agent context.** `adr_index.active_adrs`
   filters by `## Status`; retire an ADR by flipping Status, never by deleting it.
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record ADR-index seam + status invariant"
```

---

## Self-Review notes

- **Spec coverage:** A (index→context) = Tasks 1–4; A′ (lifecycle) = Tasks 3,6,7; passthrough bridge = Task 5; B (enforcement) = Tasks 8,9. The one-time retirement of already-stale ADRs (013) + dup renumber was completed in the 2026-06-04 cleanup, so it is intentionally not a task here.
- **No `governs:` frontmatter** — deliberately omitted (YAGNI / simplicity). The deterministic gate enforces retire-in-same-change without it; semantic contradiction is judged by the LLM reviewer with the index in-prompt.
- **Type consistency:** `AdrMeta` fields (`number/path/title/status/summary/superseded_by`) and `status_kind` keywords (`accepted/proposed/superseded/deprecated/unknown`) are used identically in Tasks 1,2,4,7,8. `retire_adr(adr_dir, number, *, by_number)` signature matches its call in Task 6.
- **Passthrough caveat:** Task 4's system-prompt injection only reaches API providers; the prod `claude_cli` coder relies on Task 5's skill. Both read the same `build_index` output, so they cannot disagree.
```
