import type { Action, Overview } from '@/lib/api/types'

export const SERIES: { key: Action; label: string; className: string }[] = [
  { key: 'reply_and_close', label: 'Replied & closed', className: 'bg-chart-closed' },
  { key: 'escalate', label: 'Escalated', className: 'bg-chart-escalated' },
  { key: 'request_info', label: 'Asked branch', className: 'bg-chart-waiting' },
  { key: 'move', label: 'Moved', className: 'bg-chart-moved' },
  { key: 'no_action', label: 'No action', className: 'bg-chart-other' },
]

const dayFmt = new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: 'numeric' })

/** Decisions per day by outcome, stacked, summed over the desks given. */
export function OutcomesChart({ days }: { days: Overview['by_day'] }) {
  const series = SERIES.filter((s) => days.some((d) => d[s.key] > 0))
  const totals = days.map((d) => series.reduce((n, s) => n + (d[s.key] ?? 0), 0))
  const max = Math.max(1, ...totals)
  const step = max > 200 ? 100 : max > 80 ? 50 : max > 40 ? 20 : max > 16 ? 10 : 5
  const top = Math.ceil(max / step) * step
  const ticks = [top / 2, top]
  const H = 200
  const peak = totals.indexOf(Math.max(...totals))
  const label = (iso: string) => dayFmt.format(new Date(`${iso}T12:00:00`))
  const alt = `Decisions per day over the last ${days.length} days. Peak ${totals[peak] ?? 0} on ${days[peak] ? label(days[peak].day) : '—'}.`

  return (
    <div className="flex flex-col gap-3">
      <div role="img" aria-label={alt} className="flex flex-col gap-1.5">
        <div className="relative flex items-end gap-3 border-b border-input pl-[30px]" style={{ height: H + 26 }}>
          {ticks.map((t) => (
            <div key={t} aria-hidden className="absolute right-0 left-[30px] border-t border-dashed border-border" style={{ bottom: (t / top) * H }}>
              <span className="absolute -top-2 -left-[30px] font-mono text-[11px] text-muted-foreground">{t}</span>
            </div>
          ))}
          {days.map((d, i) => (
            <div key={d.day} className="relative flex min-w-0 flex-1 flex-col items-center justify-end gap-1">
              <span className="font-mono text-xs font-semibold">{totals[i]}</span>
              <div className="flex w-full max-w-[46px] flex-col-reverse overflow-hidden rounded-t">
                {series.map((s) =>
                  d[s.key] > 0 ? <div key={s.key} className={s.className} style={{ height: (d[s.key] / top) * H, borderTop: '1px solid var(--card)' }} /> : null,
                )}
              </div>
            </div>
          ))}
        </div>
        <div aria-hidden className="flex gap-3 pl-[30px]">
          {days.map((d) => (
            <span key={d.day} className="min-w-0 flex-1 text-center font-mono text-[11px] text-muted-foreground">
              {label(d.day)}
            </span>
          ))}
        </div>
      </div>
      <ul aria-label="Legend" className="m-0 flex list-none flex-wrap gap-x-4 gap-y-1.5 p-0 text-xs text-secondary-foreground">
        {series.map((s) => (
          <li key={s.key} className="inline-flex items-center gap-1.5">
            <span aria-hidden className={`size-2.5 rounded-sm ${s.className}`} />
            {s.label} <span className="font-mono text-muted-foreground">{days.reduce((n, d) => n + (d[s.key] ?? 0), 0)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Adds the by-day rows of several desks together. */
export function sumByDay(lists: Overview['by_day'][]): Overview['by_day'] {
  const out = new Map<string, Overview['by_day'][number]>()
  for (const list of lists) {
    for (const row of list) {
      const acc = out.get(row.day) ?? { day: row.day, reply_and_close: 0, request_info: 0, escalate: 0, no_action: 0, move: 0 }
      for (const s of SERIES) acc[s.key] += row[s.key] ?? 0
      out.set(row.day, acc)
    }
  }
  return [...out.values()].sort((a, b) => a.day.localeCompare(b.day))
}
