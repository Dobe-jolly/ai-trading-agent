import type * as React from 'react'
import { Link, createFileRoute } from '@tanstack/react-router'
import { BookOpenText, CircleCheck, Clock, Mail, OctagonPause, ScanSearch, TriangleAlert } from 'lucide-react'

import { DeskErrors, Loading } from '@/components/data-state'
import { OutcomesChart, sumByDay } from '@/components/overview/outcomes-chart'
import { PageHeader } from '@/components/page-header'
import { ModeChip, StatusPill } from '@/components/status-pill'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useDeskQueries } from '@/lib/api/hooks'
import type { Approvals, Overview, ServiceDesk } from '@/lib/api/types'
import { ago, count, longDate, minutesSince, span, useNow } from '@/lib/format'
import { deskState, usePortal } from '@/lib/portal'
import { cn } from '@/lib/utils'

export const Route = createFileRoute('/')({
  head: () => ({ meta: [{ title: 'Overview · Agent Supervision' }] }),
  component: OverviewPage,
})

const OVERDUE_MINUTES = 30

function OverviewPage() {
  const { selected, desk, service } = usePortal()
  const now = useNow()
  const day = useDeskQueries<Overview>(selected, 'overview', { days: 1 })
  const week = useDeskQueries<Overview>(selected, 'overview', { days: 7 })
  const approvals = useDeskQueries<Approvals>(selected, 'approvals')

  const k = (key: keyof Overview['kpis']) => day.results.reduce((n, r) => n + ((r.data?.kpis[key] as number) ?? 0), 0)
  const pending = approvals.results.flatMap((r) => (r.data?.pending ?? []).map((a) => ({ ...a, desk: r.desk })))
  const oldest = pending.reduce<number | null>((m, a) => Math.max(m ?? 0, minutesSince(a.created_at, now) ?? 0), null)
  const waiting = selected.reduce((n, d) => n + d.approvals_pending, 0)
  const decided = k('decided')
  const attention = needsAttention(selected, service.paused, day.results, pending, now)

  return (
    <>
      <PageHeader title="Overview" description={`${longDate(now)} · ${desk === 'all' ? `all ${selected.length} desks` : selected[0].title} · last 24 hours`} />
      <DeskErrors errors={[...day.errors, ...approvals.errors]} />

      <section aria-label="Last 24 hours" className="flex flex-wrap gap-3">
        <Kpi label="Decided" value={decided} note={`${count(k('guardrail_changed'))} changed by a rule`} loading={day.isLoading} />
        <Kpi label="Replied & closed" value={k('closed')} note={decided ? `${Math.round((k('closed') / decided) * 100)}% of decided` : '—'} valueClass="text-ok-fg" loading={day.isLoading} />
        <Kpi label="Escalated" value={k('escalated')} note="Handed to a team" valueClass="text-esc-fg" loading={day.isLoading} />
        <Link
          to="/approvals"
          className="flex min-w-0 flex-[1_1_140px] flex-col gap-1 rounded-xl border border-warn-line bg-warn-row px-4 py-3.5 text-foreground no-underline hover:border-warn-dot"
        >
          <span className="text-xs font-semibold text-warn-fg">Awaiting approval</span>
          <span className="tabular text-[28px] leading-tight font-semibold text-warn-fg">{waiting}</span>
          <span className="text-xs text-warn-fg">{waiting && oldest != null ? `Oldest ${span(oldest)} · open queue` : 'Queue is clear'}</span>
        </Link>
        <Kpi label="Asked branch" value={k('request_info')} note="Kept open for the branch" valueClass="text-info-fg" loading={day.isLoading} />
        <Kpi label="Deferred" value={k('deferred')} note="Retried on a later pass" valueClass="text-plum-fg" loading={day.isLoading} />
        <Kpi label="Failed" value={k('errors')} note={k('errors') ? 'See decisions' : 'None'} tone={k('errors') ? 'bad' : undefined} loading={day.isLoading} />
      </section>

      <div className="flex flex-wrap items-stretch gap-5">
        <Card className="min-w-0 flex-[1_1_520px]">
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle>Needs attention</CardTitle>
            <CardDescription>{attention.length ? `${attention.length} item${attention.length > 1 ? 's' : ''} · most urgent first` : 'Nothing right now'}</CardDescription>
          </CardHeader>
          {day.isLoading ? <Loading /> : <NeedsAttentionList items={attention} />}
        </Card>

        <Card className="min-w-0 flex-[1_1_440px]">
          <CardHeader className="flex-row flex-wrap items-baseline justify-between border-0 pb-0">
            <CardTitle>Recent outcomes</CardTitle>
            <CardDescription>Decisions per day, last 7 days</CardDescription>
          </CardHeader>
          <CardContent>
            {week.isLoading ? <Loading /> : <OutcomesChart days={sumByDay(week.results.map((r) => r.data?.by_day ?? []))} />}
            <p className="mt-3 mb-0 text-xs text-muted-foreground">Today is still in progress. A reply waiting for approval is counted once it is decided.</p>
          </CardContent>
        </Card>
      </div>

      <section aria-labelledby="desk-health" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="desk-health" className="m-0 text-[15px] font-semibold">
            Desk health
          </h2>
          <span className="text-xs text-muted-foreground">Paused = stopped on purpose by an Admin. Stale = expected to run but has not reported.</span>
        </div>
        <div className="flex flex-wrap gap-3">
          {selected.map((d) => (
            <DeskHealthCard key={d.key} desk={d} overview={day.results.find((r) => r.desk.key === d.key)?.data} servicePaused={service.paused} now={now} />
          ))}
        </div>
      </section>
    </>
  )
}

function Kpi({ label, value, note, valueClass, tone, loading }: { label: string; value: number; note: string; valueClass?: string; tone?: 'bad'; loading?: boolean }) {
  return (
    <div className={cn('flex min-w-0 flex-[1_1_140px] flex-col gap-1 rounded-xl border px-4 py-3.5', tone === 'bad' ? 'border-bad-line bg-bad-row' : 'border-border bg-card')}>
      <span className={cn('text-xs', tone === 'bad' ? 'font-semibold text-bad-fg' : 'font-medium text-muted-foreground')}>{label}</span>
      <span className={cn('tabular text-[28px] leading-tight font-semibold', tone === 'bad' ? 'text-bad-fg' : valueClass)}>{loading ? '…' : count(value)}</span>
      <span className={cn('text-xs', tone === 'bad' ? 'text-bad-fg' : 'text-muted-foreground')}>{note}</span>
    </div>
  )
}

interface Attention {
  id: string
  tone: 'bad' | 'warn' | 'paused'
  icon: React.ComponentType<{ className?: string }>
  title: string
  body: string
  link: { to: '/runs' | '/decisions' | '/emails' | '/controls' | '/approvals' | '/anomalies' | '/knowledge'; label: string; search?: Record<string, string> }
}

function needsAttention(
  desks: ServiceDesk[],
  servicePaused: boolean,
  day: { desk: ServiceDesk; data: Overview | undefined }[],
  pending: { created_at: string; ticket_number: string; desk: ServiceDesk }[],
  now: number,
): Attention[] {
  const items: Attention[] = []
  for (const d of desks) {
    const state = deskState(d, servicePaused)
    if (d.anomaly && d.anomaly.severity !== 'ok') {
      items.push({
        id: `${d.key}-anomaly`,
        tone: d.anomaly.severity === 'incident' ? 'bad' : 'warn',
        icon: ScanSearch,
        title: `${d.title}: ${d.anomaly.headline}`,
        body: `${d.anomaly.severity === 'incident' ? 'Incident' : 'Being watched'} · reported ${ago(d.anomaly.created_at, now)}.`,
        link: { to: '/anomalies', label: 'Anomalies' },
      })
    }
    if (state === 'stale' || state === 'neverRan') {
      items.push({
        id: `${d.key}-stale`,
        tone: 'warn',
        icon: Clock,
        title: state === 'neverRan' ? `${d.title} has never reported a pass` : `${d.title} is stale — no pass in ${span(d.health.minutes_since_last_run)}`,
        body: `It should run at least every ${d.health.stale_after_minutes} min. New tickets on this desk are not being read. A pause won't fix it; the service needs checking on the server.`,
        link: { to: '/runs', label: 'View runs' },
      })
    }
    if (d.health.last_run_errors) {
      items.push({
        id: `${d.key}-run-errors`,
        tone: 'bad',
        icon: TriangleAlert,
        title: `${d.title}: the last pass had ${d.health.last_run_errors} error${d.health.last_run_errors > 1 ? 's' : ''}`,
        body: `Finished ${ago(d.health.last_run_at, now)}. Tickets that failed are tried again on the next pass.`,
        link: { to: '/runs', label: 'View runs' },
      })
    }
    const errors = day.find((r) => r.desk.key === d.key)?.data?.kpis.errors ?? 0
    if (errors) {
      items.push({
        id: `${d.key}-errors`,
        tone: 'bad',
        icon: TriangleAlert,
        title: `${errors} ticket${errors > 1 ? 's' : ''} failed on ${d.title} in the last 24 hours`,
        body: 'The agent could not finish these, usually because a lookup or osTicket did not answer. Nothing was sent to the branch.',
        link: { to: '/decisions', label: 'View decisions', search: { show: 'error' } },
      })
    }
    if (d.emails_failed) {
      items.push({
        id: `${d.key}-emails`,
        tone: 'bad',
        icon: Mail,
        title: `${d.emails_failed} email${d.emails_failed > 1 ? 's' : ''} from ${d.title} failed to send`,
        body: 'Branches or teams may not know about an update on their ticket. See which ones, and re-send those whose text is still kept.',
        link: { to: '/emails', label: 'Emails & alerts' },
      })
    }
    if (d.knowledge_pending) {
      items.push({
        id: `${d.key}-notes`,
        tone: 'warn',
        icon: BookOpenText,
        title: `${d.knowledge_pending} note${d.knowledge_pending > 1 ? 's' : ''} taught to ${d.title} waiting for an Admin`,
        body: 'Staff wrote what the agent should know for some tickets. A note is used only once an Admin who didn’t write it switches it on.',
        link: { to: '/knowledge', label: 'Knowledge' },
      })
    }
  }
  const overdue = pending.filter((a) => (minutesSince(a.created_at, now) ?? 0) > OVERDUE_MINUTES).sort((a, b) => a.created_at.localeCompare(b.created_at))
  if (overdue.length) {
    const first = overdue[0]
    items.push({
      id: 'approvals-overdue',
      tone: 'warn',
      icon: CircleCheck,
      title: `${overdue.length} approval${overdue.length > 1 ? 's' : ''} waiting over ${OVERDUE_MINUTES} min`,
      body: `Oldest is #${first.ticket_number} on ${first.desk.title} (${span(minutesSince(first.created_at, now))}). Approved replies post on the desk's next pass.`,
      link: { to: '/approvals', label: 'Open queue' },
    })
  }
  if (servicePaused) {
    items.push({
      id: 'service-paused',
      tone: 'paused',
      icon: OctagonPause,
      title: 'Every desk is paused',
      body: 'Paused on purpose from the console, not stale. Tickets wait until an Admin resumes the service.',
      link: { to: '/controls', label: 'Controls' },
    })
  } else {
    for (const d of desks.filter((x) => x.kill_switch)) {
      items.push({
        id: `${d.key}-paused`,
        tone: 'paused',
        icon: OctagonPause,
        title: `${d.title} is paused`,
        body: 'Paused on purpose from the console, not stale. Its tickets wait until an Admin resumes it.',
        link: { to: '/controls', label: 'Controls' },
      })
    }
  }
  const rank = { bad: 0, warn: 1, paused: 2 }
  return items.sort((a, b) => rank[a.tone] - rank[b.tone])
}

function NeedsAttentionList({ items }: { items: Attention[] }) {
  if (!items.length) {
    return (
      <div className="flex items-center gap-3 px-5 py-6 text-[13px] text-secondary-foreground">
        <span aria-hidden className="flex size-8 items-center justify-center rounded-lg bg-ok-bg text-ok-fg">
          <CircleCheck className="size-4" />
        </span>
        Every desk is running, nothing is overdue and no anomaly is open.
      </div>
    )
  }
  return (
    <ul className="m-0 list-none p-0">
      {items.map((item) => {
        const Icon = item.icon
        return (
          <li key={item.id} className="flex items-start gap-3.5 border-b border-border-soft px-5 py-3.5 last:border-0">
            <span
              aria-hidden
              className={cn(
                'flex size-8 shrink-0 items-center justify-center rounded-lg',
                item.tone === 'bad' && 'bg-bad-bg text-bad-fg',
                item.tone === 'warn' && 'bg-warn-bg text-warn-fg',
                item.tone === 'paused' && 'bg-paused-bg text-paused-fg',
              )}
            >
              <Icon className="size-4" />
            </span>
            <div className="flex min-w-0 flex-1 flex-col gap-[3px]">
              <strong className="font-semibold">{item.title}</strong>
              <span className="text-[13px] text-muted-foreground">{item.body}</span>
            </div>
            <Link to={item.link.to} search={item.link.search} className="shrink-0 pt-1.5 text-[13px] font-medium whitespace-nowrap">
              {item.link.label}
            </Link>
          </li>
        )
      })}
    </ul>
  )
}

function DeskHealthCard({ desk, overview, servicePaused, now }: { desk: ServiceDesk; overview?: Overview; servicePaused: boolean; now: number }) {
  const state = deskState(desk, servicePaused)
  const rows: { k: string; v: string; tone?: 'warn' | 'muted' }[] = [
    { k: 'Last pass', v: desk.health.last_run_at ? ago(desk.health.last_run_at, now) : 'never', tone: state === 'stale' || state === 'neverRan' ? 'warn' : undefined },
    { k: 'Decided, 24 h', v: overview ? count(overview.kpis.decided) : '…' },
    { k: desk.key === 'router' ? 'Moved, 24 h' : 'Closed, 24 h', v: overview ? count(desk.key === 'router' ? overview.kpis.moved : overview.kpis.closed) : '…' },
    { k: 'Awaiting approval', v: String(desk.approvals_pending), tone: desk.approvals_pending ? 'warn' : 'muted' },
  ]
  const footer = desk.anomaly && desk.anomaly.severity !== 'ok'
    ? { text: `${desk.anomaly.severity === 'incident' ? 'Incident' : 'Watching'} · ${desk.anomaly.headline}`, tone: desk.anomaly.severity === 'incident' ? 'bad' : 'warn' }
    : state === 'paused'
      ? { text: 'Paused from the console', tone: 'muted' }
      : desk.emails_failed
        ? { text: `${desk.emails_failed} email${desk.emails_failed > 1 ? 's' : ''} failed to send`, tone: 'bad' }
        : { text: 'No open incidents', tone: 'muted' }
  return (
    <article
      className={cn(
        'flex min-w-0 flex-[1_1_200px] flex-col gap-2.5 rounded-xl border bg-card px-4 py-3.5',
        state === 'stale' || state === 'neverRan' ? 'border-warn-line' : footer.tone === 'bad' ? 'border-bad-line' : 'border-border',
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <h3 className="m-0 text-sm font-semibold">{desk.title}</h3>
        <StatusPill status={state} label={state === 'stale' ? `Stale · ${span(desk.health.minutes_since_last_run)}` : undefined} />
      </div>
      <ModeChip desk={desk} className="h-[22px] self-start px-2 text-[11px]" />
      <dl className="m-0 flex flex-col gap-[5px] text-[13px]">
        {rows.map((r) => (
          <div key={r.k} className="flex justify-between gap-2">
            <dt className="text-muted-foreground">{r.k}</dt>
            <dd className={cn('m-0 font-mono text-xs font-medium', r.tone === 'warn' && 'text-warn-fg', r.tone === 'muted' && 'text-muted-foreground')}>{r.v}</dd>
          </div>
        ))}
      </dl>
      <div className={cn('mt-auto border-t border-border-soft pt-2 text-xs', footer.tone === 'bad' ? 'text-bad-fg' : footer.tone === 'warn' ? 'text-warn-fg' : 'text-muted-foreground')}>
        {footer.text}
      </div>
    </article>
  )
}
