import * as React from 'react'
import { createFileRoute } from '@tanstack/react-router'

import { DeskErrors, Empty, Loading } from '@/components/data-state'
import { NoteCard } from '@/components/knowledge/note-card'
import { PageHeader } from '@/components/page-header'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { useDeskQueries } from '@/lib/api/hooks'
import type { Knowledge, Note, ServiceDesk } from '@/lib/api/types'
import { count } from '@/lib/format'
import { usePortal } from '@/lib/portal'
import { cn } from '@/lib/utils'

export const Route = createFileRoute('/knowledge')({
  head: () => ({ meta: [{ title: 'Knowledge · Agent Supervision' }] }),
  component: KnowledgePage,
})

type Row = { note: Note; desk: ServiceDesk; k: Knowledge }

function KnowledgePage() {
  const { selected } = usePortal()
  const data = useDeskQueries<Knowledge>(selected, 'knowledge')
  const [last, setLast] = React.useState<string | null>(null)
  const all: Row[] = data.results.flatMap((r) => (r.data ? r.data.items.map((note) => ({ note, desk: r.desk, k: r.data! })) : []))
  const waiting = all.filter((r) => r.note.status === 'proposed').sort((a, b) => a.note.created_at.localeCompare(b.note.created_at))
  const past = all.filter((r) => !['proposed', 'active'].includes(r.note.status)).sort((a, b) => (b.note.retired_at ?? b.note.decided_at ?? b.note.created_at).localeCompare(a.note.retired_at ?? a.note.decided_at ?? a.note.created_at))
  const many = selected.length > 1
  const canApprove = data.results.some((r) => r.data?.can_approve)
  const unused = data.results.filter((r) => r.data && !r.data.items.some((n) => n.status === 'active')).map((r) => r.desk.title)

  return (
    <>
      <PageHeader
        title="Knowledge"
        description="What ICT staff have taught each desk: logic, codes, tables and logs, and read-only lookups. Anyone can propose a note from a ticket’s “Teach the agent” step; it is used on future tickets only once an Admin who didn’t write it switches it on."
        actions={
          <span className="text-[13px] text-muted-foreground">
            <strong className="font-semibold text-foreground">{waiting.length}</strong> waiting for an Admin
          </span>
        }
      />
      <DeskErrors errors={data.errors} />
      <p role="status" className={cn('m-0 rounded-lg bg-ok-bg px-3.5 py-2.5 text-[13px] text-ok-fg', !last && 'sr-only')}>
        {last ?? ''}
      </p>

      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between">
          <CardTitle>Waiting for an Admin</CardTitle>
          <CardDescription>{canApprove ? 'Oldest first. You can switch on notes you didn’t write.' : 'Oldest first. An Admin who didn’t write a note switches it on.'}</CardDescription>
        </CardHeader>
        {data.isLoading && !all.length ? (
          <Loading />
        ) : waiting.length ? (
          <div>
            {waiting.map((r) => (
              <NoteCard key={`${r.desk.key}-${r.note.id}`} note={r.note} desk={r.desk} knowledge={r.k} showDesk={many} onDone={setLast} />
            ))}
          </div>
        ) : (
          <Empty title="Nothing waiting">Notes are proposed from the “Teach the agent” step at the bottom of a ticket’s page.</Empty>
        )}
      </Card>

      {data.results.map(({ desk, data: k }) => (k && (!many || k.items.some((n) => n.status === 'active')) ? <InUse key={desk.key} desk={desk} k={k} onDone={setLast} /> : null))}
      {many && unused.length ? (
        <p className="m-0 text-[13px] text-muted-foreground">
          No notes switched on for {unused.join(', ')}: {unused.length > 1 ? 'they use' : 'it uses'} only {unused.length > 1 ? 'their' : 'its'} own prompt and lookups.
        </p>
      ) : null}

      {past.length ? (
        <Card>
          <Collapsible>
            <CardHeader className="flex-row flex-wrap items-center justify-between border-b-0">
              <CardTitle>
                <CollapsibleTrigger className="font-semibold text-primary hover:underline">History: {count(past.length)} turned down, switched off or withdrawn</CollapsibleTrigger>
              </CardTitle>
              <CardDescription>Kept so a decision can always be traced to the notes it used</CardDescription>
            </CardHeader>
            <CollapsibleContent className="border-t border-border">
              {past.map((r) => (
                <NoteCard key={`${r.desk.key}-${r.note.id}`} note={r.note} desk={r.desk} knowledge={r.k} showDesk={many} />
              ))}
            </CollapsibleContent>
          </Collapsible>
        </Card>
      ) : null}
    </>
  )
}

function InUse({ desk, k, onDone }: { desk: ServiceDesk; k: Knowledge; onDone: (m: string) => void }) {
  const active = k.items.filter((n) => n.status === 'active')
  const share = k.prompt.max_chars ? Math.min(1, k.prompt.chars / k.prompt.max_chars) : 0
  const leftOut = new Set(k.prompt.left_out)
  return (
    <Card>
      <CardHeader className="gap-2.5">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <CardTitle>In use on {desk.title}</CardTitle>
          <CardDescription>
            {active.length ? `${active.length} note${active.length > 1 ? 's' : ''} · newest switched on first` : 'No notes switched on'}
          </CardDescription>
        </div>
        {active.length ? (
          <div className="flex flex-col gap-1.5">
            <div
              role="meter"
              aria-label={`Prompt space used on ${desk.title}`}
              aria-valuemin={0}
              aria-valuemax={k.prompt.max_chars}
              aria-valuenow={k.prompt.chars}
              className="h-1.5 overflow-hidden rounded-full bg-muted"
            >
              <div className={cn('h-full', leftOut.size ? 'bg-warn-dot' : 'bg-primary')} style={{ width: `${Math.round(share * 100)}%` }} />
            </div>
            <span className={cn('text-xs', leftOut.size ? 'text-warn-fg' : 'text-muted-foreground')}>
              {leftOut.size
                ? `The prompt space is full: ${leftOut.size} note${leftOut.size > 1 ? 's are' : ' is'} left out (${[...leftOut].map((i) => `K${i}`).join(', ')}). Switch off notes that are no longer needed.`
                : `${count(k.prompt.chars)} of ${count(k.prompt.max_chars)} characters of prompt space. Each switched-on note goes into every ticket’s prompt on this desk.`}
            </span>
          </div>
        ) : null}
      </CardHeader>
      {active.length ? (
        <div>
          {active.map((n) => (
            <NoteCard key={n.id} note={n} desk={desk} knowledge={k} leftOut={leftOut.has(n.id)} onDone={onDone} />
          ))}
        </div>
      ) : (
        <Empty title={`${desk.title} uses only its own prompt and lookups`}>Switched-on notes appear here with how often decisions used them.</Empty>
      )}
    </Card>
  )
}
