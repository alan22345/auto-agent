import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class TaskComplexity(str, enum.Enum):
    SIMPLE = "simple"
    COMPLEX = "complex"
    COMPLEX_LARGE = "complex_large"
    # Query/research tasks — no repo needed, no coding tools, just an LLM answer.
    SIMPLE_NO_CODE = "simple_no_code"


class TaskStatus(str, enum.Enum):
    INTAKE = "intake"
    CLASSIFYING = "classifying"
    QUEUED = "queued"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    CODING = "coding"
    PR_CREATED = "pr_created"
    AWAITING_CI = "awaiting_ci"
    AWAITING_REVIEW = "awaiting_review"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"


class TaskSource(str, enum.Enum):
    SLACK = "slack"
    LINEAR = "linear"
    TELEGRAM = "telegram"
    MANUAL = "manual"
    FREEFORM = "freeform"


class SuggestionStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Repo(Base):
    __tablename__ = "repos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    url = Column(String(512), nullable=False)
    default_branch = Column(String(128), default="main")
    summary = Column(Text, nullable=True)  # Cached repo summary for context injection
    summary_updated_at = Column(DateTime(timezone=True), nullable=True)
    ci_checks = Column(Text, nullable=True)  # Extracted CI check commands from workflow files
    harness_onboarded = Column(Boolean, default=False)  # Whether harness engineering PR has been raised
    harness_pr_url = Column(String(512), nullable=True)  # URL of the harness onboarding PR
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    tasks = relationship("Task", back_populates="repo")


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(512), nullable=False)
    description = Column(Text, default="")
    source = Column(Enum(TaskSource), nullable=False)
    source_id = Column(String(255), default="")  # Slack ts, Linear issue ID, etc.
    status = Column(Enum(TaskStatus), default=TaskStatus.INTAKE, nullable=False)
    complexity = Column(Enum(TaskComplexity), nullable=True)
    repo_id = Column(Integer, ForeignKey("repos.id"), nullable=True)
    branch_name = Column(String(255), nullable=True)
    pr_url = Column(String(512), nullable=True)
    plan = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    freeform_mode = Column(Boolean, default=False)
    # Queue priority — lower number = picked up first. Default 100 (normal).
    # Set to 0 to jump to front. Freeform PO tasks default to 100.
    priority = Column(Integer, default=100, nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])
    subtasks = Column(JSONB, nullable=True)  # [{title, status, output_preview}]
    current_subtask = Column(Integer, nullable=True)  # 0-indexed, null = not started
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    repo = relationship("Repo", back_populates="tasks", lazy="selectin")
    history = relationship("TaskHistory", back_populates="task", order_by="TaskHistory.created_at")


class TaskHistory(Base):
    __tablename__ = "task_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    from_status = Column(Enum(TaskStatus), nullable=True)
    to_status = Column(Enum(TaskStatus), nullable=False)
    message = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    task = relationship("Task", back_populates="history")


class TaskMessage(Base):
    """User-posted feedback on a running task. The agent reads unread
    messages between turns and injects them into the conversation."""
    __tablename__ = "task_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    sender = Column(String(128), nullable=False)  # user display_name, or "telegram:<chat_id>"
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    read_by_agent_at = Column(DateTime(timezone=True), nullable=True)


class TaskOutcome(Base):
    """Tracks PR outcomes for the learning/feedback loop."""
    __tablename__ = "task_outcomes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, unique=True)
    pr_approved = Column(Boolean, nullable=True)  # True=merged, False=closed/rejected
    review_rounds = Column(Integer, default=0)  # How many review iterations
    time_to_complete_seconds = Column(Float, nullable=True)  # Total wall time
    tokens_used = Column(Integer, nullable=True)  # Estimated token usage
    feedback_summary = Column(Text, default="")  # Summary of review feedback patterns
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    task = relationship("Task")


class ScheduledTask(Base):
    """Recurring tasks triggered on a cron schedule."""
    __tablename__ = "scheduled_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    cron_expression = Column(String(100), nullable=False)  # e.g. "0 9 * * 1" (Monday 9am)
    task_title = Column(String(512), nullable=False)
    task_description = Column(Text, default="")
    repo_name = Column(String(255), nullable=True)
    enabled = Column(Boolean, default=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class Suggestion(Base):
    """PO-generated improvement suggestions for a repo."""
    __tablename__ = "suggestions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repos.id"), nullable=False)
    title = Column(String(512), nullable=False)
    description = Column(Text, default="")
    rationale = Column(Text, default="")
    category = Column(String(100), default="")  # ux_gap, feature, improvement
    priority = Column(Integer, default=3)  # 1=critical, 5=nice-to-have
    status = Column(Enum(SuggestionStatus), default=SuggestionStatus.PENDING)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    repo = relationship("Repo")
    task = relationship("Task")


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
    # When True, the orchestrator auto-approves PO-generated suggestions
    # for this repo (closing the continuous-improvement loop). Bounded by
    # the per-repo active task cap so the queue doesn't grow unboundedly.
    auto_approve_suggestions = Column(Boolean, default=False, nullable=False)
    auto_start_tasks = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    repo = relationship("Repo")


class User(Base):
    """Authenticated team member."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    last_login = Column(DateTime(timezone=True), nullable=True)


