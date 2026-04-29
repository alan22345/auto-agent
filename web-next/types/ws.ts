import type { TaskData, TaskMessageData } from './api';

export interface MemoryRow {
  row_id: string;
  entity: string;
  entity_type: 'project' | 'concept' | 'person' | 'repo' | 'system';
  entity_status: 'new' | 'exists';
  kind: 'decision' | 'architecture' | 'gotcha' | 'status' | 'preference' | 'fact';
  content: string;
  conflicts: { existing_content: string }[];
  resolution: 'keep_existing' | 'replace' | 'keep_both' | null;
}

export interface FreeformConfig {
  repo_name: string;
  enabled: boolean;
  dev_branch: string;
  analysis_cron: string;
  auto_approve_suggestions: boolean;
  auto_start_tasks?: boolean;
  last_analysis_at?: string | null;
}

export type WSEvent =
  | { type: 'task_list'; tasks: TaskData[] }
  | { type: 'task_update'; task: TaskData }
  | { type: 'task_deleted'; task_id: number }
  | { type: 'message'; task_id: number; message: TaskMessageData }
  | { type: 'system'; message: string }
  | { type: 'error'; message: string }
  | { type: 'freeform_config_list'; configs: FreeformConfig[] }
  | { type: 'freeform_tasks_list'; tasks: TaskData[] }
  | { type: 'repo_created'; repo_name: string }
  | { type: 'memory_rows'; rows: MemoryRow[]; source_id?: string }
  | { type: 'memory_saved'; results: { ok: boolean; error?: string }[] }
  | { type: 'memory_error'; message: string };

export type WSCommand =
  | { type: 'send_message'; task_id: number; message: string }
  | { type: 'send_guidance'; task_id: number; message: string }
  | { type: 'approve'; task_id: number }
  | { type: 'reject'; task_id: number; feedback: string }
  | { type: 'load_freeform_config' }
  | { type: 'load_freeform_tasks' }
  | { type: 'create_repo'; description: string; org: string; loop: boolean }
  | {
      type: 'toggle_freeform';
      repo_name: string;
      enabled: boolean;
      dev_branch: string;
      analysis_cron: string;
      auto_approve_suggestions: boolean;
      auto_start_tasks?: boolean;
    }
  | { type: 'memory_extract'; source_id?: string; pasted_text?: string; context_hint?: string }
  | { type: 'memory_reextract'; source_id: string; note: string }
  | { type: 'memory_save'; rows: MemoryRow[]; source_id: string | null };
