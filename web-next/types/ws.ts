import type { TaskData, TaskMessageData } from './api';

export type WSEvent =
  | { type: 'task_list'; tasks: TaskData[] }
  | { type: 'task_update'; task: TaskData }
  | { type: 'task_deleted'; task_id: number }
  | { type: 'message'; task_id: number; message: TaskMessageData }
  | { type: 'system'; message: string }
  | { type: 'error'; message: string };

export type WSCommand =
  | { type: 'send_message'; task_id: number; message: string }
  | { type: 'send_guidance'; task_id: number; message: string }
  | { type: 'approve'; task_id: number }
  | { type: 'reject'; task_id: number; feedback: string };
