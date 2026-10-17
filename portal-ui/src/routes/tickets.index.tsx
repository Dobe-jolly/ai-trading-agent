import * as React from 'react'
import { createFileRoute } from '@tanstack/react-router'

import { DeskErrors, Empty, Loading, TicketLink } from '@/components/data-state'
import { PageHeader } from '@/components/page-header'
import { SearchField, matchesQuery } from '@/components/search-field'
import { CheckChip, StatusPill } from '@/components/status-pill'
import { Card } from '@/components/ui/card'
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ChipGroup, ChipItem } from '@/components/ui/toggle-group'
import { useDeskQueries } from '@/lib/api/hooks'
import type { Queue, QueueItem, ServiceDesk } from '@/lib/api/types'
import { ago, useNow, when } from '@/lib/format'
import { usePortal } from '@/lib/portal'
import { STATUS, type StatusKey } from '@/lib/status'
import { queueDetail, queueState } from '@/lib/vocab'
import { cn } from '@/lib/utils'

export const Route = createFileRoute('/tickets/')({
  head: () => ({ meta: [{ title: 'Open tickets · Agent Supervision' }] }),
  component: OpenTickets,
})

type Row = QueueItem & { desk: ServiceDesk; status: StatusKey; label?: string }

const GROUPS: { value: string; label: string; match: (s: StatusKey) => boolean }[] = [
  { value: 'all', label: 'All open', match: () => true },
  { value: 'awaitingApproval', label: 'Awaiting approval', match: (s) => s === 'awaitingApproval' || s === 'approved' },
  { value: 'awaitingBranch', label: 'Awaiting branch', match: (s) => s === 'awaitingBranch' },
  { value: 'escalated', label: 'Escalated', match: (s) => s === 'escalated' },
  { value: 'failed', label: 'Failed', match: (s) => s === 'failed' },
  { value: 'notReached', label: 'Not reached', match: (s) => s === 'notReached' },
  { value: 'other', label: 'Other', match: (s) => !['awaitingApproval', 'approved', 'awaitingBranch', 'escalated', 'failed', 'notReached'].includes(s) },
]

function OpenTickets() {
  const { selected } = usePortal()
  const now = useNow()
  const data = useDeskQueries<Queue>(selected, 'queue')
  const [group, setGroup] = React.useState('all')
  const [query, setQuery] = React.useState('')

  const all: Row[] = data.results.flatMap((r) =>
    (r.data?.items ?? []).map((item) => {
      const s = queueState(item)
      return { ...item, desk: r.desk, status: s.status, label: s.label }
    }),
  )
  const active = GROUPS.find((g) => g.value === group) ?? GROUPS[0]
  const rows = all
    .filter((r) => active.match(r.status))
    .filter((r) => matchesQuery(query, r.ticket_number, r.help_topic, r.detail ?? '', r.desk.title))
    .sort((a, b) => (b.opened ?? '').localeCompare(a.opened ?? ''))
  const asOf = data.results.map((r) => r.data?.as_of?.finished_at ?? r.data?.as_of?.started_at).filter(Boolean) as string[]
  const oldestAsOf = asOf.sort()[0]

  return (
    <>
      <PageHeader
        title="Open tickets"
        description={`${all.length} open ticket${all.length === 1 ? '' : 's'} on ${selected.length === 1 ? selected[0].title : 'the desks'}, as each desk saw them on its last pass.`}
        actions={<SearchField label="Search open tickets" placeholder="Ticket, topic or reason" value={query} onChange={setQuery} />}
      />
      <DeskErrors errors={data.errors} />

      <ChipGroup type="single" value={group} onValueChange={(v) => v && setGroup(v)} aria-label="Filter by state">
        {GROUPS.map((g) => {
          const n = all.filter((r) => g.match(r.status)).length
          if (g.value !== 'all' && !n) return null
          const dot = g.value === 'all' || g.value === 'other' ? 'bg-neutral-dot' : g.value === 'failed' ? 'bg-bad-dot' : STATUS[g.value as StatusKey].dot
          return (
            <ChipItem key={g.value} value={g.value} className="group">
              <span aria-hidden className={cn('size-[7px] rounded-full group-data-[state=on]:bg-white', dot)} />
              {g.label} <span className="font-mono text-xs">{n}</span>
            </ChipItem>
          )
        })}
      </ChipGroup>

      <Card>
        {data.isLoading && !all.length ? (
          <Loading />
        ) : rows.length ? (
          <Table className="min-w-[900px]">
            <TableCaption>
              Showing {rows.length} of {all.length}, newest first.{oldestAsOf ? ` As of each desk's last pass (earliest ${when(oldestAsOf, now)}).` : ''}
            </TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>Ticket</TableHead>
                <TableHead>Help topic</TableHead>
                <TableHead>Desk</TableHead>
                <TableHead>State</TableHead>
                <TableHead>Detail</TableHead>
                <TableHead>What the checks showed</TableHead>
                <TableHead>Opened</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r) => (
                <TableRow key={`${r.desk.key}-${r.ticket_id}`}>
                  <TableCell>
                    <TicketLink number={r.ticket_number} desk={r.desk.key} decision={r.decision_id} />
                  </TableCell>
                  <TableCell className="max-w-[260px] text-secondary-foreground">
                    <span className="line-clamp-2">{r.help_topic}</span>
                  </TableCell>
                  <TableCell>{r.desk.title}</TableCell>
                  <TableCell>
                    <StatusPill status={r.status} label={r.label} />
                  </TableCell>
                  <TableCell className={cn('max-w-[260px]', r.status === 'failed' ? 'text-bad-fg' : 'text-secondary-foreground')}>{queueDetail(r)}</TableCell>
                  <TableCell className="max-w-[260px]">
                    <div className="flex flex-wrap gap-1">
                      {Object.entries(r.statuses ?? {})
                        .slice(0, 2)
                        .map(([subject, status]) => (
                          <CheckChip key={subject} vocab={r.desk.vocab} status={status} />
                        ))}
                    </div>
                  </TableCell>
                  <TableCell className="font-mono text-xs whitespace-nowrap text-secondary-foreground">{r.opened ? ago(r.opened, now) : '—'}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <Empty title={all.length ? 'No open ticket matches' : 'No open tickets'}>
            {all.length ? 'Try another state or search.' : selected.length === 1 ? selected[0].vocab.queue_empty ?? 'Nothing was open at the last pass.' : 'Nothing was open at the desks’ last passes.'}
          </Empty>
        )}
      </Card>
    </>
  )
}
