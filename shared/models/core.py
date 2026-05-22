import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
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
    # ADR-018 — "build something new" freeform flow. Runs intent grill →
    # root ADR → per-domain ADRs → per-domain trios → final verification.
    SCAFFOLD = "scaffold"


class TaskStatus(str, enum.Enum):
    INTAKE = "intake"
    CLASSIFYING = "classifying"
    QUEUED = "queued"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"  # ADR-015 §5 Phase 5 — complex-flow plan gate
    AWAITING_CLARIFICATION = "awaiting_clarification"
    CODING = "coding"
    VERIFYING = "verifying"          # freeform self-verification — runs after CODING, before PR_CREATED
    PR_CREATED = "pr_created"
    PR_REVIEW = "pr_review"          # ADR-015 §5 — self-PR-review gate (Phase 4: simple flow)
    ADDRESSING_COMMENTS = "addressing_comments"  # ADR-015 §5 Phase 5 — one round of self-fixups
    AWAITING_CI = "awaiting_ci"
    AWAITING_REVIEW = "awaiting_review"
    DONE = "done"
    BLOCKED_ON_AUTH = "blocked_on_auth"
    BLOCKED_ON_QUOTA = "blocked_on_quota"
    BLOCKED = "blocked"
    FAILED = "failed"
    TRIO_EXECUTING = "trio_executing"
    TRIO_REVIEW    = "trio_review"
    # ADR-015 §2 / Phase 6 — complex_large design-doc gate + backlog emit.
    ARCHITECT_DESIGNING       = "architect_designing"
    AWAITING_DESIGN_APPROVAL  = "awaiting_design_approval"
    ARCHITECT_BACKLOG_EMIT    = "architect_backlog_emit"
    # ADR-015 §4 / Phase 7 — final review + architect gap-fix loop.
    FINAL_REVIEW              = "final_review"
    ARCHITECT_GAP_FIX         = "architect_gap_fix"
    # ADR-015 §9 / Phase 8 — parent architect spawned sub-architects; the
    # parent's main session is paused while they run serially. Only resumed
    # briefly for the parent-answers-grill relay (§10).
    AWAITING_SUB_ARCHITECTS   = "awaiting_sub_architects"
    ITERATING = "iterating"  # ADR-017: trio is re-iterating a PR on user feedback
    # ADR-018 — scaffold parent state machine (freeform build-something-new flow).
    AWAITING_INTENT_GRILL        = "awaiting_intent_grill"
    BUILDING_ROOT_ADR            = "building_root_adr"
    AWAITING_ROOT_ADR_APPROVAL   = "awaiting_root_adr_approval"
    BUILDING_DOMAIN_ADRS         = "building_domain_adrs"
    # ADR-018 Stage 8 — per-domain grill round between BUILDING_DOMAIN_ADRS
    # and the domain architect's ADR write. Parked here while waiting for
    # the user to answer the domain-grill agent's pending question.
    AWAITING_DOMAIN_GRILL        = "awaiting_domain_grill"
    AWAITING_DOMAIN_ADR_APPROVAL = "awaiting_domain_adr_approval"
    # ADR-019 T7 — gate between Phase C (domain ADR approval) and Phase D
    # (child trio dispatch). Parent parks here until every architect-required
    # secret has a populated value_enc in repo_secrets.
    AWAITING_REQUIRED_SECRETS    = "awaiting_required_secrets"
    DISPATCHING_DOMAIN_BUILDS    = "dispatching_domain_builds"
    BUILDING_DOMAINS             = "building_domains"
    AWAITING_FINAL_VERIFICATION  = "awaiting_final_verification"


class TrioPhase(str, enum.Enum):
    ARCHITECTING         = "architecting"
    AWAITING_BUILDER     = "awaiting_builder"
    ARCHITECT_CHECKPOINT = "architect_checkpoint"
    ARCHITECT_ITERATING  = "architect_iterating"  # ADR-017


class TaskSource(str, enum.Enum):
    SLACK = "slack"
    LINEAR = "linear"
    TELEGRAM = "telegram"
    MANUAL = "manual"
    FREEFORM = "freeform"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Organization(Base):
    """A tenant. Every customer-facing row belongs to exactly one org.

    Created on signup (one personal org per user) or via the admin path.
    Membership is many-to-many through ``OrganizationMembership``.
    """

    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)

    plan = relationship("Plan", lazy="joined")


class Plan(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), nullable=False, unique=True)
    max_concurrent_tasks = Column(Integer, nullable=False)
    max_tasks_per_day = Column(Integer, nullable=False)
    max_input_tokens_per_day = Column(BigInteger, nullable=False)
    max_output_tokens_per_day = Column(BigInteger, nullable=False)
    max_members = Column(Integer, nullable=False)
    monthly_price_cents = Column(Integer, nullable=False, default=0)


class OrganizationMembership(Base):
    """User<->org join with a role.

    ``role`` is one of ``owner`` (exactly one per org, the creator),
    ``admin`` (manage members + integrations), or ``member`` (everyday
    use). ``last_active_at`` is bumped on login + org-switch and is used
    to resolve the user's active org on a fresh session.
    """

    __tablename__ = "organization_memberships"

    org_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    role = Column(String(32), nullable=False, default="member")
    last_active_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class Repo(Base):
    __tablename__ = "repos"
    # Uniqueness moves from global Repo.name to (organization_id, name) so
    # two orgs can each have a repo called "backend" without colliding.
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="ix_repos_org_name"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    url = Column(String(512), nullable=False)
    default_branch = Column(String(128), default="main")
    summary = Column(Text, nullable=True)  # Cached repo summary for context injection
    summary_updated_at = Column(DateTime(timezone=True), nullable=True)
    ci_checks = Column(Text, nullable=True)  # Extracted CI check commands from workflow files
    harness_onboarded = Column(Boolean, default=False)  # Whether harness engineering PR has been raised
    harness_pr_url = Column(String(512), nullable=True)  # URL of the harness onboarding PR
    # Product Owner context — repo-scoped, free-text markdown.
    # Describes the product mission, requirements, non-goals. Injected
    # into every PO prompt where the PO is asked to make a
    # product-shaped decision (today: architect clarification answers).
    product_brief = Column(Text, nullable=True)
    # ADR-015 §7 — per-repo default mode. Read by ``mode_resolver`` at
    # every gate, optionally overridden per task via ``Task.mode_override``.
    # Domain: "freeform" | "human_in_loop". Default kept conservative so
    # a repo created before the column existed routes through the
    # human-in-loop path.
    mode = Column(
        String(32), nullable=False, default="human_in_loop",
        server_default="human_in_loop",
    )
    organization_id = Column(
        Integer, ForeignKey("organizations.id"), nullable=False, index=True,
    )
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
    # ADR-015 §7 — per-task override of ``Repo.mode``. Bidirectional:
    # a freeform repo can flip a single task to human_in_loop and vice
    # versa. ``None`` means inherit from the repo. Domain:
    # "freeform" | "human_in_loop" | None.
    mode_override = Column(String(32), nullable=True)
    # Queue priority — lower number = picked up first. Default 100 (normal).
    # Set to 0 to jump to front. Freeform PO tasks default to 100.
    priority = Column(Integer, default=100, nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])
    subtasks = Column(JSONB, nullable=True)  # [{title, status, output_preview}]
    current_subtask = Column(Integer, nullable=True)  # 0-indexed, null = not started
    # Grill-before-planning Q&A — list of {question, answer} pairs accumulated
    # across AWAITING_CLARIFICATION ↔ PLANNING round-trips before the agent
    # writes a plan. NULL = not yet started; [] = grilling complete or skipped.
    intake_qa = Column(JSONB, nullable=True)
    affected_routes = Column(JSONB, nullable=False, server_default="[]")
    organization_id = Column(
        Integer, ForeignKey("organizations.id"), nullable=False, index=True,
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    # Trio (architect/builder/reviewer) columns
    parent_task_id = Column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    trio_phase = Column(SAEnum(TrioPhase, name="triophase"), nullable=True)
    trio_backlog = Column(JSONB, nullable=True)
    consulting_architect = Column(Boolean, nullable=False, default=False, server_default="false")
    # ADR-015 Phase 7.7 — the integration branch name (``auto-agent/<slug>-<id>``
    # for new tasks). NULL for in-flight tasks created before the rename;
    # those tasks fall back to the legacy ``trio/<id>`` shape via
    # ``agent.lifecycle.trio.workspace_resolver.resolve_integration_branch``.
    integration_branch = Column(String(255), nullable=True)

    repo = relationship("Repo", back_populates="tasks", lazy="selectin")
    history = relationship("TaskHistory", back_populates="task", order_by="TaskHistory.created_at")
    verify_attempts = relationship(
        "VerifyAttempt", back_populates="task", order_by="VerifyAttempt.cycle",
    )
    review_attempts = relationship(
        "ReviewAttempt", back_populates="task", order_by="ReviewAttempt.cycle",
    )


class TaskHistory(Base):
    __tablename__ = "task_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    from_status = Column(Enum(TaskStatus), nullable=True)
    to_status = Column(Enum(TaskStatus), nullable=False)
    message = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    task = relationship("Task", back_populates="history")


class GateDecision(Base):
    """One row per gate decision — ADR-015 §6.

    Persists the audit trail that the web-next gate-history panel reads.
    Both human-driven approvals (``source="user"`` via the
    ``POST /tasks/{id}/approve-plan`` endpoint) and freeform standin
    decisions (``source="po_standin" | "improvement_standin"`` via
    :mod:`agent.lifecycle.standin`) write a row here, so the panel can
    reconstruct "who decided what at every gate" without scraping the
    Redis event stream.

    The columns mirror the ``standin.decision`` event payload shape so
    the standin can write to both sinks (DB + event) with a single
    helper. ``agent_id`` is NULL for user decisions; ``cited_context``
    and ``fallback_reasons`` are JSONB arrays of strings (empty list when
    unused).
    """

    __tablename__ = "gate_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    # Domain: "grill" | "plan_approval" | "design_approval" | "pr_review".
    # Kept as a free-form String so a future gate can land without a
    # migration; consumers should treat unknown values as opaque.
    gate = Column(String(64), nullable=False)
    # Domain: "user" | "po_standin" | "improvement_standin".
    source = Column(String(64), nullable=False)
    # NULL for source=user. For standins this is ``"{kind}:{repo_id}"``.
    agent_id = Column(String(128), nullable=True)
    # Domain at the gate level — for plan/design it's "approved" | "rejected";
    # for pr_review the standin emits "passed" | "gaps_found"; for grill
    # it's the answer text. Kept as Text so any gate's verdict shape fits.
    verdict = Column(Text, nullable=False, default="")
    comments = Column(Text, nullable=False, default="")
    cited_context = Column(JSONB, nullable=False, server_default="[]")
    fallback_reasons = Column(JSONB, nullable=False, server_default="[]")
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


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


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    org_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    kind = Column(String(32), nullable=False)
    model = Column(String(64), nullable=True)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cost_cents = Column(Numeric(10, 4), nullable=False, default=0)
    occurred_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class TaskOutcome(Base):
    """Tracks PR outcomes for the learning/feedback loop."""
    __tablename__ = "task_outcomes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, unique=True,
    )
    pr_approved = Column(Boolean, nullable=True)  # True=merged, False=closed/rejected
    review_rounds = Column(Integer, default=0)  # How many review iterations
    time_to_complete_seconds = Column(Float, nullable=True)  # Total wall time
    tokens_used = Column(Integer, nullable=True)  # Estimated token usage
    feedback_summary = Column(Text, default="")  # Summary of review feedback patterns
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    task = relationship("Task")


class MessengerConversation(Base):
    """Durable per-(user, source, focus) chat history for messenger DMs."""
    __tablename__ = "messenger_conversations"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source", "focus_kind", "focus_id",
            name="uq_msgconv_user_source_focus",
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    source = Column(String(32), nullable=False)            # 'slack' | 'telegram' | ...
    focus_kind = Column(String(32), nullable=False)        # 'draft' | 'task' (v1)
    focus_id = Column(BigInteger, nullable=True)           # NULL for 'draft'; task.id for 'task'
    messages_json = Column(JSONB, nullable=False, default=list)
    last_active_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class UserFocus(Base):
    """Per-user 'what am I working on right now' pointer with 24h TTL.

    Not keyed on source — switching focus on Slack also takes effect on
    Telegram (and any future messenger).
    """
    __tablename__ = "user_focus"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    focus_kind = Column(String(32), nullable=False)        # 'draft' | 'task' | 'none'
    focus_id = Column(BigInteger, nullable=True)
    set_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)


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
    organization_id = Column(
        Integer, ForeignKey("organizations.id"), nullable=False, index=True,
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class User(Base):
    """Authenticated team member."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    last_login = Column(DateTime(timezone=True), nullable=True)
    claude_auth_status = Column(
        String(32), nullable=False, default="never_paired"
    )
    claude_paired_at = Column(DateTime(timezone=True), nullable=True)
    # Per-user messaging-platform identifiers. When set, notifications about
    # tasks owned by this user are routed here instead of fanning out to a
    # single global admin chat. NULL means "this user hasn't linked the
    # platform yet".
    telegram_chat_id = Column(String(64), nullable=True, unique=True)
    slack_user_id = Column(String(64), nullable=True, unique=True)
    # Self-serve signup with email verification (Phase 1 multi-tenant).
    # NULL email = legacy admin/seeded user (no verification required).
    email = Column(String(255), nullable=True, unique=True)
    email_verified_at = Column(DateTime(timezone=True), nullable=True)
    signup_token = Column(String(64), nullable=True, unique=True)
    # Backfilled to the default org in migration 026. After 027 this is
    # NOT NULL. Note: per-user multi-org membership lives on
    # ``organization_memberships``; this column records the user's
    # "home" org for backwards-compatible queries that haven't been
    # migrated to the membership table.
    organization_id = Column(
        Integer, ForeignKey("organizations.id"), nullable=False, index=True,
    )


class UserSecret(Base):
    """Per-user, per-org encrypted secret. Read/written via shared/secrets.py
    — never instantiate directly. ``value_enc`` is pgcrypto-encrypted
    ciphertext.

    Migration 026 extended the primary key from ``(user_id, key)`` to
    ``(user_id, organization_id, key)`` so a user in two orgs can carry
    different credentials per org without collision.
    """
    __tablename__ = "user_secrets"

    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True, nullable=False,
    )
    organization_id = Column(
        Integer, ForeignKey("organizations.id"),
        primary_key=True, nullable=False, index=True,
    )
    key = Column(String(64), primary_key=True, nullable=False)
    value_enc = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )


class SlackInstallation(Base):
    """A customer org's Slack workspace install — 1:1 with organizations."""

    __tablename__ = "slack_installations"

    org_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True, nullable=False,
    )
    team_id = Column(String(32), nullable=False, unique=True)
    team_name = Column(String(255), nullable=True)
    bot_token_enc = Column(LargeBinary, nullable=False)
    bot_user_id = Column(String(32), nullable=False)
    app_token_enc = Column(LargeBinary, nullable=True)
    installed_by_slack_user_id = Column(String(32), nullable=True)
    installed_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class GitHubInstallation(Base):
    """A customer org's GitHub App install — 1:1 with organizations."""

    __tablename__ = "github_installations"

    org_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True, nullable=False,
    )
    installation_id = Column(BigInteger, nullable=False, unique=True)
    account_login = Column(String(128), nullable=False)
    account_type = Column(String(32), nullable=False)
    installed_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class WebhookSecret(Base):
    """Per-org override for inbound webhook HMAC verification."""

    __tablename__ = "webhook_secrets"

    org_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True, nullable=False,
    )
    source = Column(String(32), primary_key=True, nullable=False)
    secret_enc = Column(LargeBinary, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SearchSession(Base):
    """A multi-turn search/research conversation owned by a user."""
    __tablename__ = "search_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id"), nullable=False, index=True,
    )
    title = Column(String(512), nullable=False, default="New search")
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    user = relationship("User")


class SearchMessage(Base):
    """A single turn in a SearchSession.

    For role='user': content is the raw user text, tool_events is empty.
    For role='assistant': content is the final markdown answer; tool_events
    is a list of {tool, args, result_summary, ts} captured during the turn,
    plus 'sources' and 'memory_hits' arrays.
    """
    __tablename__ = "search_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer, ForeignKey("search_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role = Column(String(16), nullable=False)  # "user" | "assistant"
    content = Column(Text, nullable=False, default="")
    tool_events = Column(JSONB, nullable=False, default=list)
    truncated = Column(Boolean, nullable=False, default=False)
    # Token usage for assistant turns (zero for user rows). Populated from the
    # AgentResult.tokens_used returned by the search agent loop.
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    session = relationship("SearchSession")


# --- Code graph (ADR-016) ----------------------------------------------------
#
# Two tables: per-repo settings (`RepoGraphConfig`, one row per opted-in repo)
# and per-analysis output (`RepoGraph`, one row per completed analysis).
# Phase 1 of ADR-016 creates the schema and populates `RepoGraphConfig`; the
# analyser that writes `RepoGraph` rows lands in Phase 2.


class RepoGraphConfig(Base):
    """Per-repo opt-in settings for the code-graph feature (ADR-016 §8).

    One row per repo that has graph analysis enabled. The repo_id PK keeps
    the 1:1 relationship explicit — disabling the feature deletes the row.
    """

    __tablename__ = "repo_graph_configs"

    repo_id = Column(
        Integer,
        ForeignKey("repos.id", ondelete="CASCADE"),
        primary_key=True,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The branch that gets analysed. Defaults to the repo's default_branch at
    # enable time; user can change it via PATCH /api/repos/{id}/graph.
    analysis_branch = Column(String(255), nullable=False)
    # Version string of the analyser that produced ``last_analysis_id``. Empty
    # string until the first successful analysis (Phase 2). Kept here rather
    # than on RepoGraph so the freshness banner can be rendered without
    # joining the (potentially-large) output row.
    analyser_version = Column(String(64), nullable=False, server_default="", default="")
    # Resolved on-disk workspace path (under GRAPH_WORKSPACES_DIR). Stored so
    # operators can find the checkout without recomputing the layout.
    workspace_path = Column(String(1024), nullable=False)
    # FK to the most recent successful RepoGraph row; NULL until Phase 2 runs.
    last_analysis_id = Column(
        Integer,
        ForeignKey("repo_graphs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )


class RepoGraph(Base):
    """One row per completed graph analysis (ADR-016 §8).

    Phase 1 only declares the schema; rows are written by the analyser in
    Phase 2. `graph_json` carries the full nested-node + edge blob.
    """

    __tablename__ = "repo_graphs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(
        Integer,
        ForeignKey("repos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    commit_sha = Column(String(64), nullable=False)
    generated_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    analyser_version = Column(String(64), nullable=False)
    # 'ok' / 'partial' — surface failures-isolated-per-area state in the UI.
    status = Column(String(16), nullable=False, default="ok", server_default="ok")
    graph_json = Column(JSONB, nullable=False)
    # ADR-016 checkpoint/resume columns (migration 048_repo_graph_checkpoint).
    # is_complete flips true once a full pipeline run finishes; processed_files
    # / failed_sites carry per-file resume state.
    is_complete = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    processed_files = Column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    failed_sites = Column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    # Capability/flow derivation (Phase 1 of capability-flow map spec).
    # Nullable: a freshly-completed analysis has graph_json but no
    # flow_json until the recompute endpoint is hit.
    flow_json = Column(JSONB, nullable=True)


# --- Per-repo project secrets vault (ADR-019) ---------------------------------
#
# `RepoSecret` stores project credentials per repo. Unlike `UserSecret` (which
# is per-user and has a closed key allowlist), this table accepts any uppercase
# env-var-style key and supports two sources: 'user' (typed by the user) and
# 'architect_required' (declared by the domain architect as a project need).
# value_enc is nullable so architect-declared rows can exist as placeholders
# before the user populates them.
#
# Read/written via shared/repo_secrets.py — never instantiate directly.


class RepoSecret(Base):
    """Per-repo, per-org encrypted project secret. Read/written via
    shared/repo_secrets.py — never instantiate directly. ``value_enc`` is
    pgcrypto-encrypted ciphertext; nullable so architect-required rows can
    exist before the user populates them.

    source = 'user' | 'architect_required'
    purpose is populated when source = 'architect_required' to explain to the
    user why the key is needed.
    """

    __tablename__ = "repo_secrets"
    __table_args__ = (
        UniqueConstraint("repo_id", "key", name="uq_repo_secrets_repo_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(
        Integer, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    key = Column(String(255), nullable=False)
    value_enc = Column(LargeBinary, nullable=True)
    source = Column(String(32), nullable=False, default="user", server_default="user")
    purpose = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )
