import * as React from 'react'
import { useNavigate } from '@tanstack/react-router'
import { Search } from 'lucide-react'

import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { apiGet, deskPath } from '@/lib/api/client'
import type { DecisionList } from '@/lib/api/types'
import { when } from '@/lib/format'
import { usePortal } from '@/lib/portal'
import { STATUS } from '@/lib/status'
import { cn } from '@/lib/utils'
import { decisionOutcome } from '@/lib/vocab'

type Page = '/' | '/tickets' | '/approvals' | '/decisions' | '/review' | '/knowledge' | '/anomalies' | '/emails' | '/runs' | '/controls'
const PAGES: { to: Page; label: string }[] = [
  { to: '/', label: 'Overview' },
  { to: '/approvals', label: 'Approvals' },
  { to: '/tickets', label: 'Open tickets' },
  { to: '/anomalies', label: 'Anomalies' },
  { to: '/decisions', label: 'Decisions' },
  { to: '/review', label: 'Review' },
  { to: '/knowledge', label: 'Knowledge' },
  { to: '/emails', label: 'Emails & alerts' },
  { to: '/runs', label: 'Runs' },
  { to: '/controls', label: 'Controls' },
]

type Item =
  | { kind: 'page'; key: string; label: string; meta: string; to: Page }
  | { kind: 'ticket'; key: string; label: string; meta: string; number: string; desk: string; decision: number }

/** A key press meant for the page, not for a field being typed in. */
export function isPlainKey(e: KeyboardEvent): boolean {
  if (e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return false
  const t = e.target as HTMLElement | null
  return !t?.closest?.('input, textarea, select, [contenteditable="true"], [role="dialog"]')
}

const OPEN_EVENT = 'portal:goto'
/** Opens the go-to box from a button (the sidebar's search field). */
export const openGoTo = () => window.dispatchEvent(new Event(OPEN_EVENT))

/** Ctrl+K (⌘K): go to a ticket or a page. ?: the keyboard shortcuts. Same keys as the classic console. */
export function GoTo() {
  const [open, setOpen] = React.useState<null | 'goto' | 'keys'>(null)
  React.useEffect(() => {
    const onOpen = () => setOpen('goto')
    window.addEventListener(OPEN_EVENT, onOpen)
    return () => window.removeEventListener(OPEN_EVENT, onOpen)
  }, [])
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen('goto')
      } else if (e.key === '?' && isPlainKey(e)) {
        setOpen('keys')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  return (
    <Dialog open={open !== null} onOpenChange={(o) => !o && setOpen(null)}>
      {open === 'goto' ? <GoToPanel onDone={() => setOpen(null)} /> : open === 'keys' ? <KeysPanel /> : null}
    </Dialog>
  )
}

function GoToPanel({ onDone }: { onDone: () => void }) {
  const { desks, deskByKey } = usePortal()
  const navigate = useNavigate()
  const [q, setQ] = React.useState('')
  const [tickets, setTickets] = React.useState<Item[]>([])
  const [searching, setSearching] = React.useState(false)
  const [sel, setSel] = React.useState(0)
  const listId = React.useId()
  const digits = q.replace(/\D/g, '')

  React.useEffect(() => {
    if (digits.length < 3) {
      setTickets([])
      return
    }
    let live = true
    const timer = window.setTimeout(async () => {
      setSearching(true)
      const found = await Promise.all(
        desks.map((d) =>
          apiGet<DecisionList>(deskPath(d.key, 'decisions'), { q: digits, days: 365, limit: 8 })
            .then((r) => r.items.map((x) => ({ ...x, desk: d.key })))
            .catch(() => []),
        ),
      )
      if (!live) return
      const seen = new Set<string>()
      const items = found
        .flat()
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .filter((x) => !seen.has(`${x.desk}:${x.ticket_number}`) && seen.add(`${x.desk}:${x.ticket_number}`))
        .slice(0, 8)
        .map((x): Item => {
          const o = decisionOutcome(x)
          return {
            kind: 'ticket',
            key: `${x.desk}-${x.id}`,
            label: `Ticket #${x.ticket_number}`,
            meta: `${deskByKey(x.desk)?.title ?? x.desk} · ${o.label ?? STATUS[o.status].label} · ${when(x.created_at)}`,
            number: x.ticket_number,
            desk: x.desk,
            decision: x.id,
          }
        })
      setTickets(items)
      setSearching(false)
    }, 180)
    return () => {
      live = false
      window.clearTimeout(timer)
    }
  }, [digits, desks, deskByKey])

  const text = q.trim().toLowerCase()
  const pages: Item[] = PAGES.filter((p) => !text || p.label.toLowerCase().includes(text)).map((p) => ({ kind: 'page', key: p.to, label: p.label, meta: 'page', to: p.to }))
  const items = [...tickets, ...pages]
  const active = Math.min(sel, Math.max(0, items.length - 1))

  const go = (it: Item | undefined) => {
    if (!it) return
    onDone()
    if (it.kind === 'page') navigate({ to: it.to })
    else navigate({ to: '/tickets/$ticketNumber', params: { ticketNumber: it.number }, search: { desk: it.desk, decision: it.decision } })
  }

  return (
    <DialogContent aria-describedby={undefined}>
      <DialogTitle className="sr-only">Go to a ticket or page</DialogTitle>
      <div className="flex items-center gap-2.5 border-b border-border px-4">
        <Search aria-hidden className="size-4 shrink-0 text-muted-foreground" />
        <input
          autoFocus
          value={q}
          onChange={(e) => {
            setQ(e.target.value)
            setSel(0)
          }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
              e.preventDefault()
              setSel(Math.max(0, Math.min(items.length - 1, active + (e.key === 'ArrowDown' ? 1 : -1))))
            } else if (e.key === 'Enter') {
              e.preventDefault()
              go(items[active])
            }
          }}
          role="combobox"
          aria-expanded="true"
          aria-controls={listId}
          aria-activedescendant={items[active] ? `${listId}-${active}` : undefined}
          aria-label="Ticket number or page"
          placeholder="Ticket number or page…"
          autoComplete="off"
          className="h-12 min-w-0 flex-1 bg-transparent text-[15px] outline-none placeholder:text-muted-foreground"
        />
      </div>
      <ul id={listId} role="listbox" aria-label="Results" className="m-0 max-h-[50vh] list-none overflow-y-auto p-1.5">
        {items.map((it, i) => (
          <li
            key={it.key}
            id={`${listId}-${i}`}
            role="option"
            aria-selected={i === active}
            onMouseMove={() => setSel(i)}
            onClick={() => go(it)}
            className={cn('flex cursor-pointer items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-sm', i === active && 'bg-accent')}
          >
            <span className={cn(it.kind === 'ticket' && 'font-mono font-semibold')}>{it.label}</span>
            <span className="truncate text-xs text-muted-foreground">{it.meta}</span>
          </li>
        ))}
        {!items.length ? (
          <li className="px-3 py-2.5 text-[13px] text-muted-foreground">{searching ? 'Searching…' : 'No match. Ticket numbers search the last year of decisions.'}</li>
        ) : null}
      </ul>
      <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-border px-4 py-2 text-xs text-muted-foreground">
        <span>
          <Kbd>↑</Kbd> <Kbd>↓</Kbd> choose
        </span>
        <span>
          <Kbd>Enter</Kbd> open
        </span>
        <span>
          <Kbd>Esc</Kbd> close
        </span>
        {searching ? <span role="status">Searching tickets…</span> : null}
      </div>
    </DialogContent>
  )
}

const KEYS: [string, string][] = [
  ['Ctrl K', 'Go to a ticket or page'],
  ['J', 'Approvals: next reply'],
  ['K', 'Approvals: previous reply'],
  ['A', 'Approve (then Enter to confirm)'],
  ['E', 'Edit the reply'],
  ['X', 'Reject (then Enter to confirm)'],
  ['?', 'This list'],
  ['Esc', 'Close, cancel or stop editing'],
]

function KeysPanel() {
  return (
    <DialogContent className="max-w-[440px] px-5 py-4">
      <DialogTitle className="m-0 text-base font-semibold">Keyboard shortcuts</DialogTitle>
      <DialogDescription className="mt-1 mb-3 text-[13px] text-muted-foreground">J, K, A, E and X work on a reply opened from Approvals. A, E and X need an Admin.</DialogDescription>
      <dl className="m-0 grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-2 text-[13px]">
        {KEYS.map(([k, what]) => (
          <React.Fragment key={k}>
            <dt>
              <Kbd>{k}</Kbd>
            </dt>
            <dd className="m-0">{what}</dd>
          </React.Fragment>
        ))}
      </dl>
    </DialogContent>
  )
}

export function Kbd({ children }: { children: React.ReactNode }) {
  return <kbd className="inline-flex h-5 min-w-5 items-center justify-center rounded border border-input bg-muted px-1.5 font-mono text-[11px] font-semibold text-secondary-foreground">{children}</kbd>
}
