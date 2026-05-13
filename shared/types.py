"""Shared Pydantic types used across services for type-safe data exchange.

These models are the canonical types for inter-service communication.
All API responses and parsed external data should go through these.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

# --- Classifier types ---


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ClassificationResult(BaseModel):
    classification: Literal["simple", "complex", "simple_no_code"]
    reasoning: str = ""
    estimated_files: int = 0
    risk: RiskLevel = RiskLevel.LOW


# --- Task API types (used by all services that talk to the orchestrator) ---


class TaskData(BaseModel):
    """Typed representation of a task from the orchestrator API."""
    id: int
    title: str
    description: str
    source: str
    status: str
    complexity: str | None = None
    repo_name: str | None = None
    branch_name: str | None = None
    pr_url: str | None = None
    plan: str | None = None
    error: str | None = None
    freeform_mode: bool = False
    priority: int = 100
    subtasks: list[dict] | None = None
    current_subtask: int | None = None
    # Grill-before-planning Q&A — list of {question, answer} pairs accumulated
    # across AWAITING_CLARIFICATION ↔ PLANNING round-trips. None = grilling
    # not started; [] = grilling complete or skipped.
    intake_qa: list[dict] | None = None
    created_at: str | None = None
    created_by_user_id: int | None = None
    # Phase 2 — tenant id. Optional in the wire schema until migration 027
    # flips the DB column to NOT NULL, so legacy rows still serialize cleanly.
    organization_id: int | None = None
    # Structured intent (extracted by LLM after classification)
    change_type: str | None = None          # "bugfix", "feature", "refactor", "config", "docs"
    target_areas: str | None = None         # comma-separated file paths or module areas
    acceptance_criteria: str | None = None   # what "done" looks like
    constraints: str | None = None          # what NOT to do
    # Trio (architect/builder/reviewer) — parent points to the trio parent
    # task when this is a child work item; otherwise None.
    parent_task_id: int | None = None


class TaskMessageData(BaseModel):
    """A user-posted feedback message on a task."""
    id: int
    task_id: int
    sender: str
    content: str
    created_at: str | None = None


class TaskMessagePost(BaseModel):
    """Inbound body for POST /api/tasks/{id}/messages."""
    content: str


class RepoData(BaseModel):
    """Typed representation of a repo from the orchestrator API."""
    id: int
    name: str
    url: str
    default_branch: str = "main"
    summary: str | None = None
    summary_updated_at: str | None = None
    ci_checks: str | None = None
    harness_onboarded: bool = False
    harness_pr_url: str | None = None


# --- GitHub types ---


class PRReviewComment(BaseModel):
    """A review comment from a GitHub PR."""
    author: str
    body: str
    type: Literal["review", "inline"]
    path: str = ""
    line: int | None = None


class CIStatus(BaseModel):
    """CI status for a commit."""
    sha: str
    state: Literal["success", "failure", "pending", "error"]
    message: str = ""


# --- Metrics types ---


class PROutcomeMetrics(BaseModel):
    total: int = 0
    approved: int = 0
    rejected: int = 0
    approval_rate_pct: float = 0.0
    avg_review_rounds: float = 0.0
    avg_completion_seconds: float | None = None


class MetricsResponse(BaseModel):
    period_days: int
    total_tasks: int
    active_tasks: int
    success_rate_pct: float
    by_status: dict[str, int]
    by_complexity: dict[str, int]
    by_source: dict[str, int]
    avg_duration_hours: float | None
    pr_outcomes: PROutcomeMetrics


class TimelineEntry(BaseModel):
    from_status: str | None = Field(None, alias="from")
    to_status: str = Field(alias="to")
    message: str = ""
    timestamp: str | None = None

    model_config = {"populate_by_name": True}


class TaskMetricsResponse(BaseModel):
    task_id: int
    timeline: list[TimelineEntry]
    time_in_status_seconds: dict[str, float]


# --- Feedback types ---


class FeedbackSummary(BaseModel):
    total_outcomes: int = 0
    approved: int = 0
    rejected: int = 0
    approval_rate: float = 0.0
    avg_review_rounds: float = 0.0


class OutcomeResponse(BaseModel):
    task_id: int
    pr_approved: bool
    review_rounds: int


# --- Schedule types ---


class ScheduleResponse(BaseModel):
    id: int
    name: str
    cron: str
    task_title: str
    enabled: bool
    last_run_at: str | None = None


class RepoResponse(BaseModel):
    id: int
    name: str
    url: str


# --- Linear types ---


# --- Freeform / Suggestion types ---


class SuggestionData(BaseModel):
    """Typed representation of a PO suggestion."""
    id: int
    repo_id: int | None = None
    repo_name: str | None = None
    title: str
    description: str = ""
    rationale: str = ""
    category: str = ""
    priority: int = 3
    status: str = "pending"
    task_id: int | None = None
    created_at: str | None = None
    evidence_urls: list[dict] = Field(default_factory=list)


class MarketBriefResponse(BaseModel):
    """Response shape for GET /api/repos/{repo_id}/market-brief/latest."""

    id: int
    repo_id: int
    created_at: str
    product_category: str | None = None
    competitors: list[dict] = Field(default_factory=list)
    findings: list[dict] = Field(default_factory=list)
    modality_gaps: list[dict] = Field(default_factory=list)
    strategic_themes: list[dict] = Field(default_factory=list)
    summary: str = ""
    partial: bool = False


class FreeformConfigData(BaseModel):
    """Typed representation of a freeform mode config."""
    id: int
    repo_name: str | None = None
    enabled: bool = False
    prod_branch: str = "main"
    dev_branch: str = "dev"
    analysis_cron: str = "0 9 * * 1"
    auto_approve_suggestions: bool = False
    auto_start_tasks: bool = False
    po_goal: str | None = None
    last_analysis_at: str | None = None
    # Architecture Mode — periodic improve-codebase-architecture cron.
    architecture_mode: bool = False
    architecture_cron: str = "0 9 * * 1"
    last_architecture_at: str | None = None
    architecture_knowledge: str | None = None
    run_command: str | None = None
    created_at: str | None = None


# --- Linear types ---


class LinearIssue(BaseModel):
    """A Linear issue returned from the GraphQL API."""
    id: str
    identifier: str
    title: str
    description: str = ""
    state: dict[str, str] = Field(default_factory=dict)
    url: str = ""


# --- Auth types ---


class UserData(BaseModel):
    """Typed representation of a user."""
    id: int
    username: str
    display_name: str
    created_at: str | None = None
    last_login: str | None = None
    claude_auth_status: str = "never_paired"
    claude_paired_at: str | None = None
    telegram_chat_id: str | None = None
    slack_user_id: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user: UserData


class CreateUserRequest(BaseModel):
    username: str
    password: str
    display_name: str


# --- Self-serve signup (Phase 1 multi-tenant) ---


class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=200)
    display_name: str = Field(min_length=1, max_length=255)


class SignupResponse(BaseModel):
    """Response for POST /api/auth/signup. Always returns 201 with the new
    user's id; the client should display "check your email" — never assume
    the email was actually delivered."""
    user_id: int
    email: str
    verification_sent: bool


class ChangeEmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)


# --- Per-user secrets API ---


class SecretListResponse(BaseModel):
    """Names only — values never leave the server."""
    keys: list[str]


class SecretPutRequest(BaseModel):
    """``value=None`` clears the secret (equivalent to DELETE)."""
    value: str | None = None


class SecretTestResponse(BaseModel):
    ok: bool
    detail: str = ""


# --- Usage / quota types ---


class PlanRead(BaseModel):
    id: int
    name: str
    max_concurrent_tasks: int
    max_tasks_per_day: int
    max_input_tokens_per_day: int
    max_output_tokens_per_day: int

    model_config = {"from_attributes": True}


class UsageSummary(BaseModel):
    plan: PlanRead
    active_tasks: int
    tasks_today: int
    input_tokens_today: int
    output_tokens_today: int


# --- Memory tab types ---

KindLiteral = Literal["decision", "architecture", "gotcha", "status", "preference", "fact"]
EntityStatus = Literal["new", "exists"]
Resolution = Literal["keep_existing", "replace", "keep_both"]


class ConflictInfo(BaseModel):
    fact_id: str
    existing_content: str


class ProposedFact(BaseModel):
    row_id: str
    entity: str
    entity_type: str = "concept"
    entity_status: EntityStatus = "new"
    entity_match_score: float | None = None
    kind: KindLiteral = "fact"
    content: str
    conflicts: list[ConflictInfo] = Field(default_factory=list)
    resolution: Resolution | None = None


class MemorySaveResult(BaseModel):
    row_id: str
    ok: bool
    error: str | None = None
    fact_id: str | None = None


# --- Memory browser (read-side) types ---


class MemoryEntitySummary(BaseModel):
    """Lightweight entity card for search results / recent list."""
    id: str
    name: str
    type: str
    tags: list[str] = Field(default_factory=list)
    fact_count: int = 0
    latest_fact_at: str | None = None


class MemoryFact(BaseModel):
    """A fact row as seen in the browser detail view."""
    id: str
    content: str
    kind: str
    source: str | None = None
    author: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None


class MemoryEntityDetail(BaseModel):
    entity: MemoryEntitySummary
    facts: list[MemoryFact] = Field(default_factory=list)


# --- Freeform self-verification types ---


class AffectedRoute(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str
    label: str


class IntentVerdict(BaseModel):
    ok: bool
    reasoning: str
    tool_calls: list[dict] = Field(default_factory=list)


class ReviewDimensionVerdict(BaseModel):
    # verdict comes from LLM output; distinct from lowercase pass/fail statuses persisted on attempt rows
    verdict: Literal["OK", "NOT-OK", "SKIPPED"]
    reasoning: str


class ReviewCombinedVerdict(BaseModel):
    code_review: ReviewDimensionVerdict
    ui_check: ReviewDimensionVerdict


class VerifyAttemptOut(BaseModel):
    """API shape for a verify attempt row."""
    id: int
    cycle: int
    status: Literal["pass", "fail", "error"]
    boot_check: Literal["pass", "fail", "skipped"] | None = None
    intent_check: Literal["pass", "fail"] | None = None
    intent_judgment: str | None = None
    tool_calls: list[dict] | None = None
    failure_reason: str | None = None
    log_tail: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class ReviewAttemptOut(BaseModel):
    """API shape for a review attempt row."""
    id: int
    cycle: int
    status: Literal["pass", "fail", "error"]
    code_review_verdict: str | None = None
    ui_check: Literal["pass", "fail", "skipped"] | None = None
    ui_judgment: str | None = None
    tool_calls: list[dict] | None = None
    failure_reason: str | None = None
    log_tail: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


# --- Architect/Builder/Reviewer trio types ---


class WorkItem(BaseModel):
    """One backlog item the architect dispatches to a builder child task."""
    id: str
    title: str
    description: str
    status: Literal["pending", "in_progress", "done", "skipped"] = "pending"
    assigned_task_id: int | None = None
    discovered_in_attempt_id: int | None = None


class TrioPhaseLiteral(BaseModel):
    """Pydantic wrapper for the trio_phase enum used in API responses."""
    phase: Literal["architecting", "awaiting_builder", "architect_checkpoint"] | None


class RepairContext(BaseModel):
    """Passed to architect.checkpoint on parent re-entry after integration PR CI failure."""
    ci_log: str
    failed_pr_url: str


class ArchitectDecision(BaseModel):
    """The decision field on an ArchitectAttempt row when phase=checkpoint."""
    action: Literal["continue", "revise", "done", "awaiting_clarification", "blocked"]
    reason: str | None = None
    question: str | None = None  # only when action=awaiting_clarification


class ArchitectAttemptOut(BaseModel):
    """API shape for an architect_attempts row."""
    id: int
    task_id: int
    phase: Literal["initial", "consult", "checkpoint", "revision"]
    cycle: int
    reasoning: str
    decision: dict | None = None
    consult_question: str | None = None
    consult_why: str | None = None
    architecture_md_after: str | None = None
    commit_sha: str | None = None
    tool_calls: list[dict]
    created_at: datetime


class TrioReviewAttemptOut(BaseModel):
    """API shape for a trio_review_attempts row."""
    id: int
    task_id: int
    cycle: int
    ok: bool
    feedback: str
    tool_calls: list[dict]
    created_at: datetime
