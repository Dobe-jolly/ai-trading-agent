import type * as React from 'react'
import { Link } from '@tanstack/react-router'
import * as ToggleGroupPrimitive from '@radix-ui/react-toggle-group'
import { OctagonPause, ShieldCheck, UserRound } from 'lucide-react'

import { MixedModeChip, ModeChip } from '@/components/status-pill'
import type { ServiceDesk } from '@/lib/api/types'
import { ago, span, useNow } from '@/lib/format'
import { deskState, isHeld, usePortal } from '@/lib/portal'
import { cn } from '@/lib/utils'

export function TopBar() {
  const { desk, setDesk, desks, service, isAdmin } = usePortal()
  const all = {
    approvals: desks.reduce((n, d) => n + d.approvals_pending, 0),
    incidents: desks.filter((d) => d.anomaly?.severity === 'incident').length,
  }

  return (
    <header className="flex flex-col gap-2.5 border-b border-border bg-card px-4 pt-2.5 pb-3 sm:px-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <ToggleGroupPrimitive.Root type="single" value={desk} onValueChange={(v) => v && setDesk(v)} aria-label="Desk" className="flex flex-wrap items-center gap-1">
          <span className="mr-1.5 text-xs font-semibold text-muted-foreground">Desk</span>
          <DeskButton value="all" name="All desks" approvals={all.approvals} incidents={all.incidents} />
          {desks.map((d) => {
            const state = deskState(d, service.paused)
            return (
              <DeskButton
                key={d.key}
                value={d.key}
                name={d.title}
                approvals={d.approvals_pending}
                incidents={d.anomaly?.severity === 'incident' ? 1 : 0}
                paused={d.kill_switch}
                stale={!service.paused && (state === 'stale' || state === 'neverRan')}
              />
            )
          })}
        </ToggleGroupPrimitive.Root>

        <span className="inline-flex items-center gap-2 text-xs text-muted-foreground">
          <UserRound aria-hidden className="size-3.5" />
          <span className="font-medium text-foreground">{service.user}</span>
          <span
            className={cn(
              'inline-flex h-6 items-center gap-1 rounded-full px-2 text-xs font-semibold',
              isAdmin ? 'bg-sidebar text-white' : 'bg-muted text-secondary-foreground',
            )}
          >
            {isAdmin ? <ShieldCheck aria-hidden className="size-3.5" /> : null}
            {isAdmin ? 'Admin' : 'Viewer'}
          </span>
        </span>
      </div>

      {service.paused ? (
        <div role="status" className="flex flex-wrap items-center gap-2 rounded-lg bg-paused-bg px-3 py-2 text-[13px] text-paused-fg">
          <OctagonPause aria-hidden className="size-4" />
          <strong className="font-semibold">Every desk is paused.</strong>
          <span>Nothing runs until an Admin resumes the service.</span>
          <Link to="/controls" className="font-medium">
            Controls
          </Link>
        </div>
      ) : null}

      <StatusStrip />
    </header>
  )
}

function DeskButton({ value, name, approvals, incidents, paused, stale }: { value: string; name: string; approvals: number; incidents: number; paused?: boolean; stale?: boolean }) {
  return (
    <ToggleGroupPrimitive.Item
      value={value}
      className="group inline-flex h-[38px] items-center gap-1.5 rounded-lg border border-border bg-card px-[11px] text-[13px] font-medium text-foreground outline-none transition-colors hover:bg-muted data-[state=on]:border-primary data-[state=on]:bg-primary data-[state=on]:font-semibold data-[state=on]:text-white"
    >
      <span>{name}</span>
      {approvals > 0 && (
        <Badge className="bg-warn-bg text-warn-fg" label={`${approvals} awaiting approval`}>
          {approvals}
        </Badge>
      )}
      {incidents > 0 && (
        <Badge className="bg-bad-bg text-bad-fg" label={`${incidents} open incident${incidents > 1 ? 's' : ''}`}>
          !{incidents}
        </Badge>
      )}
      {paused && <span className="inline-flex h-5 items-center rounded-full bg-paused-bg px-1.5 text-[11px] font-semibold text-paused-fg">Paused</span>}
      {stale && !paused && <span className="inline-flex h-5 items-center rounded-full bg-warn-bg px-1.5 text-[11px] font-semibold text-warn-fg">Stale</span>}
    </ToggleGroupPrimitive.Item>
  )
}

function Badge({ className, label, children }: { className: string; label: string; children: React.ReactNode }) {
  return (
    <span className={cn('inline-flex h-5 min-w-5 items-center justify-center rounded-full px-[5px] font-mono text-[11px] font-semibold', className)}>
      <span aria-hidden>{children}</span>
      <span className="sr-only">{label}</span>
    </span>
  )
}

/** Active desk, its mode and how fresh its data is — always visible. */
function StatusStrip() {
  const { desk, desks, service, updatedAt } = usePortal()
  const now = useNow(15_000)

  let title: string
  let mode: React.ReactNode
  let fresh: { tone: 'ok' | 'warn' | 'paused'; text: string }
  let extra: string | undefined

  if (desk === 'all') {
    title = 'All desks'
    const counts = new Map<string, number>()
    desks.forEach((d) => {
      const label = `${d.effective_mode === 'live' ? 'Live' : d.effective_mode === 'notes' ? 'Notes' : 'Shadow'}${isHeld(d) ? ' hold' : ''}`
      counts.set(label, (counts.get(label) ?? 0) + 1)
    })
    mode = <MixedModeChip label={[...counts].map(([label, n]) => `${n} ${label}`).join(' · ')} />
    const states = desks.map((d) => ({ d, state: deskState(d, service.paused) }))
    const stale = states.filter((s) => s.state === 'stale' || s.state === 'neverRan')
    const running = states.filter((s) => s.state === 'running')
    if (service.paused) {
      fresh = { tone: 'paused', text: 'Service paused' }
    } else if (stale.length) {
      fresh = {
        tone: 'warn',
        text: `${stale.map((s) => s.d.title).join(', ')} ${stale.length > 1 ? 'are' : 'is'} stale — ${staleText(stale[0].d)}`,
      }
      extra = running.length ? `Other running desks: ${lastPassRange(running.map((s) => s.d))}` : undefined
    } else if (running.length) {
      fresh = { tone: 'ok', text: `Last passes ${lastPassRange(running.map((s) => s.d))}` }
      const paused = states.filter((s) => s.state === 'paused')
      extra = paused.length ? `${paused.map((s) => s.d.title).join(', ')} paused` : undefined
    } else {
      fresh = { tone: 'paused', text: 'Every desk is paused' }
    }
  } else {
    const d = desks.find((x) => x.key === desk) as ServiceDesk
    title = d.title
    mode = <ModeChip desk={d} />
    const state = deskState(d, service.paused)
    if (state === 'paused') {
      fresh = { tone: 'paused', text: service.paused ? 'Paused with every desk' : 'Paused from the portal' }
      extra = d.health.last_run_at ? `Last pass ${ago(d.health.last_run_at, now)}` : undefined
    } else if (state === 'stale' || state === 'neverRan') {
      fresh = { tone: 'warn', text: `Stale — ${staleText(d)}` }
      extra = `Expected at least every ${d.health.stale_after_minutes} min`
    } else {
      fresh = { tone: 'ok', text: d.health.running ? 'Pass running now' : `Last pass ${ago(d.health.last_run_at, now)}` }
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-x-3.5 gap-y-2 text-[13px]">
      <span className="text-sm font-semibold">{title}</span>
      <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">Mode {mode}</span>
      <span
        role="status"
        className={cn(
          'inline-flex h-[26px] items-center gap-1.5 rounded-full px-2.5 text-xs font-semibold',
          fresh.tone === 'ok' && 'text-ok-fg',
          fresh.tone === 'warn' && 'bg-warn-bg text-warn-fg',
          fresh.tone === 'paused' && 'bg-paused-bg text-paused-fg',
        )}
      >
        <span aria-hidden className={cn('size-[7px] rounded-full', fresh.tone === 'ok' ? 'bg-ok-dot' : fresh.tone === 'warn' ? 'bg-warn-dot' : 'bg-paused-dot')} />
        {fresh.text}
      </span>
      {extra ? <span className="text-xs text-muted-foreground">{extra}</span> : null}
      <span className="ml-auto font-mono text-xs text-muted-foreground">
        Updated {updatedAt ? new Date(updatedAt).toLocaleTimeString('en-GB') : '—'} · refresh 30 s
      </span>
    </div>
  )
}

function staleText(d: ServiceDesk) {
  return d.health.minutes_since_last_run == null ? 'it has never reported a pass' : `no pass in ${span(d.health.minutes_since_last_run)}`
}

function lastPassRange(ds: ServiceDesk[]) {
  const mins = ds.map((d) => d.health.minutes_since_last_run ?? 0)
  const lo = Math.floor(Math.min(...mins))
  const hi = Math.floor(Math.max(...mins))
  return lo === hi ? `${span(lo)} ago` : `${lo}–${span(hi)} ago`
}
