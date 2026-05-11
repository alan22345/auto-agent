'use client';
import { useMemo, useState } from 'react';
import { Plus } from 'lucide-react';
import { useTasks } from '@/hooks/useTasks';
import { TaskList } from '@/components/tasks/task-list';
import { NewTaskForm } from '@/components/tasks/new-task-form';
import { TaskActions } from '@/components/tasks/task-actions';
import { TaskDetailPanel } from '@/components/tasks/task-detail-panel';
import {
  TaskFilterBar,
  applyTaskFilter,
  type TaskFilter,
} from '@/components/tasks/task-filter-bar';
import { ChatArea } from '@/components/chat/chat-area';
import { MessageInput } from '@/components/chat/message-input';
import { Button } from '@/components/ui/button';

export default function TasksPage() {
  const { data: tasks = [] } = useTasks();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [filter, setFilter] = useState<TaskFilter>({ status: 'all', repo: 'all' });

  const selected = useMemo(
    () => tasks.find((t) => t.id === selectedId) || null,
    [tasks, selectedId],
  );

  const visibleTasks = useMemo(() => applyTaskFilter(tasks, filter), [tasks, filter]);

  return (
    <div className="flex h-full">
      <div className="flex w-80 flex-col border-r">
        <div className="border-b p-2">
          <Button
            variant={selected ? 'outline' : 'secondary'}
            className="w-full justify-start"
            onClick={() => setSelectedId(null)}
          >
            <Plus className="h-4 w-4" />
            New Task
          </Button>
        </div>
        <TaskFilterBar tasks={tasks} filter={filter} onChange={setFilter} />
        <div className="flex-1 overflow-auto">
          <TaskList tasks={visibleTasks} selectedId={selectedId} onSelect={setSelectedId} />
        </div>
      </div>
      <div className="flex flex-1 flex-col">
        {selected ? (
          <>
            <div className="border-b p-3 text-sm font-medium">{selected.title}</div>
            <TaskDetailPanel task={selected} />
            <ChatArea taskId={selectedId} />
            <MessageInput task={selected} />
            <TaskActions task={selected} />
          </>
        ) : (
          <NewTaskForm />
        )}
      </div>
    </div>
  );
}
