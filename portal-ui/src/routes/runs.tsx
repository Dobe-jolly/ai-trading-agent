import { Link, createFileRoute } from '@tanstack/react-router'
import { Clock } from 'lucide-react'

import { DeskErrors, Empty, Loading, TicketLink } from '@/components/data-state'
import { PageHeader } from '@/components/page-header'
import { StatusPill } from '@/components/status-pill'
import { Card } from '@/components/ui/card'
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useDeskQueries } from '@/lib/api/hooks'
import type { Live, Run, Runs, ServiceDesk } from '@/lib/api/types'
import { ago, duration, span, useNow, when } from '@/lib/format'
import { deskState, usePortal } from '@/lib/portal'
import type { StatusKey } from '@/lib/status'
import { cn } from '@/lib/utils'

export const Route = createFileRoute('/runs')({
  head: () => ({ meta: [{ title: 'Runs · Agent Supervision' }] }),
  component: RunsPage,
})

const SLOW_SECONDS = 120

function runStatus(r: Run): StatusKey {
  if (!r.finished_at) return 'runUnfinished'
  return r.errors ? 'runWithErrors' : 'runCompleted'
}

function RunsPage() {
  const { selected, service } = usePortal()
  const now = useNow(10_000)
  const runs = useDeskQueries<Runs>(selected, 'runs', { limit: 40 })
  const live = useDeskQueries<Live>(selected, 'live')
  const rows = runs.results
    .flatMap((r) => (r.data?.items ?? []).filter((x) => x.mode !== 'audit').map((x) => ({ ...x, desk: r.desk })))
    .sort((a, b) => b.started_at.localeCompare(a.started_at))
    .slice(0, 80)
  const running = live.results.filter((r) => r.data?.running)
  const stale = selected.filter((d) => {
    const s = deskState(d, service.paused)
    return s === 'stale' || s === 'neverRan'
  })

  return (
    <>
      <PageHeader title="Runs" description="Each pass a desk makes over its open tickets: what it read, what it decided, errors and how long it took." />
      <DeskErrors errors={[...runs.errors, ...live.errors]} />

      {running.length || stale.length ? (
        <div className="flex flex-wrap gap-3">
          {running.map(({ desk, data }) => (
            <RunningNow key={desk.key} desk={desk} live={data!} now={now} />
          ))}
          {stale.map((d) => (
            <section key={d.key} aria-label={`${d.title} is stale`} className="flex min-w-0 flex-[1_1_420px] flex-col gap-2.5 rounded-xl border border-warn-line bg-warn-row px-5 py-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="m-0 text-[15px] font-semibold text-warn-fg">
                  {d.title} · {d.health.last_run_at ? `no pass since ${when(d.health.last_run_at, now)}` : 'never ran'}
                </h2>
                <StatusPill status={d.health.minutes_since_last_run == null ? 'neverRan' : 'stale'} label={d.health.minutes_since_last_run == null ? undefined : `Stale · ${span(d.health.minutes_since_last_run)}`} />
              </div>
              <p className="m-0 text-[13px] text-warn-fg">
                Expected at least every {d.health.stale_after_minutes} min. It is not paused, so the timer or the service on the server needs checking
                (systemctl status nmbz-agents.timer).
              </p>
              <Link to="/controls" className="self-start text-[13px] font-semibold text-warn-fg">
                Open controls
              </Link>
            </section>
          ))}
        </div>
      ) : null}

      <Card>
        {runs.isLoading && !rows.length ? (
          <Loading />
        ) : rows.length ? (
          <Table className="min-w-[960px]">
            <TableCaption>Passes over {SLOW_SECONDS / 60} min are highlighted. Showing the latest {rows.length}.</TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>Started</TableHead>
                <TableHead>Desk</TableHead>
                <TableHead>Mode</TableHead>
                <TableHead>Duration</TableHead>
                <TableHead>Tickets read</TableHead>
                <TableHead>Decided</TableHead>
                <TableHead>Errors</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r) => {
                const status = runStatus(r)
                const slow = (r.duration_s ?? 0) > SLOW_SECONDS
                return (
                  <TableRow key={`${r.desk.key}-${r.run_id}`} className={cn(r.errors > 0 && 'bg-warn-row')}>
                    <TableCell className="font-mono text-xs whitespace-nowrap">{when(r.started_at, now)}</TableCell>
                    <TableCell className="whitespace-nowrap">{r.desk.title}</TableCell>
                    <TableCell className="text-secondary-foreground capitalize">{r.mode}</TableCell>
                    <TableCell className={cn('font-mono text-xs', slow ? 'font-semibold text-warn-fg' : 'text-secondary-foreground')}>{r.finished_at ? duration(r.duration_s) : '—'}</TableCell>
                    <TableCell className="font-mono text-xs">{r.tickets_seen}</TableCell>
                    <TableCell className="font-mono text-xs">{r.decisions}</TableCell>
                    <TableCell className={cn('font-mono text-xs', r.errors ? 'font-semibold text-bad-fg' : 'text-muted-foreground')}>{r.errors || '—'}</TableCell>
                    <TableCell>
                      <StatusPill status={status} />
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        ) : (
          <Empty title="No passes yet">The desks have not run.</Empty>
        )}
      </Card>
    </>
  )
}

function RunningNow({ desk, live, now }: { desk: ServiceDesk; live: Live; now: number }) {
  const steps = live.steps ?? []
  return (
    <section aria-label={`${desk.title} running now`} className="flex min-w-0 flex-[1_1_420px] flex-col gap-2.5 rounded-xl border border-border bg-card px-5 py-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="m-0 text-[15px] font-semibold">Running now · {desk.title}</h2>
        <span className="inline-flex items-center gap-1.5 font-mono text-xs text-muted-foreground">
          <Clock aria-hidden className="size-3.5" />
          started {ago(live.started_at, now)}
        </span>
      </div>
      {live.ticket_number ? (
        <span className="text-[13px] text-secondary-foreground">
          Reading <TicketLink number={live.ticket_number} desk={desk.key} />
          {live.help_topic ? ` · ${live.help_topic}` : ''}
        </span>
      ) : null}
      {steps.length ? (
        <ol className="m-0 flex flex-col gap-1 pl-5 font-mono text-xs text-secondary-foreground">
          {steps.slice(-6).map((s, i) => (
            <li key={i}>{s.text}</li>
          ))}
        </ol>
      ) : null}
    </section>
  )
}
