'use client';
import { useEffect, useState } from 'react';
import { useWS } from './useWS';
import type { TaskMessageData } from '@/types/api';

export function useTaskMessages(taskId: number | null) {
  const [messages, setMessages] = useState<TaskMessageData[]>([]);
  useEffect(() => { setMessages([]); }, [taskId]);
  useWS('message', (e) => {
    if (e.task_id !== taskId) return;
    setMessages((prev) => [...prev, e.message]);
  });
  return messages;
}
