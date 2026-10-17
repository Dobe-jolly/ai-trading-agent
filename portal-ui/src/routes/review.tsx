import * as React from 'react'
import { createFileRoute } from '@tanstack/react-router'
import { Check, CircleX } from 'lucide-react'

import { DeskErrors, Empty, ErrorNote, Loading, TicketLink } from '@/components/data-state'
import { PageHeader } from '@/components/page-header'
import { StatusPill } from '@/components/status-pill'
import { Button } from '@/components/ui/button'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Label, NativeSelect } from '@/components/ui/input'
import { useDeskQueries, useReview } from '@/lib/api/hooks'
import type { Action, DecisionSummary, Quality, ServiceDesk } from '@/lib/api/types'
import { pct, useNow, when } from '@/lib/format'
import { usePortal } from '@/lib/portal'
import { ACTIONS, ACTION_LABEL, actionLabel, decisionOutcome } from '@/lib/vocab'
import { cn } from '@/lib/utils'

export const Route = createFileRoute('/review')({
  head: () => ({ meta: [{ title: 'Review · Agent Supervision' }] }),
  component: ReviewPage,
})

const FLAG_BELOW = 0.85
const REOPEN_FLAG = 0.05
const PAGE = 25

function ReviewPage() {
  const { selected } = usePortal()
  const data = useDeskQueries<Quality>(selected, 'quality', { days: 30 })
  const [last, setLast] = React.useState<string | null>(null)
  const [shown, setShown] = React.useState(PAGE)
  const items = data.results
    .flatMap((r) => (r.data?.needs_review ?? []).map((d) => ({ ...d, desk: r.desk })))
    .sort((a, b) => Number(b.guardrail_changed) - Number(a.guardrail_changed) || (a.confidence ?? 1) - (b.confidence ?? 1) || b.created_at.localeCompare(a.created_at))
  const corrections = data.results.flatMap((r) => (r.data?.corrections ?? []).map((c) => ({ ...c, desk: r.desk })))

  return (
    <>
      <PageHeader
        title="Review"
        description="Decisions worth a second look, for people to mark right or wrong. Viewers and Admins can both review."
        actions={<span className="text-[13px] text-muted-foreground"><strong className="font-semibold text-foreground">{items.length}</strong> waiting for a review</span>}
      />
      <DeskErrors errors={data.errors} />

      <section aria-label="Accuracy by desk, last 30 days" className="flex flex-wrap gap-3">
        {data.results.map(({ desk, data: q }) => (
          <div key={desk.key} className="flex min-w-0 flex-[1_1_180px] flex-col gap-2 rounded-xl border border-border bg-card px-4 py-3.5">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-[13px] font-semibold">{desk.title}</span>
              <span className={cn('font-mono text-xl font-semibold', q?.accuracy != null && q.accuracy < FLAG_BELOW ? 'text-warn-fg' : 'text-ok-fg')}>
                {q ? pct(q.accuracy) : '…'}
              </span>
            </div>
            <div aria-hidden className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div className={cn('h-full', q?.accuracy != null && q.accuracy < FLAG_BELOW ? 'bg-warn-dot' : 'bg-primary')} style={{ width: `${Math.round((q?.accuracy ?? 0) * 100)}%` }} />
            </div>
            <span className="text-xs text-muted-foreground">{q ? (q.reviewed ? `${q.correct} of ${q.reviewed} marked correct` : 'Nothing reviewed yet') : 'Loading…'}</span>
            {q?.close_precision && q.reopens ? (
              <dl className="m-0 grid grid-cols-[1fr_auto] gap-x-3 gap-y-0.5 border-t border-border-soft pt-2 text-xs">
                <dt className="text-muted-foreground">Closes marked right</dt>
                <dd className={cn('m-0 text-right font-mono', q.close_precision.precision != null && q.close_precision.precision < FLAG_BELOW && 'text-warn-fg')}>
                  {q.close_precision.reviewed ? `${pct(q.close_precision.precision)} · ${q.close_precision.correct}/${q.close_precision.reviewed}` : '—'}
                </dd>
                <dt className="text-muted-foreground">Reopened after it closed</dt>
                <dd className={cn('m-0 text-right font-mono', q.reopens.rate != null && q.reopens.rate > REOPEN_FLAG && 'text-warn-fg')}>
                  {q.reopens.checked ? `${pct(q.reopens.rate)} · ${q.reopens.reopened}/${q.reopens.checked}` : '—'}
                </dd>
              </dl>
            ) : null}
          </div>
        ))}
      </section>
      <p className="-mt-2 mb-0 text-xs text-muted-foreground">
        Accuracy = reviewed decisions marked correct, last 30 days; the latest review of a decision counts. Below {Math.round(FLAG_BELOW * 100)}% is flagged. Closes marked right = of the
        closes people reviewed, how many were right. Reopened = of the tickets the agent really closed, how many osTicket shows reopened since (checked every few hours, for 30 days); above{' '}
        {Math.round(REOPEN_FLAG * 100)}% is flagged.
      </p>

      {corrections.length ? (
        <Card>
          <CardHeader className="flex-row flex-wrap items-center justify-between">
            <CardTitle>Where reviewers disagreed</CardTitle>
            <CardDescription>What the agent did, and what reviewers said it should have done</CardDescription>
          </CardHeader>
          <ul className="m-0 list-none p-0">
            {corrections.map((c, i) => (
              <li key={i} className="flex flex-wrap items-center gap-2 border-b border-border-soft px-5 py-2.5 text-[13px] last:border-0">
                <span className="w-[140px] shrink-0 text-muted-foreground">{c.desk.title}</span>
                <span>
                  {actionLabel(c.agent)} → should have been <strong className="font-semibold">{actionLabel(c.expected).toLowerCase()}</strong>
                </span>
                <span className="ml-auto font-mono text-xs text-muted-foreground">×{c.count}</span>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      <p role="status" className={cn('m-0 rounded-lg bg-ok-bg px-3.5 py-2.5 text-[13px] text-ok-fg', !last && 'sr-only')}>
        {last ?? ''}
      </p>

      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between">
          <CardTitle>Needs a review</CardTitle>
          <CardDescription>Changed by a rule, low confidence, decided by the rules, escalated or asked the branch — not yet reviewed</CardDescription>
        </CardHeader>
        {data.isLoading && !items.length ? (
          <Loading />
        ) : items.length ? (
          <>
            <ul className="m-0 list-none p-0">
              {items.slice(0, shown).map((d) => (
                <ReviewItem key={`${d.desk.key}-${d.id}`} d={d} onDone={setLast} />
              ))}
            </ul>
            {items.length > shown ? (
              <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border-soft px-5 py-3 text-[13px] text-muted-foreground">
                <span>
                  Showing {shown} of {items.length}, rule changes and lowest confidence first.
                </span>
                <Button variant="outline" size="sm" onClick={() => setShown((n) => n + PAGE)}>
                  Show {Math.min(PAGE, items.length - shown)} more
                </Button>
              </div>
            ) : null}
          </>
        ) : (
          <Empty title="Nothing waiting for a review">Every decision that needed a second look in the last 30 days has one.</Empty>
        )}
      </Card>
    </>
  )
}

function ReviewItem({ d, onDone }: { d: DecisionSummary & { desk: ServiceDesk }; onDone: (message: string) => void }) {
  const now = useNow()
  const review = useReview()
  const [choosing, setChoosing] = React.useState(false)
  const [expected, setExpected] = React.useState<Action>(d.final_action === 'escalate' ? 'reply_and_close' : 'escalate')
  const [done, setDone] = React.useState<null | { verdict: 'correct' | 'incorrect'; expected?: Action }>(null)
  const outcome = decisionOutcome(d)
  // The reviewed item drops out of the list when it refetches, so the page-level message is set from the promise,
  // which still resolves after this item has gone (per-call onSuccess would not).
  const save = (verdict: 'correct' | 'incorrect') =>
    review
      .mutateAsync({ desk: d.desk.key, decisionId: d.id, verdict, expected: verdict === 'incorrect' ? expected : null })
      .then(() => {
        setDone({ verdict, expected: verdict === 'incorrect' ? expected : undefined })
        setChoosing(false)
        onDone(
          verdict === 'correct'
            ? `Marked #${d.ticket_number} correct.`
            : `Marked #${d.ticket_number} incorrect — expected ${actionLabel(expected).toLowerCase()}.`,
        )
      })
      .catch(() => {})

  return (
    <li className="flex flex-wrap items-center gap-x-5 gap-y-3 border-b border-border-soft px-5 py-3.5 last:border-0">
      <TicketLink number={d.ticket_number} desk={d.desk.key} decision={d.id} from="review" className="w-[76px] shrink-0" />
      <div className="flex min-w-0 flex-[1_1_360px] flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill status={outcome.status} label={outcome.label} />
          {d.guardrail_changed ? <span className="text-xs text-warn-fg">proposed {actionLabel(d.proposed_action).toLowerCase()}, changed by a rule</span> : null}
        </div>
        <span className="text-xs text-muted-foreground">
          {d.desk.title} · <span className="font-mono">{when(d.created_at, now)}</span> · confidence {pct(d.confidence)}
          {d.guardrail_notes[0] ? ` · ${d.guardrail_notes[0]}` : ''}
        </span>
        <ErrorNote error={review.error} className="mt-1" />
      </div>
      {done ? (
        <StatusPill
          status={done.verdict === 'correct' ? 'reviewCorrect' : 'reviewIncorrect'}
          label={done.verdict === 'correct' ? 'Marked correct' : `Marked incorrect — expected ${actionLabel(done.expected).toLowerCase()}`}
        />
      ) : choosing ? (
        <div className="flex flex-wrap items-end gap-2">
          <Label className="text-xs">
            Should have been
            <NativeSelect value={expected} onChange={(e) => setExpected(e.target.value as Action)} className="h-10">
              {ACTIONS.filter((a) => a !== d.final_action).map((a) => (
                <option key={a} value={a}>
                  {ACTION_LABEL[a]}
                </option>
              ))}
            </NativeSelect>
          </Label>
          <Button onClick={() => save('incorrect')} disabled={review.isPending}>
            Save
          </Button>
          <Button variant="ghost" onClick={() => setChoosing(false)}>
            Cancel
          </Button>
        </div>
      ) : (
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => save('correct')} disabled={review.isPending}>
            <Check aria-hidden strokeWidth={2.4} />
            Correct
          </Button>
          <Button variant="destructive" onClick={() => setChoosing(true)} disabled={review.isPending}>
            <CircleX aria-hidden />
            Incorrect
          </Button>
        </div>
      )}
    </li>
  )
}
