import * as React from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'

import { cn } from '@/lib/utils'

const Dialog = DialogPrimitive.Root
const DialogTitle = DialogPrimitive.Title
const DialogDescription = DialogPrimitive.Description

function DialogContent({ className, children, ...props }: React.ComponentProps<typeof DialogPrimitive.Content>) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-[oklch(0.2_0.02_160/0.35)]" />
      <DialogPrimitive.Content
        data-slot="dialog-content"
        className={cn(
          'fixed top-[12vh] left-1/2 z-50 flex w-[calc(100%-32px)] max-w-[560px] -translate-x-1/2 flex-col overflow-hidden rounded-xl border border-border bg-card text-foreground shadow-xl focus:outline-none',
          className,
        )}
        {...props}
      >
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  )
}

export { Dialog, DialogContent, DialogTitle, DialogDescription }
