import type { TaskData, TaskMessageData } from './api';

export type WSEvent =
  | { type: 'task_list'; tasks: TaskData[] }
  | { type: 'task_update'; task: TaskData }
  | { type: 'task_deleted'; task_id: number }
  | { type: 'message'; task_id: number; message: TaskMessageData }
  | { type: 'clarification_needed'; task_id: number; question: string }
  | { type: 'subtask_update'; task_id: number; subtasks: unknown }
  | { type: 'error'; message: string };

export type WSCommand =
  | { type: 'create_task'; title: string; description?: string; repo?: string }
  | { type: 'send_message'; task_id: number; content: string }
  | { type: 'approve'; task_id: number }
  | { type: 'reject'; task_id: number; feedback?: string }
  | { type: 'mark_done'; task_id: number }
  | { type: 'cancel_task'; task_id: number }
  | { type: 'delete_task'; task_id: number }
  | { type: 'set_priority'; task_id: number; priority: number }
  | { type: 'send_clarification'; task_id: number; answer: string };
