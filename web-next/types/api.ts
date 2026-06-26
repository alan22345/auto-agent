/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

type RiskLevel = "low" | "medium" | "high";

export interface AffectedRoute {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  label: string;
}
/**
 * API shape for an architect_attempts row.
 */
export interface ArchitectAttemptOut {
  id: number;
  task_id: number;
  phase: "initial" | "consult" | "checkpoint" | "revision";
  cycle: number;
  reasoning: string;
  decision?: {
    [k: string]: unknown;
  } | null;
  consult_question?: string | null;
  consult_why?: string | null;
  architecture_md_after?: string | null;
  commit_sha?: string | null;
  tool_calls: {
    [k: string]: unknown;
  }[];
  clarification_question?: string | null;
  clarification_answer?: string | null;
  clarification_source?: ("user" | "po") | null;
  created_at: string;
}
/**
 * The decision field on an ArchitectAttempt row when phase=checkpoint.
 */
export interface ArchitectDecision {
  action: "continue" | "revise" | "done" | "awaiting_clarification" | "blocked";
  reason?: string | null;
  question?: string | null;
}
/**
 * Per-area outcome (ADR-016 §10 — failures isolated per area).
 */
export interface AreaStatus {
  name: string;
  status: "ok" | "partial" | "failed";
  error?: string | null;
  unresolved_dynamic_sites?: number;
}
/**
 * One named capability — a group of related flows.
 *
 * Phase 1 emits exactly one capability with ``id="unlabeled"`` covering
 * every derived flow. Phase 2 groups flows into ~5-12 capabilities and
 * populates ``name`` / ``description``. ``flow_membership_hash`` is the
 * SHA-256 of the sorted ``flow_ids`` list; Phase 2 skips re-labelling
 * capabilities whose membership hash matches the persisted value.
 */
export interface Capability {
  id: string;
  flow_ids: string[];
  flow_membership_hash: string;
  name?: string | null;
  description?: string | null;
  labeled_at_commit?: string | null;
}
export interface ChangeEmailRequest {
  email: string;
}
export interface ClassificationResult {
  classification: "simple" | "complex" | "complex_large" | "simple_no_code" | "scaffold";
  reasoning?: string;
  estimated_files?: number;
  risk?: RiskLevel;
}
/**
 * A group of >= 2 duplicated code blocks (a "clone group").
 *
 * ``token_len`` is the length of the duplicated token sequence.
 * ``mode`` is the normalization level that detected it (strict ->
 * semantic, increasing recall). ``family_id`` links clone groups that
 * involve the same files (systematic copy-paste) when set.
 */
export interface CloneGroup {
  id: string;
  token_len: number;
  mode: "strict" | "mild" | "weak" | "semantic";
  instances: CloneInstance[];
  family_id?: string | null;
}
/**
 * One occurrence of a duplicated code block (ADR-016 quality layer §2).
 */
export interface CloneInstance {
  node_id: string;
  file: string;
  line_start: number;
  line_end: number;
}
export interface ConflictInfo {
  fact_id: string;
  existing_content: string;
}
export interface CreateUserRequest {
  username: string;
  password: string;
  display_name: string;
}
/**
 * One dead-code finding in the module graph (ADR-016 quality layer §4).
 *
 * ``kind`` categorises the finding; ``target`` is the node id or
 * identifier it refers to (e.g. ``"api/routes.py::unused_helper"`` for
 * an unused export, or ``"file:api/legacy.py"`` for an unused file).
 * ``reason`` is a short human-readable explanation.
 */
export interface DeadCodeFinding {
  kind: "unused_export" | "unused_file" | "unused_dependency" | "undeclared_dependency";
  target: string;
  file?: string | null;
  reason: string;
}
/**
 * API shape for one ADR file under ``docs/decisions/``.
 */
export interface DecisionOut {
  filename: string;
  title: string;
  url: string;
}
/**
 * One circular-dependency cycle detected in the module graph
 * (ADR-016 quality layer §3). Computed by Tarjan SCC over ``imports``
 * edges. ``members`` are the import-graph vertex ids participating in
 * the cycle (e.g. ``module:agent.a``); ``closing_edges`` cite the
 * ``imports`` edges whose source and target are both in the cycle.
 */
export interface DependencyCycle {
  id: string;
  kind: "import" | "call";
  members: string[];
  closing_edges: EdgeEvidence[];
}
/**
 * Cited proof of an edge's existence — see ADR-016 §3.
 */
export interface EdgeEvidence {
  file: string;
  line: number;
  snippet: string;
}
/**
 * One edge in the graph.
 *
 * Phase 2 only emits edges with ``source_kind="ast"``. Phase 3 added
 * ``source_kind="llm"`` edges using the same fields.
 *
 * Phase 5 (ADR-016 §7) starts populating ``boundary_violation`` and adds
 * the companion ``violation_reason`` field. ``violation_reason`` is one
 * of:
 *
 * * ``"internal_access"`` — a cross-area edge whose target is private to
 *   its area (convention-based public-surface inference); flagged by the
 *   pipeline's boundary stage.
 * * ``"explicit_rule:<index>"`` — the edge matches an explicit
 *   ``boundaries.forbid`` rule from ``.auto-agent/graph.yml``; the
 *   ``<index>`` is the 0-based position of the rule in the file. Takes
 *   precedence over an internal-access reason.
 * * ``None`` — the edge does not violate any boundary.
 *
 * HTTP edges (``kind="http"``) are NEVER flagged — they are an
 * intentional cross-language pattern, not a layering breach.
 */
export interface Edge {
  source: string;
  target: string;
  kind: "calls" | "imports" | "inherits" | "http";
  evidence: EdgeEvidence;
  source_kind: "ast" | "llm";
  boundary_violation?: boolean;
  violation_reason?: string | null;
}
/**
 * Optional body for ``POST /api/repos/{repo_id}/graph``.
 *
 * All fields optional — the endpoint defaults the analysis branch to the
 * repo's ``default_branch`` if the caller omits it.
 */
export interface EnableRepoGraphRequest {
  analysis_branch?: string | null;
}
/**
 * One node detected as a flow entry point — see spec §3 step 1.
 */
export interface EntryPoint {
  node_id: string;
  kind: "http" | "queue" | "cron" | "cli";
}
export interface FeedbackSummary {
  total_outcomes?: number;
  approved?: number;
  rejected?: number;
  approval_rate?: number;
  avg_review_rounds?: number;
}
/**
 * Per-file maintainability (ADR-016 quality layer §6).
 *
 * ``maintainability_index`` (0..100) = 100 - complexity_density*30
 * - dead_code_ratio*20 - fan_out_penalty, clamped to [0,100].
 * ``band``: good (70-100), moderate (40-70), poor (0-40).
 * ``crap`` (untested-complexity risk) is reserved — it needs per-function
 * coverage the graph does not yet ingest, so it is always None for now.
 */
export interface FileHealth {
  file: string;
  maintainability_index: number;
  band: "good" | "moderate" | "poor";
  crap?: number | null;
}
/**
 * One flow — entry point through forward trace to a terminal effect.
 *
 * ``name`` and ``description`` are produced by the Phase 2 LLM labeller;
 * Phase 1 leaves them ``None``. ``file_set_hash`` is the SHA-256 over the
 * file contents of ``file_set``, sorted by path and concatenated before
 * hashing; Phase 2 uses it to skip re-labelling unchanged flows (spec §4).
 */
export interface Flow {
  id: string;
  entry_point: EntryPoint;
  terminal_node_id: string;
  terminal_kind: "response" | "queue_publish" | "external_http" | "db_write" | "none";
  steps: FlowStep[];
  file_set: string[];
  file_set_hash: string;
  name?: string | null;
  description?: string | null;
  labeled_at_commit?: string | null;
}
/**
 * One node on a flow's forward trace.
 *
 * ``depth`` is the BFS distance from the entry point along the dominant
 * path; branch nodes carry the branch root's depth. ``is_branch_root``
 * flags a node that fans out into multiple outgoing call edges at the
 * same depth (rendered as a branch fork in Phase 3). ``is_cycle_back``
 * marks the back-edge target when a cycle was detected and the trace
 * stopped without re-expanding (spec §3 step 4).
 */
export interface FlowStep {
  node_id: string;
  depth: number;
  is_branch_root?: boolean;
  is_cycle_back?: boolean;
}
/**
 * Full capability/flow derivation result — payload of
 * ``RepoGraph.flow_json``.
 *
 * ``unreached`` is the list of node ids in the underlying graph that
 * no flow's forward trace touched. Surfaced as the Unreached tray in
 * the Phase 3 UI (spec §3 step 6). Phase 2 added ``labeled_at_commit``
 * (on ``Flow``/``Capability``) and ``labeler_model`` (on this blob) for
 * LLM provenance tracking; they remain ``None`` on Phase 1-derived blobs.
 */
export interface FlowJsonBlob {
  capabilities: Capability[];
  flows: Flow[];
  unreached: string[];
  derived_at_commit: string;
  deriver_version: string;
  labeler_model?: string | null;
}
/**
 * Typed representation of a freeform mode config.
 */
export interface FreeformConfigData {
  id: number;
  repo_name?: string | null;
  enabled?: boolean;
  prod_branch?: string;
  dev_branch?: string;
  analysis_cron?: string;
  auto_approve_suggestions?: boolean;
  auto_start_tasks?: boolean;
  po_goal?: string | null;
  last_analysis_at?: string | null;
  architecture_mode?: boolean;
  architecture_cron?: string;
  last_architecture_at?: string | null;
  architecture_knowledge?: string | null;
  run_command?: string | null;
  created_at?: string | null;
}
/**
 * One gap the final reviewer reported on a trio parent.
 */
export interface GapFixGap {
  description: string;
  affected_routes?: string[];
}
/**
 * Gap-fix activity snapshot for a trio parent — UI panel data.
 *
 * Returned by GET /api/tasks/{id}/gap-fix-state. When no gap-fix
 * rounds have run, all fields are empty / zero and the UI hides the
 * panel.
 */
export interface GapFixState {
  rounds_completed: number;
  max_rounds: number;
  latest_action: string | null;
  latest_item_count: number;
  latest_oversized_count: number;
  gaps: GapFixGap[];
}
/**
 * Markdown content the human or standin is being asked to approve.
 *
 * Returned by GET /api/tasks/{id}/gate-artefact so the web-next UI can
 * render ``.auto-agent/plan.md`` (complex flow) or ``.auto-agent/design.md``
 * (complex_large flow) without the orchestrator hard-coding which file
 * is "the artefact" — the resolution is driven by task status.
 */
export interface GateArtefact {
  kind: "plan" | "design";
  path: string;
  body: string;
}
/**
 * One row in the gate-history audit panel — ADR-015 §6.
 *
 * Stable wire shape across user and standin sources so the panel
 * doesn't have to branch on origin to render an entry.
 */
export interface GateDecisionOut {
  id: number;
  task_id: number;
  gate: string;
  source: string;
  agent_id?: string | null;
  verdict: string;
  comments?: string;
  cited_context?: string[];
  fallback_reasons?: string[];
  created_at: string;
}
/**
 * ``GET /api/repos/{id}/graph/code`` payload — Phase 7 side panel.
 *
 * Returns a clamped window of source from the analyser workspace so
 * the React side-panel can render the code under a node without
 * pulling the whole file. The endpoint enforces bounds (``line_end -
 * line_start <= 500``, body <= 50 KiB) and refuses path-traversal so
 * it can't be coerced into reading anything outside the workspace.
 */
export interface GraphCodePreviewResponse {
  file: string;
  line_start: number;
  line_end: number;
  content: string;
}
/**
 * ``GET /api/repos/{id}/graph/staleness`` payload — ADR-016 Phase 7 §11.
 *
 * Surfaces the comparison between the stored graph's ``commit_sha`` and
 * reality so the freshness banner can show an amber "repo has moved —
 * refresh" hint without re-fetching the whole graph blob.
 *
 * ``origin_sha`` is the tip of ``origin/<analysis_branch>`` per
 * ``git ls-remote`` (ADR-024) — the authoritative drift signal; the
 * workspace HEAD only moves on refresh. ``None`` when origin couldn't
 * be asked, in which case ``drifted`` falls back to the workspace
 * comparison. ``workspace_sha`` is ``None`` when the workspace can't
 * be inspected; with both unknown, ``drifted`` is conservatively
 * ``True`` rather than pretending the graph is fresh.
 */
export interface GraphStalenessResponse {
  graph_sha: string;
  workspace_sha?: string | null;
  origin_sha?: string | null;
  drifted: boolean;
}
/**
 * A churn x complexity refactoring hotspot (ADR-016 quality layer §5).
 *
 * ``churn`` is the 90-day-half-life-decayed commit weight for the file;
 * ``complexity_density`` is total cyclomatic complexity / lines of code;
 * ``score`` (0..100) is the normalized product of churn and density —
 * a file ranks high only if it is BOTH actively changing AND complex.
 * ``trend`` compares commit frequency in the first vs second half of
 * the window.
 */
export interface Hotspot {
  file: string;
  churn: number;
  complexity_density: number;
  score: number;
  trend: "accelerating" | "stable" | "cooling";
}
export interface IntentVerdict {
  ok: boolean;
  reasoning: string;
  tool_calls?: {
    [k: string]: unknown;
  }[];
}
/**
 * ``GET /api/repos/{id}/graph/flows`` payload — the Phase 3 Map view
 * consumes this directly. ``blob`` is ``None`` until a recompute lands.
 *
 * ``repo_graph_id`` and ``generated_at`` come from the graph row whose
 * ``flow_json`` produced ``blob``; the UI uses them in the freshness
 * banner / "Recompute map" button. The endpoint reads the row pointed
 * to by ``RepoGraphConfig.last_analysis_id`` so it always matches the
 * blob the agent op ``which_capability`` resolves against.
 */
export interface LatestFlowsData {
  repo_id: number;
  repo_graph_id?: number | null;
  generated_at?: string | null;
  blob?: FlowJsonBlob | null;
}
/**
 * ``GET /api/repos/{id}/graph/latest`` payload — the freshness banner
 * + Cytoscape renderer consume this directly. ``blob`` is ``None`` when
 * no analysis has completed yet.
 */
export interface LatestRepoGraphData {
  repo_id: number;
  analysis_branch: string;
  repo_graph_id?: number | null;
  commit_sha?: string | null;
  generated_at?: string | null;
  analyser_version?: string | null;
  status?: ("ok" | "partial" | "failed") | null;
  blob?: RepoGraphBlob | null;
  is_complete: boolean;
  processed_files_count: number;
  total_files_estimate: number;
}
/**
 * Full graph analysis output — the payload stored in
 * ``RepoGraph.graph_json`` and surfaced to the UI / agent tool.
 *
 * ``public_symbols`` (ADR-016 Phase 6 §12) is the union of per-area
 * public-surface node ids the pipeline computed at analysis time. The
 * ``query_repo_graph.public_surface`` op reads this directly rather
 * than re-deriving the convention rules from source bytes. Defaulted
 * to an empty list so blobs persisted before Phase 6 still deserialise.
 */
export interface RepoGraphBlob {
  commit_sha: string;
  generated_at: string;
  analyser_version: string;
  areas: AreaStatus[];
  nodes: Node[];
  edges: Edge[];
  public_symbols?: string[];
  cycles?: DependencyCycle[];
  dead_code?: DeadCodeFinding[];
  clones?: CloneGroup[];
  hotspots?: Hotspot[];
  file_health?: FileHealth[];
  health?: RepoHealth | null;
}
/**
 * One node in the hierarchical compound graph (ADR-016 §2).
 *
 * ``id`` is the canonical Cytoscape id. ``parent`` points to the parent
 * compound node (area → file → class → function nesting). ``area`` is
 * duplicated on every node so query callers can filter without walking
 * the parent chain.
 */
export interface Node {
  id: string;
  kind: "area" | "file" | "class" | "function";
  label: string;
  file?: string | null;
  line_start?: number | null;
  line_end?: number | null;
  area: string;
  parent?: string | null;
  decorators?: string[];
  cyclomatic?: number | null;
  cognitive?: number | null;
  loc?: number | null;
}
/**
 * Repo-level health summary (ADR-016 quality layer §6).
 *
 * ``score`` is the composite — the weighted mean of the five sub-scores
 * below (each 0..100, higher = better). The counts are headline totals from
 * the quality findings. Sub-score fields default to 100.0 so blobs produced
 * before composite scoring stay valid.
 */
export interface RepoHealth {
  score: number;
  clone_count: number;
  cycle_count: number;
  dead_count: number;
  hotspot_count: number;
  maintainability?: number;
  duplication?: number;
  dead_code?: number;
  cycles?: number;
  coupling?: number;
}
/**
 * A Linear issue returned from the GraphQL API.
 */
export interface LinearIssue {
  id: string;
  identifier: string;
  title: string;
  description?: string;
  state?: {
    [k: string]: string;
  };
  url?: string;
}
export interface LoginRequest {
  username: string;
  password: string;
}
export interface LoginResponse {
  token: string;
  user: UserData;
}
/**
 * Typed representation of a user.
 */
export interface UserData {
  id: number;
  username: string;
  display_name: string;
  created_at?: string | null;
  last_login?: string | null;
  claude_auth_status?: string;
  claude_paired_at?: string | null;
  telegram_chat_id?: string | null;
  slack_user_id?: string | null;
}
/**
 * Response shape for GET /api/repos/{repo_id}/market-brief/latest.
 */
export interface MarketBriefResponse {
  id: number;
  repo_id: number;
  created_at: string;
  product_category?: string | null;
  competitors?: {
    [k: string]: unknown;
  }[];
  findings?: {
    [k: string]: unknown;
  }[];
  modality_gaps?: {
    [k: string]: unknown;
  }[];
  strategic_themes?: {
    [k: string]: unknown;
  }[];
  summary?: string;
  partial?: boolean;
}
export interface MemoryEntityDetail {
  entity: MemoryEntitySummary;
  facts?: MemoryFact[];
}
/**
 * Lightweight entity card for search results / recent list.
 */
export interface MemoryEntitySummary {
  id: string;
  name: string;
  type: string;
  tags?: string[];
  fact_count?: number;
  latest_fact_at?: string | null;
}
/**
 * A fact row as seen in the browser detail view.
 */
export interface MemoryFact {
  id: string;
  content: string;
  kind: string;
  source?: string | null;
  author?: string | null;
  valid_from?: string | null;
  valid_until?: string | null;
}
export interface MemorySaveResult {
  row_id: string;
  ok: boolean;
  error?: string | null;
  fact_id?: string | null;
}
export interface MetricsResponse {
  period_days: number;
  total_tasks: number;
  active_tasks: number;
  success_rate_pct: number;
  by_status: {
    [k: string]: number;
  };
  by_complexity: {
    [k: string]: number;
  };
  by_source: {
    [k: string]: number;
  };
  avg_duration_hours: number | null;
  pr_outcomes: PROutcomeMetrics;
}
export interface PROutcomeMetrics {
  total?: number;
  approved?: number;
  rejected?: number;
  approval_rate_pct?: number;
  avg_review_rounds?: number;
  avg_completion_seconds?: number | null;
}
export interface OutcomeResponse {
  task_id: number;
  pr_approved: boolean;
  review_rounds: number;
}
/**
 * A review comment from a GitHub PR.
 */
export interface PRReviewComment {
  author: string;
  body: string;
  type: "review" | "inline";
  path?: string;
  line?: number | null;
}
/**
 * Inbound body for POST /api/tasks/{id}/approve-plan.
 *
 * Writes ``.auto-agent/plan_approval.json`` and persists a
 * :class:`shared.models.GateDecision` row so the gate-history audit
 * panel can render the human's verdict alongside any standin ones.
 */
export interface PlanApprovalRequest {
  verdict: "approved" | "rejected";
  comments?: string;
}
export interface PlanRead {
  id: number;
  name: string;
  max_concurrent_tasks: number;
  max_tasks_per_day: number;
  max_input_tokens_per_day: number;
  max_output_tokens_per_day: number;
}
export interface ProposedFact {
  row_id: string;
  entity: string;
  entity_type?: string;
  entity_status?: "new" | "exists";
  entity_match_score?: number | null;
  kind?: "decision" | "architecture" | "gotcha" | "status" | "preference" | "fact";
  content: string;
  conflicts?: ConflictInfo[];
  resolution?: ("keep_existing" | "replace" | "keep_both") | null;
}
/**
 * ``POST /api/repos/{id}/graph/flows/recompute`` response body.
 */
export interface RecomputeFlowsResponse {
  repo_id: number;
  flow_count: number;
  capability_count: number;
  unreached_count: number;
  derived_at_commit: string;
  labeled_flow_count?: number;
}
/**
 * Passed to architect.checkpoint on parent re-entry after integration PR CI failure.
 */
export interface RepairContext {
  ci_log: string;
  failed_pr_url: string;
}
/**
 * Typed representation of a repo from the orchestrator API.
 */
export interface RepoData {
  id: number;
  name: string;
  url: string;
  default_branch?: string;
  summary?: string | null;
  summary_updated_at?: string | null;
  ci_checks?: string | null;
  harness_onboarded?: boolean;
  harness_pr_url?: string | null;
  product_brief?: string | null;
  mode?: "freeform" | "human_in_loop";
}
/**
 * Per-repo code-graph settings (ADR-016 §8).
 *
 * Phase 1: ``last_analysis_id`` is always ``None`` and ``analyser_version``
 * is the empty string — both are populated by the Phase 2 analyser.
 */
export interface RepoGraphConfigData {
  repo_id: number;
  repo_name: string;
  repo_url: string;
  analysis_branch: string;
  analyser_version?: string;
  workspace_path: string;
  last_analysis_id?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
}
/**
 * Lightweight progress snapshot for /repos/{id}/graph/progress.
 */
export interface RepoGraphProgressData {
  is_complete: boolean;
  processed: number;
  total: number;
  last_file?: string | null;
  status: "running" | "idle" | "unchanged";
}
/**
 * ``POST /api/repos/{id}/graph/refresh`` response body.
 */
export interface RepoGraphRefreshResponse {
  request_id: string;
  status?: "accepted";
}
export interface RepoResponse {
  id: number;
  name: string;
  url: string;
}
/**
 * One entry in the per-repo secrets list.  Values are never included.
 */
export interface RepoSecretListEntry {
  key: string;
  is_set: boolean;
  source: string;
  purpose?: string | null;
  updated_at?: string | null;
}
/**
 * Response body for GET /repos/{id}/secrets.
 */
export interface RepoSecretListResponse {
  keys: RepoSecretListEntry[];
}
/**
 * Body for PUT /repos/{id}/secrets/{key}.
 *
 * ``value=None`` or ``value=""`` clears the row (equivalent to DELETE).
 */
export interface RepoSecretPutRequest {
  value?: string | null;
}
/**
 * Response body for POST /repos/{id}/secrets/{key}/reveal.
 */
export interface RepoSecretRevealResponse {
  value?: string | null;
}
/**
 * Response body for POST /repos/{id}/secrets/{key}/test.
 */
export interface RepoSecretTestResponse {
  ok: boolean;
  kind?: string | null;
  message?: string | null;
}
/**
 * API shape for a review attempt row.
 */
export interface ReviewAttemptOut {
  id: number;
  cycle: number;
  status: "pass" | "fail" | "error";
  code_review_verdict?: string | null;
  ui_check?: ("pass" | "fail" | "skipped") | null;
  ui_judgment?: string | null;
  tool_calls?:
    | {
        [k: string]: unknown;
      }[]
    | null;
  failure_reason?: string | null;
  log_tail?: string | null;
  started_at: string;
  finished_at?: string | null;
}
export interface ReviewCombinedVerdict {
  code_review: ReviewDimensionVerdict;
  ui_check: ReviewDimensionVerdict;
}
export interface ReviewDimensionVerdict {
  verdict: "OK" | "NOT-OK" | "SKIPPED";
  reasoning: string;
}
/**
 * Markdown body of a scaffold artefact (intent.md or root ADR).
 *
 * Symmetric with :class:`GateArtefact` but kept distinct because
 * scaffold artefacts don't carry a ``kind`` discriminator — the
 * endpoint URL fixes the file.
 */
export interface ScaffoldArtefactMarkdown {
  markdown: string;
}
/**
 * One domain ADR entry returned by
 * GET /api/tasks/{id}/scaffold/domain-adrs.
 *
 * ``approval`` is omitted when no verdict file exists yet for the slug;
 * when present it carries the latest persisted verdict so the UI can
 * render the current state without a second round-trip.
 */
export interface ScaffoldDomainAdrEntry {
  slug: string;
  name?: string;
  index: number;
  markdown?: string;
  approval?: {
    [k: string]: unknown;
  } | null;
}
/**
 * Inbound body for POST /api/tasks/{id}/scaffold/domain-adr-verdict.
 *
 * The endpoint records a per-domain verdict; the parent advances to
 * DISPATCHING_DOMAIN_BUILDS only when every domain ADR has a
 * non-``revise`` verdict (ADR-018 §6).
 */
export interface ScaffoldDomainAdrVerdictRequest {
  domain_slug: string;
  verdict: "approved" | "revise" | "rejected";
  comments?: string;
}
/**
 * Inbound body for POST /api/tasks/{id}/scaffold/domain-grill-answer.
 *
 * Writes ``.auto-agent/domain_grill_answers/<slug>.json`` so the
 * domain-grill agent's session can resume with the user's answer to
 * its pending question (ADR-018 §5, Stage 8). The ``domain_slug``
 * identifies which domain's grill is being answered.
 */
export interface ScaffoldDomainGrillAnswerRequest {
  domain_slug: string;
  answer: string;
}
/**
 * The pending domain-grill question for one domain.
 *
 * Returned by GET /api/tasks/{id}/scaffold/domain-grill-question?slug=...
 * when the SCAFFOLD parent is parked in ``AWAITING_DOMAIN_GRILL`` and
 * a question file has been written under
 * ``.auto-agent/domain_grill_questions/<slug>.json``.
 */
export interface ScaffoldDomainGrillQuestion {
  domain_slug: string;
  question: string;
}
/**
 * Inbound body for POST /api/tasks/{id}/scaffold/intent-grill-answer.
 *
 * Writes ``.auto-agent/intent_grill_answer.json`` so the intent-grill
 * agent's session can resume with the user's answer to its pending
 * question (ADR-018 §2).
 */
export interface ScaffoldIntentGrillAnswerRequest {
  answer: string;
}
/**
 * Inbound body for POST /api/tasks/{id}/scaffold/root-adr-verdict.
 *
 * The verdict is applied via
 * ``agent.lifecycle.scaffold.root_adr_approval.apply_verdict`` which
 * persists the verdict to ``.auto-agent/root_adr_approval.json`` and
 * transitions the state machine (ADR-018 §4).
 */
export interface ScaffoldRootAdrVerdictRequest {
  verdict: "approved" | "revise" | "rejected";
  comments?: string;
}
export interface ScheduleResponse {
  id: number;
  name: string;
  cron: string;
  task_title: string;
  enabled: boolean;
  last_run_at?: string | null;
}
/**
 * Names only — values never leave the server.
 */
export interface SecretListResponse {
  keys: string[];
}
/**
 * ``value=None`` clears the secret (equivalent to DELETE).
 */
export interface SecretPutRequest {
  value?: string | null;
}
export interface SecretTestResponse {
  ok: boolean;
  detail?: string;
}
export interface SignupRequest {
  email: string;
  password: string;
  display_name: string;
}
/**
 * Response for POST /api/auth/signup. Always returns 201 with the new
 * user's id; the client should display "check your email" — never assume
 * the email was actually delivered.
 */
export interface SignupResponse {
  user_id: number;
  email: string;
  verification_sent: boolean;
}
/**
 * Typed representation of a PO suggestion.
 */
export interface SuggestionData {
  id: number;
  repo_id?: number | null;
  repo_name?: string | null;
  title: string;
  description?: string;
  rationale?: string;
  category?: string;
  priority?: number;
  status?: string;
  task_id?: number | null;
  created_at?: string | null;
  evidence_urls?: {
    [k: string]: unknown;
  }[];
}
/**
 * Typed representation of a task from the orchestrator API.
 */
export interface TaskData {
  id: number;
  title: string;
  description: string;
  source: string;
  status: string;
  complexity?: string | null;
  repo_name?: string | null;
  branch_name?: string | null;
  pr_url?: string | null;
  plan?: string | null;
  error?: string | null;
  freeform_mode?: boolean;
  mode_override?: ("freeform" | "human_in_loop") | null;
  effective_mode?: ("freeform" | "human_in_loop") | null;
  priority?: number;
  subtasks?:
    | {
        [k: string]: unknown;
      }[]
    | {
        [k: string]: unknown;
      }
    | null;
  current_subtask?: number | null;
  intake_qa?:
    | {
        [k: string]: unknown;
      }[]
    | null;
  created_at?: string | null;
  created_by_user_id?: number | null;
  organization_id?: number | null;
  change_type?: string | null;
  target_areas?: string | null;
  acceptance_criteria?: string | null;
  constraints?: string | null;
  parent_task_id?: number | null;
  trio_phase?: string | null;
  trio_backlog?:
    | {
        [k: string]: unknown;
      }[]
    | null;
}
/**
 * A user-posted feedback message on a task.
 */
export interface TaskMessageData {
  id: number;
  task_id: number;
  sender: string;
  content: string;
  created_at?: string | null;
}
/**
 * Inbound body for POST /api/tasks/{id}/messages.
 */
export interface TaskMessagePost {
  content: string;
}
export interface TaskMetricsResponse {
  task_id: number;
  timeline: TimelineEntry[];
  time_in_status_seconds: {
    [k: string]: number;
  };
}
export interface TimelineEntry {
  from?: string | null;
  to: string;
  message?: string;
  timestamp?: string | null;
}
/**
 * API shape for a trio_review_attempts row.
 */
export interface TrioReviewAttemptOut {
  id: number;
  task_id: number;
  cycle: number;
  ok: boolean;
  feedback: string;
  tool_calls: {
    [k: string]: unknown;
  }[];
  created_at: string;
}
/**
 * Body for ``PATCH /api/repos/{repo_id}/graph``.
 */
export interface UpdateRepoGraphRequest {
  analysis_branch: string;
}
export interface UsageSummary {
  plan: PlanRead;
  active_tasks: number;
  tasks_today: number;
  input_tokens_today: number;
  output_tokens_today: number;
}
/**
 * API shape for a verify attempt row.
 */
export interface VerifyAttemptOut {
  id: number;
  cycle: number;
  status: "pass" | "fail" | "error";
  boot_check?: ("pass" | "fail" | "skipped") | null;
  intent_check?: ("pass" | "fail") | null;
  intent_judgment?: string | null;
  tool_calls?:
    | {
        [k: string]: unknown;
      }[]
    | null;
  failure_reason?: string | null;
  log_tail?: string | null;
  started_at: string;
  finished_at?: string | null;
}
