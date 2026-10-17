import * as React from 'react'
import { Link, createFileRoute } from '@tanstack/react-router'
import { Check, Clock } from 'lucide-react'

import { Confidence } from '@/components/confidence'
import { DeskErrors, Empty, Loading, TicketLink } from '@/components/data-state'
import { PageHeader } from '@/components/page-header'
import { SearchField, matchesQuery } from '@/components/search-field'
import { StatusPill } from '@/components/status-pill'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Label, NativeSelect } from '@/components/ui/input'
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useDeskQueries } from '@/lib/api/hooks'
import type { Approvals, ApprovalRow, ServiceDesk } from '@/lib/api/types'
import { minutesSince, sentence, span, useNow, when } from '@/lib/format'
import { usePortal } from '@/lib/portal'
import { ACTION_STATUS, actionLabel, approvalStatus } from '@/lib/vocab'
import { cn } from '@/lib/utils'

export const Route = createFileRoute('/approvals')({
  head: () => ({ meta: [{ title: 'Approvals · Agent Supervision' }] }),
  component: ApprovalsPage,
})

const SORTS = { oldest: 'Oldest first', confidence: 'Lowest confidence first', desk: 'Desk' } as const
const OVERDUE_MINUTES = 30
const APPROVAL_MODE: Record<string, string> = {
  unscripted: 'replies that go beyond the standard outcome wait here',
  all: 'every reply and close waits here',
  off: 'nothing waits; the agent acts within the rules',
}

type Row = ApprovalRow & { desk: ServiceDesk }

function ApprovalsPage() {
  const { selected, desk } = usePortal()
  const now = useNow()
  const [query, setQuery] = React.useState('')
  const [sort, setSort] = React.useState<keyof typeof SORTS>('oldest')
  const data = useDeskQueries<Approvals>(selected, 'approvals')

  const pending: Row[] = data.results
    .flatMap((r) => (r.data?.pending ?? []).map((a) => ({ ...a, desk: r.desk })))
    .filter((r) => matchesQuery(query, r.ticket_number, r.why ?? '', r.desk.title))
    .sort((a, b) => {
      if (sort === 'confidence') return (a.confidence ?? 1) - (b.confidence ?? 1)
      if (sort === 'desk') return a.desk.title.localeCompare(b.desk.title) || a.created_at.localeCompare(b.created_at)
      return a.created_at.localeCompare(b.created_at)
    })
  const decided: Row[] = data.results
    .flatMap((r) => (r.data?.decided ?? []).map((a) => ({ ...a, desk: r.desk })))
    .sort((a, b) => (b.decided_at ?? b.created_at).localeCompare(a.decided_at ?? a.created_at))
    .slice(0, 25)
  const modes = [...new Set(selected.map((d) => d.approval_mode))]

  return (
    <>
      <PageHeader
        title="Approvals"
        description="Replies the agents held back for a person to approve, oldest first."
        actions={<SearchField label="Search approvals" placeholder="Ticket number or reason" value={query} onChange={setQuery} />}
      />

      <div className="flex items-start gap-3 rounded-[10px] bg-info-bg px-4 py-3 text-[13px] text-info-fg">
        <Clock aria-hidden className="mt-0.5 size-4 shrink-0" />
        <span>
          <strong className="font-semibold">Approving does not post straight away.</strong> An approved reply is posted on that desk&apos;s next pass, and only then
          shows as Posted. A rejected reply never reaches the branch: the agent leaves an escalation note with your comment instead. If the ticket changes before
          anyone decides, the reply is dropped and the agent decides again.
          {modes.length === 1 && modes[0] ? ` Approval setting: ${APPROVAL_MODE[modes[0]] ?? modes[0]}.` : ''}
        </span>
      </div>

      <DeskErrors errors={data.errors} />

      <div className="flex flex-wrap items-center justify-end gap-2.5">
        <Label className="flex-row items-center gap-2 font-normal">
          Sort
          <NativeSelect value={sort} onChange={(e) => setSort(e.target.value as keyof typeof SORTS)} className="h-9">
            {Object.entries(SORTS).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </NativeSelect>
        </Label>
      </div>

      <Card>
        {data.isLoading && !pending.length ? (
          <Loading />
        ) : pending.length ? (
          <Table className="min-w-[940px] [&_td]:px-3 [&_td:first-child]:pl-5 [&_td:last-child]:pr-5 [&_th]:px-3 [&_th:first-child]:pl-5 [&_th:last-child]:pr-5">
            <TableCaption>Waiting is time since the reply was held. Rows waiting over {OVERDUE_MINUTES} min are tinted and listed on Overview.</TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>Ticket</TableHead>
                <TableHead>Desk</TableHead>
                <TableHead>Proposed action</TableHead>
                <TableHead>Held because</TableHead>
                <TableHead>Waiting</TableHead>
                <TableHead>Confidence</TableHead>
                <TableHead>
                  <span className="sr-only">Open</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {pending.map((r) => {
                const mins = minutesSince(r.created_at, now) ?? 0
                const overdue = mins > OVERDUE_MINUTES
                const changed = r.proposed_action && r.proposed_action !== r.action
                return (
                  <TableRow key={`${r.desk.key}-${r.id}`} className={cn(overdue && 'bg-warn-row')}>
                    <TableCell>
                      <TicketLink number={r.ticket_number} desk={r.desk.key} decision={r.decision_id} />
                    </TableCell>
                    <TableCell>{r.desk.title}</TableCell>
                    <TableCell>
                      <div className="flex flex-col items-start gap-1">
                        <StatusPill status={ACTION_STATUS[r.action]} label={actionLabel(r.action)} />
                        {changed ? (
                          <span className="text-xs text-warn-fg">
                            Changed from <s>{actionLabel(r.proposed_action)}</s> by a rule
                          </span>
                        ) : null}
                      </div>
                    </TableCell>
                    <TableCell className="max-w-[300px] text-secondary-foreground">
                      <span className="line-clamp-2" title={r.why ?? ''}>
                        {r.why ? sentence(r.why) : '—'}
                      </span>
                    </TableCell>
                    <TableCell>
                      <span className={cn('font-mono whitespace-nowrap', overdue && 'font-semibold text-warn-fg')}>{span(mins)}</span>
                    </TableCell>
                    <TableCell>
                      <Confidence value={r.confidence} className="min-w-[92px]" />
                    </TableCell>
                    <TableCell className="w-px text-right whitespace-nowrap">
                      <Link
                        to="/tickets/$ticketNumber"
                        params={{ ticketNumber: r.ticket_number }}
                        search={{ desk: r.desk.key, decision: r.decision_id, from: 'approvals' }}
                        aria-label={`Review ticket ${r.ticket_number}`}
                        className="inline-flex h-9 items-center rounded-md border border-primary px-3 text-[13px] font-semibold text-primary-strong no-underline hover:bg-accent"
                      >
                        Review
                      </Link>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        ) : (
          <Empty
            title={`Nothing waiting${desk === 'all' ? '' : ` on ${selected[0]?.title}`}`}
            icon={
              <span aria-hidden className="flex size-10 items-center justify-center rounded-[10px] bg-ok-bg text-ok-fg">
                <Check className="size-[18px]" />
              </span>
            }
          >
            {query ? 'No held reply matches the search.' : emptyNote(selected)}
          </Empty>
        )}
      </Card>

      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between">
          <CardTitle>Decided recently</CardTitle>
          <CardDescription>Last 7 days, newest first</CardDescription>
        </CardHeader>
        {decided.length ? (
          <Table className="min-w-[760px]">
            <TableHeader>
              <TableRow>
                <TableHead>Ticket</TableHead>
                <TableHead>Desk</TableHead>
                <TableHead>Action</TableHead>
                <TableHead>Where it stands</TableHead>
                <TableHead>Decided by</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {decided.map((r) => (
                <TableRow key={`${r.desk.key}-${r.id}`}>
                  <TableCell>
                    <TicketLink number={r.ticket_number} desk={r.desk.key} decision={r.decision_id} />
                  </TableCell>
                  <TableCell>{r.desk.title}</TableCell>
                  <TableCell>{actionLabel(r.action)}</TableCell>
                  <TableCell>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <StatusPill status={approvalStatus(r.status)} />
                      {r.edited ? <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-secondary-foreground">edited</span> : null}
                    </div>
                  </TableCell>
                  <TableCell className="text-secondary-foreground">
                    {r.decided_by ? `${r.decided_by} · ${when(r.decided_at, now)}` : r.status === 'dropped' ? `Dropped ${when(r.finished_at, now)}` : '—'}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <p className="m-0 px-5 py-4 text-[13px] text-muted-foreground">{data.isLoading ? 'Loading…' : 'None in the last 7 days.'}</p>
        )}
      </Card>
    </>
  )
}

function emptyNote(desks: ServiceDesk[]) {
  if (desks.length === 1) {
    const d = desks[0]
    if (d.approval_mode === 'off') return `${d.title} is set to act without approval, so nothing waits here.`
    if (d.effective_mode !== 'live') return `${d.title} is in ${d.effective_mode} mode, so it posts nothing and holds nothing for approval.`
  }
  return 'No reply is waiting for a decision.'
}
