import { ArrowRight } from 'lucide-react'

import { StepEyebrow } from '@/components/page-header'
import { StatusPill } from '@/components/status-pill'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import type { DecisionDetail } from '@/lib/api/types'
import { ACTION_STATUS, actionLabel } from '@/lib/vocab'

/** What the rules in code did to the agent's proposal, in plain words. */
export function RulesPanel({ decision: d }: { decision: DecisionDetail }) {
  const notes = d.guardrail_notes ?? []
  const why = d.approval?.why ?? (d.approval_note?.startsWith('waiting: ') ? d.approval_note.slice(9) : null)
  const changed = d.guardrail_changed && d.proposed_action && d.final_action
  const count = notes.length + (why ? 1 : 0)
  return (
    <Card>
      <CardHeader>
        <StepEyebrow n={4}>{changed ? 'Why it changed' : 'What the rules said'}</StepEyebrow>
        <h2 className="m-0 text-base font-semibold">Rules · {count ? `${count} applied` : 'nothing applied'}</h2>
      </CardHeader>
      <CardContent className="flex flex-col gap-3.5">
        {changed ? (
          <div className="flex flex-wrap items-center gap-2.5 rounded-[10px] border border-warn-line bg-warn-panel px-4 py-3.5 text-sm">
            <span className="inline-flex items-center gap-1.5">
              <span className="text-muted-foreground">Proposed</span>
              <StatusPill status={ACTION_STATUS[d.proposed_action!]} label={actionLabel(d.proposed_action)} />
            </span>
            <ArrowRight aria-hidden className="size-[18px] text-warn-fg" />
            <span className="inline-flex min-w-0 flex-wrap items-center gap-1.5">
              <span className="text-muted-foreground">Rule</span>
              <strong className="font-semibold">{notes[0] ?? 'a rule in code'}</strong>
            </span>
            <ArrowRight aria-hidden className="size-[18px] text-warn-fg" />
            <span className="inline-flex items-center gap-1.5">
              <span className="text-muted-foreground">Changed to</span>
              <StatusPill status={ACTION_STATUS[d.final_action!]} label={actionLabel(d.final_action)} />
            </span>
          </div>
        ) : !count ? (
          <p className="m-0 text-[13px] text-muted-foreground">No rule changed, blocked or held this proposal. The rules in code checked it and let it through.</p>
        ) : null}

        {count ? (
          <ul className="m-0 flex list-none flex-col p-0">
            {notes.map((note, i) => (
              <li key={i} className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b border-border-soft py-3 last:border-0">
                <span className="min-w-0 flex-[1_1_360px] text-[13px]">{note}</span>
                <StatusPill status={changed && i === 0 ? 'ruleChanged' : 'ruleNote'} className="font-semibold" />
              </li>
            ))}
            {why ? (
              <li className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b border-border-soft py-3 last:border-0">
                <span className="min-w-0 flex-[1_1_360px] text-[13px]">
                  <span className="font-semibold">Held for approval:</span> {why}
                </span>
                <StatusPill status="ruleHeld" className="font-semibold" />
              </li>
            ) : null}
          </ul>
        ) : null}
      </CardContent>
    </Card>
  )
}
