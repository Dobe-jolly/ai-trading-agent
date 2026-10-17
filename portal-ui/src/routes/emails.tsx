import * as React from 'react'
import { createFileRoute } from '@tanstack/react-router'
import { Lock, RotateCw } from 'lucide-react'

import { DeskErrors, Empty, ErrorNote, Loading, TicketLink } from '@/components/data-state'
import { PageHeader } from '@/components/page-header'
import { StatusPill } from '@/components/status-pill'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ChipGroup, ChipItem } from '@/components/ui/toggle-group'
import { useDeskQueries, useRetryEmails } from '@/lib/api/hooks'
import type { Notification, Notifications, ServiceDesk } from '@/lib/api/types'
import { count, useNow, when } from '@/lib/format'
import { usePortal } from '@/lib/portal'
import type { StatusKey } from '@/lib/status'
import { emailKind } from '@/lib/vocab'
import { cn } from '@/lib/utils'

export const Route = createFileRoute('/emails')({
  head: () => ({ meta: [{ title: 'Emails & alerts · Agent Supervision' }] }),
  component: EmailsPage,
})

const STATUS_KEY: Record<Notification['status'], StatusKey> = { sent: 'sent', failed: 'sendFailed', pending: 'queued', skipped: 'sendSkipped' }
const FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'failed', label: 'Failed' },
  { value: 'pending', label: 'Queued' },
  { value: 'sent', label: 'Sent' },
  { value: 'skipped', label: 'Not sent' },
] as const

function EmailsPage() {
  const { selected, isAdmin } = usePortal()
  const now = useNow()
  const data = useDeskQueries<Notifications>(selected, 'notifications', { days: 30 })
  const [filter, setFilter] = React.useState<(typeof FILTERS)[number]['value']>('all')
  const [notice, setNotice] = React.useState<string | null>(null)
  const all = data.results
    .flatMap((r) => (r.data?.items ?? []).map((n) => ({ ...n, desk: r.desk })))
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
  const rows = all.filter((n) => filter === 'all' || n.status === filter)
  const sum = (s: Notification['status']) => data.results.reduce((n, r) => n + (r.data?.counts[s] ?? 0), 0)
  const retryable = data.results.filter((r) => (r.data?.retryable ?? 0) > 0)

  return (
    <>
      <PageHeader
        title="Emails & alerts"
        description="What the service sent outside osTicket in the last 30 days: updates to branches and teams, and service alerts to ICT."
        actions={
          <div className="flex gap-5 text-[13px]">
            <span>
              <strong className="font-mono font-semibold">{count(sum('sent'))}</strong> <span className="text-muted-foreground">sent</span>
            </span>
            <span>
              <strong className={cn('font-mono font-semibold', sum('failed') && 'text-bad-fg')}>{count(sum('failed'))}</strong> <span className="text-muted-foreground">failed</span>
            </span>
            <span>
              <strong className="font-mono font-semibold">{count(sum('pending'))}</strong> <span className="text-muted-foreground">queued</span>
            </span>
          </div>
        }
      />
      <DeskErrors errors={data.errors} />

      {retryable.length ? (
        isAdmin ? (
          <div className="flex flex-col gap-2">
            {retryable.map((r) => (
              <RetryBar key={r.desk.key} desk={r.desk} count={r.data!.retryable} onDone={setNotice} />
            ))}
          </div>
        ) : (
          <div className="flex items-center gap-2 rounded-lg bg-muted px-3.5 py-2.5 text-[13px] text-secondary-foreground">
            <Lock aria-hidden className="size-4" />
            Failed emails can be re-sent by an Admin.
          </div>
        )
      ) : null}

      <p role="status" className={cn('m-0 rounded-lg bg-ok-bg px-3.5 py-2.5 text-[13px] text-ok-fg', !notice && 'sr-only')}>
        {notice ?? ''}
      </p>

      {sum('failed') > retryable.reduce((n, r) => n + (r.data?.retryable ?? 0), 0) ? (
        <p className="m-0 text-xs text-muted-foreground">
          Some failed emails can’t be re-sent: their text is no longer kept. The outcome is still on each ticket in osTicket.
        </p>
      ) : null}

      <ChipGroup type="single" value={filter} onValueChange={(v) => v && setFilter(v as typeof filter)} aria-label="Filter by status">
        {FILTERS.map((f) => (
          <ChipItem key={f.value} value={f.value}>
            {f.label} <span className="font-mono text-xs">{f.value === 'all' ? all.length : all.filter((n) => n.status === f.value).length}</span>
          </ChipItem>
        ))}
      </ChipGroup>

      <Card>
        {data.isLoading && !all.length ? (
          <Loading />
        ) : rows.length ? (
          <Table className="min-w-[880px]">
            <TableCaption>Message bodies are not kept once sent; they hold real account and phone numbers. Showing {rows.length} of {all.length}.</TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>Type · desk</TableHead>
                <TableHead>To</TableHead>
                <TableHead>Subject</TableHead>
                <TableHead>Ticket</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((n) => (
                <TableRow key={`${n.desk.key}-${n.id}`} className={cn(n.status === 'failed' && 'bg-bad-row')}>
                  <TableCell className="w-[92px] font-mono text-xs text-secondary-foreground">{when(n.created_at, now)}</TableCell>
                  <TableCell>
                    <span className="block font-medium whitespace-nowrap">{emailKind(n.kind)}</span>
                    <span className="block text-xs whitespace-nowrap text-muted-foreground">{n.desk.title}</span>
                  </TableCell>
                  <TableCell className="max-w-[200px]">
                    <span className="block truncate font-mono text-xs" title={n.recipient ?? ''}>
                      {n.recipient ?? '—'}
                    </span>
                    {n.cc ? <span className="block truncate font-mono text-[11px] text-muted-foreground">cc {n.cc}</span> : null}
                  </TableCell>
                  <TableCell className="max-w-[290px]">
                    <span className="block truncate" title={n.subject}>
                      {n.subject}
                    </span>
                    {n.last_error ? <span className="block text-xs text-bad-fg">{n.last_error}</span> : null}
                  </TableCell>
                  <TableCell>{n.ticket_number ? <TicketLink number={n.ticket_number} desk={n.desk.key} decision={n.decision_id} /> : <span className="text-muted-foreground">—</span>}</TableCell>
                  <TableCell>
                    <div className="flex flex-col items-start gap-0.5">
                      <StatusPill status={STATUS_KEY[n.status]} />
                      {n.attempts > 1 ? <span className="text-[11px] text-muted-foreground">{n.attempts} attempts</span> : null}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <Empty title="No emails">{filter === 'all' ? 'Nothing was sent in the last 30 days.' : 'None with this status.'}</Empty>
        )}
      </Card>
    </>
  )
}

function RetryBar({ desk, count: n, onDone }: { desk: ServiceDesk; count: number; onDone: (message: string) => void }) {
  const retry = useRetryEmails()
  const resend = () =>
    retry
      .mutateAsync({ desk: desk.key })
      .then((r) => onDone(`${r.requeued} email${r.requeued === 1 ? '' : 's'} on ${desk.title} queued again. ${r.requeued === 1 ? 'It goes' : 'They go'} out on the desk’s next pass.`))
      .catch(() => {})
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-bad-line bg-bad-row px-3.5 py-2.5 text-[13px] text-bad-fg">
      <span className="flex-[1_1_320px]">
        <strong className="font-semibold">
          {n} failed email{n > 1 ? 's' : ''} on {desk.title}
        </strong>{' '}
        can be re-sent. They go out with the desk’s next pass.
      </span>
      {retry.data ? (
        <span role="status" className="font-medium">
          {retry.data.requeued} queued again
        </span>
      ) : (
        <Button variant="destructive" size="sm" onClick={resend} disabled={retry.isPending}>
          <RotateCw aria-hidden className="size-[13px]" />
          {retry.isPending ? 'Queuing…' : `Re-send ${n}`}
        </Button>
      )}
      <ErrorNote error={retry.error} className="basis-full" />
    </div>
  )
}
