import * as React from 'react'
import { Link, createFileRoute, useNavigate } from '@tanstack/react-router'
import { ExternalLink } from 'lucide-react'

import { DeskErrors, ErrorNote, Loading, TICKET_FROM, TicketLink, type TicketFrom } from '@/components/data-state'
import { Mono, StepEyebrow } from '@/components/page-header'
import { isPlainKey } from '@/components/shell/go-to'
import { ModeChip, StatusPill } from '@/components/status-pill'
import { ActionBar } from '@/components/ticket/action-bar'
import { EvidencePanel } from '@/components/ticket/evidence-panel'
import { OutcomeTimeline } from '@/components/ticket/outcome-timeline'
import { RecommendationPanel } from '@/components/ticket/recommendation-panel'
import { RulesPanel } from '@/components/ticket/rules-panel'
import { TeachPanel } from '@/components/ticket/teach-panel'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { useDeskQueries, useDeskQuery } from '@/lib/api/hooks'
import type { Approvals, DecisionDetail, DecisionList, ServiceDesk } from '@/lib/api/types'
import { useNow, when } from '@/lib/format'
import { usePortal } from '@/lib/portal'
import { buildTimeline } from '@/lib/timeline'
import { firstMessage, lookups, ticketHeader } from '@/lib/transcript'
import { decisionOutcome } from '@/lib/vocab'

interface TicketSearch {
  desk?: string
  decision?: number
  from?: TicketFrom
}

export const Route = createFileRoute('/tickets/$ticketNumber')({
  validateSearch: (s: Record<string, unknown>): TicketSearch => ({
    desk: typeof s.desk === 'string' && /^[a-z][a-z0-9_]*$/.test(s.desk) ? s.desk : undefined,
    decision: Number.isInteger(Number(s.decision)) && Number(s.decision) > 0 ? Number(s.decision) : undefined,
    from: TICKET_FROM.includes(s.from as TicketFrom) ? (s.from as TicketFrom) : undefined,
  }),
  head: ({ params }) => ({ meta: [{ title: `#${params.ticketNumber} · Agent Supervision` }] }),
  component: TicketPage,
})

/** A ticket's newest decision on any desk, unless the address names one. */
function TicketPage() {
  const { ticketNumber } = Route.useParams()
  const { desk, decision, from } = Route.useSearch()
  const { desks, deskByKey } = usePortal()

  if (desk && decision && deskByKey(desk)) {
    return <Investigation deskInfo={deskByKey(desk)!} decisionId={decision} ticketNumber={ticketNumber} from={from} />
  }
  return <FindLatest ticketNumber={ticketNumber} desks={desk && deskByKey(desk) ? [deskByKey(desk)!] : desks} from={from} />
}

function FindLatest({ ticketNumber, desks, from }: { ticketNumber: string; desks: ServiceDesk[]; from?: TicketFrom }) {
  const valid = /^[A-Za-z0-9-]{1,20}$/.test(ticketNumber)
  const found = useDeskQueries<DecisionList>(valid ? desks : [], 'decisions', { q: ticketNumber, days: 365, limit: 20 })
  if (!valid) return <NotFound ticketNumber={ticketNumber} />
  if (found.isLoading) return <Loading label={`Looking for #${ticketNumber} on every desk…`} />
  const latest = found.results
    .flatMap((r) => (r.data?.items ?? []).filter((d) => d.ticket_number === ticketNumber).map((d) => ({ d, desk: r.desk })))
    .sort((a, b) => b.d.created_at.localeCompare(a.d.created_at))[0]
  if (!latest) {
    return (
      <>
        <DeskErrors errors={found.errors} />
        <NotFound ticketNumber={ticketNumber} />
      </>
    )
  }
  return <Investigation deskInfo={latest.desk} decisionId={latest.d.id} ticketNumber={ticketNumber} from={from} />
}

function Investigation({ deskInfo, decisionId, ticketNumber, from }: { deskInfo: ServiceDesk; decisionId: number; ticketNumber: string; from?: TicketFrom }) {
  const { desks } = usePortal()
  const now = useNow()
  const query = useDeskQuery<DecisionDetail>(deskInfo.key, `decisions/${decisionId}`)
  const approvals = useDeskQueries<Approvals>(from === 'approvals' ? desks : [], 'approvals')
  const navigate = useNavigate()

  const queue = approvals.results.flatMap((r) => (r.data?.pending ?? []).map((a) => ({ ...a, desk: r.desk }))).sort((a, b) => a.created_at.localeCompare(b.created_at))
  const isThis = (a: { decision_id: number; desk: ServiceDesk }) => a.decision_id === decisionId && a.desk.key === deskInfo.key
  const position = queue.findIndex(isThis)
  // The one after this in the queue (oldest first); once this one is decided and drops out, the oldest left.
  const next = (position >= 0 ? queue[position + 1] : undefined) ?? queue.find((a) => !isThis(a))
  const prev = position > 0 ? queue[position - 1] : undefined

  // J / K: next and previous reply in the approvals queue, as in the classic console.
  React.useEffect(() => {
    if (from !== 'approvals') return
    const onKey = (e: KeyboardEvent) => {
      if (!isPlainKey(e)) return
      const k = e.key.toLowerCase()
      const to = k === 'j' ? next : k === 'k' ? prev : undefined
      if (!to) return
      e.preventDefault()
      navigate({ to: '/tickets/$ticketNumber', params: { ticketNumber: to.ticket_number }, search: { desk: to.desk.key, decision: to.decision_id, from: 'approvals' } })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [from, next, prev, navigate])

  if (query.isPending) return <Loading label={`Loading #${ticketNumber}…`} />
  if (query.error || !query.data) {
    return (
      <>
        <Breadcrumb ticketNumber={ticketNumber} from={from} />
        <ErrorNote error={query.error} />
      </>
    )
  }

  const d = query.data
  const header = ticketHeader(d.transcript)
  const found = lookups(d.transcript)
  const outcome = decisionOutcome({ ...d, approval: d.approval?.status ?? null })
  const { steps, note } = buildTimeline(d, deskInfo, now)
  const message = firstMessage(header.text)
  const osticket = deskInfo.osticket_ticket_url?.replace('{ticket_id}', String(d.ticket_id)).replace('{number}', d.ticket_number)


  return (
    <>
      <div className="-mt-1 flex flex-wrap items-center justify-between gap-2 text-[13px]">
        <Breadcrumb ticketNumber={ticketNumber} from={from} />
        {from === 'approvals' && queue.length ? (
          <span className="text-muted-foreground">
            {position >= 0 ? `${position + 1} of ${queue.length} waiting` : `${queue.length} waiting`}
            {next ? (
              <>
                {' · '}
                <Link to="/tickets/$ticketNumber" params={{ ticketNumber: next.ticket_number }} search={{ desk: next.desk.key, decision: next.decision_id, from: 'approvals' }} className="font-medium">
                  Next in queue: #{next.ticket_number}
                  {next.desk.key !== deskInfo.key ? ` (${next.desk.title})` : ''}
                </Link>
              </>
            ) : null}
          </span>
        ) : null}
      </div>

      <Card>
        <CardContent className="flex flex-col gap-4 px-6 py-5">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="flex min-w-0 flex-[1_1_520px] flex-col gap-2">
              <StepEyebrow n={1}>The ticket</StepEyebrow>
              <div className="flex flex-wrap items-center gap-3">
                <Mono className="rounded-md bg-muted px-2 py-[3px] text-[15px] font-semibold">#{d.ticket_number}</Mono>
                <h1 className="m-0 text-[22px] font-semibold tracking-[-0.01em]">{header.subject ?? 'Ticket'}</h1>
              </div>
            </div>
            {osticket ? (
              <a
                href={osticket}
                target="_blank"
                rel="noopener"
                className="inline-flex h-10 items-center gap-2 rounded-md border border-input bg-card px-4 text-sm font-medium text-foreground no-underline hover:bg-muted"
              >
                <ExternalLink aria-hidden className="size-4" />
                Open in osTicket
              </a>
            ) : null}
          </div>
          <dl className="m-0 grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-x-6 gap-y-3.5 text-[13px]">
            <Meta label="Desk">{deskInfo.title}</Meta>
            <Meta label="Raised by">{header.branch ?? '—'}</Meta>
            <Meta label="Help topic">{header.topic ?? '—'}</Meta>
            <Meta label="Decided">
              <span className="inline-flex flex-wrap items-center gap-1.5">
                <Mono>{when(d.created_at, now)}</Mono>
                <ModeChip mode={d.mode === 'audit' ? 'shadow' : d.mode} className="h-[22px] px-2 text-[11px]" />
              </span>
            </Meta>
            <Meta label="Where it stands">
              <StatusPill status={outcome.status} label={outcome.label} />
            </Meta>
          </dl>
          {message ? (
            <figure className="m-0 flex flex-col gap-1 rounded-lg bg-background px-3.5 py-3">
              <figcaption className="text-xs text-muted-foreground">From the ticket owner{header.branch ? ` · ${header.branch}` : ''}</figcaption>
              <blockquote className="m-0 text-[13px] whitespace-pre-line">{message}</blockquote>
            </figure>
          ) : null}
          {header.text ? (
            <Collapsible>
              <CollapsibleTrigger className="text-[13px] font-medium text-primary hover:underline">The ticket as the agent read it (masked)</CollapsibleTrigger>
              <CollapsibleContent>
                <pre className="mt-2 mb-0 max-h-[360px] overflow-auto rounded-lg bg-code px-3.5 py-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-code-foreground">{header.text}</pre>
              </CollapsibleContent>
            </Collapsible>
          ) : null}
        </CardContent>
      </Card>

      <div className="flex flex-wrap items-start gap-5">
        <EvidencePanel decision={d} lookups={found} vocab={deskInfo.vocab} />
        <RecommendationPanel decision={d} vocab={deskInfo.vocab} />
      </div>
      <RulesPanel decision={d} />
      <OutcomeTimeline steps={steps} note={note} />
      <ActionBar decision={d} desk={deskInfo} />
      <TeachPanel key={`${deskInfo.key}-${d.id}`} decision={d} desk={deskInfo} topic={header.topic ?? null} />

      {d.history?.length ? (
        <Card>
          <CardHeader>
            <CardTitle>Earlier decisions on this ticket</CardTitle>
          </CardHeader>
          <ul className="m-0 list-none p-0">
            {d.history.map((h) => {
              const o = decisionOutcome(h)
              return (
                <li key={h.id} className="flex flex-wrap items-center gap-3 border-b border-border-soft px-5 py-2.5 text-[13px] last:border-0">
                  <TicketLink number={h.ticket_number} desk={deskInfo.key} decision={h.id}>
                    {when(h.created_at, now)}
                  </TicketLink>
                  <StatusPill status={o.status} label={o.label} />
                  {h.guardrail_changed ? <span className="text-xs text-warn-fg">changed by a rule</span> : null}
                </li>
              )
            })}
          </ul>
        </Card>
      ) : null}
    </>
  )
}

const BACK: Record<TicketFrom | 'tickets', { to: '/approvals' | '/decisions' | '/review' | '/tickets'; label: string }> = {
  approvals: { to: '/approvals', label: 'Approvals' },
  decisions: { to: '/decisions', label: 'Decisions' },
  review: { to: '/review', label: 'Review' },
  tickets: { to: '/tickets', label: 'Open tickets' },
}

function Breadcrumb({ ticketNumber, from }: { ticketNumber: string; from?: TicketFrom }) {
  const back = BACK[from ?? 'tickets']
  return (
    <nav aria-label="Breadcrumb" className="flex items-center gap-2 text-[13px] text-muted-foreground">
      <Link to={back.to} className="font-medium">
        {back.label}
      </Link>
      <span aria-hidden>/</span>
      <Mono aria-current="page" className="text-foreground">
        #{ticketNumber}
      </Mono>
    </nav>
  )
}

function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col items-start gap-1">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="m-0 font-medium">{children}</dd>
    </div>
  )
}

function NotFound({ ticketNumber }: { ticketNumber: string }) {
  return (
    <div className="flex flex-col items-start gap-2 py-12">
      <h1 className="m-0 text-2xl font-semibold">No decision on #{ticketNumber}</h1>
      <p className="m-0 text-muted-foreground">No desk has decided anything on this ticket in the last year. It may not have been read yet, or it belongs to a desk that is switched off.</p>
      <Link to="/tickets" className="font-medium">
        Back to open tickets
      </Link>
    </div>
  )
}
