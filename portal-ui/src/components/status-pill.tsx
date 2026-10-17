import type { DeskVocab, Mode, ServiceDesk } from '@/lib/api/types'
import { MODE, STATUS, type StatusKey } from '@/lib/status'
import { TONE_CLASS, checkStatus } from '@/lib/vocab'
import { cn } from '@/lib/utils'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

export function StatusPill({
  status,
  label,
  size = 'md',
  className,
}: {
  status: StatusKey
  /** Overrides the vocabulary label, e.g. a specific proposed action. */
  label?: string
  size?: 'sm' | 'md' | 'lg'
  className?: string
}) {
  const s = STATUS[status]
  return (
    <span
      className={cn(
        'inline-flex max-w-full items-center gap-1.5 rounded-full border font-medium whitespace-nowrap',
        size === 'sm' && 'h-[22px] px-2 text-xs',
        size === 'md' && 'h-6 px-2.5 text-xs',
        size === 'lg' && 'h-8 gap-2 px-3 text-sm font-semibold',
        s.className,
        className,
      )}
    >
      <span aria-hidden className={cn('shrink-0 rounded-full', size === 'lg' ? 'size-[7px]' : 'size-1.5', s.dot)} />
      <span className="truncate">{label ?? s.label}</span>
    </span>
  )
}

/** A desk's operating mode. A Notes or Shadow hold put on from the portal says "hold". */
export function ModeChip({
  desk,
  mode,
  className,
}: {
  desk?: Pick<ServiceDesk, 'configured_mode' | 'effective_mode'>
  mode?: Mode
  className?: string
}) {
  const effective = desk?.effective_mode ?? mode ?? 'shadow'
  const held = desk ? desk.effective_mode !== desk.configured_mode : false
  const m = MODE[effective]
  const label = held ? `${m.label} hold` : m.label
  const tip = held ? `${m.tip} Held from the portal; configured ${MODE[desk!.configured_mode].label.toLowerCase()} on the server.` : m.tip
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0} className={cn('inline-flex h-[26px] items-center gap-1.5 rounded-full border px-2.5 text-xs font-semibold whitespace-nowrap', m.className, className)}>
          <span aria-hidden className={cn('size-[7px] rounded-full', m.dot)} />
          {label}
        </span>
      </TooltipTrigger>
      <TooltipContent>{tip}</TooltipContent>
    </Tooltip>
  )
}

export function MixedModeChip({ label }: { label: string }) {
  return (
    <span className="inline-flex h-[26px] items-center gap-1.5 rounded-full border border-muted bg-muted px-2.5 text-xs font-semibold text-secondary-foreground">
      <span aria-hidden className="size-[7px] rounded-full bg-neutral-dot" />
      {label}
    </span>
  )
}

/** A check status (what the lookups showed) in the desk's own words. */
export function CheckChip({ vocab, status, subject }: { vocab?: DeskVocab; status: string; subject?: string }) {
  const s = checkStatus(vocab, status)
  return (
    <span className={cn('inline-flex max-w-full items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium', TONE_CLASS[s.tone] ?? TONE_CLASS.idle)}>
      {subject && !/^(latest|transaction|topic|connect)$/.test(subject) ? <span className="font-mono opacity-80">{subject}</span> : null}
      <span className="truncate" title={status}>
        {s.label}
      </span>
    </span>
  )
}
