"""Stage 2 unit tests for the scaffold lifecycle package — ADR-018.

Covers:
1. Driver — phases dispatch in order; external-gate phases return
   without recursing.
2. Root-ADR approval verdicts (apply_verdict) — approved / revise /
   revise-cap / rejected transitions.
3. Validators — root and domain ADR shape checks.
4. ``parse_domains`` — extracts the YAML-block list from a root ADR.
5. ``dispatch_children.run`` — spawns one COMPLEX_LARGE child Task per
   approved domain and publishes ``task_created``.
6. ``final_verification.run`` — writes the verdict JSON file based on
   the verify_primitives signal.

Real agent runs are out of scope (Stage 6 integration); ``create_agent``
is mocked everywhere.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scaffold_task(
    *,
    task_id: int = 1,
    status: str = "awaiting_intent_grill",
    subtasks: dict | None = None,
    organization_id: int = 7,
    parent_task_id: int | None = None,
):
    """Build a SimpleNamespace stand-in for the ``Task`` ORM row."""

    from shared.models import TaskComplexity, TaskSource, TaskStatus

    return SimpleNamespace(
        id=task_id,
        title="Scaffold parent",
        description="Build a TODO app.",
        status=TaskStatus(status),
        complexity=TaskComplexity.SCAFFOLD,
        repo_id=None,
        repo=None,
        freeform_mode=True,
        organization_id=organization_id,
        created_by_user_id=None,
        parent_task_id=parent_task_id,
        subtasks=subtasks,
        source=TaskSource.MANUAL,
        affected_routes=[],
    )


def _patched_session_factory(task):
    """Yield a fake session whose execute() returns ``task``."""

    @asynccontextmanager
    async def factory():
        class _Result:
            def __init__(self, t):
                self._t = t

            def scalar_one(self):
                return self._t

            def scalar_one_or_none(self):
                return self._t

            def scalars(self):
                inner_t = self._t

                class _Scalars:
                    def all(self):
                        return [inner_t] if inner_t is not None else []

                return _Scalars()

        class _Session:
            def __init__(self, t):
                self._t = t
                self.committed = False
                self.added: list = []

            async def execute(self, *_a, **_kw):
                return _Result(self._t)

            def add(self, obj):
                self.added.append(obj)

            async def commit(self):
                self.committed = True

            async def flush(self):
                pass

        yield _Session(task)

    return factory


# ---------------------------------------------------------------------------
# 1. Driver — phases dispatch in order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domain_grill_runs_before_domain_architect_writes_adr(tmp_path):
    """ADR-018 Stage 8 — the per-domain loop runs grill FIRST per domain.

    Patches ``domain_grill.run`` and the underlying ``create_agent`` call
    in the architect path; asserts:

    1. ``domain_grill.run`` is awaited once per domain BEFORE the architect
       prompt runs (call ordering).
    2. The grill summary file's presence is the only signal we need —
       when the grill returns ``summary_written`` the architect proceeds.
    """

    from agent.lifecycle.scaffold import domain_architect as da_mod
    from agent.lifecycle.scaffold import domain_grill as dg_mod
    from agent.lifecycle.workspace_paths import (
        ROOT_ADR_PATH,
        domain_adr_path,
        domain_grill_path,
    )

    # Seed the workspace with a passing root ADR (two domains).
    (tmp_path / ".auto-agent" / "adrs").mkdir(parents=True)
    (tmp_path / ROOT_ADR_PATH).write_text(_PASSING_ROOT_ADR)

    task = _make_scaffold_task(status="building_domain_adrs")

    call_log: list[str] = []

    async def fake_grill_run(t, domain):
        call_log.append(f"grill:{domain.get('slug')}")
        # Simulate the skill's write so the architect step has authoritative
        # context to read.
        slug = domain.get("slug") or "domain"
        idx = int(domain.get("index") or 1)
        p = tmp_path / domain_grill_path(idx, slug)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# Domain grill — {slug}\n\n## Scope\nstub\n")
        return {"status": "summary_written", "summary_path": str(p)}

    architect_call_count = {"n": 0}

    async def fake_architect_agent_run(prompt, system=None, resume=False):
        # Architects are created per-domain (one create_agent call per
        # loop iteration) so use the prompt's leading line to identify
        # the domain — it embeds the slug verbatim.
        if f"slug `auth`" in prompt:
            slug, idx = "auth", 1
        else:
            slug, idx = "billing", 2
        architect_call_count["n"] += 1
        call_log.append(f"architect:{slug}")
        # Stub a passing ADR (≥80 words in scope) so validator accepts.
        scope = " ".join(["word"] * 90)
        (tmp_path / domain_adr_path(idx, slug)).write_text(
            f"# {idx:03d} — {slug.title()} ADR\n\n"
            f"## Scope\n{scope}\n\n## Aggregates\n- x\n\n## Public surface\n- x\n\n"
            "## Integration points\n- x\n\n## Affected routes\n- /x\n"
        )
        return MagicMock(output="ok")

    def _fake_create_agent(*_args, **_kwargs):
        # Each call yields a fresh agent — production creates one per
        # domain so the runs don't share session state.
        return SimpleNamespace(run=AsyncMock(side_effect=fake_architect_agent_run))

    with (
        patch.object(
            da_mod,
            "prepare_scaffold_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
        patch.object(da_mod, "home_dir_for_task", new=AsyncMock(return_value=str(tmp_path))),
        patch.object(da_mod, "create_agent", side_effect=_fake_create_agent),
        patch.object(dg_mod, "run", new=AsyncMock(side_effect=fake_grill_run)),
        patch.object(
            da_mod,
            "_persist_current_domain_idx",
            new=AsyncMock(return_value=None),
        ),
    ):
        outcome = await da_mod.run(task)

    assert outcome["status"] == "all_complete"
    # Each domain's grill must run BEFORE its architect — strict ordering.
    assert call_log == [
        "grill:auth",
        "architect:auth",
        "grill:billing",
        "architect:billing",
    ]


@pytest.mark.asyncio
async def test_domain_architect_returns_awaiting_grill_when_first_grill_pauses(tmp_path):
    """When the first domain's grill paused on a question, we must short-circuit
    out of the loop without ever invoking the architect."""

    from agent.lifecycle.scaffold import domain_architect as da_mod
    from agent.lifecycle.scaffold import domain_grill as dg_mod
    from agent.lifecycle.workspace_paths import ROOT_ADR_PATH

    (tmp_path / ".auto-agent" / "adrs").mkdir(parents=True)
    (tmp_path / ROOT_ADR_PATH).write_text(_PASSING_ROOT_ADR)

    task = _make_scaffold_task(status="building_domain_adrs")

    architect_calls = {"n": 0}

    def _fake_create_agent(*_args, **_kwargs):
        architect_calls["n"] += 1
        return SimpleNamespace(run=AsyncMock())

    async def fake_grill_run(_t, domain):
        return {
            "status": "awaiting_user",
            "domain_slug": domain.get("slug"),
            "question": "what scope?",
        }

    with (
        patch.object(
            da_mod,
            "prepare_scaffold_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
        patch.object(da_mod, "home_dir_for_task", new=AsyncMock(return_value=str(tmp_path))),
        patch.object(da_mod, "create_agent", side_effect=_fake_create_agent),
        patch.object(dg_mod, "run", new=AsyncMock(side_effect=fake_grill_run)),
        patch.object(
            da_mod,
            "_persist_current_domain_idx",
            new=AsyncMock(return_value=None),
        ),
    ):
        outcome = await da_mod.run(task)

    assert outcome["status"] == "awaiting_grill"
    assert outcome["domain_slug"] == "auth"
    assert architect_calls["n"] == 0, "architect must NOT run while grill is paused"


@pytest.mark.asyncio
async def test_driver_transitions_to_awaiting_domain_grill_on_pause():
    """When ``domain_architect.run`` returns ``awaiting_grill``, the driver
    parks the parent in ``AWAITING_DOMAIN_GRILL`` and returns."""

    from agent.lifecycle.scaffold import parent as parent_mod
    from shared.models import TaskStatus

    task = _make_scaffold_task(status="building_domain_adrs")

    transitions: list[str] = []

    async def fake_transition_and_reload(task_id, to_status, *, message=""):
        transitions.append(to_status.value)
        new = SimpleNamespace(**{**task.__dict__, "status": to_status})
        return new

    domain_mock = AsyncMock(
        return_value={
            "status": "awaiting_grill",
            "domain_slug": "auth",
            "question": "?",
        }
    )

    with (
        patch.object(parent_mod.domain_architect, "run", domain_mock),
        patch.object(parent_mod, "_transition_and_reload", fake_transition_and_reload),
    ):
        await parent_mod.run_scaffold_parent(task)

    assert transitions == [TaskStatus.AWAITING_DOMAIN_GRILL.value]


@pytest.mark.asyncio
async def test_driver_runs_intent_grill_then_root_then_returns_at_gate():
    """A fresh AWAITING_INTENT_GRILL task should run intent grill, transition
    to BUILDING_ROOT_ADR, run root architect, transition to
    AWAITING_ROOT_ADR_APPROVAL, then return (external gate)."""

    from agent.lifecycle.scaffold import parent as parent_mod

    task = _make_scaffold_task(status="awaiting_intent_grill")

    # Simulate the state machine: each _transition_and_reload returns a
    # fresh task with the new status. We'll track calls so we can verify
    # ordering.
    transitions: list[str] = []

    async def fake_transition_and_reload(task_id, to_status, *, message=""):
        transitions.append(to_status.value)
        # Build a new task with the new status — preserves the rest of
        # the namespace shape.
        new = SimpleNamespace(
            id=task.id,
            title=task.title,
            description=task.description,
            status=to_status,
            complexity=task.complexity,
            repo_id=task.repo_id,
            repo=task.repo,
            freeform_mode=task.freeform_mode,
            organization_id=task.organization_id,
            created_by_user_id=task.created_by_user_id,
            parent_task_id=task.parent_task_id,
            subtasks=task.subtasks,
            source=task.source,
            affected_routes=task.affected_routes,
        )
        return new

    intent_mock = AsyncMock()
    root_mock = AsyncMock()
    domain_mock = AsyncMock()
    dispatch_mock = AsyncMock()
    final_mock = AsyncMock()

    with (
        patch.object(parent_mod.intent_grill, "run", intent_mock),
        patch.object(parent_mod.root_architect, "run", root_mock),
        patch.object(parent_mod.domain_architect, "run", domain_mock),
        patch.object(parent_mod.dispatch_children, "run", dispatch_mock),
        patch.object(parent_mod.final_verification, "run", final_mock),
        patch.object(parent_mod, "_transition_and_reload", fake_transition_and_reload),
    ):
        await parent_mod.run_scaffold_parent(task)

    intent_mock.assert_awaited_once()
    root_mock.assert_awaited_once()
    domain_mock.assert_not_awaited()
    dispatch_mock.assert_not_awaited()
    final_mock.assert_not_awaited()
    assert transitions == ["building_root_adr", "awaiting_root_adr_approval"]


@pytest.mark.asyncio
async def test_driver_returns_at_external_gate_status_without_running_phases():
    """If the driver is invoked while parked at AWAITING_ROOT_ADR_APPROVAL it
    must not run any phase — the gate is still open."""

    from agent.lifecycle.scaffold import parent as parent_mod

    task = _make_scaffold_task(status="awaiting_root_adr_approval")

    intent_mock = AsyncMock()
    root_mock = AsyncMock()

    with (
        patch.object(parent_mod.intent_grill, "run", intent_mock),
        patch.object(parent_mod.root_architect, "run", root_mock),
    ):
        await parent_mod.run_scaffold_parent(task)

    intent_mock.assert_not_awaited()
    root_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_driver_final_verification_passed_transitions_to_done():
    from agent.lifecycle.scaffold import parent as parent_mod

    task = _make_scaffold_task(status="awaiting_final_verification")

    transitions: list[str] = []

    async def fake_transition_and_reload(task_id, to_status, *, message=""):
        transitions.append(to_status.value)
        return SimpleNamespace(
            id=task.id,
            status=to_status,
            complexity=task.complexity,
            subtasks=task.subtasks,
            title=task.title,
            description=task.description,
            repo_id=task.repo_id,
            repo=task.repo,
            freeform_mode=task.freeform_mode,
            organization_id=task.organization_id,
            created_by_user_id=task.created_by_user_id,
            parent_task_id=task.parent_task_id,
            source=task.source,
            affected_routes=task.affected_routes,
        )

    final_mock = AsyncMock(return_value="passed")

    # Mock async_session for the round counter bump.
    @asynccontextmanager
    async def fake_session():
        class _Result:
            def scalar_one(self):
                return SimpleNamespace(subtasks=None)

        class _Session:
            async def execute(self, *_a, **_kw):
                return _Result()

            async def commit(self):
                pass

            def add(self, _obj):
                pass

        yield _Session()

    with (
        patch.object(parent_mod.final_verification, "run", final_mock),
        patch.object(parent_mod, "_transition_and_reload", fake_transition_and_reload),
        patch.object(parent_mod, "async_session", fake_session),
    ):
        await parent_mod.run_scaffold_parent(task)

    final_mock.assert_awaited_once()
    assert transitions == ["done"]


# ---------------------------------------------------------------------------
# 2. Root-ADR approval verdicts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_adr_approval_approved_transitions_to_building_domain_adrs():
    from agent.lifecycle.scaffold import root_adr_approval
    from shared.models import TaskStatus

    task = _make_scaffold_task(status="awaiting_root_adr_approval")

    transition_mock = AsyncMock()
    with (
        patch.object(root_adr_approval, "async_session", _patched_session_factory(task)),
        patch.object(root_adr_approval, "transition", transition_mock),
    ):
        result = await root_adr_approval.apply_verdict(
            task.id, {"verdict": "approved", "comments": "lgtm"}
        )

    assert result == TaskStatus.BUILDING_DOMAIN_ADRS
    transition_mock.assert_awaited_once()
    args = transition_mock.await_args.args
    assert args[2] == TaskStatus.BUILDING_DOMAIN_ADRS


@pytest.mark.asyncio
async def test_root_adr_approval_revise_increments_counter_and_loops():
    from agent.lifecycle.scaffold import root_adr_approval
    from shared.models import TaskStatus

    task = _make_scaffold_task(status="awaiting_root_adr_approval", subtasks=None)

    transition_mock = AsyncMock()
    with (
        patch.object(root_adr_approval, "async_session", _patched_session_factory(task)),
        patch.object(root_adr_approval, "transition", transition_mock),
    ):
        result = await root_adr_approval.apply_verdict(
            task.id, {"verdict": "revise", "comments": "rework auth"}
        )

    assert result == TaskStatus.BUILDING_ROOT_ADR
    assert task.subtasks == {"scaffold": {"root_revise": 1}}


@pytest.mark.asyncio
async def test_root_adr_approval_revise_cap_blocks_after_three_rounds():
    from agent.lifecycle.scaffold import root_adr_approval
    from shared.models import TaskStatus

    # Already had 3 revise rounds; a 4th must BLOCK.
    task = _make_scaffold_task(
        status="awaiting_root_adr_approval",
        subtasks={"scaffold": {"root_revise": 3}},
    )

    transition_mock = AsyncMock()
    with (
        patch.object(root_adr_approval, "async_session", _patched_session_factory(task)),
        patch.object(root_adr_approval, "transition", transition_mock),
    ):
        result = await root_adr_approval.apply_verdict(
            task.id, {"verdict": "revise", "comments": "still not right"}
        )

    assert result == TaskStatus.BLOCKED
    args = transition_mock.await_args.args
    assert args[2] == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_root_adr_approval_rejected_transitions_to_blocked():
    from agent.lifecycle.scaffold import root_adr_approval
    from shared.models import TaskStatus

    task = _make_scaffold_task(status="awaiting_root_adr_approval")

    transition_mock = AsyncMock()
    with (
        patch.object(root_adr_approval, "async_session", _patched_session_factory(task)),
        patch.object(root_adr_approval, "transition", transition_mock),
    ):
        result = await root_adr_approval.apply_verdict(
            task.id, {"verdict": "rejected", "comments": "scope explosion"}
        )

    assert result == TaskStatus.BLOCKED


# ---------------------------------------------------------------------------
# 3. Validators — root and domain ADR
# ---------------------------------------------------------------------------


_PASSING_ROOT_ADR = """\
# 000 — System ADR

## Vision
This is what we are building. It does the things and serves the users.

## Cross-cutting concerns
- Auth — JWT-based.

## Domains

```yaml
domains:
  - name: Auth
    slug: auth
    scope_summary: Authentication and session management for the app.
  - name: Billing
    slug: billing
    scope_summary: Billing, plans, invoices, and webhook ingestion from Stripe.
```
"""


def test_validate_root_adr_passes_on_good_input():
    from agent.lifecycle.scaffold.validators import validate_root_adr

    result = validate_root_adr(_PASSING_ROOT_ADR)
    assert result.ok, f"expected ok, got errors: {result.errors}"


def test_validate_root_adr_rejects_missing_vision_and_too_many_domains():
    from agent.lifecycle.scaffold.validators import validate_root_adr

    too_many = "\n".join(f"  - name: D{i}\n    slug: d{i}\n    scope_summary: x" for i in range(11))
    bad = f"""\
# Root ADR

```yaml
domains:
{too_many}
```
"""
    result = validate_root_adr(bad)
    assert not result.ok
    assert any("Vision" in e for e in result.errors)
    assert any("too many domains" in e for e in result.errors)


def test_validate_root_adr_rejects_missing_scope_summary():
    from agent.lifecycle.scaffold.validators import validate_root_adr

    bad = """\
# 000

## Vision
x.

```yaml
domains:
  - name: Auth
    slug: auth
    scope_summary: ""
```
"""
    result = validate_root_adr(bad)
    assert not result.ok
    assert any("scope_summary" in e for e in result.errors)


def test_parse_domains_extracts_yaml_block():
    from agent.lifecycle.scaffold.validators import parse_domains

    parsed = parse_domains(_PASSING_ROOT_ADR)
    assert [d["slug"] for d in parsed] == ["auth", "billing"]
    assert all(d["scope_summary"] for d in parsed)


def test_parse_domains_returns_empty_when_no_yaml_block():
    from agent.lifecycle.scaffold.validators import parse_domains

    assert parse_domains("# Just markdown, no yaml block.") == []


def test_validate_domain_adr_passes_with_all_sections_and_long_scope():
    from agent.lifecycle.scaffold.validators import validate_domain_adr

    scope_body = " ".join(["word"] * 85)
    adr = f"""\
# 001 — Auth ADR

## Scope
{scope_body}

## Aggregates
- User

## Public surface
- POST /login

## Integration points
- Billing — emits user.created

## Affected routes
- /login
"""
    result = validate_domain_adr(adr)
    assert result.ok, result.errors


def test_validate_domain_adr_rejects_short_scope():
    from agent.lifecycle.scaffold.validators import validate_domain_adr

    adr = """\
# Auth

## Scope
too short.

## Aggregates
- x

## Public surface
- x

## Integration points
- x

## Affected routes
- x
"""
    result = validate_domain_adr(adr)
    assert not result.ok
    assert any("Scope" in e and "too short" in e for e in result.errors)


# ---------------------------------------------------------------------------
# 5. dispatch_children — creates one child per approved domain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_children_creates_one_complex_large_child_per_approved_domain(
    tmp_path: Path,
):
    """Two domains, one approved + one rejected — only one child Task spawns."""

    from agent.lifecycle.scaffold import dispatch_children as dispatch_mod
    from agent.lifecycle.workspace_paths import (
        DOMAIN_ADR_APPROVALS_DIR,
        ROOT_ADR_PATH,
        domain_adr_path,
    )

    # Seed the workspace with root ADR + two domain ADRs + verdicts.
    auto_agent = tmp_path / ".auto-agent"
    (auto_agent / "adrs").mkdir(parents=True)
    (auto_agent / "domain_adr_approvals").mkdir(parents=True)
    (tmp_path / ROOT_ADR_PATH).write_text(_PASSING_ROOT_ADR)
    (tmp_path / domain_adr_path(1, "auth")).write_text("# Auth ADR\n\nbody")
    (tmp_path / domain_adr_path(2, "billing")).write_text("# Billing ADR\n\nbody")
    assert (tmp_path / DOMAIN_ADR_APPROVALS_DIR).exists()
    (tmp_path / DOMAIN_ADR_APPROVALS_DIR / "auth.json").write_text(
        json.dumps({"verdict": "approved"})
    )
    (tmp_path / DOMAIN_ADR_APPROVALS_DIR / "billing.json").write_text(
        json.dumps({"verdict": "rejected"})
    )

    task = _make_scaffold_task(status="dispatching_domain_builds")

    # Track tasks added to the fake session.
    added: list = []

    @asynccontextmanager
    async def fake_session_factory():
        class _Result:
            def scalars(self):
                class _S:
                    def all(self):
                        return []  # no existing children

                return _S()

            def scalar_one(self):
                return task

        next_id = [100]

        class _Session:
            async def execute(self, *_a, **_kw):
                return _Result()

            def add(self, obj):
                obj.id = next_id[0]
                next_id[0] += 1
                added.append(obj)

            async def commit(self):
                pass

            async def flush(self):
                pass

        yield _Session()

    publish_mock = AsyncMock()
    with (
        patch.object(
            dispatch_mod,
            "prepare_scaffold_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
        patch.object(dispatch_mod, "async_session", fake_session_factory),
        patch.object(dispatch_mod, "publish", publish_mock),
    ):
        created_ids = await dispatch_mod.run(task)

    from shared.models import TaskComplexity

    assert len(added) == 1
    child = added[0]
    assert child.parent_task_id == task.id
    assert child.complexity == TaskComplexity.COMPLEX_LARGE
    assert child.freeform_mode is True
    assert "auth" in (child.title or "").lower()
    assert created_ids == [child.id]
    publish_mock.assert_awaited()  # task_created was published


# ---------------------------------------------------------------------------
# 6. final_verification — writes the verdict file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_verification_writes_passed_verdict_when_no_gaps(tmp_path: Path):
    from agent.lifecycle.scaffold import final_verification as fv_mod
    from agent.lifecycle.workspace_paths import SCAFFOLD_FINAL_VERIFICATION_PATH

    task = _make_scaffold_task(status="awaiting_final_verification")

    # Fake handle: disabled smoke + no routes → no gaps.
    disabled_handle = MagicMock()
    disabled_handle.state = "disabled"
    disabled_handle.teardown = AsyncMock()

    with (
        patch.object(
            fv_mod,
            "prepare_scaffold_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
        patch.object(fv_mod, "_collect_union_routes", new=AsyncMock(return_value=[])),
        patch.object(
            fv_mod.verify_primitives,
            "boot_dev_server",
            new=AsyncMock(return_value=disabled_handle),
        ),
    ):
        verdict = await fv_mod.run(task)

    assert verdict == "passed"
    payload = json.loads((tmp_path / SCAFFOLD_FINAL_VERIFICATION_PATH).read_text())
    assert payload["verdict"] == "passed"
    assert payload["gaps"] == []


@pytest.mark.asyncio
async def test_final_verification_writes_gaps_found_when_boot_fails(tmp_path: Path):
    from agent.lifecycle.scaffold import final_verification as fv_mod
    from agent.lifecycle.workspace_paths import SCAFFOLD_FINAL_VERIFICATION_PATH

    task = _make_scaffold_task(status="awaiting_final_verification")

    failed_handle = MagicMock()
    failed_handle.state = "failed"
    failed_handle.failure_reason = "health_check_timeout"
    failed_handle.teardown = AsyncMock()

    with (
        patch.object(
            fv_mod,
            "prepare_scaffold_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
        patch.object(fv_mod, "_collect_union_routes", new=AsyncMock(return_value=[])),
        patch.object(
            fv_mod.verify_primitives,
            "boot_dev_server",
            new=AsyncMock(return_value=failed_handle),
        ),
    ):
        verdict = await fv_mod.run(task)

    assert verdict == "gaps_found"
    payload = json.loads((tmp_path / SCAFFOLD_FINAL_VERIFICATION_PATH).read_text())
    assert payload["verdict"] == "gaps_found"
    assert payload["gaps"]
    assert payload["gaps"][0]["kind"] == "boot_failure"


# ---------------------------------------------------------------------------
# 6b. final_verification — route sourcing + inspect_ui layer
# ---------------------------------------------------------------------------


def _write_child_backlog(workspaces_dir: Path, *, org_id: int, child_id: int, items: list[dict]):
    """Materialise <workspaces_dir>/<org>/task-<id>/.auto-agent/backlog.json."""

    from agent.lifecycle.workspace_paths import BACKLOG_PATH

    child_ws = workspaces_dir / str(org_id) / f"task-{child_id}"
    (child_ws / ".auto-agent").mkdir(parents=True)
    (child_ws / BACKLOG_PATH).write_text(json.dumps({"schema_version": "1", "items": items}))
    return child_ws


@pytest.mark.asyncio
async def test_collect_union_routes_reads_children_backlogs(tmp_path: Path, monkeypatch):
    """``_collect_union_routes`` should union routes across every child's
    backlog.json, dedupe, and tolerate children whose backlog is missing.
    """

    from agent.lifecycle.scaffold import final_verification as fv_mod
    from shared.models import TaskComplexity, TaskSource, TaskStatus

    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
    # The module captured WORKSPACES_DIR at import time; patch the bound name.
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))

    _write_child_backlog(
        tmp_path,
        org_id=7,
        child_id=11,
        items=[
            {"affected_routes": ["/login", "/"]},
            {"affected_routes": ["/campaigns", "/"]},  # `/` duplicate
        ],
    )
    _write_child_backlog(
        tmp_path,
        org_id=7,
        child_id=12,
        items=[{"affected_routes": ["/leads/{id}", "/campaigns"]}],  # `/campaigns` dup
    )
    # child 13 has no backlog on disk — should be silently skipped.

    children = [
        SimpleNamespace(
            id=cid,
            organization_id=7,
            status=TaskStatus.DONE,
            complexity=TaskComplexity.COMPLEX_LARGE,
            source=TaskSource.MANUAL,
        )
        for cid in (11, 12, 13)
    ]

    @asynccontextmanager
    async def factory():
        class _Result:
            def scalars(self):
                class _S:
                    def all(self):
                        return children

                return _S()

        class _Session:
            async def execute(self, *_a, **_kw):
                return _Result()

        yield _Session()

    with patch.object(fv_mod, "async_session", factory):
        routes = await fv_mod._collect_union_routes(parent_id=99)

    assert routes == ["/login", "/", "/campaigns", "/leads/{id}"]


@pytest.mark.asyncio
async def test_run_invokes_inspect_ui_with_intent_for_each_ui_route(tmp_path: Path):
    """When boot succeeds and routes have UI entries, ``run()`` must call
    ``inspect_ui`` for each UI route with the scaffold's ``intent.md``
    body passed as the ``intent`` arg.
    """

    from agent.lifecycle.scaffold import final_verification as fv_mod
    from agent.lifecycle.workspace_paths import INTENT_PATH

    (tmp_path / ".auto-agent").mkdir(parents=True)
    (tmp_path / INTENT_PATH).write_text("Build a campaign-management app.")

    task = _make_scaffold_task(status="awaiting_final_verification")

    running_handle = MagicMock()
    running_handle.state = "running"
    running_handle.base_url = "http://localhost:9999"
    running_handle.teardown = AsyncMock()

    # /login + /campaigns are UI; /api/health is not.
    routes = ["/login", "/campaigns", "/api/health"]

    def _ok_result():
        r = MagicMock()
        r.ok = True
        r.status = 200
        r.reason = ""
        return r

    route_results = {r: _ok_result() for r in routes}
    inspect_calls: list[dict] = []

    async def fake_inspect_ui(*, route, intent, base_url):
        inspect_calls.append({"route": route, "intent": intent, "base_url": base_url})
        ui = MagicMock()
        ui.ok = True
        ui.reason = ""
        return ui

    with (
        patch.object(
            fv_mod, "prepare_scaffold_workspace", new=AsyncMock(return_value=str(tmp_path))
        ),
        patch.object(fv_mod, "_collect_union_routes", new=AsyncMock(return_value=routes)),
        patch.object(
            fv_mod.verify_primitives, "boot_dev_server", new=AsyncMock(return_value=running_handle)
        ),
        patch.object(
            fv_mod.verify_primitives, "exercise_routes", new=AsyncMock(return_value=route_results)
        ),
        patch.object(fv_mod.verify_primitives, "inspect_ui", new=fake_inspect_ui),
    ):
        verdict = await fv_mod.run(task)

    assert verdict == "passed"
    # Only the two UI routes — /api/health is not UI.
    assert [c["route"] for c in inspect_calls] == ["/login", "/campaigns"]
    assert all(c["intent"] == "Build a campaign-management app." for c in inspect_calls)
    assert all(c["base_url"] == "http://localhost:9999" for c in inspect_calls)


@pytest.mark.asyncio
async def test_run_records_ui_mismatch_as_gap_but_ignores_missing_playwright(tmp_path: Path):
    """An ``inspect_ui`` failure should be a gap, except when Playwright
    is unavailable — that's an environment issue, not a UI mismatch.
    """

    from agent.lifecycle.scaffold import final_verification as fv_mod
    from agent.lifecycle.workspace_paths import SCAFFOLD_FINAL_VERIFICATION_PATH

    (tmp_path / ".auto-agent").mkdir(parents=True)

    task = _make_scaffold_task(status="awaiting_final_verification")

    running_handle = MagicMock()
    running_handle.state = "running"
    running_handle.base_url = "http://localhost:9999"
    running_handle.teardown = AsyncMock()

    routes = ["/broken", "/no-playwright"]

    def _ok_result():
        r = MagicMock()
        r.ok = True
        r.status = 200
        r.reason = ""
        return r

    route_results = {r: _ok_result() for r in routes}

    async def fake_inspect_ui(*, route, intent, base_url):
        ui = MagicMock()
        if route == "/broken":
            ui.ok = False
            ui.reason = "verdict=FAIL: no campaign list rendered"
        else:
            ui.ok = False
            ui.reason = "playwright_not_installed"
        return ui

    with (
        patch.object(
            fv_mod, "prepare_scaffold_workspace", new=AsyncMock(return_value=str(tmp_path))
        ),
        patch.object(fv_mod, "_collect_union_routes", new=AsyncMock(return_value=routes)),
        patch.object(
            fv_mod.verify_primitives, "boot_dev_server", new=AsyncMock(return_value=running_handle)
        ),
        patch.object(
            fv_mod.verify_primitives, "exercise_routes", new=AsyncMock(return_value=route_results)
        ),
        patch.object(fv_mod.verify_primitives, "inspect_ui", new=fake_inspect_ui),
    ):
        verdict = await fv_mod.run(task)

    assert verdict == "gaps_found"
    payload = json.loads((tmp_path / SCAFFOLD_FINAL_VERIFICATION_PATH).read_text())
    kinds = [g["kind"] for g in payload["gaps"]]
    assert kinds == ["ui_mismatch"]
    assert payload["gaps"][0]["route"] == "/broken"
