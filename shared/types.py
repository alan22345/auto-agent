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
    classification: Literal["simple", "complex", "complex_large", "simple_no_code", "scaffold"]
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
    # ADR-015 §7 Phase 12 — per-task mode override (``None`` ⇒ inherit
    # from the repo). The UI toggle on task intake sets this; the gate
    # history surfaces it on the audit panel.
    mode_override: Literal["freeform", "human_in_loop"] | None = None
    # Effective mode after resolving ``mode_override`` against the repo
    # default. Computed server-side so the UI doesn't need to know the
    # resolution rule. ``None`` when no repo is attached (legacy rows).
    effective_mode: Literal["freeform", "human_in_loop"] | None = None
    priority: int = 100
    # ``subtasks`` is a multi-purpose JSONB column. Trio parents store a list
    # of child-task descriptors here; SCAFFOLD parents (ADR-018) store a dict
    # of round counters / progress markers (e.g. ``{"scaffold":
    # {"current_domain_idx": 4, "final_verify_rounds": 1}}``). Wire schema
    # accepts both so a scaffold task with populated state still serialises.
    subtasks: list[dict] | dict | None = None
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
    change_type: str | None = None  # "bugfix", "feature", "refactor", "config", "docs"
    target_areas: str | None = None  # comma-separated file paths or module areas
    acceptance_criteria: str | None = None  # what "done" looks like
    constraints: str | None = None  # what NOT to do
    # Trio (architect/builder/reviewer) — parent points to the trio parent
    # task when this is a child work item; otherwise None.
    parent_task_id: int | None = None
    # Trio orchestration state — only populated for trio parent tasks.
    trio_phase: str | None = None
    trio_backlog: list[dict] | None = None


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
    product_brief: str | None = None
    # ADR-015 §7 Phase 12 — repo default mode. The web-next task-intake
    # toggle reads this to label the override ("force human review" vs
    # "run in freeform mode") so the UI mirrors the resolver's bias.
    mode: Literal["freeform", "human_in_loop"] = "human_in_loop"


# --- GitHub types ---


class PRReviewComment(BaseModel):
    """A review comment from a GitHub PR."""

    author: str
    body: str
    type: Literal["review", "inline"]
    path: str = ""
    line: int | None = None


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

    cyclomatic: int | None = Field(default=None, ge=0)
    """McCabe cyclomatic complexity for this node. Only populated on
    ``kind="function"`` nodes (including methods, which are function-kind);
    ``None`` for area, file, and class nodes. Populated by the Phase 8
    complexity pass."""

    cognitive: int | None = Field(default=None, ge=0)
    """Cognitive complexity (Sonarqube/SonarSource metric) for this node.
    Same applicability as ``cyclomatic`` — only set on function-kind nodes
    (including methods, which are function-kind). Populated by the Phase 8
    complexity pass."""

    loc: int | None = Field(default=None, ge=0)
    """Lines of code for this node (``line_end - line_start + 1``). Used
    by downstream consumers to compute complexity density. Only populated
    on ``kind="function"`` nodes (including methods) by the Phase 8
    complexity pass; ``None`` for area, file, and class nodes (deferred)."""


class EdgeEvidence(BaseModel):
    """Cited proof of an edge's existence — see ADR-016 §3."""

    file: str
    line: int
    snippet: str


class Edge(BaseModel):
    """One edge in the graph.

    Phase 2 only emits edges with ``source_kind="ast"``. Phase 3 added
    ``source_kind="llm"`` edges using the same fields.

    Phase 5 (ADR-016 §7) starts populating ``boundary_violation`` and adds
    the companion ``violation_reason`` field. ``violation_reason`` is one
    of:

    * ``"internal_access"`` — a cross-area edge whose target is private to
      its area (convention-based public-surface inference); flagged by the
      pipeline's boundary stage.
    * ``"explicit_rule:<index>"`` — the edge matches an explicit
      ``boundaries.forbid`` rule from ``.auto-agent/graph.yml``; the
      ``<index>`` is the 0-based position of the rule in the file. Takes
      precedence over an internal-access reason.
    * ``None`` — the edge does not violate any boundary.

    HTTP edges (``kind="http"``) are NEVER flagged — they are an
    intentional cross-language pattern, not a layering breach.
    """

    source: str
    target: str
    kind: Literal["calls", "imports", "inherits", "http"]
    evidence: EdgeEvidence
    source_kind: Literal["ast", "llm"]
    boundary_violation: bool = False
    violation_reason: str | None = None


class DependencyCycle(BaseModel):
    """One circular-dependency cycle detected in the module graph
    (ADR-016 quality layer §3). Computed by Tarjan SCC over ``imports``
    edges. ``members`` are the import-graph vertex ids participating in
    the cycle (e.g. ``module:agent.a``); ``closing_edges`` cite the
    ``imports`` edges whose source and target are both in the cycle."""

    id: str
    kind: Literal["import", "call"]
    members: list[str]
    closing_edges: list[EdgeEvidence]


class DeadCodeFinding(BaseModel):
    """One dead-code finding in the module graph (ADR-016 quality layer §4).

    ``kind`` categorises the finding; ``target`` is the node id or
    identifier it refers to (e.g. ``"api/routes.py::unused_helper"`` for
    an unused export, or ``"file:api/legacy.py"`` for an unused file).
    ``reason`` is a short human-readable explanation."""

    kind: Literal[
        "unused_export",
        "unused_file",
        "unused_dependency",
        "undeclared_dependency",
    ]
    target: str
    file: str | None = None
    reason: str


class CloneInstance(BaseModel):
    """One occurrence of a duplicated code block (ADR-016 quality layer §2)."""

    node_id: str
    file: str
    line_start: int
    line_end: int


class CloneGroup(BaseModel):
    """A group of >= 2 duplicated code blocks (a "clone group").

    ``token_len`` is the length of the duplicated token sequence.
    ``mode`` is the normalization level that detected it (strict ->
    semantic, increasing recall). ``family_id`` links clone groups that
    involve the same files (systematic copy-paste) when set."""

    id: str
    token_len: int
    mode: Literal["strict", "mild", "weak", "semantic"]
    instances: list[CloneInstance]
    family_id: str | None = None


class Hotspot(BaseModel):
    """A churn x complexity refactoring hotspot (ADR-016 quality layer §5).

    ``churn`` is the 90-day-half-life-decayed commit weight for the file;
    ``complexity_density`` is total cyclomatic complexity / lines of code;
    ``score`` (0..100) is the normalized product of churn and density —
    a file ranks high only if it is BOTH actively changing AND complex.
    ``trend`` compares commit frequency in the first vs second half of
    the window."""

    file: str
    churn: float
    complexity_density: float
    score: float
    trend: Literal["accelerating", "stable", "cooling"]


class FileHealth(BaseModel):
    """Per-file maintainability (ADR-016 quality layer §6).

    ``maintainability_index`` (0..100) = 100 - complexity_density*30
    - dead_code_ratio*20 - fan_out_penalty, clamped to [0,100].
    ``band``: good (70-100), moderate (40-70), poor (0-40).
    ``crap`` (untested-complexity risk) is reserved — it needs per-function
    coverage the graph does not yet ingest, so it is always None for now."""

    file: str
    maintainability_index: float
    band: Literal["good", "moderate", "poor"]
    crap: float | None = None


class RepoHealth(BaseModel):
    """Repo-level health summary (ADR-016 quality layer §6).

    ``score`` is the LOC-weighted mean of per-file maintainability_index;
    the counts are headline totals from the quality findings."""

    score: float
    clone_count: int
    cycle_count: int
    dead_count: int
    hotspot_count: int


class AreaStatus(BaseModel):
    """Per-area outcome (ADR-016 §10 — failures isolated per area)."""

    name: str
    status: Literal["ok", "partial", "failed"]
    error: str | None = None
    unresolved_dynamic_sites: int = 0


class RepoGraphBlob(BaseModel):
    """Full graph analysis output — the payload stored in
    ``RepoGraph.graph_json`` and surfaced to the UI / agent tool.

    ``public_symbols`` (ADR-016 Phase 6 §12) is the union of per-area
    public-surface node ids the pipeline computed at analysis time. The
    ``query_repo_graph.public_surface`` op reads this directly rather
    than re-deriving the convention rules from source bytes. Defaulted
    to an empty list so blobs persisted before Phase 6 still deserialise.
    """

    commit_sha: str
    generated_at: datetime
    analyser_version: str
    areas: list[AreaStatus]
    nodes: list[Node]
    edges: list[Edge]
    public_symbols: list[str] = Field(default_factory=list)
    cycles: list[DependencyCycle] = Field(default_factory=list)
    """Import-cycle records computed by Phase 9 Tarjan SCC pass; empty on
    pre-Phase-9 blobs (backward-compatible default)."""
    dead_code: list[DeadCodeFinding] = Field(default_factory=list)
    """Dead-code findings from the Phase 10 quality pass; empty on
    pre-Phase-10 blobs (backward-compatible default). v1 populates
    ``unused_export`` and ``unused_file`` kinds; ``unused_dependency``
    and ``undeclared_dependency`` are reserved for a follow-up."""
    clones: list[CloneGroup] = Field(default_factory=list)
    """Clone-group records from the Phase 11 duplication pass; empty on
    pre-Phase-11 blobs (backward-compatible default)."""
    hotspots: list[Hotspot] = Field(default_factory=list)
    """Churn-hotspot records from the Phase 12 quality pass; empty on
    pre-Phase-12 blobs (backward-compatible default)."""
    file_health: list[FileHealth] = Field(default_factory=list)
    """Per-file maintainability records from the Phase 13 health pass; empty on
    pre-Phase-13 blobs (backward-compatible default)."""
    health: RepoHealth | None = None
    """Repo-level health summary from the Phase 13 health pass; None on
    pre-Phase-13 blobs (backward-compatible default)."""


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
    is_complete: bool
    processed_files_count: int
    total_files_estimate: int


class RepoGraphProgressData(BaseModel):
    """Lightweight progress snapshot for /repos/{id}/graph/progress."""

    is_complete: bool
    processed: int
    total: int
    last_file: str | None = None
    status: Literal["running", "idle", "unchanged"]


class GraphCodePreviewResponse(BaseModel):
    """``GET /api/repos/{id}/graph/code`` payload — Phase 7 side panel.

    Returns a clamped window of source from the analyser workspace so
    the React side-panel can render the code under a node without
    pulling the whole file. The endpoint enforces bounds (``line_end -
    line_start <= 500``, body <= 50 KiB) and refuses path-traversal so
    it can't be coerced into reading anything outside the workspace.
    """

    file: str
    line_start: int
    line_end: int
    content: str


class GraphStalenessResponse(BaseModel):
    """``GET /api/repos/{id}/graph/staleness`` payload — ADR-016 Phase 7 §11.

    Surfaces the comparison between the stored graph's ``commit_sha`` and
    the current ``HEAD`` of the analyser workspace so the freshness
    banner can show an amber "workspace has moved — refresh" hint
    without re-fetching the whole graph blob.

    ``workspace_sha`` is ``None`` when the workspace can't be inspected
    (missing directory, not a git checkout, permission denied). In that
    case ``drifted`` is conservatively ``True`` — the banner shows the
    same warning rather than pretending the graph is fresh.
    """

    graph_sha: str
    workspace_sha: str | None = None
    drifted: bool


# --- Capability / flow derivation (Phase 1 of capability-flow map spec) ---
#
# A *flow* is one forward-trace from a detected entry point to a terminal
# side effect, recorded over the nodes/edges in `RepoGraphBlob`. A
# *capability* is a named group of flows. Phase 1 derives flows; Phase 2
# labels them via an LLM call. The shape supports both phases: name and
# description fields are nullable so Phase 1 blobs round-trip through a
# Phase 2-aware deserialiser.

EntryPointKind = Literal["http", "queue", "cron", "cli"]
TerminalKind = Literal["response", "queue_publish", "external_http", "db_write", "none"]


class EntryPoint(BaseModel):
    """One node detected as a flow entry point — see spec §3 step 1."""

    node_id: str
    kind: EntryPointKind


class FlowStep(BaseModel):
    """One node on a flow's forward trace.

    ``depth`` is the BFS distance from the entry point along the dominant
    path; branch nodes carry the branch root's depth. ``is_branch_root``
    flags a node that fans out into multiple outgoing call edges at the
    same depth (rendered as a branch fork in Phase 3). ``is_cycle_back``
    marks the back-edge target when a cycle was detected and the trace
    stopped without re-expanding (spec §3 step 4).
    """

    node_id: str
    depth: int
    is_branch_root: bool = False
    is_cycle_back: bool = False


class Flow(BaseModel):
    """One flow — entry point through forward trace to a terminal effect.

    ``name`` and ``description`` are produced by the Phase 2 LLM labeller;
    Phase 1 leaves them ``None``. ``file_set_hash`` is the SHA-256 over the
    file contents of ``file_set``, sorted by path and concatenated before
    hashing; Phase 2 uses it to skip re-labelling unchanged flows (spec §4).
    """

    id: str
    entry_point: EntryPoint
    terminal_node_id: str
    terminal_kind: TerminalKind
    steps: list[FlowStep]
    file_set: list[str]
    file_set_hash: str
    name: str | None = None
    description: str | None = None
    labeled_at_commit: str | None = None
    """Commit SHA at which this flow's name+description were generated by
    the Phase 2 labeller. ``None`` until the first label. Reused on
    subsequent recomputes when ``file_set_hash`` matches the prior blob."""


class Capability(BaseModel):
    """One named capability — a group of related flows.

    Phase 1 emits exactly one capability with ``id="unlabeled"`` covering
    every derived flow. Phase 2 groups flows into ~5-12 capabilities and
    populates ``name`` / ``description``. ``flow_membership_hash`` is the
    SHA-256 of the sorted ``flow_ids`` list; Phase 2 skips re-labelling
    capabilities whose membership hash matches the persisted value.
    """

    id: str
    flow_ids: list[str]
    flow_membership_hash: str
    name: str | None = None
    description: str | None = None
    labeled_at_commit: str | None = None
    """Commit SHA at which this capability's name+description were
    generated. ``None`` until the first label. Reused when
    ``flow_membership_hash`` matches the prior blob."""


class FlowJsonBlob(BaseModel):
    """Full capability/flow derivation result — payload of
    ``RepoGraph.flow_json``.

    ``unreached`` is the list of node ids in the underlying graph that
    no flow's forward trace touched. Surfaced as the Unreached tray in
    the Phase 3 UI (spec §3 step 6). Phase 2 added ``labeled_at_commit``
    (on ``Flow``/``Capability``) and ``labeler_model`` (on this blob) for
    LLM provenance tracking; they remain ``None`` on Phase 1-derived blobs.
    """

    capabilities: list[Capability]
    flows: list[Flow]
    unreached: list[str]
    derived_at_commit: str
    deriver_version: str
    labeler_model: str | None = None
    """Identifier of the LLM model that produced the most recent labels
    (e.g. ``"claude-haiku-4-5"``). ``None`` if no labelling has happened
    yet (Phase 1 emits this as ``None``)."""


class RecomputeFlowsResponse(BaseModel):
    """``POST /api/repos/{id}/graph/flows/recompute`` response body."""

    repo_id: int
    flow_count: int
    capability_count: int
    unreached_count: int
    derived_at_commit: str
    labeled_flow_count: int = 0
    """Number of flows that received a non-null name from the Phase 2
    labeller. 0 in Phase 1; matches ``flow_count`` once all flows label
    successfully."""


class LatestFlowsData(BaseModel):
    """``GET /api/repos/{id}/graph/flows`` payload — the Phase 3 Map view
    consumes this directly. ``blob`` is ``None`` until a recompute lands.

    ``repo_graph_id`` and ``generated_at`` come from the graph row whose
    ``flow_json`` produced ``blob``; the UI uses them in the freshness
    banner / "Recompute map" button. The endpoint reads the row pointed
    to by ``RepoGraphConfig.last_analysis_id`` so it always matches the
    blob the agent op ``which_capability`` resolves against.
    """

    repo_id: int
    repo_graph_id: int | None = None
    generated_at: str | None = None
    blob: FlowJsonBlob | None = None


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


# --- Per-repo secrets API (ADR-019) ---


class RepoSecretListEntry(BaseModel):
    """One entry in the per-repo secrets list.  Values are never included."""

    key: str
    is_set: bool
    source: str
    purpose: str | None = None
    updated_at: datetime | None = None


class RepoSecretListResponse(BaseModel):
    """Response body for GET /repos/{id}/secrets."""

    keys: list[RepoSecretListEntry]


class RepoSecretPutRequest(BaseModel):
    """Body for PUT /repos/{id}/secrets/{key}.

    ``value=None`` or ``value=""`` clears the row (equivalent to DELETE).
    """

    value: str | None = None


class RepoSecretTestResponse(BaseModel):
    """Response body for POST /repos/{id}/secrets/{key}/test."""

    ok: bool
    kind: str | None = None
    message: str | None = None


class RepoSecretRevealResponse(BaseModel):
    """Response body for POST /repos/{id}/secrets/{key}/reveal."""

    value: str | None = None


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
    # Clarification fields (set when decision.action="awaiting_clarification").
    # session_blob_path stays internal — not exposed here.
    clarification_question: str | None = None
    clarification_answer: str | None = None
    clarification_source: Literal["user", "po"] | None = None
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


class DecisionOut(BaseModel):
    """API shape for one ADR file under ``docs/decisions/``."""

    filename: str
    title: str
    url: str


# --- Gate decision audit types — ADR-015 §6 Phase 12 ---


class PlanApprovalRequest(BaseModel):
    """Inbound body for POST /api/tasks/{id}/approve-plan.

    Writes ``.auto-agent/plan_approval.json`` and persists a
    :class:`shared.models.GateDecision` row so the gate-history audit
    panel can render the human's verdict alongside any standin ones.
    """

    verdict: Literal["approved", "rejected"]
    comments: str = Field(default="", max_length=5000)


class GateDecisionOut(BaseModel):
    """One row in the gate-history audit panel — ADR-015 §6.

    Stable wire shape across user and standin sources so the panel
    doesn't have to branch on origin to render an entry.
    """

    id: int
    task_id: int
    gate: str  # "grill" | "plan_approval" | "design_approval" | "pr_review"
    source: str  # "user" | "po_standin" | "improvement_standin"
    agent_id: str | None = None
    verdict: str
    comments: str = ""
    cited_context: list[str] = Field(default_factory=list)
    fallback_reasons: list[str] = Field(default_factory=list)
    created_at: datetime


class GateArtefact(BaseModel):
    """Markdown content the human or standin is being asked to approve.

    Returned by GET /api/tasks/{id}/gate-artefact so the web-next UI can
    render ``.auto-agent/plan.md`` (complex flow) or ``.auto-agent/design.md``
    (complex_large flow) without the orchestrator hard-coding which file
    is "the artefact" — the resolution is driven by task status.
    """

    kind: Literal["plan", "design"]
    path: str
    body: str


# --- Scaffold (ADR-018) gate types ---


class ScaffoldIntentGrillAnswerRequest(BaseModel):
    """Inbound body for POST /api/tasks/{id}/scaffold/intent-grill-answer.

    Writes ``.auto-agent/intent_grill_answer.json`` so the intent-grill
    agent's session can resume with the user's answer to its pending
    question (ADR-018 §2).
    """

    answer: str = Field(min_length=1, max_length=20_000)


class ScaffoldRootAdrVerdictRequest(BaseModel):
    """Inbound body for POST /api/tasks/{id}/scaffold/root-adr-verdict.

    The verdict is applied via
    ``agent.lifecycle.scaffold.root_adr_approval.apply_verdict`` which
    persists the verdict to ``.auto-agent/root_adr_approval.json`` and
    transitions the state machine (ADR-018 §4).
    """

    verdict: Literal["approved", "revise", "rejected"]
    comments: str = Field(default="", max_length=5000)


class ScaffoldDomainAdrVerdictRequest(BaseModel):
    """Inbound body for POST /api/tasks/{id}/scaffold/domain-adr-verdict.

    The endpoint records a per-domain verdict; the parent advances to
    DISPATCHING_DOMAIN_BUILDS only when every domain ADR has a
    non-``revise`` verdict (ADR-018 §6).
    """

    domain_slug: str = Field(min_length=1, max_length=128)
    verdict: Literal["approved", "revise", "rejected"]
    comments: str = Field(default="", max_length=5000)


class ScaffoldDomainAdrEntry(BaseModel):
    """One domain ADR entry returned by
    GET /api/tasks/{id}/scaffold/domain-adrs.

    ``approval`` is omitted when no verdict file exists yet for the slug;
    when present it carries the latest persisted verdict so the UI can
    render the current state without a second round-trip.
    """

    slug: str
    name: str = ""
    index: int
    markdown: str = ""
    approval: dict | None = None


class ScaffoldArtefactMarkdown(BaseModel):
    """Markdown body of a scaffold artefact (intent.md or root ADR).

    Symmetric with :class:`GateArtefact` but kept distinct because
    scaffold artefacts don't carry a ``kind`` discriminator — the
    endpoint URL fixes the file.
    """

    markdown: str


class ScaffoldDomainGrillAnswerRequest(BaseModel):
    """Inbound body for POST /api/tasks/{id}/scaffold/domain-grill-answer.

    Writes ``.auto-agent/domain_grill_answers/<slug>.json`` so the
    domain-grill agent's session can resume with the user's answer to
    its pending question (ADR-018 §5, Stage 8). The ``domain_slug``
    identifies which domain's grill is being answered.
    """

    domain_slug: str = Field(min_length=1, max_length=128)
    answer: str = Field(min_length=1, max_length=20_000)


class ScaffoldDomainGrillQuestion(BaseModel):
    """The pending domain-grill question for one domain.

    Returned by GET /api/tasks/{id}/scaffold/domain-grill-question?slug=...
    when the SCAFFOLD parent is parked in ``AWAITING_DOMAIN_GRILL`` and
    a question file has been written under
    ``.auto-agent/domain_grill_questions/<slug>.json``.
    """

    domain_slug: str
    question: str
