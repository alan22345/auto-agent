'use client';
import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  useIntent,
  useSubmitIntentGrillAnswer,
} from '@/hooks/useScaffoldArtefacts';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { ApiError } from '@/lib/api';

function errorMessage(e: unknown): string {
  return e instanceof ApiError
    ? e.detail
    : e instanceof Error
      ? e.message
      : 'Failed to submit answer';
}

async function submitIntentGrillAnswer(args: {
  taskId: number;
  answer: string;
  submit: ReturnType<typeof useSubmitIntentGrillAnswer>;
  setAnswer: (v: string) => void;
  setSubmitting: (v: boolean) => void;
  setLocalError: (v: string | null) => void;
}): Promise<void> {
  const { taskId, answer, submit, setAnswer, setSubmitting, setLocalError } =
    args;
  setSubmitting(true);
  setLocalError(null);
  try {
    await submit.mutateAsync({ taskId, answer });
    setAnswer('');
  } catch (e) {
    setLocalError(errorMessage(e));
  } finally {
    setSubmitting(false);
  }
}

// ADR-018 Phase A gate card — the intent-grill agent pauses on a
// question and waits for the user to answer. The answer is written to
// ``.auto-agent/intent_grill_answer.json`` and the scaffold driver is
// re-invoked. Any intent.md the agent has already written (e.g. from a
// previous round) is rendered as context so the user can see what's
// been captured so far.

export function IntentGrillCard({ taskId }: { taskId: number }) {
  const intentQuery = useIntent(taskId);
  const submit = useSubmitIntentGrillAnswer();
  const [answer, setAnswer] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  async function onSubmit() {
    if (!answer.trim() || submitting) return;
    await submitIntentGrillAnswer({
      taskId,
      answer,
      submit,
      setAnswer,
      setSubmitting,
      setLocalError,
    });
  }

  const intentMd = intentQuery.data?.markdown ?? '';

  return (
    <div className="rounded border bg-muted/30">
      <div className="border-b px-3 py-2">
        <div className="text-sm font-medium">
          Intent grill — Answer the agent
        </div>
        <div className="text-[11px] text-muted-foreground">
          The agent is grilling you to pin down what to build. Answer its
          pending question to advance the scaffold.
        </div>
      </div>
      {intentMd && (
        <div className="border-b">
          <div className="px-3 pt-2 text-[10px] uppercase tracking-wide text-muted-foreground">
            intent.md (captured so far)
          </div>
          <div className="prose prose-sm max-h-[30vh] max-w-none overflow-y-auto px-3 pb-2 text-xs leading-relaxed">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{intentMd}</ReactMarkdown>
          </div>
        </div>
      )}
      <div className="space-y-2 p-3">
        <Textarea
          placeholder="Type your answer to the agent's question…"
          rows={4}
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
          className="min-h-[88px] text-xs"
          disabled={submitting}
        />
        {localError && (
          <div className="text-xs text-destructive">{localError}</div>
        )}
        <div className="flex items-center justify-end">
          <Button
            size="sm"
            onClick={onSubmit}
            disabled={submitting || !answer.trim()}
          >
            {submitting ? 'Submitting…' : 'Submit answer'}
          </Button>
        </div>
      </div>
    </div>
  );
}
