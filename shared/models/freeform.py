import enum
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .core import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SuggestionStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class MarketBrief(Base):
    """Versioned market-research brief produced by the market_researcher agent.

    Consumed by the PO analyzer to ground its suggestions in cited evidence.
    """
    __tablename__ = "market_briefs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repos.id"), nullable=False, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id"), nullable=False, index=True,
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    product_category = Column(Text, nullable=True)
    competitors = Column(JSONB, default=list, nullable=False)
    findings = Column(JSONB, default=list, nullable=False)
    modality_gaps = Column(JSONB, default=list, nullable=False)
    strategic_themes = Column(JSONB, default=list, nullable=False)
    summary = Column(Text, default="", nullable=False)
    raw_sources = Column(JSONB, default=list, nullable=False)
    partial = Column(Boolean, default=False, nullable=False)
    agent_turns = Column(Integer, default=0, nullable=False)

    repo = relationship("Repo")


class Suggestion(Base):
    """PO-generated improvement suggestions for a repo."""
    __tablename__ = "suggestions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repos.id"), nullable=False)
    title = Column(String(512), nullable=False)
    description = Column(Text, default="")
    rationale = Column(Text, default="")
    category = Column(String(100), default="")  # ux_gap, feature, improvement, architecture
    priority = Column(Integer, default=3)  # 1=critical, 5=nice-to-have
    status = Column(Enum(SuggestionStatus), default=SuggestionStatus.PENDING)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id"), nullable=False, index=True,
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    evidence_urls = Column(JSONB, default=list, nullable=False)
    brief_id = Column(Integer, ForeignKey("market_briefs.id"), nullable=True)

    repo = relationship("Repo")
    task = relationship("Task")
    brief = relationship("MarketBrief")


# Categories whose tasks arrive pre-grilled — the analyzer that produced the
# suggestion has already applied the grill-with-docs lens, so the resulting
# task should skip the grill phase. Encoded as intake_qa = [] on the Task.
PRE_GRILLED_SUGGESTION_CATEGORIES: frozenset[str] = frozenset({"architecture"})


def intake_qa_for_suggestion(category: str | None) -> list | None:
    """Return the initial intake_qa for a Task created from a Suggestion.

    [] = grilling complete (skip the grill phase entirely).
    None = grill normally.
    """
    if category and category in PRE_GRILLED_SUGGESTION_CATEGORIES:
        return []
    return None


class FreeformConfig(Base):
    """Per-repo freeform mode configuration."""
    __tablename__ = "freeform_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repos.id"), nullable=False, unique=True)
    enabled = Column(Boolean, default=False)
    # Production branch — PR target when freeform is OFF (normal human-review
    # path) and the destination of `promote` operations. Defaults to the repo's
    # default_branch at config creation time.
    prod_branch = Column(String(128), default="main", nullable=False)
    # Dev/integration branch — freeform PRs target and auto-merge here. If it
    # doesn't exist on the remote at task time, the orchestrator creates it
    # from `prod_branch` before cloning.
    dev_branch = Column(String(128), default="dev")
    analysis_cron = Column(String(100), default="0 9 * * 1")  # weekly Monday 9am
    last_analysis_at = Column(DateTime(timezone=True), nullable=True)
    ux_knowledge = Column(Text, nullable=True)  # PO's accumulated understanding
    # Free-text goal that steers what the PO looks for. When set, prepended to
    # the PO prompt so suggestions are evaluated against this objective rather
    # than just "any improvement".
    po_goal = Column(Text, nullable=True)
    # When True, the orchestrator auto-approves PO-generated suggestions
    # for this repo (closing the continuous-improvement loop). Bounded by
    # the per-repo active task cap so the queue doesn't grow unboundedly.
    auto_approve_suggestions = Column(Boolean, default=False, nullable=False)
    auto_start_tasks = Column(Boolean, default=False, nullable=False)
    run_command = Column(Text, nullable=True)
    # Improvement Mode (formerly "Architecture Mode") — when True, the
    # improvement_agent cron runs the improve-codebase-architecture skill
    # against this repo and produces deepening-opportunity suggestions
    # analogous to PO suggestions. Column name kept for backwards
    # compatibility per ADR-015 §14.
    architecture_mode = Column(Boolean, default=False, nullable=False)
    architecture_cron = Column(String(100), default="0 9 * * 1", nullable=False)
    last_architecture_at = Column(DateTime(timezone=True), nullable=True)
    architecture_knowledge = Column(Text, nullable=True)  # Agent's accumulated depth map
    last_market_research_at = Column(DateTime(timezone=True), nullable=True)
    market_brief_max_age_days = Column(Integer, default=7, nullable=False)
    organization_id = Column(
        Integer, ForeignKey("organizations.id"), nullable=False, index=True,
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    repo = relationship("Repo")


class VerifyAttempt(Base):
    __tablename__ = "verify_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "cycle", name="ix_verify_attempts_task_cycle"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    cycle = Column(Integer, nullable=False)  # 1 or 2
    status = Column(String(16), nullable=False)  # pass / fail / error
    boot_check = Column(String(16), nullable=True)  # pass / fail / skipped
    intent_check = Column(String(16), nullable=True)  # pass / fail
    intent_judgment = Column(Text, nullable=True)
    tool_calls = Column(JSONB, nullable=True)
    failure_reason = Column(Text, nullable=True)
    log_tail = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    task = relationship("Task", back_populates="verify_attempts")


class ReviewAttempt(Base):
    __tablename__ = "review_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "cycle", name="ix_review_attempts_task_cycle"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    cycle = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False)
    code_review_verdict = Column(Text, nullable=True)
    ui_check = Column(String(16), nullable=True)  # pass / fail / skipped
    ui_judgment = Column(Text, nullable=True)
    tool_calls = Column(JSONB, nullable=True)
    failure_reason = Column(Text, nullable=True)
    log_tail = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    task = relationship("Task", back_populates="review_attempts")
