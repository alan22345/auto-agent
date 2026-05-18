'use client';
import { useMemo, useState } from 'react';
import {
  useDomainAdrs,
  useDomainGrillQuestion,
  useSubmitDomainGrillAnswer,
} from '@/hooks/useScaffoldArtefacts';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { ApiError } from '@/lib/api';

// ADR-018 Stage 8 — per-domain grill gate card. Each domain ADR is
// preceded by a grill round. When the parent task is parked in
// AWAITING_DOMAIN_GRILL the grill agent has written a pending question
// for some domain to ``.auto-agent/domain_grill_questions/<slug>.json``.
//
// We pick the active slug heuristically: the first domain entry from
// ``list_scaffold_domain_adrs`` whose grill question exists. The list
// endpoint surfaces every domain the root ADR declared, and the
// per-slug GET 404s when there's no pending question — so the first
// non-404 hit identifies the slug the grill is paused on. In practice
// the loop is serial, so there's only ever one pending question at a
// time.

export function DomainGrillCard({ taskId }: { taskId: number }) {
  const domains = useDomainAdrs(taskId);
  const [answer, setAnswer] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const submit = useSubmitDomainGrillAnswer();

  // The driver runs domains serially, so the first domain without an
  // approval verdict (i.e. the next one in line) is the one currently
  // being grilled. We pass that slug to the question query.
  const activeSlug = useMemo<string | null>(() => {
    const list = domains.data ?? [];
    if (list.length === 0) return null;
    const next = list.find((d) => !d.approval);
    return (next ?? list[0]).slug;
  }, [domains.data]);

  const questionQuery = useDomainGrillQuestion(taskId, activeSlug, !!activeSlug);

  async function onSubmit() {
    if (!answer.trim() || submitting || !activeSlug) return;
    setSubmitting(true);
    setLocalError(null);
    try {
      await submit.mutateAsync({ taskId, domainSlug: activeSlug, answer });
      setAnswer('');
    } catch (e) {
      setLocalError(
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : 'Failed to submit answer',
      );
    } finally {
      setSubmitting(false);
    }
  }

  if (!activeSlug) {
    return (
      <div className="rounded border bg-muted/30 p-3 text-xs text-muted-foreground">
        Waiting on the grill agent to surface a question…
      </div>
    );
  }

  const question = questionQuery.data?.question ?? '';

  return (
    <div className="rounded border bg-muted/30">
      <div className="border-b px-3 py-2">
        <div className="text-sm font-medium">
          Domain grill — Answer the agent
        </div>
        <div className="text-[11px] text-muted-foreground">
          The grill agent for domain{' '}
          <span className="font-mono">{activeSlug}</span> is pinning down
          this domain&apos;s scope before the architect writes its ADR.
        </div>
      </div>
      {question && (
        <div className="border-b px-3 py-2 text-xs leading-relaxed">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
            Pending question
          </div>
          <div className="whitespace-pre-wrap">{question}</div>
        </div>
      )}
      {!question && questionQuery.isLoading && (
        <div className="border-b px-3 py-2 text-xs text-muted-foreground">
          Loading question…
        </div>
      )}
      {!question && !questionQuery.isLoading && (
        <div className="border-b px-3 py-2 text-xs text-muted-foreground">
          No pending question for <span className="font-mono">{activeSlug}</span>{' '}
          yet — the agent is still grilling.
        </div>
      )}
      <div className="space-y-2 p-3">
        <Textarea
          placeholder="Type your answer to the agent's question…"
          rows={4}
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
          className="min-h-[88px] text-xs"
          disabled={submitting || !question}
        />
        {localError && (
          <div className="text-xs text-destructive">{localError}</div>
        )}
        <div className="flex items-center justify-end">
          <Button
            size="sm"
            onClick={onSubmit}
            disabled={submitting || !answer.trim() || !question}
          >
            {submitting ? 'Submitting…' : 'Submit answer'}
          </Button>
        </div>
      </div>
    </div>
  );
}
