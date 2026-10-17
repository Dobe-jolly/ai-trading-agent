import * as React from 'react'

import { cn } from '@/lib/utils'

function Input({ className, type, ...props }: React.ComponentProps<'input'>) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        'h-10 w-full min-w-0 rounded-md border border-input bg-white px-3 text-[13px] text-foreground placeholder:text-faint outline-none disabled:opacity-50',
        className,
      )}
      {...props}
    />
  )
}

function Textarea({ className, ...props }: React.ComponentProps<'textarea'>) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        'w-full min-w-0 resize-y rounded-md border border-input bg-white px-3.5 py-3 text-[13px] leading-relaxed text-foreground outline-none',
        className,
      )}
      {...props}
    />
  )
}

/** Styled native <select>: accessible, keyboard friendly, no portal. */
function NativeSelect({ className, children, ...props }: React.ComponentProps<'select'>) {
  return (
    <select
      data-slot="native-select"
      className={cn('h-10 rounded-md border border-input bg-white px-2.5 text-[13px] text-foreground outline-none', className)}
      {...props}
    >
      {children}
    </select>
  )
}

function Label({ className, ...props }: React.ComponentProps<'label'>) {
  return <label data-slot="label" className={cn('flex flex-col gap-1.5 text-xs font-semibold text-muted-foreground', className)} {...props} />
}

export { Input, Textarea, NativeSelect, Label }
