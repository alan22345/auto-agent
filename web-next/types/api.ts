/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export type RiskLevel = "low" | "medium" | "high";

/**
 * CI status for a commit.
 */
export interface CIStatus {
  sha: string;
  state: "success" | "failure" | "pending" | "error";
  message?: string;
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
  last_analysis_at?: string | null;
  created_at?: string | null;
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
export interface RepoResponse {
  id: number;
  name: string;
  url: string;
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
 * Typed representation of a PO suggestion.
 */
export interface SuggestionData {
  id: number;
  repo_name?: string | null;
  title: string;
  description?: string;
  rationale?: string;
  category?: string;
  priority?: number;
  status?: string;
  task_id?: number | null;
  created_at?: string | null;
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
  created_at?: string | null;
  created_by_user_id?: number | null;
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
