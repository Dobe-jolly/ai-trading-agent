import * as React from 'react'
import { createFileRoute } from '@tanstack/react-router'
import { ArrowRight, Download } from 'lucide-react'

import { Confidence } from '@/components/confidence'
import { DeskErrors, Empty, Loading, TicketLink } from '@/components/data-state'
import { PageHeader } from '@/components/page-header'
import { StatusPill } from '@/components/status-pill'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input, Label, NativeSelect } from '@/components/ui/input'
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useDeskQueries } from '@/lib/api/hooks'
import { deskPath } from '@/lib/api/client'
import type { DecisionList, DecisionSummary, ServiceDesk } from '@/lib/api/types'
import { count, sentence, useNow, when } from '@/lib/format'
import { usePortal } from '@/lib/portal'
import { ACTIONS, ACTION_LABEL, ACTION_STATUS, actionLabel, decisionOutcome } from '@/lib/vocab'
import { cn } from '@/lib/utils'

type Show = 'all' | 'guardrail' | 'error' | 'fallback'
interface DecisionsSearch {
  show?: Show
}

export const Route = createFileRoute('/decisions')({
  validateSearch: (s: Record<string, unknown>): DecisionsSearch => ({
    show: s.show === 'guardrail' || s.show === 'error' || s.show === 'fallback' ? s.show : undefined,
  }),
  head: () => ({ meta: [{ title: 'Decisions · Agent Supervision' }] }),
  component: DecisionsPage,
})

const PAGE = 50

function DecisionsPage() {
  const { selected } = usePortal()
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  const now = useNow()
  const [action, setAction] = React.useState('')
  const [days, setDays] = React.useState('7')
  const [review, setReview] = React.useState('')
  const [q, setQ] = React.useState('')
  const [limit, setLimit] = React.useState(PAGE)
  const show: Show = search.show ?? 'all'
  const ticket = /^[A-Za-z0-9-]{1,20}$/.test(q.trim().replace(/^#/, '')) ? q.trim().replace(/^#/, '') : undefined

  const params = { days, action: action || undefined, review: review || undefined, flag: show === 'all' ? undefined : show, q: ticket, limit }
  const data = useDeskQueries<DecisionList>(selected, 'decisions', params)
  const rows = data.results
    .flatMap((r) => (r.data?.items ?? []).map((d) => ({ ...d, desk: r.desk })))
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
  const total = data.results.reduce((n, r) => n + (r.data?.total ?? 0), 0)
  const more = data.results.some((r) => (r.data?.total ?? 0) > (r.data?.items.length ?? 0))

  return (
    <>
      <PageHeader
        title="Decisions"
        description="Every proposal the agents made, what the rules did to it, and what actually happened."
        actions={
          selected.length === 1 ? (
            <Button variant="secondary" asChild>
              <a href={`${deskPath(selected[0].key, 'export/decisions.csv')}?${new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined).map(([k, v]) => [k, String(v)])).toString()}`}>
                <Download aria-hidden />
                Export CSV
              </a>
            </Button>
          ) : null
        }
      />

      <form className="flex flex-wrap items-end gap-3 rounded-xl border border-border bg-card px-4 py-3.5" onSubmit={(e) => e.preventDefault()} aria-label="Filters">
        <Label className="flex-[1_1_200px]">
          Ticket number
          <Input value={q} onChange={(e) => { setQ(e.target.value); setLimit(PAGE) }} placeholder="e.g. 600512" className="h-10" inputMode="search" />
        </Label>
        <Label>
          Outcome
          <NativeSelect value={action} onChange={(e) => { setAction(e.target.value); setLimit(PAGE) }} className="h-10">
            <option value="">Any outcome</option>
            {ACTIONS.map((a) => (
              <option key={a} value={a}>
                {ACTION_LABEL[a]}
              </option>
            ))}
            <option value="held">Waiting for approval</option>
          </NativeSelect>
        </Label>
        <Label>
          Show
          <NativeSelect value={show} onChange={(e) => { navigate({ search: { show: e.target.value === 'all' ? undefined : (e.target.value as Show) } }); setLimit(PAGE) }} className="h-10">
            <option value="all">Everything</option>
            <option value="guardrail">Changed by a rule</option>
            <option value="error">Failed</option>
            <option value="fallback">Decided by the rules (model down)</option>
          </NativeSelect>
        </Label>
        <Label>
          Review
          <NativeSelect value={review} onChange={(e) => { setReview(e.target.value); setLimit(PAGE) }} className="h-10">
            <option value="">Any</option>
            <option value="unreviewed">Not reviewed</option>
            <option value="correct">Marked correct</option>
            <option value="incorrect">Marked incorrect</option>
          </NativeSelect>
        </Label>
        <Label>
          Period
          <NativeSelect value={days} onChange={(e) => { setDays(e.target.value); setLimit(PAGE) }} className="h-10">
            <option value="1">Last 24 hours</option>
            <option value="7">Last 7 days</option>
            <option value="14">Last 14 days</option>
            <option value="30">Last 30 days</option>
          </NativeSelect>
        </Label>
      </form>

      <DeskErrors errors={data.errors} />

      <Card>
        {data.isLoading && !rows.length ? (
          <Loading />
        ) : rows.length ? (
          <Table className="min-w-[900px]">
            <TableCaption>
              Showing {count(rows.length)} of {count(total)} decision{total === 1 ? '' : 's'}, newest first. Audit replays are left out.
            </TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>Ticket · desk</TableHead>
                <TableHead>Proposed → final</TableHead>
                <TableHead>Rule</TableHead>
                <TableHead>Confidence</TableHead>
                <TableHead>Result</TableHead>
                <TableHead>Review</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((d) => (
                <DecisionRow key={`${d.desk.key}-${d.id}`} d={d} now={now} />
              ))}
            </TableBody>
          </Table>
        ) : (
          <Empty title="No decisions match">Try a longer period or fewer filters.</Empty>
        )}
        {more ? (
          <div className="border-t border-border-soft px-5 py-3">
            <Button variant="secondary" size="sm" onClick={() => setLimit((l) => l + PAGE)} disabled={data.isLoading}>
              Show {PAGE} more per desk
            </Button>
          </div>
        ) : null}
      </Card>
    </>
  )
}

function DecisionRow({ d, now }: { d: DecisionSummary & { desk: ServiceDesk }; now: number }) {
  const outcome = decisionOutcome(d)
  const changed = d.guardrail_changed
  return (
    <TableRow>
      <TableCell className="w-[92px] font-mono text-xs text-secondary-foreground">{when(d.created_at, now)}</TableCell>
      <TableCell>
        <div className="flex flex-col">
          <TicketLink number={d.ticket_number} desk={d.desk.key} decision={d.id} from="decisions" />
          <span className="text-xs text-muted-foreground">{d.desk.title}</span>
        </div>
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap items-center gap-1.5">
          {changed ? <span className="text-[13px] text-warn-fg line-through">{actionLabel(d.proposed_action)}</span> : null}
          {changed ? <ArrowRight aria-hidden className="size-3.5 text-faint" /> : null}
          {d.final_action ? <StatusPill status={ACTION_STATUS[d.final_action]} label={actionLabel(d.final_action)} /> : <span className="text-muted-foreground">—</span>}
        </div>
      </TableCell>
      <TableCell className="max-w-[190px] min-w-[150px]">
        <span className={cn('line-clamp-2 text-xs', changed ? 'text-warn-fg' : 'text-muted-foreground')} title={d.guardrail_notes.join('; ')}>
          {d.error ? d.error : d.guardrail_notes[0] ? sentence(d.guardrail_notes[0]) : d.category === 'rules_fallback' ? 'Decided by the rules' : 'All rules passed'}
        </span>
      </TableCell>
      <TableCell>
        <Confidence value={d.confidence} className="min-w-[76px]" />
      </TableCell>
      <TableCell>
        <StatusPill status={outcome.status} label={outcome.label} />
      </TableCell>
      <TableCell>
        {d.review ? <StatusPill size="sm" status={d.review === 'correct' ? 'reviewCorrect' : 'reviewIncorrect'} /> : <span className="text-xs text-muted-foreground">—</span>}
      </TableCell>
    </TableRow>
  )
}
