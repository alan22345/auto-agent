'use client';
import { useEffect, useState } from 'react';
import { useWS } from './useWS';
import { wsClient } from '@/lib/ws';
import type { ChatEntry } from '@/types/ws';

const STREAM_TOOL_ICON: Record<string, string> = {
  file_read: '📖',
  file_write: '✏️',
  file_edit: '✏️',
  grep: '🔍',
  glob: '📂',
  bash: '⚡',
  git: '📦',
  test_runner: '🧪',
};

function formatEvent(eventType: string, payload: Record<string, unknown>): string {
  const summary = (payload?.summary as string) || (payload?.message as string) || '';
  return summary ? `${eventType}: ${summary}` : eventType;
}

export function useTaskMessages(taskId: number | null) {
  const [entries, setEntries] = useState<ChatEntry[]>([]);

  useEffect(() => {
    setEntries([]);
    if (taskId !== null) wsClient.send({ type: 'load_history', task_id: taskId });
  }, [taskId]);

  useWS('history', (e) => {
    if (e.task_id !== taskId) return;
    const merged: ChatEntry[] = [];
    for (const ent of e.entries || []) {
      const msg = ent.message || '';
      const userMatch = msg.match(/^\[([^\]]+)\] ([\s\S]+)$/);
      if (userMatch && ent.from_status === ent.to_status) {
        merged.push({ kind: 'user', sender: userMatch[1], message: userMatch[2], ts: ent.timestamp || '' });
      } else if (ent.to_status === 'awaiting_clarification' && msg) {
        merged.push({ kind: 'agent', sender: 'agent', message: msg, ts: ent.timestamp || '' });
      } else {
        merged.push({ kind: 'event', message: msg ? `[${ent.to_status}] ${msg}` : `[${ent.to_status}]`, ts: ent.timestamp || '' });
      }
    }
    for (const m of e.messages || []) {
      merged.push({
        kind: 'user',
        sender: m.sender,
        message: m.content,
        ts: m.created_at || '',
      });
    }
    merged.sort((a, b) => (a.ts || '').localeCompare(b.ts || ''));
    setEntries(merged);
  });

  useWS('user', (e) => {
    if (e.task_id !== taskId) return;
    const sender = e.display_name || e.username || null;
    setEntries((prev) => [...prev, { kind: 'user', sender, message: e.message, ts: new Date().toISOString() }]);
  });

  useWS('guidance_sent', (e) => {
    if (e.task_id !== taskId) return;
    const sender = e.display_name || e.username || null;
    setEntries((prev) => [...prev, { kind: 'user', sender, message: `[Guidance]: ${e.message}`, ts: new Date().toISOString() }]);
  });

  useWS('agent_stream', (e) => {
    if (e.task_id !== taskId) return;
    let line = '';
    if (e.tool) {
      const icon = STREAM_TOOL_ICON[e.tool] || '🔧';
      line = `${icon} ${e.tool} ${e.args_preview || ''}`.trim();
    } else if (e.text) {
      const preview = e.text.length > 300 ? e.text.slice(0, 300) + '…' : e.text;
      line = `💭 ${preview}`;
    }
    if (line) setEntries((prev) => [...prev, { kind: 'stream', message: line, ts: new Date().toISOString() }]);
  });

  useWS('event', (e) => {
    if (taskId === null || e.task_id !== taskId) return;
    const ts = new Date().toISOString();
    if (e.event_type === 'task.clarification_needed') {
      const question = (e.payload?.question as string) || '';
      if (question) {
        setEntries((prev) => [...prev, { kind: 'agent', sender: 'agent', message: question, ts }]);
        return;
      }
    }
    setEntries((prev) => [...prev, {
      kind: 'event',
      message: formatEvent(e.event_type, e.payload || {}),
      ts,
    }]);
  });

  useWS('system', (e) => {
    setEntries((prev) => [...prev, { kind: 'system', message: e.message, ts: new Date().toISOString() }]);
  });

  useWS('error', (e) => {
    setEntries((prev) => [...prev, { kind: 'error', message: e.message, ts: new Date().toISOString() }]);
  });

  return entries;
}
