import type * as React from 'react'
import { Link } from '@tanstack/react-router'
import { CircleAlert, Loader2 } from 'lucide-react'

import type { ServiceDesk } from '@/lib/api/types'
import { cn } from '@/lib/utils'

export function Loading({ label = 'Loading…', className }: { label?: string; className?: string }) {
  return (
    <div role="status" className={cn('flex items-center gap-2 px-5 py-10 text-[13px] text-muted-foreground', className)}>
      <Loader2 aria-hidden className="size-4 animate-spin" />
      {label}
    </div>
  )
}

export function ErrorNote({ error, className }: { error: Error | null | undefined; className?: string }) {
  if (!error) return null
  return (
    <div role="alert" className={cn('flex items-start gap-3 rounded-[10px] border border-bad-line bg-bad-row px-4 py-3 text-[13px] text-bad-fg', className)}>
      <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
      <span>{error.message}</span>
    </div>
  )
}

/** One line per desk whose read failed, so a broken desk never hides the others. */
export function DeskErrors({ errors }: { errors: { desk: ServiceDesk; error: Error }[] }) {
  if (!errors.length) return null
  return (
    <div role="alert" className="flex flex-col gap-1 rounded-[10px] border border-bad-line bg-bad-row px-4 py-3 text-[13px] text-bad-fg">
      {errors.map(({ desk, error }) => (
        <span key={desk.key}>
          <strong className="font-semibold">{desk.title}:</strong> {error.message}
        </span>
      ))}
    </div>
  )
}

export function Empty({ title, children, icon }: { title: string; children?: React.ReactNode; icon?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-1.5 px-5 py-12 text-center">
      {icon}
      <strong className="font-semibold">{title}</strong>
      {children ? <span className="max-w-[560px] text-[13px] text-muted-foreground">{children}</span> : null}
    </div>
  )
}

/** A ticket number that opens the investigation of one decision (or the latest one when no decision is given). */
/** The list a ticket was opened from; it keeps that list highlighted and names it in the breadcrumb. */
export type TicketFrom = 'approvals' | 'decisions' | 'review'
export const TICKET_FROM: TicketFrom[] = ['approvals', 'decisions', 'review']

export function TicketLink({ number, desk, decision, from, className, children }: { number: string | null | undefined; desk?: string; decision?: number | null; from?: TicketFrom; className?: string; children?: React.ReactNode }) {
  if (!number) return <span className="font-mono text-muted-foreground">—</span>
  return (
    <Link
      to="/tickets/$ticketNumber"
      params={{ ticketNumber: number }}
      search={{ desk, decision: decision ?? undefined, from }}
      className={cn('font-mono text-[13px] font-semibold', className)}
    >
      {children ?? `#${number}`}
    </Link>
  )
}
