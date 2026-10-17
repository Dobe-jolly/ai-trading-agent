import * as React from 'react'
import { Check, ChevronDown, CircleMinus, Paperclip, TriangleAlert } from 'lucide-react'

import { StepEyebrow } from '@/components/page-header'
import { CheckChip, StatusPill } from '@/components/status-pill'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import type { DecisionDetail, DeskVocab } from '@/lib/api/types'
import { argsLine, pretty, type Lookup } from '@/lib/transcript'
import { humanize } from '@/lib/vocab'
import { cn } from '@/lib/utils'

const ICON = {
  found: { Icon: Check, className: 'bg-ok-bg text-ok-fg' },
  noMatch: { Icon: CircleMinus, className: 'bg-muted text-secondary-foreground' },
  lookupFailed: { Icon: TriangleAlert, className: 'bg-bad-bg text-bad-fg' },
}

export function EvidencePanel({ decision, lookups, vocab }: { decision: DecisionDetail; lookups: Lookup[]; vocab?: DeskVocab }) {
  const counts = {
    found: lookups.filter((e) => e.status === 'found').length,
    noMatch: lookups.filter((e) => e.status === 'noMatch').length,
    failed: lookups.filter((e) => e.status === 'lookupFailed').length,
  }
  const summary = [counts.found && `${counts.found} found`, counts.noMatch && `${counts.noMatch} no matching record`, counts.failed && `${counts.failed} lookup failed`]
    .filter(Boolean)
    .join(' · ')
  const statuses = Object.entries(decision.statuses ?? {})
  const attachments = decision.attachments ?? []

  return (
    <Card className="min-w-0 flex-[1_1_560px] self-start">
      <CardHeader>
        <StepEyebrow n={2}>What the agent found</StepEyebrow>
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="m-0 text-base font-semibold">Evidence · {lookups.length} lookup{lookups.length === 1 ? '' : 's'}</h2>
          {summary ? <span className="text-xs text-muted-foreground">{summary}</span> : null}
        </div>
        <p className="m-0 text-xs text-muted-foreground">
          No matching record = the lookup worked and found nothing. Lookup failed = we could not check, so the answer is unknown. Numbers are masked.
        </p>
      </CardHeader>

      {statuses.length ? (
        <div className="flex flex-col gap-1.5 border-b border-border-soft px-5 py-3">
          <span className="text-xs text-muted-foreground">What the checks showed</span>
          <div className="flex flex-wrap gap-1.5">
            {statuses.map(([subject, status]) => (
              <CheckChip key={subject} vocab={vocab} status={status} subject={subject} />
            ))}
          </div>
        </div>
      ) : null}

      {lookups.length ? (
        <ul className="m-0 list-none p-0">
          {lookups.map((l) => (
            <LookupRow key={l.id} lookup={l} />
          ))}
        </ul>
      ) : (
        <CardContent className="text-[13px] text-muted-foreground">
          {decision.category === 'rules_fallback'
            ? 'The model was not reachable, so the rules decided from the standard checks. No lookups were recorded.'
            : 'No lookups were recorded for this decision.'}
        </CardContent>
      )}

      {decision.evidence?.length ? (
        <div className="flex flex-col gap-1.5 border-t border-border-soft px-5 py-3">
          <span className="text-xs text-muted-foreground">What the agent relied on</span>
          <ul className="m-0 flex flex-col gap-1 pl-5 text-[13px]">
            {decision.evidence.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {attachments.length ? (
        <div className="flex flex-col gap-1.5 border-t border-border-soft px-5 py-3">
          <span className="text-xs text-muted-foreground">Attachments</span>
          <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-[13px]">
            {attachments.map((a, i) => (
              <li key={i} className="flex items-start gap-2">
                <Paperclip aria-hidden className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                <span>
                  <span className="font-medium">{a.name ?? 'Attachment'}</span>
                  {a.status === 'read' ? <span className="text-secondary-foreground"> — {a.summary ?? 'nothing relevant found'}</span> : <span className="text-muted-foreground"> — {a.note ?? 'not read'}</span>}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  )
}

function LookupRow({ lookup }: { lookup: Lookup }) {
  const failed = lookup.status === 'lookupFailed'
  const [open, setOpen] = React.useState(failed)
  const { Icon, className } = ICON[lookup.status]
  const args = argsLine(lookup.input)

  return (
    <li className={cn('border-b border-border-soft px-5 py-3.5 last:border-0', failed && 'bg-bad-row')}>
      <Collapsible open={open} onOpenChange={setOpen} className="flex flex-col gap-2.5">
        <div className="flex items-start gap-3">
          <span aria-hidden className={cn('flex size-7 shrink-0 items-center justify-center rounded-[7px]', className)}>
            <Icon className="size-[15px]" strokeWidth={2.4} />
          </span>
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5">
              <span className="min-w-0 font-semibold">
                {humanize(lookup.name)}
                {args ? <span className="font-mono text-xs font-normal break-all text-muted-foreground"> · {args}</span> : null}
              </span>
              <StatusPill status={lookup.status} />
            </div>
            <span className={cn('text-[13px] break-words', failed ? 'text-bad-fg' : 'text-secondary-foreground')}>{lookup.summary}</span>
            <CollapsibleTrigger asChild>
              <Button variant="link" size="sm" className="mt-0.5 h-8 gap-1 self-start">
                <ChevronDown aria-hidden className={cn('size-[15px] transition-transform', open && 'rotate-180')} />
                {open ? 'Hide what was sent and what came back' : 'Show what was sent and what came back'}
              </Button>
            </CollapsibleTrigger>
          </div>
        </div>
        <CollapsibleContent>
          <div className="ml-10 grid gap-2 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
            <div className="flex min-w-0 flex-col gap-1">
              <span className="text-[11px] font-semibold tracking-[0.06em] text-muted-foreground uppercase">Sent</span>
              <pre className="m-0 overflow-x-auto rounded-lg bg-code px-3.5 py-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-code-foreground">
                {JSON.stringify(lookup.input, null, 2)}
              </pre>
            </div>
            <div className="flex min-w-0 flex-col gap-1">
              <span className="text-[11px] font-semibold tracking-[0.06em] text-muted-foreground uppercase">Came back</span>
              <pre className="m-0 max-h-[420px] overflow-auto rounded-lg bg-code px-3.5 py-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-code-foreground">
                {pretty(lookup.result)}
              </pre>
            </div>
          </div>
        </CollapsibleContent>
      </Collapsible>
    </li>
  )
}
