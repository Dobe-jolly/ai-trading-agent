import * as React from 'react'
import { createFileRoute } from '@tanstack/react-router'
import { Lock, Pause, Play } from 'lucide-react'

import { DeskErrors, ErrorNote, Loading } from '@/components/data-state'
import { PageHeader } from '@/components/page-header'
import { ModeChip, StatusPill } from '@/components/status-pill'
import { Button } from '@/components/ui/button'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { SegmentedGroup, SegmentedItem } from '@/components/ui/toggle-group'
import { useDeskControls, useDeskQueries, usePauseService } from '@/lib/api/hooks'
import type { Controls, Hold, ServiceDesk } from '@/lib/api/types'
import { span, useNow, when } from '@/lib/format'
import { deskState, usePortal } from '@/lib/portal'
import { MODE } from '@/lib/status'

export const Route = createFileRoute('/controls')({
  head: () => ({ meta: [{ title: 'Controls · Agent Supervision' }] }),
  component: ControlsPage,
})

type HoldChoice = 'none' | Hold
const RANK = { shadow: 0, notes: 1, live: 2 } as const
const HOLD_NOTE: Record<HoldChoice, string> = {
  none: 'No hold: runs as configured',
  notes: 'Decisions become internal notes',
  shadow: 'Decides and logs only',
}

function ControlsPage() {
  const { selected, isAdmin, service } = usePortal()
  const data = useDeskQueries<Controls>(selected, 'controls')
  const now = useNow()

  const events = data.results
    .flatMap((r) => (r.data?.events ?? []).map((e) => ({ ...e, desk: r.desk })))
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .filter((e, i, all) => !e.action.includes('every desk') || all.findIndex((x) => x.action === e.action && x.created_at === e.created_at && x.actor === e.actor) === i)
    .slice(0, 40)

  return (
    <>
      <PageHeader
        title="Controls"
        description="Pause a desk, or hold it back to Notes or Shadow. The portal can only make a desk more careful: going Live is a server setting."
        actions={
          !isAdmin ? (
            <span className="inline-flex items-center gap-2 rounded-lg bg-muted px-3 py-2 text-[13px] text-secondary-foreground">
              <Lock aria-hidden className="size-3.5" />
              Read-only — controls need an Admin
            </span>
          ) : null
        }
      />
      <DeskErrors errors={data.errors} />

      <ServiceCard paused={service.paused} />

      <Card>
        {data.isLoading && !data.results.some((r) => r.data) ? (
          <Loading />
        ) : (
          <Table className="min-w-[920px]">
            <TableHeader>
              <TableRow>
                <TableHead>Desk · mode now</TableHead>
                <TableHead>Running</TableHead>
                <TableHead>Hold</TableHead>
                <TableHead>Live</TableHead>
                <TableHead>Last change</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.results.map(({ desk, data: c }) => (
                <DeskRow key={desk.key} desk={desk} controls={c} now={now} />
              ))}
            </TableBody>
          </Table>
        )}
      </Card>

      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between">
          <CardTitle>Control log</CardTitle>
          <CardDescription>Every pause, resume, hold, release, approval and re-send, newest first</CardDescription>
        </CardHeader>
        {events.length ? (
          <ul className="m-0 list-none p-0">
            {events.map((e) => (
              <li key={`${e.desk.key}-${e.id}`} className="flex flex-wrap gap-x-4 gap-y-1 border-b border-border-soft px-5 py-[11px] text-[13px] last:border-0">
                <span className="w-[150px] shrink-0 font-mono text-xs whitespace-nowrap text-secondary-foreground">{when(e.created_at, now)}</span>
                <span className="w-[150px] shrink-0 text-muted-foreground">{e.action.includes('every desk') ? 'Every desk' : e.desk.title}</span>
                <span className="min-w-0 flex-[1_1_300px]">
                  <strong className="font-semibold">{e.actor}</strong> {e.action}
                </span>
                {e.detail ? <span className="text-xs text-muted-foreground">“{e.detail}”</span> : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="m-0 px-5 py-4 text-[13px] text-muted-foreground">Nothing changed from the console yet.</p>
        )}
      </Card>
    </>
  )
}

function ServiceCard({ paused }: { paused: boolean }) {
  const { isAdmin, desks } = usePortal()
  const pause = usePauseService()
  const [confirming, setConfirming] = React.useState(false)
  const [note, setNote] = React.useState('')
  return (
    <Card className="flex-row flex-wrap items-center justify-between gap-x-6 gap-y-3 px-5 py-4">
      <div className="flex flex-col gap-1">
        <h2 className="m-0 text-[15px] font-semibold">Every desk</h2>
        <span className="text-[13px] text-secondary-foreground">
          {paused ? 'Paused: no desk runs until the service is resumed.' : `${desks.length} desks run one after another on each pass of the service timer.`}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2.5">
        <span className="inline-flex items-center gap-2 text-xs text-muted-foreground">
          <Lock aria-hidden className="size-3.5" />
          Restarting the service is done on the server, not here.
        </span>
        {paused ? (
          <Button variant="outline" size="sm" disabled={!isAdmin || pause.isPending} onClick={() => pause.mutate({ pause: false })}>
            <Play aria-hidden />
            Resume every desk
          </Button>
        ) : confirming ? (
          <form
            className="flex flex-wrap items-center gap-1.5"
            onSubmit={(e) => {
              e.preventDefault()
              pause.mutate({ pause: true, note: note.trim() }, { onSuccess: () => { setConfirming(false); setNote('') } })
            }}
          >
            <Input value={note} onChange={(e) => setNote(e.target.value)} aria-label="Reason for pausing every desk" placeholder="Reason (shown in the log)" className="h-9 w-56" required maxLength={300} />
            <Button type="submit" variant="destructive" size="sm" disabled={pause.isPending}>
              Pause every desk
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </form>
        ) : (
          <Button variant="destructive" size="sm" disabled={!isAdmin} onClick={() => setConfirming(true)}>
            <Pause aria-hidden />
            Pause every desk
          </Button>
        )}
      </div>
      <ErrorNote error={pause.error} className="basis-full" />
    </Card>
  )
}

function DeskRow({ desk, controls, now }: { desk: ServiceDesk; controls?: Controls; now: number }) {
  const { isAdmin, service } = usePortal()
  const change = useDeskControls()
  const [pending, setPending] = React.useState<null | { kind: 'pause' } | { kind: 'hold'; hold: HoldChoice }>(null)
  const [note, setNote] = React.useState('')
  const state = deskState(desk, service.paused)
  const hold: HoldChoice = controls?.mode_override ?? desk.mode_override ?? 'none'
  const configured = controls?.configured_mode ?? desk.configured_mode
  const last = controls?.events[0]
  // A hold only means something when it is more careful than the configured mode.
  const usable = (h: HoldChoice) => h === 'none' || RANK[h] < RANK[configured]

  const apply = () => {
    if (!pending) return
    const body = pending.kind === 'pause' ? { kill_switch: true } : { mode_override: pending.hold === 'none' ? null : pending.hold }
    change.mutate({ desk: desk.key, ...body, note: note.trim() }, { onSuccess: () => { setPending(null); setNote('') } })
  }

  return (
    <TableRow className="align-top">
      <TableCell className="py-4">
        <div className="flex flex-col items-start gap-1.5">
          <span className="font-semibold">{desk.title}</span>
          <ModeChip desk={controls ?? desk} />
        </div>
      </TableCell>
      <TableCell className="py-4">
        <div className="flex flex-col items-start gap-2">
          <StatusPill status={state} label={state === 'stale' ? `Stale · ${span(desk.health.minutes_since_last_run)}` : undefined} />
          {desk.kill_switch ? (
            <Button variant="outline" size="sm" disabled={!isAdmin || change.isPending} onClick={() => change.mutate({ desk: desk.key, kill_switch: false })}>
              <Play aria-hidden />
              Resume desk
            </Button>
          ) : (
            <Button variant="destructive" size="sm" disabled={!isAdmin || pending !== null} onClick={() => setPending({ kind: 'pause' })}>
              <Pause aria-hidden />
              Pause desk
            </Button>
          )}
          {state === 'stale' || state === 'neverRan' ? <span className="max-w-[220px] text-xs text-muted-foreground">Pausing won’t fix a stale desk; the service needs checking on the server.</span> : null}
          {service.paused && !desk.kill_switch ? <span className="max-w-[220px] text-xs text-muted-foreground">Every desk is paused above.</span> : null}
        </div>
      </TableCell>
      <TableCell className="py-4">
        <div className="flex flex-col gap-1.5">
          <SegmentedGroup
            type="single"
            value={pending?.kind === 'hold' ? pending.hold : hold}
            onValueChange={(v) => v && v !== hold && setPending({ kind: 'hold', hold: v as HoldChoice })}
            disabled={!isAdmin || change.isPending}
            aria-label={`Hold for ${desk.title}`}
            className="self-start"
          >
            {(['none', 'notes', 'shadow'] as HoldChoice[]).map((h) => (
              <SegmentedItem
                key={h}
                value={h}
                disabled={!usable(h)}
                title={usable(h) ? undefined : `Configured ${MODE[configured].label.toLowerCase()} is already as careful`}
                className="h-8 px-3 text-xs font-semibold capitalize"
              >
                {h}
              </SegmentedItem>
            ))}
          </SegmentedGroup>
          <span className="text-xs text-muted-foreground">{HOLD_NOTE[hold]}</span>
        </div>
      </TableCell>
      <TableCell className="py-4">
        <span className="flex max-w-[190px] items-start gap-2 text-xs text-secondary-foreground">
          <Lock aria-hidden className="mt-px size-3.5 shrink-0" />
          {configured === 'live' ? 'Live permitted · set in server configuration' : `Configured ${MODE[configured].label.toLowerCase()} on the server; going live is a server change`}
        </span>
      </TableCell>
      <TableCell className="py-4">
        {pending ? (
          <form
            className="flex max-w-[360px] flex-col gap-2 rounded-lg border border-input bg-background p-2.5"
            onSubmit={(e) => {
              e.preventDefault()
              apply()
            }}
          >
            <strong className="text-[13px] font-semibold">
              {pending.kind === 'pause'
                ? `Pause ${desk.title}?`
                : pending.hold === 'none'
                  ? `Release the hold on ${desk.title}?`
                  : `Hold ${desk.title} at ${MODE[pending.hold].label}?`}
            </strong>
            <Input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              aria-label="Reason (shown in the control log)"
              placeholder={desk.vocab.pause_placeholder ?? 'Reason (shown in the control log)'}
              required={pending.kind === 'pause'}
              maxLength={300}
              className="h-9"
            />
            <div className="flex gap-1.5">
              <Button type="submit" size="sm" variant={pending.kind === 'pause' ? 'destructive' : 'default'} disabled={change.isPending}>
                {change.isPending ? 'Saving…' : 'Apply'}
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => { setPending(null); setNote('') }}>
                Cancel
              </Button>
            </div>
            <ErrorNote error={change.error} />
          </form>
        ) : last ? (
          <div className="flex flex-col gap-0.5 text-xs">
            <span>
              {last.actor} {last.action} · {when(last.created_at, now)}
            </span>
            {last.detail ? <span className="text-muted-foreground">“{last.detail}”</span> : null}
          </div>
        ) : (
          <span className="text-xs text-muted-foreground">No changes yet</span>
        )}
      </TableCell>
    </TableRow>
  )
}
