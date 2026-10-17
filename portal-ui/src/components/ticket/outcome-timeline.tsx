import { StepEyebrow } from '@/components/page-header'
import { Card } from '@/components/ui/card'
import { when } from '@/lib/format'
import type { TimelineKind, TimelineStep } from '@/lib/timeline'
import { cn } from '@/lib/utils'

const MARKER: Record<TimelineKind, { marker: string; line: string; title: string; note: string }> = {
  done: { marker: 'bg-primary text-white border-primary', line: 'bg-primary', title: 'text-foreground', note: 'text-muted-foreground' },
  warn: { marker: 'bg-warn-bg text-warn-fg border-warn-dot', line: 'bg-warn-dot', title: 'text-foreground', note: 'text-warn-fg' },
  current: { marker: 'bg-card text-warn-fg border-warn-dot', line: 'bg-border', title: 'text-warn-fg', note: 'text-warn-fg' },
  queued: { marker: 'bg-card text-ok-fg border-primary border-dashed', line: 'bg-border', title: 'text-ok-fg', note: 'text-ok-fg' },
  bad: { marker: 'bg-bad-bg text-bad-fg border-bad-dot', line: 'bg-border', title: 'text-bad-fg', note: 'text-bad-fg' },
  pending: { marker: 'bg-card text-faint border-input', line: 'bg-border', title: 'text-muted-foreground', note: 'text-muted-foreground' },
}

export function OutcomeTimeline({ steps, note }: { steps: TimelineStep[]; note: string }) {
  return (
    <Card className="gap-4 px-5 pt-4 pb-5">
      <div className="flex flex-col gap-1.5">
        <StepEyebrow n={5}>Where it stands</StepEyebrow>
        <h2 className="m-0 text-base font-semibold">Outcome timeline</h2>
      </div>
      <ol className="m-0 flex list-none flex-wrap gap-y-4 p-0">
        {steps.map((s, i) => {
          const m = MARKER[s.kind]
          const last = i === steps.length - 1
          return (
            <li key={`${s.label}-${i}`} className="flex min-w-0 flex-[1_1_150px] flex-col gap-2" aria-current={s.kind === 'current' ? 'step' : undefined}>
              <div className="flex items-center">
                <span aria-hidden className={cn('flex size-[26px] shrink-0 items-center justify-center rounded-full border-2 font-mono text-xs font-semibold', m.marker)}>
                  {i + 1}
                </span>
                {!last ? <span aria-hidden className={cn('mx-2 h-0.5 flex-1', m.line)} /> : null}
              </div>
              <div className="flex flex-col gap-0.5 pr-3">
                <span className={cn('font-semibold', m.title)}>{s.label}</span>
                <span className="font-mono text-xs text-muted-foreground">{s.at ? when(s.at) : '—'}</span>
                <span className={cn('text-xs break-words', m.note)}>{s.note}</span>
              </div>
            </li>
          )
        })}
      </ol>
      <p className="m-0 rounded-lg bg-info-bg px-3.5 py-2.5 text-[13px] text-info-fg">{note}</p>
    </Card>
  )
}
