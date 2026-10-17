import { Search } from 'lucide-react'

import { cn } from '@/lib/utils'

export function SearchField({
  label,
  placeholder,
  value,
  onChange,
  className,
}: {
  label: string
  placeholder: string
  value: string
  onChange: (v: string) => void
  className?: string
}) {
  return (
    <label className={cn('flex h-10 min-w-[260px] items-center gap-2 rounded-md border border-input bg-white px-3 text-muted-foreground', className)}>
      <Search aria-hidden className="size-4 shrink-0" />
      <span className="sr-only">{label}</span>
      <input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="min-w-0 flex-1 border-0 bg-transparent text-[13px] text-foreground outline-none placeholder:text-faint"
      />
    </label>
  )
}

/** Case-insensitive match of a query against several fields. */
export function matchesQuery(query: string, ...fields: (string | undefined)[]) {
  const q = query.trim().toLowerCase().replace(/^#/, '')
  if (!q) return true
  return fields.some((f) => f?.toLowerCase().includes(q))
}
