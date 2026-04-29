'use client';
import { useMemo, useState } from 'react';
import { useTasks } from '@/hooks/useTasks';
import { TaskList } from '@/components/tasks/task-list';
import { NewTaskForm } from '@/components/tasks/new-task-form';
import { TaskActions } from '@/components/tasks/task-actions';
import { ChatArea } from '@/components/chat/chat-area';
import { MessageInput } from '@/components/chat/message-input';
import { ApprovalBar } from '@/components/chat/approval-bar';
import { ClarificationBar } from '@/components/chat/clarification-bar';
import { DoneBar } from '@/components/chat/done-bar';

const DONE_BAR_STATUSES = new Set(['awaiting_review', 'queued', 'blocked', 'failed']);

export default function TasksPage() {
  const { data: tasks = [] } = useTasks();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const selected = useMemo(() => tasks.find((t) => t.id === selectedId) || null, [tasks, selectedId]);
  const isFreeform = selected?.freeform_mode === true;

  return (
    <div className="flex h-full">
      <div className="flex w-80 flex-col border-r">
        <div className="flex-1 overflow-auto"><TaskList tasks={tasks} selectedId={selectedId} onSelect={setSelectedId} /></div>
        <NewTaskForm />
      </div>
      <div className="flex flex-1 flex-col">
        <div className="border-b p-3 text-sm font-medium">{selected?.title || 'Select a task'}</div>
        <ChatArea taskId={selectedId} />
        {selected && selected.status === 'awaiting_approval' && !isFreeform && <ApprovalBar taskId={selected.id} />}
        {selected && selected.status === 'awaiting_clarification' && !isFreeform && <ClarificationBar taskId={selected.id} />}
        {selected && DONE_BAR_STATUSES.has(selected.status) && !isFreeform && <DoneBar taskId={selected.id} />}
        {selected && <MessageInput task={selected} />}
        {selected && <TaskActions task={selected} />}
      </div>
    </div>
  );
}
