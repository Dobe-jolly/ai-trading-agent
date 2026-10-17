import { createFileRoute } from '@tanstack/react-router'
import { CircleMinus, Megaphone } from 'lucide-react'

import { DeskErrors, Loading } from '@/components/data-state'
import { PageHeader } from '@/components/page-header'
import { Card } from '@/components/ui/card'
import { useDeskQueries } from '@/lib/api/hooks'
import type { Anomalies, ServiceDesk, Severity } from '@/lib/api/types'
import { ago, useNow, when } from '@/lib/format'
import { usePortal } from '@/lib/portal'
import { cn } from '@/lib/utils'

export const Route = createFileRoute('/anomalies')({
  head: () => ({ meta: [{ title: 'Anomalies · Agent Supervision' }] }),
  component: AnomaliesPage,
})

const SEVERITY: Record<Severity, { label: string; pill: string; bar: string; border: string }> = {
  incident: { label: 'Incident', pill: 'bg-bad-bg text-bad-fg', bar: 'bg-bad-dot', border: 'border-bad-line' },
  watch: { label: 'Watching', pill: 'bg-warn-bg text-warn-fg', bar: 'bg-warn-dot', border: 'border-warn-line' },
  ok: { label: 'All normal', pill: 'bg-ok-bg text-ok-fg', bar: 'bg-ok-line', border: 'border-border' },
}

function AnomaliesPage() {
  const { selected } = usePortal()
  const watched = selected.filter((d) => d.anomaly_watch)
  const unwatched = selected.filter((d) => !d.anomaly_watch)
  const data = useDeskQueries<Anomalies>(watched, 'anomalies')

  return (
    <>
      <PageHeader
        title="Anomalies"
        description="Patterns across tickets and systems that no single ticket shows. Checked every hour on the desks that have an anomaly watch."
      />
      <DeskErrors errors={data.errors} />

      {watched.length ? (
        data.isLoading && !data.results.some((r) => r.data) ? (
          <Loading />
        ) : (
          <div className="flex flex-col gap-4">
            {data.results.map(({ desk, data: a }) => (a ? <DeskAnomaly key={desk.key} desk={desk} anomalies={a} /> : null))}
          </div>
        )
      ) : null}

      {unwatched.length ? (
        <div
          className={cn(
            'flex flex-col gap-2 rounded-xl border border-dashed border-input bg-card',
            watched.length ? 'px-4 py-3' : 'items-center px-6 py-14 text-center',
          )}
        >
          <span className={cn('flex items-center gap-2 text-[13px] text-secondary-foreground', !watched.length && 'flex-col')}>
            <CircleMinus aria-hidden className={cn('shrink-0', watched.length ? 'size-4' : 'size-6')} />
            <span>
              <strong className="font-semibold">Not available</strong> for {unwatched.map((d) => d.title).join(', ')} — {unwatched.length > 1 ? 'these desks have' : 'this desk has'} no anomaly watch yet.
            </span>
          </span>
          {unwatched.length === 1 && unwatched[0].vocab.anomaly?.unavailable ? (
            <span className="max-w-[640px] text-[13px] text-muted-foreground">{unwatched[0].vocab.anomaly.unavailable}</span>
          ) : null}
        </div>
      ) : null}
    </>
  )
}

function DeskAnomaly({ desk, anomalies }: { desk: ServiceDesk; anomalies: Anomalies }) {
  const now = useNow()
  const r = anomalies.report
  if (!r) {
    return (
      <Card className="px-5 py-4 text-[13px] text-muted-foreground">
        <strong className="font-semibold text-foreground">{desk.title}</strong> — the anomaly watch has not reported yet.
      </Card>
    )
  }
  const sev = SEVERITY[r.severity] ?? SEVERITY.ok
  const s = r.summary ?? {}
  const signals = (r.analysis?.signals ?? []).filter((x) => x.severity !== 'ok')
  const active = anomalies.state?.active ? anomalies.state : null
  return (
    <article className={cn('flex flex-col gap-4 rounded-xl border bg-card px-5 py-4', sev.border)}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-[1_1_520px] flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className={cn('inline-flex h-[22px] items-center rounded-full px-2 font-semibold', sev.pill)}>{sev.label}</span>
            <span className="font-semibold text-secondary-foreground">{desk.title}</span>
            <span className="text-muted-foreground">
              · checked {ago(r.created_at, now)}
              {active?.started_at ? ` · since ${when(active.started_at, now)}` : ''}
            </span>
          </div>
          <h2 className="m-0 text-base font-semibold">{r.headline}</h2>
        </div>
        <History items={anomalies.history} />
      </div>

      {s.summary ? <p className="m-0 max-w-[900px] text-[13px] text-secondary-foreground">{s.summary}</p> : null}

      {signals.length ? (
        <div className="relative overflow-x-auto">
          <table className="w-full min-w-[640px] border-collapse text-[13px]">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th scope="col" className="py-2 pr-3 font-semibold">Signal</th>
                <th scope="col" className="py-2 pr-3 font-semibold">Now</th>
                <th scope="col" className="py-2 pr-3 font-semibold">Usual</th>
                <th scope="col" className="py-2 font-semibold">Detail</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((x) => (
                <tr key={x.key} className="border-b border-border-soft last:border-0">
                  <td className="py-2 pr-3">
                    <span className="inline-flex items-center gap-2">
                      <span aria-hidden className={cn('size-2 shrink-0 rounded-full', (SEVERITY[x.severity] ?? SEVERITY.ok).bar)} />
                      <span className="sr-only">{(SEVERITY[x.severity] ?? SEVERITY.ok).label}: </span>
                      {x.label}
                    </span>
                  </td>
                  <td className="py-2 pr-3 font-mono text-xs font-semibold">{x.value ?? '—'}</td>
                  <td className="py-2 pr-3 font-mono text-xs text-muted-foreground">{x.usual ?? '—'}</td>
                  <td className="py-2 text-xs text-secondary-foreground">{x.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <div className="flex flex-wrap gap-4">
        {s.likely_causes?.length ? <ListBlock title="Likely causes" items={s.likely_causes} /> : null}
        {s.actions?.length ? <ListBlock title="What to do" items={s.actions} ordered /> : null}
      </div>

      {s.branch_message ? (
        <figure className="m-0 flex items-start gap-3 rounded-lg bg-info-bg px-3.5 py-3 text-[13px] text-info-fg">
          <Megaphone aria-hidden className="mt-0.5 size-4 shrink-0" />
          <div>
            <figcaption className="mb-0.5 text-xs font-semibold">Suggested message to branches</figcaption>
            <blockquote className="m-0">{s.branch_message}</blockquote>
          </div>
        </figure>
      ) : null}
      {r.alert ? <p className="m-0 text-xs text-muted-foreground">An alert email went to ICT operations with this report ({r.alert}).</p> : null}
    </article>
  )
}

function ListBlock({ title, items, ordered }: { title: string; items: string[]; ordered?: boolean }) {
  const List = ordered ? 'ol' : 'ul'
  return (
    <div className="flex min-w-0 flex-[1_1_360px] flex-col gap-1.5">
      <span className="text-xs font-semibold text-muted-foreground">{title}</span>
      <List className="m-0 flex flex-col gap-1 pl-5 text-[13px]">
        {items.map((x, i) => (
          <li key={i}>{x}</li>
        ))}
      </List>
    </div>
  )
}

/** The last day of hourly checks, oldest on the left. */
function History({ items }: { items: Anomalies['history'] }) {
  const last = items.slice(0, 24).reverse()
  if (!last.length) return null
  return (
    <figure className="m-0 flex flex-col gap-1">
      <figcaption className="text-xs text-muted-foreground">Last {last.length} checks</figcaption>
      <div role="img" aria-label={`Last ${last.length} checks: ${last.filter((h) => h.severity === 'incident').length} incident, ${last.filter((h) => h.severity === 'watch').length} watching.`} className="flex items-end gap-[3px]">
        {last.map((h) => (
          <span key={h.id} title={`${when(h.created_at)} · ${(SEVERITY[h.severity] ?? SEVERITY.ok).label}: ${h.headline}`} className={cn('w-2 rounded-sm', (SEVERITY[h.severity] ?? SEVERITY.ok).bar, h.severity === 'incident' ? 'h-7' : h.severity === 'watch' ? 'h-5' : 'h-3')} />
        ))}
      </div>
    </figure>
  )
}

