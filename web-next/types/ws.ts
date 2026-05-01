import type {
  MemoryEntityDetail,
  MemoryEntitySummary,
  TaskData,
  TaskMessageData,
} from './api';

export interface ChatEntry {
  kind: 'user' | 'system' | 'event' | 'stream' | 'error';
  message: string;
  sender?: string | null;
  ts?: string;
}

export interface TaskHistoryItem {
  message?: string | null;
  from_status?: string | null;
  to_status: string;
  timestamp?: string | null;
}

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
  | {
      type: 'history';
      task_id: number;
      entries: TaskHistoryItem[];
      messages: TaskMessageData[];
    }
  | {
      type: 'event';
      task_id?: number;
      event_type: string;
      payload?: Record<string, unknown>;
    }
  | {
      type: 'agent_stream';
      task_id: number;
      tool?: string;
      args_preview?: string;
      text?: string;
    }
  | { type: 'user'; task_id?: number; message: string; username?: string; display_name?: string }
  | { type: 'guidance_sent'; task_id: number; message: string; username?: string; display_name?: string }
  | { type: 'system'; message: string }
  | { type: 'error'; message: string }
  | { type: 'freeform_config_list'; configs: FreeformConfig[] }
  | { type: 'freeform_tasks_list'; tasks: TaskData[] }
  | { type: 'repo_created'; repo_name: string }
  | { type: 'memory_rows'; rows: MemoryRow[]; source_id?: string }
  | { type: 'memory_saved'; results: { ok: boolean; error?: string }[] }
  | { type: 'memory_error'; message: string }
  | {
      type: 'memory_search_results';
      query: string;
      entities: MemoryEntitySummary[];
    }
  | {
      type: 'memory_entity';
      include_superseded: boolean;
      detail: MemoryEntityDetail;
    }
  | { type: 'memory_fact_corrected'; fact_id: string; new_fact_id: string }
  | { type: 'memory_fact_deleted'; fact_id: string };

export type WSCommand =
  | { type: 'load_history'; task_id: number }
  | { type: 'refresh' }
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
  | { type: 'memory_save'; rows: MemoryRow[]; source_id: string | null }
  | { type: 'memory_search'; query: string }
  | { type: 'memory_get_entity'; entity: string; include_superseded?: boolean }
  | { type: 'memory_correct_fact'; fact_id: string; content: string }
  | { type: 'memory_delete_fact'; fact_id: string };
