import * as React from 'react'
import { Database, Lock } from 'lucide-react'

import { ErrorNote, TicketLink } from '@/components/data-state'
import { Mono } from '@/components/page-header'
import { StatusPill } from '@/components/status-pill'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { useDecideNote, type NoteVerdict } from '@/lib/api/hooks'
import type { Knowledge, Note, ServiceDesk } from '@/lib/api/types'
import { useNow, when } from '@/lib/format'
import { noteId, noteStatus } from '@/lib/knowledge'
import { cn } from '@/lib/utils'

/** One note as staff wrote it (masked), with who did what to it and the step the signed-in person can take. */
export function NoteCard({
  note: n,
  desk,
  knowledge,
  showDesk,
  leftOut,
  onDone,
}: {
  note: Note
  desk: ServiceDesk
  knowledge: Pick<Knowledge, 'user' | 'can_approve' | 'slot_kinds'>
  showDesk?: boolean
  /** In use but left out of the prompt: the prompt space ran out. */
  leftOut?: boolean
  onDone?: (message: string) => void
}) {
  const now = useNow()
  const s = noteStatus(n.status)
  const slots = Object.entries(n.query_slots ?? {})
  const open = n.status === 'proposed' || n.status === 'active'

  return (
    <article aria-labelledby={`note-${desk.key}-${n.id}`} className="flex flex-col gap-3 border-b border-border-soft px-5 py-4 last:border-0">
      <header className="flex flex-wrap items-start gap-x-3 gap-y-1.5">
        <Mono className="rounded-md bg-muted px-1.5 py-0.5 text-xs font-semibold">{noteId(n.id)}</Mono>
        <h3 id={`note-${desk.key}-${n.id}`} className="m-0 min-w-0 flex-[1_1_260px] text-[15px] leading-snug font-semibold">
          {n.title}
        </h3>
        <StatusPill status={s.status} label={s.label} />
      </header>

      <p className="m-0 text-xs text-muted-foreground">
        {showDesk ? <>{desk.title} · </> : null}
        Taught by <strong className="font-medium text-secondary-foreground">{n.created_by}</strong>
        {n.source_ticket ? (
          <>
            {' '}from{' '}
            <TicketLink number={n.source_ticket} desk={desk.key} decision={n.source_decision_id} className="text-xs" />
          </>
        ) : null}
        {' · '}
        <Mono>{when(n.created_at, now)}</Mono>
        {' · '}
        {n.topic ? (
          <>
            for <strong className="font-medium text-secondary-foreground">{n.topic}</strong> tickets
          </>
        ) : (
          'for every ticket on this desk'
        )}
      </p>

      <p className="m-0 text-[13px] leading-relaxed whitespace-pre-line">{n.body}</p>

      {n.example ? (
        <Collapsible>
          <CollapsibleTrigger className="text-[13px] font-medium text-primary hover:underline">Logs or data it was given (masked)</CollapsibleTrigger>
          <CollapsibleContent>
            <pre className="mt-2 mb-0 max-h-[300px] overflow-auto rounded-lg bg-code px-3.5 py-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-code-foreground">{n.example}</pre>
          </CollapsibleContent>
        </Collapsible>
      ) : null}

      {n.query_sql ? (
        <div className="flex flex-col gap-2 rounded-lg border border-border bg-background px-3.5 py-3">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
            <Database aria-hidden className="size-4 text-muted-foreground" />
            <span className="font-semibold">Lookup</span>
            <Mono className="text-xs break-all">{n.tool_name ?? 'named when proposed'}</Mono>
            <span className="text-muted-foreground">on {n.query_db}</span>
          </div>
          {n.query_about ? <p className="m-0 text-[13px] text-secondary-foreground">{n.query_about}</p> : null}
          {slots.length ? (
            <ul className="m-0 flex list-none flex-wrap gap-1.5 p-0 text-xs">
              {slots.map(([name, kind]) => (
                <li key={name} className="rounded-md border border-input bg-card px-2 py-0.5">
                  <Mono>{`{{${name}}}`}</Mono> <span className="text-muted-foreground">· {knowledge.slot_kinds[kind]?.label ?? kind}</span>
                </li>
              ))}
            </ul>
          ) : null}
          <Collapsible>
            <CollapsibleTrigger className="text-[13px] font-medium text-primary hover:underline">The query</CollapsibleTrigger>
            <CollapsibleContent>
              <pre className="mt-2 mb-0 max-h-[300px] overflow-auto rounded-lg bg-code px-3.5 py-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-code-foreground">{n.query_sql}</pre>
            </CollapsibleContent>
          </Collapsible>
        </div>
      ) : null}

      <History note={n} now={now} />
      {leftOut ? (
        <p className="m-0 rounded-lg bg-warn-panel px-3.5 py-2.5 text-xs text-warn-fg">
          Left out of the prompt: the desk’s prompt space is full, so the agent doesn’t see this note. Switch off notes that are no longer needed.
        </p>
      ) : null}
      {open ? <NoteActions note={n} desk={desk} knowledge={knowledge} onDone={onDone} /> : null}
    </article>
  )
}

function History({ note: n, now }: { note: Note; now: number }) {
  const lines: React.ReactNode[] = []
  if (n.decided_by && n.decided_at) {
    lines.push(
      <>
        {n.status === 'rejected' ? 'Turned down' : 'Switched on'} by <strong className="font-medium">{n.decided_by}</strong> · <Mono>{when(n.decided_at, now)}</Mono>
        {n.decision_note ? <> · “{n.decision_note}”</> : null}
      </>,
    )
  }
  if (n.retired_by && n.retired_at) {
    lines.push(
      <>
        {n.status === 'withdrawn' ? 'Withdrawn' : 'Switched off'} by <strong className="font-medium">{n.retired_by}</strong> · <Mono>{when(n.retired_at, now)}</Mono>
        {n.retire_note ? <> · “{n.retire_note}”</> : null}
      </>,
    )
  }
  if (n.status === 'active' || n.status === 'retired') {
    lines.push(n.uses ? <>Used in {n.uses} decision{n.uses > 1 ? 's' : ''}, last <Mono>{when(n.last_used_at, now)}</Mono></> : <>Not used in a decision yet</>)
  }
  if (!lines.length) return null
  return (
    <ul className="m-0 flex list-none flex-col gap-1 p-0 text-xs text-muted-foreground">
      {lines.map((l, i) => (
        <li key={i}>{l}</li>
      ))}
    </ul>
  )
}

const CONFIRM: Record<NoteVerdict, { ask: (n: Note, desk: ServiceDesk) => string; yes: string; noteLabel: string; done: string; tone: 'ok' | 'bad' }> = {
  approve: {
    ask: (n, desk) =>
      `Switch it on? From ${desk.title}’s next pass, ${n.topic ? `every ${n.topic} ticket` : 'every ticket'} gets this note${n.query_sql ? ', and the agent may run its lookup' : ''}. The desk’s rules and approvals don’t change.`,
    yes: 'Yes, switch it on',
    noteLabel: 'Note for the log (optional)',
    done: 'switched on',
    tone: 'ok',
  },
  reject: { ask: () => 'Turn it down? It is kept in the history with your reason, and is never used.', yes: 'Yes, turn it down', noteLabel: 'Why (the teacher sees this)', done: 'turned down', tone: 'bad' },
  retire: { ask: () => 'Switch it off? The agent stops getting it from the next pass. It stays in the history.', yes: 'Yes, switch it off', noteLabel: 'Why (optional)', done: 'switched off', tone: 'bad' },
  withdraw: { ask: () => 'Withdraw your note? It won’t be used, and an Admin no longer needs to look at it.', yes: 'Yes, withdraw it', noteLabel: 'Why (optional)', done: 'withdrawn', tone: 'bad' },
}

function NoteActions({ note: n, desk, knowledge, onDone }: { note: Note; desk: ServiceDesk; knowledge: Pick<Knowledge, 'user' | 'can_approve'>; onDone?: (message: string) => void }) {
  const decide = useDecideNote()
  const [confirm, setConfirm] = React.useState<NoteVerdict | null>(null)
  const [why, setWhy] = React.useState('')
  const yesRef = React.useRef<HTMLButtonElement>(null)
  const whyId = React.useId()
  React.useEffect(() => {
    if (confirm) yesRef.current?.focus()
  }, [confirm])

  const mine = n.created_by === knowledge.user
  const verdicts: NoteVerdict[] =
    n.status === 'proposed' ? [...(knowledge.can_approve && !mine ? (['approve', 'reject'] as const) : []), ...(mine ? (['withdraw'] as const) : [])] : knowledge.can_approve ? ['retire'] : []

  const submit = (verdict: NoteVerdict) =>
    decide
      .mutateAsync({ desk: desk.key, id: n.id, verdict, note: why.trim() || undefined })
      .then(() => {
        setConfirm(null)
        setWhy('')
        onDone?.(`${noteId(n.id)} “${n.title}” ${CONFIRM[verdict].done}.`)
      })
      .catch(() => setConfirm(null))

  const blocked =
    n.status === 'proposed' && knowledge.can_approve && mine
      ? 'You wrote this note, so another Admin must switch it on (maker-checker).'
      : n.status === 'proposed' && !knowledge.can_approve
        ? 'An Admin who didn’t write it switches it on.'
        : null

  if (!verdicts.length && !blocked) return null
  const c = confirm ? CONFIRM[confirm] : null
  return (
    <div className="flex flex-col gap-2.5">
      <ErrorNote error={decide.error} />
      {c && confirm ? (
        <div className={cn('flex flex-col gap-2.5 rounded-lg border px-3.5 py-3', c.tone === 'ok' ? 'border-primary bg-ok-bg' : 'border-destructive bg-bad-row')}>
          <strong className={cn('text-[13px] font-semibold', c.tone === 'ok' ? 'text-ok-fg' : 'text-bad-fg')}>{c.ask(n, desk)}</strong>
          <label htmlFor={whyId} className="text-xs font-semibold text-secondary-foreground">
            {c.noteLabel}
          </label>
          <Input id={whyId} value={why} maxLength={500} onChange={(e) => setWhy(e.target.value)} className="h-10" />
          <div className="flex flex-wrap gap-2">
            <Button ref={yesRef} variant={c.tone === 'ok' ? 'default' : 'destructive'} onClick={() => submit(confirm)} disabled={decide.isPending}>
              {decide.isPending ? 'Saving…' : c.yes}
            </Button>
            <Button variant="secondary" onClick={() => setConfirm(null)} disabled={decide.isPending}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          {verdicts.includes('approve') ? <Button onClick={() => setConfirm('approve')}>Switch on</Button> : null}
          {verdicts.includes('reject') ? (
            <Button variant="destructive" onClick={() => setConfirm('reject')}>
              Turn down
            </Button>
          ) : null}
          {verdicts.includes('retire') ? (
            <Button variant="secondary" onClick={() => setConfirm('retire')}>
              Switch off
            </Button>
          ) : null}
          {verdicts.includes('withdraw') ? (
            <Button variant="secondary" onClick={() => setConfirm('withdraw')}>
              Withdraw
            </Button>
          ) : null}
          {blocked ? (
            <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
              <Lock aria-hidden className="size-3.5" />
              {blocked}
            </span>
          ) : null}
        </div>
      )}
    </div>
  )
}
