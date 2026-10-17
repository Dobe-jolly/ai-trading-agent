import * as React from 'react'

import { cn } from '@/lib/utils'

function Progress({
  value,
  max = 100,
  className,
  indicatorClassName,
  ...props
}: React.ComponentProps<'div'> & { value: number; max?: number; indicatorClassName?: string }) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100))
  return (
    <div
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={value}
      data-slot="progress"
      className={cn('h-1.5 w-full overflow-hidden rounded-full bg-muted', className)}
      {...props}
    >
      <div className={cn('h-full bg-primary', indicatorClassName)} style={{ width: `${pct}%` }} />
    </div>
  )
}

export { Progress }
