"""Stage 6 end-to-end orchestration test for the SCAFFOLD parent flow — ADR-018.

This test walks a SCAFFOLD parent task through every phase of the driver
state machine (intent grill → root ADR → root-approval gate → domain ADRs
→ per-domain gates → dispatch children trios → child fan-in → final
verification → DONE) without invoking a real LLM, a real DB, or real
GitHub.

It complements the per-phase unit tests in ``test_scaffold_lifecycle.py``
by exercising the full flow as orchestrated by ``run_scaffold_parent``,
including the external-event re-entry boundaries (gate verdicts, child
trio completion fan-in).

Approach — in-memory mocks throughout
-------------------------------------
- ``Task`` is a ``SimpleNamespace`` stand-in with the attribute surface
  the driver and gate helpers read.
- ``parent._transition_and_reload`` is patched to a pure-Python version
  that mutates an in-memory dict of tasks. No DB.
- ``parent.async_session`` is patched (for the final-verification round
  counter bump) with a tiny fake session.
- Each phase's ``run`` function is patched to write the canonical
  artefact under ``tmp_path/.auto-agent/...`` (intent.md, the root ADR
  with a YAML domains block, two domain ADRs) — these are the same paths
  the real phases write to, so anything downstream that reads them works
  unchanged.
- ``prepare_scaffold_workspace`` is patched to return ``tmp_path``.
- External events (verdict submission, child completion) are simulated
  by calling the gate-helper modules directly between driver invocations
  rather than going through the HTTP layer or the event bus.

The whole test runs synchronously in a single asyncio loop with no
external dependencies.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ROOT_ADR_FIXTURE = """\
# 000 — System ADR

## Vision
We are building a small CSV-to-JSON CLI tool. It reads a CSV file, parses
each row, and emits a JSON array on stdout. It exists so users do not
have to install jq or write a one-off Python script.

## Cross-cutting concerns
- Logging — stderr only; stdout is reserved for JSON output.
- Errors — exit non-zero with a clear message on malformed CSV.

## Domains

```yaml
domains:
  - name: Core
    slug: core
    scope_summary: CSV parsing, type inference, and JSON serialisation. Pure functions.
  - name: Cli
    slug: cli
    scope_summary: Argument parsing, file IO, and the user-facing command-line entry point.
```
"""


_DOMAIN_ADR_TEMPLATE = """\
# {index:03d} — {name} ADR

## Scope
{scope_long}

## Aggregates
- {name}Service

## Public surface
- `{slug}.run()` — entry point used by callers.

## Integration points
- Logging — stderr writer.

## Affected routes
- /{slug}
"""


def _domain_adr_md(index: int, name: str, slug: str) -> str:
    scope_long = " ".join([f"{slug}-word"] * 90)
    return _DOMAIN_ADR_TEMPLATE.format(
        index=index,
        name=name,
        slug=slug,
        scope_long=scope_long,
    )


def _make_scaffold_task(
    *,
    task_id: int = 1,
    status_value: str = "awaiting_intent_grill",
    subtasks: dict | None = None,
    organization_id: int = 7,
    parent_task_id: int | None = None,
):
    """Build a SimpleNamespace stand-in for the ``Task`` ORM row."""

    from shared.models import TaskComplexity, TaskSource, TaskStatus

    return SimpleNamespace(
        id=task_id,
        title="Build a CSV-to-JSON CLI",
        description=(
            "Build a small CLI tool that converts CSV files to JSON. "
            "Reads a CSV from a path argument, parses it, and emits a JSON "
            "array on stdout."
        ),
        status=TaskStatus(status_value),
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


def _clone_task(task, **overrides):
    """Return a fresh SimpleNamespace with the same fields and any overrides."""

    fields = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "complexity": task.complexity,
        "repo_id": task.repo_id,
        "repo": task.repo,
        "freeform_mode": task.freeform_mode,
        "organization_id": task.organization_id,
        "created_by_user_id": task.created_by_user_id,
        "parent_task_id": task.parent_task_id,
        "subtasks": task.subtasks,
        "source": task.source,
        "affected_routes": task.affected_routes,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# ---------------------------------------------------------------------------
# The e2e test — full SCAFFOLD parent flow, fully in-memory.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scaffold_parent_walks_full_flow_to_done(tmp_path: Path) -> None:
    """A SCAFFOLD parent starts at AWAITING_INTENT_GRILL and reaches DONE.

    Every phase is mocked. Between driver invocations we simulate the
    external events (gate verdicts, child completion fan-in) by calling
    the gate-helper modules directly, then re-invoking the driver — just
    as the HTTP router + ``_maybe_advance_scaffold_parent_on_child_finish``
    do in production.
    """

    from agent.lifecycle.scaffold import (
        domain_architect,
        intent_grill,
        root_architect,
    )
    from agent.lifecycle.scaffold import (
        parent as parent_mod,
    )
    from agent.lifecycle.workspace_paths import (
        INTENT_PATH,
        ROOT_ADR_PATH,
        domain_adr_path,
        domain_grill_path,
    )
    from shared.models import TaskComplexity, TaskStatus

    # --- Set up the in-memory task store. ----------------------------------
    parent_task = _make_scaffold_task(task_id=1)
    tasks: dict[int, SimpleNamespace] = {parent_task.id: parent_task}

    async def fake_transition_and_reload(task_id, to_status, *, message=""):
        live = tasks[task_id]
        live.status = to_status
        # Re-load: return a fresh namespace so the driver's local
        # variable doesn't alias the dict entry — matches production.
        return _clone_task(live)

    @asynccontextmanager
    async def fake_async_session():
        """Used only by the final-verification round-counter bump.

        The bump path opens a session, reloads the task, mutates
        ``task.subtasks``, and commits. We back it with the in-memory
        ``tasks`` dict so the mutation is visible on the next reload.
        """

        class _Result:
            def __init__(self, task):
                self._task = task

            def scalar_one(self):
                return self._task

        class _Session:
            async def execute(self, *_a, **_kw):
                # The driver only does ``select(Task).where(Task.id == ...)``
                # in this code path and we have a single parent task in flight.
                return _Result(tasks[parent_task.id])

            async def commit(self):
                pass

            def add(self, _obj):
                pass

        yield _Session()

    # --- Phase stubs. Each writes its canonical artefact to tmp_path. ------

    workspace_root = str(tmp_path)
    (tmp_path / ".auto-agent").mkdir(exist_ok=True)
    (tmp_path / ".auto-agent" / "adrs").mkdir(exist_ok=True)
    (tmp_path / ".auto-agent" / "domain_adr_approvals").mkdir(exist_ok=True)

    async def fake_intent_run(task):
        target = tmp_path / INTENT_PATH
        target.write_text(
            "# Intent\n\nBuild a CSV-to-JSON CLI. The user runs `csv2json file.csv`.\n"
        )
        return str(target)

    async def fake_root_architect_run(task):
        target = tmp_path / ROOT_ADR_PATH
        target.write_text(_ROOT_ADR_FIXTURE)
        return str(target)

    # ADR-018 Stage 8 — the domain loop is now (grill → architect) per
    # domain. The fake mirrors what production does: write a grill summary
    # for each domain before its ADR. We use a counter so the first
    # invocation simulates a grill pause on the first domain (parent
    # transitions to AWAITING_DOMAIN_GRILL), and the second invocation
    # (after the simulated user answer) runs both grills + both ADRs to
    # completion.
    domain_architect_calls = {"n": 0}

    async def fake_domain_architect_run(task):
        domain_architect_calls["n"] += 1
        domain_specs = [("Core", "core"), ("Cli", "cli")]

        if domain_architect_calls["n"] == 1:
            # Simulate a grill pause on the first domain — no artefacts
            # written yet for that domain.
            return {
                "status": "awaiting_grill",
                "domain_slug": "core",
                "question": "Should we accept stdin too?",
            }

        # Re-entry after the user (simulated below) answered the grill.
        # Write both grill summaries and both ADRs as if the agent loop
        # ran to completion.
        results = []
        for idx, (name, slug) in enumerate(domain_specs, start=1):
            grill_rel = domain_grill_path(idx, slug)
            grill_abs = tmp_path / grill_rel
            grill_abs.parent.mkdir(parents=True, exist_ok=True)
            grill_abs.write_text(
                f"# Domain grill — {name}\n\n## Scope\nstub\n\n"
                "## Open questions answered\n- Q: ...\n  A: ...\n\n"
                "## Out of scope for this domain\n- (none)\n\n"
                "## Constraints surfaced\n- (none)\n"
            )
            rel = domain_adr_path(idx, slug)
            abs_path = tmp_path / rel
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(_domain_adr_md(idx, name, slug))
            results.append({"name": name, "slug": slug, "index": idx, "adr_path": rel})
        return {"status": "all_complete", "results": results}

    # dispatch_children: do not create real DB rows; instead create
    # in-memory child Task stand-ins so the fan-in simulation can flip
    # them DONE without needing the orchestrator.
    created_child_ids: list[int] = []

    async def fake_dispatch_children_run(task):
        from shared.models import TaskSource, TaskStatus

        next_id = max(tasks.keys()) + 1
        for slug in ("core", "cli"):
            child = SimpleNamespace(
                id=next_id,
                title=f"Domain build: {slug.title()} ({slug})",
                description=f"Build {slug}",
                status=TaskStatus.INTAKE,
                complexity=TaskComplexity.COMPLEX_LARGE,
                repo_id=None,
                repo=None,
                freeform_mode=True,
                organization_id=task.organization_id,
                created_by_user_id=None,
                parent_task_id=task.id,
                subtasks=None,
                source=TaskSource.MANUAL,
                affected_routes=[],
            )
            tasks[child.id] = child
            created_child_ids.append(child.id)
            next_id += 1
        return list(created_child_ids)

    final_verification_calls = {"n": 0}

    async def fake_final_verification_run(task):
        final_verification_calls["n"] += 1
        # Return the verdict the parent expects to short-circuit to DONE.
        return "passed"

    # --- Patch root_adr_approval + domain_adr_approval to use the same -----
    # in-memory tasks dict and tmp_path workspace. We need to bypass their
    # internal ``async_session`` + ``prepare_scaffold_workspace`` calls.

    from agent.lifecycle.scaffold import (
        domain_adr_approval,
        root_adr_approval,
    )

    @asynccontextmanager
    async def gate_session_factory():
        """Shared in-memory session used by the verdict helpers."""

        class _Result:
            def __init__(self, task):
                self._task = task

            def scalar_one(self):
                return self._task

        class _Session:
            async def execute(self, *_a, **_kw):
                return _Result(tasks[parent_task.id])

            async def commit(self):
                pass

            def add(self, _obj):
                pass

        yield _Session()

    async def fake_state_transition(_session, task, to_status, message=""):
        """Stand-in for ``orchestrator.state_machine.transition``.

        Skips the validity check so the test stays focused on the
        scaffold flow rather than on every transition's whitelist (those
        are covered in ``test_scaffold_state_machine``). Writes the
        new status onto both the local and the in-memory store row.
        """

        task.status = to_status
        if task.id in tasks:
            tasks[task.id].status = to_status
        return task

    # ADR-019 T7 — mock the secrets gate check to simulate zero missing
    # secrets. In the e2e flow we don't want to stand up a real DB for
    # pgcrypto; we just need to verify the driver routes through the new
    # AWAITING_REQUIRED_SECRETS status correctly before proceeding to
    # DISPATCHING_DOMAIN_BUILDS.
    async def fake_check_secrets_gate(task):
        """Simulate a green secrets gate: no missing secrets → transition + True."""
        tasks[task.id].status = TaskStatus.DISPATCHING_DOMAIN_BUILDS
        return True

    # --- Run the test under the full patch suite. --------------------------

    with (
        patch.object(parent_mod, "_transition_and_reload", fake_transition_and_reload),
        patch.object(parent_mod, "async_session", fake_async_session),
        patch.object(intent_grill, "run", AsyncMock(side_effect=fake_intent_run)),
        patch.object(root_architect, "run", AsyncMock(side_effect=fake_root_architect_run)),
        patch.object(domain_architect, "run", AsyncMock(side_effect=fake_domain_architect_run)),
        patch(
            "agent.lifecycle.scaffold.dispatch_children.run",
            AsyncMock(side_effect=fake_dispatch_children_run),
        ),
        patch(
            "agent.lifecycle.scaffold.dispatch_children.check_secrets_gate",
            AsyncMock(side_effect=fake_check_secrets_gate),
        ),
        patch(
            "agent.lifecycle.scaffold.final_verification.run",
            AsyncMock(side_effect=fake_final_verification_run),
        ),
        patch.object(root_adr_approval, "async_session", gate_session_factory),
        patch.object(root_adr_approval, "transition", fake_state_transition),
        patch.object(domain_adr_approval, "async_session", gate_session_factory),
        patch.object(domain_adr_approval, "transition", fake_state_transition),
        patch.object(
            domain_adr_approval,
            "prepare_scaffold_workspace",
            new=AsyncMock(return_value=workspace_root),
        ),
    ):
        # ===== Driver invocation #1: AWAITING_INTENT_GRILL → AWAITING_ROOT_ADR_APPROVAL
        await parent_mod.run_scaffold_parent(parent_task)

        # After intent → root architect → external gate. The driver must
        # have left us parked at AWAITING_ROOT_ADR_APPROVAL with both
        # artefacts on disk.
        assert (tmp_path / INTENT_PATH).is_file(), "intent.md was not written"
        assert (tmp_path / ROOT_ADR_PATH).is_file(), "000-system.md was not written"
        root_adr_md = (tmp_path / ROOT_ADR_PATH).read_text()
        assert "domains:" in root_adr_md, "root ADR missing YAML domains block"
        assert tasks[parent_task.id].status == TaskStatus.AWAITING_ROOT_ADR_APPROVAL

        # ===== Simulate root-ADR approval verdict (freeform: PO approves) =====
        result_status = await root_adr_approval.apply_verdict(
            parent_task.id, {"verdict": "approved", "comments": "lgtm"}
        )
        assert result_status == TaskStatus.BUILDING_DOMAIN_ADRS
        assert tasks[parent_task.id].status == TaskStatus.BUILDING_DOMAIN_ADRS

        # ===== Driver invocation #2a: BUILDING_DOMAIN_ADRS → AWAITING_DOMAIN_GRILL
        # First invocation: the grill on `core` pauses. The driver parks
        # the parent at AWAITING_DOMAIN_GRILL.
        reloaded = _clone_task(tasks[parent_task.id])
        await parent_mod.run_scaffold_parent(reloaded)
        assert tasks[parent_task.id].status == TaskStatus.AWAITING_DOMAIN_GRILL
        assert domain_architect_calls["n"] == 1

        # ===== Simulate the user answering the grill question. The endpoint
        # transitions AWAITING_DOMAIN_GRILL → BUILDING_DOMAIN_ADRS; we
        # mimic that directly here.
        tasks[parent_task.id].status = TaskStatus.BUILDING_DOMAIN_ADRS

        # ===== Driver invocation #2b: BUILDING_DOMAIN_ADRS → BUILDING_DOMAINS
        # Re-entry: both grills + both ADRs complete; the freeform driver
        # walks straight through AWAITING_DOMAIN_ADR_APPROVAL (PO standin
        # verdicts each domain), AWAITING_REQUIRED_SECRETS (mocked to pass),
        # DISPATCHING_DOMAIN_BUILDS, and parks at BUILDING_DOMAINS — the
        # driver's only wait-on-children state.
        reloaded = _clone_task(tasks[parent_task.id])
        await parent_mod.run_scaffold_parent(reloaded)

        assert (tmp_path / domain_adr_path(1, "core")).is_file(), "core ADR missing"
        assert (tmp_path / domain_adr_path(2, "cli")).is_file(), "cli ADR missing"
        assert (tmp_path / domain_grill_path(1, "core")).is_file(), "core grill missing"
        assert (tmp_path / domain_grill_path(2, "cli")).is_file(), "cli grill missing"
        assert domain_architect_calls["n"] == 2

        # Two children created, one per approved domain.
        children = [t for t in tasks.values() if t.parent_task_id == parent_task.id]
        assert len(children) == 2, f"expected 2 children, got {len(children)}"
        assert all(c.complexity == TaskComplexity.COMPLEX_LARGE for c in children)
        assert tasks[parent_task.id].status == TaskStatus.BUILDING_DOMAINS

        # ===== Simulate every child reaching DONE, then the fan-in handler
        # transitions the parent to AWAITING_FINAL_VERIFICATION. We mimic
        # ``_maybe_advance_scaffold_parent_on_child_finish`` from run.py
        # without needing the DB.
        for child in children:
            child.status = TaskStatus.DONE
        tasks[parent_task.id].status = TaskStatus.AWAITING_FINAL_VERIFICATION

        # ===== Driver invocation #4: AWAITING_FINAL_VERIFICATION → DONE =======
        reloaded = _clone_task(tasks[parent_task.id])
        await parent_mod.run_scaffold_parent(reloaded)

        assert final_verification_calls["n"] == 1
        assert tasks[parent_task.id].status == TaskStatus.DONE


# ---------------------------------------------------------------------------
# Smaller smoke test — driver is a no-op when re-invoked while parked at
# an external gate. Stage 6 in-memory verification that the re-entry
# behaviour the production HTTP layer relies on is intact.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_driver_is_noop_when_parked_at_building_domains() -> None:
    """BUILDING_DOMAINS is a pure wait-state — the driver must not run
    any phase when re-invoked there, because it would otherwise re-fire
    dispatch_children and re-create children every fan-in."""

    from agent.lifecycle.scaffold import parent as parent_mod

    task = _make_scaffold_task(status_value="building_domains")

    with (
        patch.object(parent_mod.intent_grill, "run", AsyncMock()) as i,
        patch.object(parent_mod.root_architect, "run", AsyncMock()) as r,
        patch.object(parent_mod.domain_architect, "run", AsyncMock()) as d,
        patch.object(parent_mod.dispatch_children, "run", AsyncMock()) as disp,
        patch.object(parent_mod.final_verification, "run", AsyncMock()) as fv,
    ):
        await parent_mod.run_scaffold_parent(task)

    for m in (i, r, d, disp, fv):
        m.assert_not_awaited()
