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


# --- Code graph (ADR-016) ---


class RepoGraphConfigData(BaseModel):
    """Per-repo code-graph settings (ADR-016 §8).

    Phase 1: ``last_analysis_id`` is always ``None`` and ``analyser_version``
    is the empty string — both are populated by the Phase 2 analyser.
    """

    repo_id: int
    repo_name: str
    repo_url: str
    analysis_branch: str
    analyser_version: str = ""
    workspace_path: str
    last_analysis_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


class EnableRepoGraphRequest(BaseModel):
    """Optional body for ``POST /api/repos/{repo_id}/graph``.

    All fields optional — the endpoint defaults the analysis branch to the
    repo's ``default_branch`` if the caller omits it.
    """

    analysis_branch: str | None = Field(default=None, max_length=255)


class UpdateRepoGraphRequest(BaseModel):
    """Body for ``PATCH /api/repos/{repo_id}/graph``."""

    analysis_branch: str = Field(min_length=1, max_length=255)


# --- Code graph blob schema (ADR-016 Phase 2) --------------------------------
#
# This is the **locked** wire schema for a single graph analysis result.
# Phase 2 ships AST-derived nodes and edges; Phase 3 will add ``source_kind:
# "llm"`` edges to the same shape without changing the schema. Adding fields
# is allowed; removing or renaming requires a coordinated UI + analyser bump.


class Node(BaseModel):
    """One node in the hierarchical compound graph (ADR-016 §2).

    ``id`` is the canonical Cytoscape id. ``parent`` points to the parent
    compound node (area → file → class → function nesting). ``area`` is
    duplicated on every node so query callers can filter without walking
    the parent chain.
    """

    id: str
    kind: Literal["area", "file", "class", "function"]
    label: str
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    area: str
    parent: str | None = None
    decorators: list[str] = Field(default_factory=list)
    """Raw decorator source for decorated Python defs/classes (e.g.
    ``["@router.get(\"/api/repos\")"]``). Captured by the parser; consumed
    by the Phase 4 HTTP-matching stage to find FastAPI/Flask route handlers.
    Always ``[]`` for non-Python nodes and for undecorated defs."""


class EdgeEvidence(BaseModel):
    """Cited proof of an edge's existence — see ADR-016 §3."""

    file: str
    line: int
    snippet: str


class Edge(BaseModel):
    """One edge in the graph.

    Phase 2 only emits edges with ``source_kind="ast"``. Phase 3 will add
    ``source_kind="llm"`` edges using the same fields — no schema change.
    ``boundary_violation`` is reserved for Phase 5 and is always ``False``
    in Phase 2 output.
    """

    source: str
    target: str
    kind: Literal["calls", "imports", "inherits", "http"]
    evidence: EdgeEvidence
    source_kind: Literal["ast", "llm"]
    boundary_violation: bool = False


class AreaStatus(BaseModel):
    """Per-area outcome (ADR-016 §10 — failures isolated per area)."""

    name: str
    status: Literal["ok", "partial", "failed"]
    error: str | None = None
    unresolved_dynamic_sites: int = 0


class RepoGraphBlob(BaseModel):
    """Full graph analysis output — the payload stored in
    ``RepoGraph.graph_json`` and surfaced to the UI / agent tool.
    """

    commit_sha: str
    generated_at: datetime
    analyser_version: str
    areas: list[AreaStatus]
    nodes: list[Node]
    edges: list[Edge]


class RepoGraphRefreshResponse(BaseModel):
    """``POST /api/repos/{id}/graph/refresh`` response body."""

    request_id: str
    status: Literal["accepted"] = "accepted"


class LatestRepoGraphData(BaseModel):
    """``GET /api/repos/{id}/graph/latest`` payload — the freshness banner
    + Cytoscape renderer consume this directly. ``blob`` is ``None`` when
    no analysis has completed yet.
    """

    repo_id: int
    analysis_branch: str
    repo_graph_id: int | None = None
    commit_sha: str | None = None
    generated_at: str | None = None
    analyser_version: str | None = None
    status: Literal["ok", "partial", "failed"] | None = None
    blob: RepoGraphBlob | None = None


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
