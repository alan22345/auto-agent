/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export type RiskLevel = "low" | "medium" | "high";

export interface AffectedRoute {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  label: string;
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
 * CI status for a commit.
 */
export interface CIStatus {
  sha: string;
  state: "success" | "failure" | "pending" | "error";
  message?: string;
}
export interface ChangeEmailRequest {
  email: string;
}
export interface ClassificationResult {
  classification: "simple" | "complex" | "simple_no_code";
  reasoning?: string;
  estimated_files?: number;
  risk?: RiskLevel;
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
 * One edge in the graph.
 *
 * Phase 2 only emits edges with ``source_kind="ast"``. Phase 3 will add
 * ``source_kind="llm"`` edges using the same fields — no schema change.
 * ``boundary_violation`` is reserved for Phase 5 and is always ``False``
 * in Phase 2 output.
 */
export interface Edge {
  source: string;
  target: string;
  kind: "calls" | "imports" | "inherits" | "http";
  evidence: EdgeEvidence;
  source_kind: "ast" | "llm";
  boundary_violation?: boolean;
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
 * Optional body for ``POST /api/repos/{repo_id}/graph``.
 *
 * All fields optional — the endpoint defaults the analysis branch to the
 * repo's ``default_branch`` if the caller omits it.
 */
export interface EnableRepoGraphRequest {
  analysis_branch?: string | null;
}
export interface FeedbackSummary {
  total_outcomes?: number;
  approved?: number;
  rejected?: number;
  approval_rate?: number;
  avg_review_rounds?: number;
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
export interface IntentVerdict {
  ok: boolean;
  reasoning: string;
  tool_calls?: {
    [k: string]: unknown;
  }[];
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
}
/**
 * Full graph analysis output — the payload stored in
 * ``RepoGraph.graph_json`` and surfaced to the UI / agent tool.
 */
export interface RepoGraphBlob {
  commit_sha: string;
  generated_at: string;
  analyser_version: string;
  areas: AreaStatus[];
  nodes: Node[];
  edges: Edge[];
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
  priority?: number;
  subtasks?:
    | {
        [k: string]: unknown;
      }[]
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
