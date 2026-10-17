import type * as React from 'react'

import { cn } from '@/lib/utils'

export function PageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: React.ReactNode
  description?: React.ReactNode
  actions?: React.ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex flex-wrap items-end justify-between gap-3', className)}>
      <div className="min-w-0">
        <h1 className="m-0 text-2xl font-semibold tracking-[-0.01em]">{title}</h1>
        {description ? <p className="mt-1 mb-0 text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-3">{actions}</div> : null}
    </div>
  )
}

/** Numbered eyebrow used on the ticket investigation to answer its six questions in order. */
export function StepEyebrow({ n, children }: { n: number; children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-2 text-[11px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
      <span aria-hidden className="inline-flex size-[18px] items-center justify-center rounded-full bg-sidebar font-mono text-[11px] tracking-normal text-white">
        {n}
      </span>
      {children}
    </span>
  )
}

export function Mono({ className, ...props }: React.ComponentProps<'span'>) {
  return <span className={cn('font-mono', className)} {...props} />
}

/** Visually hidden text for screen readers. */
export function SrOnly({ children }: { children: React.ReactNode }) {
  return <span className="sr-only">{children}</span>
}
