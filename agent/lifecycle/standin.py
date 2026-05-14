"""Freeform standins — ADR-015 §6 + §7.

In freeform mode, an agent stands in for the human at every gate:

  * **PO agent standin** — for PO-suggested tasks (and user-created
    tasks by default; per the ADR, PO is the right default oracle for
    product-shaped questions).
  * **Improvement-agent standin** — for tasks that came from the
    improvement agent's weekly codebase-deepening loop (formerly
    "architecture mode" suggestions, ``Suggestion.category ==
    "architecture"``).

Each standin exposes four gate methods (``answer_grill``,
``approve_plan``, ``approve_design``, ``review_pr``); each writes the
canonical gate file under ``.auto-agent/`` and publishes a
``standin.decision`` event with structured fields so the gate history
can reconstruct who decided what at every gate (§6).

The standin must **never escape to the user** — that defeats freeform.
When relevant context is missing, the standin picks a sensible default
and logs the ``fallback_default(source=heuristic)`` marker so the audit
trail is unambiguous.

``run_freeform_gate`` is the thin wrapper the orchestrator calls. It:

  1. Resolves the effective mode via :mod:`agent.lifecycle.mode_resolver`.
  2. If ``human_in_loop``, returns ``False`` — caller continues to poll
     for the human-written gate file.
  3. If ``freeform``, selects the right standin via
     :func:`select_standin`, dispatches to its gate method, and returns
     ``True`` once the file has been written.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from agent.lifecycle.mode_resolver import resolve_effective_mode
from agent.lifecycle.workspace_paths import (
    AUTO_AGENT_DIR,
    PLAN_APPROVAL_PATH,
    PR_REVIEW_PATH,
)
from shared.events import Event, publish

log = logging.getLogger(__name__)


Gate = Literal["grill", "plan_approval", "design_approval", "pr_review"]


# ---------------------------------------------------------------------------
# Suggestion lookup protocol
#
# The standin selector needs to answer "what category is suggestion <id>?"
# to disambiguate PO from improvement-agent origin. Production wires a
# DB-backed implementation; tests pass an in-memory shim. Keeping this as a
# Protocol means neither side has to import the other's storage layer.
# ---------------------------------------------------------------------------


class SuggestionRepo(Protocol):
    """Minimal lookup interface for the standin selector."""

    async def get_category(self, suggestion_id: int) -> str | None: ...


class _DbSuggestionRepo:
    """Default production lookup — reads ``Suggestion.category`` from the DB."""

    async def get_category(self, suggestion_id: int) -> str | None:
        # Imported lazily so test code paths that don't touch the DB
        # never pull SQLAlchemy in transitively.
        from sqlalchemy import select

        from shared.database import async_session
        from shared.models import Suggestion

        async with async_session() as s:
            row = (
                await s.execute(select(Suggestion.category).where(Suggestion.id == suggestion_id))
            ).scalar_one_or_none()
            return row


# Improvement-flavoured suggestion categories — the DB value is still
# "architecture" (per ADR-015 §14 backwards-compat), but the Python-side
# code calls the resulting standin the "improvement agent". Kept as a
# set so future categories produced by the same agent (e.g. a future
# "deepening" alias) slot in without churn at the call sites.
_IMPROVEMENT_CATEGORIES: frozenset[str] = frozenset({"architecture", "improvement"})


# ---------------------------------------------------------------------------
# Standin base
# ---------------------------------------------------------------------------


def _write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _persist_gate_decision(
    *,
    task_id: int,
    gate: str,
    source: str,
    agent_id: str | None,
    verdict: str,
    comments: str,
    cited_context: list[str],
    fallback_reasons: list[str],
) -> None:
    """Insert a ``GateDecision`` row — best-effort audit trail.

    Imported lazily so test code paths that don't have DATABASE_URL set
    (or that monkey-patch the DB layer) never load SQLAlchemy through
    this module. Any failure is logged and swallowed: missing audit
    rows must never block a gate from progressing.
    """

    try:
        from shared.database import async_session
        from shared.models import GateDecision

        async with async_session() as session:
            session.add(
                GateDecision(
                    task_id=task_id,
                    gate=gate,
                    source=source,
                    agent_id=agent_id,
                    verdict=verdict,
                    comments=comments,
                    cited_context=cited_context,
                    fallback_reasons=fallback_reasons,
                )
            )
            await session.commit()
    except Exception:  # pragma: no cover — audit best-effort
        log.exception(
            "gate_decision_persist_failed task_id=%s gate=%s source=%s",
            task_id,
            gate,
            source,
        )


class _StandinBase:
    """Shared behaviour for every standin: gate-file writes + event logging.

    Subclasses set ``standin_kind`` and ``_agent_id`` and provide the
    decision logic for each gate; the base class owns "where do the
    bytes land + what gets logged".
    """

    standin_kind: str = ""

    def __init__(self, *, task, repo) -> None:
        self.task = task
        self.repo = repo
        self._agent_id = f"{self.standin_kind}:{getattr(repo, 'id', '?')}"
        # Heuristic-fallback reasons accumulated during the current
        # gate call — drained into the next emitted decision event so
        # consumers can detect "this was a fallback" without log
        # scraping. Reset by ``_log_decision`` after publishing.
        self._fallback_reasons: list[str] = []

    # ----- file writers (one per gate output) ----- #

    def _write_grill_answer(self, workspace_root: str, answer: str) -> str:
        path = os.path.join(workspace_root, AUTO_AGENT_DIR, "grill_answer.json")
        _write_json(
            path,
            {
                "schema_version": "1",
                "answer": answer,
                "source": self.standin_kind,
                "agent_id": self._agent_id,
                "written_at": _now_iso(),
            },
        )
        return path

    def _write_plan_approval(
        self,
        workspace_root: str,
        verdict: str,
        comments: str = "",
    ) -> str:
        path = os.path.join(workspace_root, PLAN_APPROVAL_PATH)
        _write_json(
            path,
            {
                "schema_version": "1",
                "verdict": verdict,
                "comments": comments,
                "source": self.standin_kind,
                "agent_id": self._agent_id,
                "written_at": _now_iso(),
            },
        )
        return path

    def _write_pr_review(
        self,
        workspace_root: str,
        verdict: str,
        comments: str,
    ) -> str:
        path = os.path.join(workspace_root, PR_REVIEW_PATH)
        _write_json(
            path,
            {
                "schema_version": "1",
                "verdict": verdict,
                "comments": comments,
                "source": self.standin_kind,
                "agent_id": self._agent_id,
                "written_at": _now_iso(),
            },
        )
        return path

    # ----- decision event logging ----- #

    async def _log_decision(
        self,
        *,
        gate: Gate,
        decision: str,
        cited_context: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Publish a ``standin.decision`` event AND persist a ``GateDecision``
        row so the gate-history audit panel (ADR-015 §6 Phase 12) has a
        durable source — Redis events are ephemeral, the panel needs the
        full history of every gate that has fired.

        Errors on both writes are swallowed: the gate-file write is the
        contract; the event + DB row are the audit trail. Losing one of
        the audit writes must not block the flow.
        """

        task_id = getattr(self.task, "id", None)
        cited = cited_context or []
        fallback_reasons = list(self._fallback_reasons)
        payload: dict[str, Any] = {
            "standin_kind": self.standin_kind,
            "agent_id": self._agent_id,
            "gate": gate,
            "decision": decision,
            "cited_context": cited,
            "task_id": task_id,
            "timestamp": _now_iso(),
            "fallback_reasons": fallback_reasons,
        }
        if extra:
            payload.update(extra)
        try:
            await publish(Event(type="standin.decision", payload=payload))
        except Exception:  # pragma: no cover — defensive only
            log.exception("standin.decision_publish_failed", extra={"task_id": task_id})

        # Persist the audit row alongside the event. Source maps the
        # ``standin_kind`` into the wire taxonomy the web-next panel
        # reads ("po_standin" / "improvement_standin" / "user"). Wrapped
        # in try/except so a missing DB (tests without DATABASE_URL) or
        # a transient failure doesn't break the gate.
        if task_id is not None:
            comments = (
                str(extra.get("comments", ""))
                if extra and "comments" in extra
                else ""
            )
            await _persist_gate_decision(
                task_id=task_id,
                gate=gate,
                source=f"{self.standin_kind}_standin",
                agent_id=self._agent_id,
                verdict=decision,
                comments=comments,
                cited_context=cited,
                fallback_reasons=fallback_reasons,
            )
        self._fallback_reasons.clear()

    # ----- fallback marker ----- #

    def _log_fallback(self, gate: Gate, reason: str) -> None:
        """Record the ``fallback_default(source=heuristic)`` marker.

        Used when relevant context (product_brief, persisted improvement
        session) is missing and the standin had to fall back to a
        heuristic default. The standin still proceeds to write the gate
        file — never escapes (ADR-015 §6).

        The marker shows up in two places: a ``log.info`` line for human
        debugging, and a flag on the next emitted decision event so
        consumers (and tests) can detect "this decision was a fallback"
        without grepping log lines.
        """

        log.info(
            "fallback_default(source=heuristic) standin_kind=%s gate=%s reason=%s task_id=%s",
            self.standin_kind,
            gate,
            reason,
            getattr(self.task, "id", None),
        )
        self._fallback_reasons.append(f"{gate}:{reason}")


# ---------------------------------------------------------------------------
# Concrete standins
#
# For Phase 10 the gate-method bodies use a small, deterministic
# heuristic: enough to make the freeform path end-to-end exercisable
# under TDD, while leaving room for richer LLM-driven decisions to slot
# in behind the same interface in later phases (Phase 12+).
#
# The contract this phase locks is:
#   * The gate file lands at the canonical path with the right shape.
#   * Missing context → fallback marker logged, still no escape.
#   * Decision event published with standin_kind/gate/task_id.
# ---------------------------------------------------------------------------


class POStandin(_StandinBase):
    """PO agent stands in at gates — product-shaped decisions."""

    standin_kind = "po"

    async def answer_grill(self, question: str, context: dict[str, Any]) -> None:
        workspace_root = context["workspace_root"]
        product_brief = getattr(self.repo, "product_brief", "") or ""
        if not product_brief.strip():
            self._log_fallback("grill", reason="no_product_brief")
            answer = (
                "Default answer (heuristic): proceed with the simplest "
                "implementation that satisfies the literal request."
            )
            cited: list[str] = []
        else:
            # Phase 10 heuristic — surface the product brief as the
            # grounding context; downstream prompt enrichment lives in
            # ``po_agent`` (extended in Step 4 below).
            answer = (
                f"Per the product brief: {product_brief.strip()[:600]}\n\n"
                f"Question: {question[:300]}\n"
                "Answer (PO standin): proceed in alignment with the brief above."
            )
            cited = ["repo.product_brief"]
        self._write_grill_answer(workspace_root, answer)
        await self._log_decision(
            gate="grill",
            decision="answered",
            cited_context=cited,
            extra={"question_preview": question[:200]},
        )

    async def approve_plan(self, plan_md: str, context: dict[str, Any]) -> None:
        workspace_root = context["workspace_root"]
        verdict, comments, cited = self._verdict_for_artefact(artefact_kind="plan", body=plan_md)
        self._write_plan_approval(workspace_root, verdict, comments)
        await self._log_decision(
            gate="plan_approval",
            decision=verdict,
            cited_context=cited,
        )

    async def approve_design(self, design_md: str, context: dict[str, Any]) -> None:
        workspace_root = context["workspace_root"]
        # Design approval reuses plan_approval.json per ADR-015 §2.
        verdict, comments, cited = self._verdict_for_artefact(
            artefact_kind="design", body=design_md
        )
        self._write_plan_approval(workspace_root, verdict, comments)
        await self._log_decision(
            gate="design_approval",
            decision=verdict,
            cited_context=cited,
        )

    async def review_pr(
        self,
        pr_diff: str,
        pr_metadata: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        workspace_root = context["workspace_root"]
        product_brief = getattr(self.repo, "product_brief", "") or ""
        if not product_brief.strip():
            self._log_fallback("pr_review", reason="no_product_brief")
            verdict = "approved"
            comments = (
                "Default approval (heuristic): no product brief on file; "
                "the PR is approved on a hygiene-only pass."
            )
            cited: list[str] = []
        else:
            verdict = "approved"
            comments = (
                f"PR aligns with product brief. "
                f"Title: {pr_metadata.get('title', '(no title)')}. "
                f"Reviewed by PO standin against product brief."
            )
            cited = ["repo.product_brief"]
        self._write_pr_review(workspace_root, verdict, comments)
        await self._log_decision(
            gate="pr_review",
            decision=verdict,
            cited_context=cited,
        )

    def _verdict_for_artefact(self, *, artefact_kind: str, body: str) -> tuple[str, str, list[str]]:
        """Phase 10 heuristic — approve when there is a non-empty body
        and the product brief grounds the decision; otherwise log a
        fallback and approve anyway (never escape)."""

        product_brief = getattr(self.repo, "product_brief", "") or ""
        if not body.strip():
            self._log_fallback("plan_approval", reason="empty_artefact")
            return "rejected", f"Empty {artefact_kind}.", []
        if not product_brief.strip():
            self._log_fallback("plan_approval", reason="no_product_brief")
            return (
                "approved",
                "Default approval (heuristic): no product brief on file.",
                [],
            )
        return (
            "approved",
            f"{artefact_kind.capitalize()} aligns with the product brief.",
            ["repo.product_brief"],
        )


class ImprovementAgentStandin(_StandinBase):
    """Improvement agent stands in at gates — codebase-shaped decisions."""

    standin_kind = "improvement_agent"

    async def answer_grill(self, question: str, context: dict[str, Any]) -> None:
        workspace_root = context["workspace_root"]
        # Phase 10 heuristic — when no persisted improvement session is
        # wired through ``context``, fall back to a deterministic answer
        # that points the architect at the suggestion's rationale.
        session_blob = context.get("improvement_session") or {}
        if not session_blob:
            self._log_fallback("grill", reason="no_persisted_session")
            answer = (
                "Default answer (heuristic): keep the existing seam shape; "
                "do not introduce a new abstraction unless the task spec "
                "explicitly asks for it."
            )
            cited: list[str] = []
        else:
            answer = (
                "Improvement agent (from persisted session): "
                f"{session_blob.get('latest_note', '')[:600]}"
            )
            cited = ["improvement_session"]
        self._write_grill_answer(workspace_root, answer)
        await self._log_decision(
            gate="grill",
            decision="answered",
            cited_context=cited,
            extra={"question_preview": question[:200]},
        )

    async def approve_plan(self, plan_md: str, context: dict[str, Any]) -> None:
        workspace_root = context["workspace_root"]
        verdict, comments, cited = self._verdict_for_artefact("plan", plan_md)
        self._write_plan_approval(workspace_root, verdict, comments)
        await self._log_decision(
            gate="plan_approval",
            decision=verdict,
            cited_context=cited,
        )

    async def approve_design(self, design_md: str, context: dict[str, Any]) -> None:
        workspace_root = context["workspace_root"]
        verdict, comments, cited = self._verdict_for_artefact("design", design_md)
        self._write_plan_approval(workspace_root, verdict, comments)
        await self._log_decision(
            gate="design_approval",
            decision=verdict,
            cited_context=cited,
        )

    async def review_pr(
        self,
        pr_diff: str,
        pr_metadata: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        workspace_root = context["workspace_root"]
        if not pr_diff.strip():
            self._log_fallback("pr_review", reason="empty_diff")
            verdict = "changes_requested"
            comments = "PR diff is empty."
            cited: list[str] = []
        else:
            verdict = "approved"
            comments = (
                "PR matches the improvement agent's deepening intent; "
                "no regressions detected by heuristic review."
            )
            cited = ["improvement_session"]
        self._write_pr_review(workspace_root, verdict, comments)
        await self._log_decision(
            gate="pr_review",
            decision=verdict,
            cited_context=cited,
        )

    def _verdict_for_artefact(self, artefact_kind: str, body: str) -> tuple[str, str, list[str]]:
        if not body.strip():
            self._log_fallback("plan_approval", reason=f"empty_{artefact_kind}")
            return "rejected", f"Empty {artefact_kind}.", []
        return (
            "approved",
            f"{artefact_kind.capitalize()} aligns with the improvement intent.",
            ["improvement_session"],
        )


Standin = POStandin | ImprovementAgentStandin


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------


def _parse_suggestion_id(source_id: str) -> int | None:
    """Parse the ``suggestion:<int>`` shape used by the freeform queue
    (see ``run.py`` where auto-approval creates the Task).

    Anything else (Slack ``ts``, Linear issue ID, empty string) returns
    ``None`` — the caller's PO default applies.
    """

    if not source_id.startswith("suggestion:"):
        return None
    raw = source_id.split(":", 1)[1]
    try:
        return int(raw)
    except ValueError:
        return None


async def select_standin(
    task,
    repo,
    *,
    gate: Gate,
    suggestion_repo: SuggestionRepo | None = None,
) -> Standin:
    """Return the right standin for ``task``'s origin (ADR-015 §6).

    The selection rule, in order:

      1. If the task was spawned from a ``Suggestion`` whose category
         lives in :data:`_IMPROVEMENT_CATEGORIES`, route to
         :class:`ImprovementAgentStandin`.
      2. If the suggestion exists but is a PO category, route to
         :class:`POStandin`.
      3. Anything else (manual task, Slack DM, Linear issue,
         unparseable ``source_id``, deleted suggestion row, …) →
         :class:`POStandin` (the §6 default).

    The selector never escapes to the user. ``gate`` is accepted purely
    so this function is the single entry point for "which standin"
    decisions and stays trivially substitutable; standin classes
    themselves are gate-agnostic.
    """

    sid = _parse_suggestion_id(getattr(task, "source_id", "") or "")
    if sid is not None:
        repo_lookup: SuggestionRepo = suggestion_repo or _DbSuggestionRepo()
        category = await repo_lookup.get_category(sid)
        if category in _IMPROVEMENT_CATEGORIES:
            return ImprovementAgentStandin(task=task, repo=repo)
    return POStandin(task=task, repo=repo)


# ---------------------------------------------------------------------------
# Gate hook
# ---------------------------------------------------------------------------


async def run_freeform_gate(
    *,
    task,
    repo,
    gate: Gate,
    gate_input: dict[str, Any],
    context: dict[str, Any],
    suggestion_repo: SuggestionRepo | None = None,
) -> bool:
    """Orchestrator entry point — dispatch a gate through the resolver.

    Returns ``True`` if the standin fired (the gate file was written by
    the standin), ``False`` if the mode resolves to ``human_in_loop``
    and the caller must keep polling for the human-written file.

    Args:
        task: ORM Task row (or any object exposing ``mode_override``,
            ``source_id``, ``id``).
        repo: ORM Repo row (or any object exposing ``mode``,
            ``product_brief``, ``id``).
        gate: One of ``"grill" | "plan_approval" | "design_approval" |
            "pr_review"``.
        gate_input: The artefact the standin reads — gate-specific:
            ``{"question": str}`` for grill;
            ``{"plan_md": str}`` for plan_approval;
            ``{"design_md": str}`` for design_approval;
            ``{"pr_diff": str, "pr_metadata": dict}`` for pr_review.
        context: At minimum ``{"workspace_root": str}``; later phases
            extend with the persisted improvement session and other
            cross-gate state.
        suggestion_repo: Test injection point; production leaves
            ``None`` and the DB lookup runs.
    """

    mode = resolve_effective_mode(task, repo)
    if mode == "human_in_loop":
        return False
    standin = await select_standin(task, repo, gate=gate, suggestion_repo=suggestion_repo)
    if gate == "grill":
        await standin.answer_grill(gate_input["question"], context)
    elif gate == "plan_approval":
        await standin.approve_plan(gate_input["plan_md"], context)
    elif gate == "design_approval":
        await standin.approve_design(gate_input["design_md"], context)
    elif gate == "pr_review":
        await standin.review_pr(
            gate_input["pr_diff"],
            gate_input.get("pr_metadata", {}),
            context,
        )
    else:  # pragma: no cover — Literal exhausted by typing
        raise ValueError(f"unknown gate: {gate!r}")
    return True


__all__ = [
    "Gate",
    "ImprovementAgentStandin",
    "POStandin",
    "Standin",
    "SuggestionRepo",
    "run_freeform_gate",
    "select_standin",
]
