import * as React from 'react'
import * as ToggleGroupPrimitive from '@radix-ui/react-toggle-group'

import { cn } from '@/lib/utils'

/** Segmented control on a muted track — used for role, hold and period switches. */
function SegmentedGroup({ className, ...props }: React.ComponentProps<typeof ToggleGroupPrimitive.Root>) {
  return (
    <ToggleGroupPrimitive.Root
      data-slot="segmented-group"
      className={cn('inline-flex items-center rounded-[9px] bg-muted p-[3px]', className)}
      {...props}
    />
  )
}

function SegmentedItem({ className, ...props }: React.ComponentProps<typeof ToggleGroupPrimitive.Item>) {
  return (
    <ToggleGroupPrimitive.Item
      data-slot="segmented-item"
      className={cn(
        'inline-flex h-[34px] items-center justify-center rounded-[7px] px-3.5 text-[13px] font-medium text-secondary-foreground outline-none transition-colors hover:text-foreground disabled:opacity-50 data-[state=on]:bg-card data-[state=on]:text-foreground data-[state=on]:shadow-[0_1px_2px_rgb(16_48_42/0.14)]',
        className,
      )}
      {...props}
    />
  )
}

/** Pill-shaped filter chips (single choice). */
function ChipGroup({ className, ...props }: React.ComponentProps<typeof ToggleGroupPrimitive.Root>) {
  return <ToggleGroupPrimitive.Root data-slot="chip-group" className={cn('flex flex-wrap gap-1.5', className)} {...props} />
}

function ChipItem({ className, ...props }: React.ComponentProps<typeof ToggleGroupPrimitive.Item>) {
  return (
    <ToggleGroupPrimitive.Item
      data-slot="chip-item"
      className={cn(
        'inline-flex h-9 items-center gap-2 rounded-full border border-input bg-card px-3 text-[13px] font-medium text-foreground outline-none transition-colors hover:bg-muted data-[state=on]:border-sidebar data-[state=on]:bg-sidebar data-[state=on]:text-white',
        className,
      )}
      {...props}
    />
  )
}

export { SegmentedGroup, SegmentedItem, ChipGroup, ChipItem }
