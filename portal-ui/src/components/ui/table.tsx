import * as React from 'react'

import { cn } from '@/lib/utils'

/** Scrolls sideways inside its own box on narrow screens, never the page. */
function Table({ className, wrapperClassName, ...props }: React.ComponentProps<'table'> & { wrapperClassName?: string }) {
  return (
    <div data-slot="table-container" className={cn('relative w-full overflow-x-auto', wrapperClassName)}>
      <table data-slot="table" className={cn('w-full caption-bottom border-collapse text-[13px]', className)} {...props} />
    </div>
  )
}

function TableHeader({ className, ...props }: React.ComponentProps<'thead'>) {
  return <thead data-slot="table-header" className={cn('[&_tr]:border-b [&_tr]:border-border', className)} {...props} />
}

function TableBody({ className, ...props }: React.ComponentProps<'tbody'>) {
  return <tbody data-slot="table-body" className={cn('[&_tr:last-child]:border-0', className)} {...props} />
}

function TableRow({ className, ...props }: React.ComponentProps<'tr'>) {
  return <tr data-slot="table-row" className={cn('border-b border-border-soft align-middle', className)} {...props} />
}

function TableHead({ className, ...props }: React.ComponentProps<'th'>) {
  return (
    <th
      data-slot="table-head"
      scope="col"
      className={cn('h-10 px-4 text-left align-middle text-xs font-semibold whitespace-nowrap text-muted-foreground first:pl-5 last:pr-5', className)}
      {...props}
    />
  )
}

function TableCell({ className, ...props }: React.ComponentProps<'td'>) {
  return <td data-slot="table-cell" className={cn('px-4 py-3 align-middle first:pl-5 last:pr-5', className)} {...props} />
}

function TableCaption({ className, ...props }: React.ComponentProps<'caption'>) {
  return <caption data-slot="table-caption" className={cn('px-5 py-3 text-left text-xs text-muted-foreground', className)} {...props} />
}

export { Table, TableHeader, TableBody, TableRow, TableHead, TableCell, TableCaption }
