import { cn } from '@/lib/utils'

/** Confidence bar for a 0–1 value: green at 90%+, neutral 75–89%, amber below 75%. */
export function Confidence({ value, className }: { value: number | null | undefined; className?: string }) {
  if (value == null) return <span className="font-mono text-xs text-muted-foreground">—</span>
  const pct = Math.round(value * 100)
  return (
    <div className={cn('flex min-w-[110px] items-center gap-2', className)}>
      <div aria-hidden className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
        <div className={cn('h-full', pct >= 90 ? 'bg-primary' : pct >= 75 ? 'bg-faint' : 'bg-warn-dot')} style={{ width: `${pct}%` }} />
      </div>
      <span className={cn('w-[34px] text-right font-mono text-xs', pct < 75 && 'text-warn-fg')}>{pct}%</span>
    </div>
  )
}
