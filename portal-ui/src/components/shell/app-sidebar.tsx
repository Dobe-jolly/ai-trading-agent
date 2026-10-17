import { Link, useRouterState } from '@tanstack/react-router'
import {
  Activity,
  BookOpenText,
  ClipboardCheck,
  Gauge,
  History,
  Inbox,
  ListChecks,
  Mail,
  ScanSearch,
  Search,
  SlidersHorizontal,
  type LucideIcon,
} from 'lucide-react'

import { openGoTo } from '@/components/shell/go-to'
import { usePortal } from '@/lib/portal'
import { cn } from '@/lib/utils'

type NavTo = '/' | '/tickets' | '/approvals' | '/decisions' | '/review' | '/knowledge' | '/anomalies' | '/emails' | '/runs' | '/controls'
type Tone = 'plain' | 'warn' | 'bad'

interface NavItem {
  to: NavTo
  label: string
  icon: LucideIcon
  count?: number
  tone?: Tone
  countLabel?: string
}

export function AppSidebar() {
  const { desks, service } = usePortal()
  const approvals = desks.reduce((n, d) => n + d.approvals_pending, 0)
  const anomalies = desks.filter((d) => d.anomaly && d.anomaly.severity !== 'ok').length
  const emailsFailed = desks.reduce((n, d) => n + d.emails_failed, 0)
  const notesWaiting = desks.reduce((n, d) => n + (d.knowledge_pending ?? 0), 0)
  // A ticket opened from Approvals, Decisions or Review keeps that list highlighted instead of Open tickets.
  const location = useRouterState({ select: (s) => s.location })
  const from = location.pathname.startsWith('/tickets/') ? (location.search as { from?: string }).from : undefined

  const groups: { label: string; items: NavItem[] }[] = [
    {
      label: 'Monitor',
      items: [
        { to: '/', label: 'Overview', icon: Gauge },
        { to: '/tickets', label: 'Open tickets', icon: Inbox },
        { to: '/approvals', label: 'Approvals', icon: ListChecks, count: approvals, tone: 'warn', countLabel: 'replies waiting' },
        { to: '/decisions', label: 'Decisions', icon: History },
      ],
    },
    {
      label: 'Quality',
      items: [
        { to: '/review', label: 'Review', icon: ClipboardCheck },
        { to: '/knowledge', label: 'Knowledge', icon: BookOpenText, count: notesWaiting, tone: 'warn', countLabel: 'notes waiting for an Admin' },
        { to: '/anomalies', label: 'Anomalies', icon: ScanSearch, count: anomalies, tone: 'warn', countLabel: 'desks with an open anomaly' },
      ],
    },
    {
      label: 'Service',
      items: [
        { to: '/emails', label: 'Emails & alerts', icon: Mail, count: emailsFailed, tone: 'bad', countLabel: 'failed to send' },
        { to: '/runs', label: 'Runs', icon: Activity },
        { to: '/controls', label: 'Controls', icon: SlidersHorizontal },
      ],
    },
  ]

  return (
    <nav
      aria-label="Primary"
      className="flex flex-col gap-4 bg-sidebar px-3 pt-4 pb-3 text-sidebar-foreground lg:sticky lg:top-0 lg:h-screen lg:w-[232px] lg:shrink-0 lg:gap-[22px] lg:overflow-y-auto lg:pt-5 lg:pb-6"
    >
      <div className="flex items-center gap-2.5 px-2">
        <div aria-hidden className="flex size-[34px] items-center justify-center rounded-lg bg-sidebar-accent font-mono text-[13px] font-semibold text-sidebar">
          ICT
        </div>
        <div className="flex flex-col">
          <span className="text-sm font-semibold text-white">NMBZ ICT Helpdesk</span>
          <span className="text-xs text-sidebar-subtle">Agent supervision</span>
        </div>
      </div>

      <button
        type="button"
        onClick={openGoTo}
        aria-keyshortcuts="Control+K"
        className="flex h-9 items-center gap-2 rounded-lg border border-sidebar-border px-2.5 text-left text-[13px] text-sidebar-subtle hover:bg-white/5 hover:text-sidebar-foreground focus-visible:outline-2 focus-visible:outline-sidebar-accent"
      >
        <Search aria-hidden className="size-3.5 shrink-0" />
        <span className="flex-1">Go to ticket</span>
        <kbd className="hidden rounded border border-sidebar-border px-1.5 font-mono text-[11px] lg:inline">Ctrl K</kbd>
      </button>

      <div className="relative -mx-3 flex gap-1 overflow-x-auto px-3 pb-1 lg:mx-0 lg:flex-col lg:gap-[22px] lg:overflow-visible lg:px-0 lg:pb-0">
        {groups.map((group) => (
          <div key={group.label} className="flex shrink-0 gap-1 lg:flex-col lg:gap-0.5">
            <span className="hidden px-2.5 pb-1.5 text-[11px] font-semibold tracking-[0.08em] text-sidebar-muted uppercase lg:block">{group.label}</span>
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                item={item}
                exact={item.to === '/' || (item.to === '/tickets' && !!from)}
                forceActive={!!from && item.to === `/${from}`}
              />
            ))}
          </div>
        ))}
      </div>

      <div className="mt-auto hidden flex-col gap-1 border-t border-sidebar-border px-2.5 pt-3 text-xs text-sidebar-subtle lg:flex">
        <span>
          Signed in as <span className="font-medium text-sidebar-foreground">{service.user}</span>
        </span>
        <a href="/classic" className="text-sidebar-subtle underline-offset-2 hover:text-white hover:underline">
          Classic console
        </a>
      </div>
    </nav>
  )
}

function NavLink({ item, exact, forceActive }: { item: NavItem; exact: boolean; forceActive: boolean }) {
  const Icon = item.icon
  const showCount = item.count !== undefined && item.count > 0
  return (
    <Link
      to={item.to}
      activeOptions={{ exact }}
      {...(forceActive ? { 'data-force': 'on', 'aria-current': 'page' as const } : {})}
      className="group flex min-h-10 items-center justify-between gap-2 rounded-lg px-2.5 font-medium whitespace-nowrap text-sidebar-foreground no-underline transition-colors hover:bg-sidebar-border hover:text-white data-[force=on]:bg-sidebar-accent data-[force=on]:font-semibold data-[force=on]:text-sidebar data-[status=active]:bg-sidebar-accent data-[status=active]:font-semibold data-[status=active]:text-sidebar"
    >
      <span className="flex items-center gap-2.5">
        <Icon aria-hidden className="size-4 opacity-80" />
        {item.label}
      </span>
      {showCount ? (
        <span
          className={cn(
            'inline-flex h-5 min-w-[22px] items-center justify-center rounded-full px-1.5 font-mono text-xs font-semibold',
            item.tone === 'warn' && 'bg-warn-bg text-warn-fg',
            item.tone === 'bad' && 'bg-bad-bg text-bad-fg',
            (!item.tone || item.tone === 'plain') && 'bg-sidebar-border text-sidebar-foreground',
          )}
        >
          <span aria-hidden>{item.count}</span>
          <span className="sr-only">
            {item.count} {item.countLabel}
          </span>
        </span>
      ) : null}
    </Link>
  )
}
