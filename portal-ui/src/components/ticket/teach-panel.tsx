import * as React from 'react'
import { Link } from '@tanstack/react-router'
import { Database, FlaskConical, GraduationCap, Loader2, ShieldCheck } from 'lucide-react'

import { Confidence } from '@/components/confidence'
import { ErrorNote } from '@/components/data-state'
import { Mono, StepEyebrow } from '@/components/page-header'
import { StatusPill } from '@/components/status-pill'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input, Label, NativeSelect, Textarea } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { useDeskQuery, useTeach, useTrial, useTryNote } from '@/lib/api/hooks'
import type { DecisionDetail, Knowledge, Note, NoteDraft, ServiceDesk, Trial, TrialResult } from '@/lib/api/types'
import { count, useNow, when } from '@/lib/format'
import { LIMITS, guessKind, noteId, noteStatus, slotsIn } from '@/lib/knowledge'
import { usePortal } from '@/lib/portal'
import { ACTION_STATUS, actionLabel } from '@/lib/vocab'
import { cn } from '@/lib/utils'

/**
 * Step 7 on a ticket: teach the desk what it should know for tickets like this one: the logic, what a code means, a
 * table or log to read, or a read-only lookup. Try it on this ticket first (nothing is posted or sent), then propose
 * it; an Admin who didn't write it switches it on for future tickets.
 */
export function TeachPanel({ decision: d, desk, topic }: { decision: DecisionDetail; desk: ServiceDesk; topic: string | null }) {
  const [open, setOpen] = React.useState(false)
  const [proposed, setProposed] = React.useState<Note | null>(null)
  return (
    <Card className="gap-3.5 px-5 pt-4 pb-5">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div className="flex flex-col gap-1.5">
          <StepEyebrow n={7}>Teach the agent</StepEyebrow>
          <h2 className="m-0 text-base font-semibold">Give it the logic, tables or logs for tickets like this</h2>
        </div>
        <Link to="/knowledge" className="text-[13px] font-medium">
          Everything taught to the desks
        </Link>
      </div>
      <UsedOnThisDecision decision={d} desk={desk} />

      {proposed ? (
        <div role="status" className="flex flex-col gap-1 rounded-lg bg-ok-bg px-3.5 py-3 text-[13px] text-ok-fg">
          <strong className="font-semibold">
            Proposed as {noteId(proposed.id)}: “{proposed.title}”.
          </strong>
          <span>
            It is used on future {desk.title} tickets once an Admin who didn’t write it switches it on on the <Link to="/knowledge">Knowledge</Link> page. Numbers in it were masked
            when it was saved.
          </span>
        </div>
      ) : null}

      {open ? (
        <TeachForm
          decision={d}
          desk={desk}
          topic={topic}
          onCancel={() => setOpen(false)}
          onProposed={(n) => {
            setProposed(n)
            setOpen(false)
          }}
        />
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="outline" size="lg" onClick={() => { setProposed(null); setOpen(true) }}>
            <GraduationCap aria-hidden />
            {proposed ? 'Teach something else' : 'Teach the agent'}
          </Button>
          <span className="flex-[1_1_320px] text-[13px] text-muted-foreground">
            Explain what it missed or should check, paste the log lines or rows that matter, or give it a read-only query. Try it on this ticket first: nothing is posted or sent.
          </span>
        </div>
      )}
    </Card>
  )
}

/** Which staff notes were switched on when this decision was made, which it used, and the prompt version. */
function UsedOnThisDecision({ decision: d }: { decision: DecisionDetail; desk: ServiceDesk }) {
  const active = d.knowledge_active ?? []
  const used = d.knowledge_used ?? []
  if (!active.length && !used.length && !d.prompt_version) return null
  return (
    <p className="m-0 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
      {used.length ? (
        <>
          <span>This decision used</span>
          {used.map((n) => (
            <span key={n.id} className="inline-flex items-center gap-1 rounded-md border border-input bg-card px-1.5 py-0.5 text-secondary-foreground">
              <Mono className="font-semibold">{noteId(n.id)}</Mono>
              {n.title ? <span className="max-w-[260px] truncate">{n.title}</span> : null}
              {n.status && n.status !== 'active' ? <span className="text-warn-fg">({noteStatus(n.status).label.toLowerCase()})</span> : null}
            </span>
          ))}
        </>
      ) : (
        <span>{active.length ? `${active.length} staff note${active.length > 1 ? 's were' : ' was'} switched on; it used none.` : 'No staff notes were switched on when it decided.'}</span>
      )}
      {d.prompt_version ? (
        <span>
          · prompt version <Mono>{d.prompt_version}</Mono>
        </span>
      ) : null}
    </p>
  )
}

interface FormState {
  title: string
  body: string
  example: string
  forTopic: boolean
  withQuery: boolean
  database: string
  sql: string
  slots: Record<string, string>
  about: string
}

const EMPTY: FormState = { title: '', body: '', example: '', forTopic: true, withQuery: false, database: '', sql: '', slots: {}, about: '' }

function TeachForm({
  decision: d,
  desk,
  topic,
  onCancel,
  onProposed,
}: {
  decision: DecisionDetail
  desk: ServiceDesk
  topic: string | null
  onCancel: () => void
  onProposed: (n: Note) => void
}) {
  const knowledge = useDeskQuery<Knowledge>(desk.key, 'knowledge')
  const teach = useTeach()
  const tryNote = useTryNote()
  const [f, setF] = React.useState<FormState>({ ...EMPTY, forTopic: Boolean(topic) })
  const [trialId, setTrialId] = React.useState<number | null>(null)
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setF((s) => ({ ...s, [key]: value }))
  const ids = { title: React.useId(), body: React.useId(), example: React.useId(), db: React.useId(), sql: React.useId(), about: React.useId(), dbs: React.useId(), query: React.useId() }
  const k = knowledge.data
  const slotNames = slotsIn(f.sql)

  const draft = (): NoteDraft => ({
    title: f.title.trim(),
    body: f.body.trim(),
    example: f.example.trim() || null,
    topic: f.forTopic && topic ? topic : null,
    query: f.withQuery
      ? { database: f.database.trim(), sql: f.sql.trim(), about: f.about.trim(), slots: Object.fromEntries(slotNames.map((s) => [s, f.slots[s] ?? guessKind(s)])) }
      : null,
  })
  const missing = problems(f, slotNames.length)

  const propose = () =>
    teach
      .mutateAsync({ desk: desk.key, decisionId: d.id, draft: draft() })
      .then((n) => onProposed(n))
      .catch(() => {})
  const attempt = () =>
    tryNote
      .mutateAsync({ desk: desk.key, decisionId: d.id, draft: draft() })
      .then((t) => setTrialId(t.id))
      .catch(() => {})

  return (
    <div className="flex flex-col gap-4 border-t border-border-soft pt-4">
      <div className="flex items-start gap-2.5 rounded-lg bg-info-bg px-3.5 py-3 text-[13px] text-info-fg">
        <ShieldCheck aria-hidden className="mt-0.5 size-4 shrink-0" />
        <span>
          Account, card and phone numbers you type or paste are masked before anything is saved or sent to Claude, like the rest of the log. A note can’t change the desk’s rules: what it may post,
          when a person must approve, and its mode stay as they are.
        </span>
      </div>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor={ids.title} className="text-[13px] text-foreground">
          What is it about?
        </Label>
        <Input id={ids.title} value={f.title} maxLength={LIMITS.title[1]} onChange={(e) => set('title', e.target.value)} placeholder="e.g. Response 91 means the issuer timed out" />
      </div>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor={ids.body} className="text-[13px] text-foreground">
          What should the agent know or do?
          <span className="font-normal text-muted-foreground">The logic, in plain words: what to check, in which table or log, and what each result means for the reply.</span>
        </Label>
        <Textarea
          id={ids.body}
          rows={6}
          value={f.body}
          maxLength={LIMITS.body[1]}
          onChange={(e) => set('body', e.target.value)}
          placeholder={'e.g. When Postilion shows response 91 on the 0200 leg the issuer did not answer in time. The client may still have been debited, so check core banking for a debit on the same day before replying. If there is a debit with no reversal, escalate to Card Operations.'}
        />
        <Counter value={f.body} limits={LIMITS.body} />
      </div>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor={ids.example} className="text-[13px] text-foreground">
          <span>
            Logs, rows or a table to go with it <span className="font-normal text-muted-foreground">(optional)</span>
          </span>
          <span className="font-normal text-muted-foreground">Paste only the lines that matter, for example a log extract or a few rows with their column names.</span>
        </Label>
        <Textarea id={ids.example} rows={4} value={f.example} maxLength={LIMITS.example[1]} onChange={(e) => set('example', e.target.value)} className="font-mono text-xs" />
        <Counter value={f.example} limits={LIMITS.example} optional />
      </div>

      <fieldset className="m-0 flex flex-col gap-2 border-0 p-0">
        <legend className="mb-1.5 p-0 text-[13px] font-semibold">Use it for</legend>
        <label className="flex items-center gap-2 text-[13px]">
          <input type="radio" name={`${ids.title}-scope`} checked={f.forTopic && Boolean(topic)} disabled={!topic} onChange={() => set('forTopic', true)} />
          {topic ? (
            <>
              Tickets under <strong className="font-semibold">{topic}</strong> (and its subtopics)
            </>
          ) : (
            'This ticket’s help topic (it has none)'
          )}
        </label>
        <label className="flex items-center gap-2 text-[13px]">
          <input type="radio" name={`${ids.title}-scope`} checked={!f.forTopic || !topic} onChange={() => set('forTopic', false)} />
          Every ticket on {desk.title}
        </label>
      </fieldset>

      <div className="flex flex-col gap-3 rounded-lg border border-border bg-background px-3.5 py-3">
        <div className="flex items-center gap-2.5">
          <Switch id={ids.query} checked={f.withQuery} onCheckedChange={(v) => set('withQuery', v)} />
          <label htmlFor={ids.query} className="flex items-center gap-1.5 text-[13px] font-semibold">
            <Database aria-hidden className="size-4 text-muted-foreground" />
            Give it a lookup (a read-only query)
          </label>
        </div>
        {f.withQuery ? (
          <>
            <p className="m-0 text-xs text-muted-foreground">
              You write the query; the agent only supplies values from the ticket, and Metabase fills them in where you put <Mono>{'{{name}}'}</Mono>. One SELECT with a row limit
              {k ? ` (at most ${k.max_query_rows} rows)` : ''}; the agent sees {k ? `the first ${k.rows_to_model}` : 'the first few'}, masked. Never type account or phone numbers into it.
            </p>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={ids.db} className="text-[13px] text-foreground">
                Metabase database
              </Label>
              <Input id={ids.db} list={ids.dbs} value={f.database} maxLength={100} onChange={(e) => set('database', e.target.value)} placeholder="e.g. POST OFFICE" className="max-w-[360px]" />
              <datalist id={ids.dbs}>
                {(k?.databases ?? []).map((db) => (
                  <option key={db} value={db} />
                ))}
              </datalist>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={ids.sql} className="text-[13px] text-foreground">
                Query
              </Label>
              <Textarea
                id={ids.sql}
                rows={6}
                value={f.sql}
                maxLength={LIMITS.sql[1]}
                spellCheck={false}
                onChange={(e) => set('sql', e.target.value)}
                className="font-mono text-xs"
                placeholder={'SELECT TOP 20 datetime_req, message_type, rsp_code_rsp\nFROM post_tran_leg WITH (NOLOCK)\nWHERE from_account_id = {{account}}\nORDER BY datetime_req DESC'}
              />
            </div>
            {slotNames.length ? (
              <div className="flex flex-col gap-2">
                <span className="text-[13px] font-semibold">What fills each value</span>
                {slotNames.map((name) => {
                  const kind = f.slots[name] ?? guessKind(name)
                  return (
                    <div key={name} className="flex flex-wrap items-center gap-x-3 gap-y-1">
                      <Mono className="w-[150px] shrink-0 text-xs break-all">{`{{${name}}}`}</Mono>
                      <NativeSelect aria-label={`Kind of value for ${name}`} value={kind} onChange={(e) => set('slots', { ...f.slots, [name]: e.target.value })} className="h-9">
                        {Object.entries(k?.slot_kinds ?? {}).map(([key, v]) => (
                          <option key={key} value={key}>
                            {v.label}
                          </option>
                        ))}
                        {!k ? <option value={kind}>{kind}</option> : null}
                      </NativeSelect>
                      <span className="min-w-0 flex-[1_1_220px] text-xs text-muted-foreground">{k?.slot_kinds[kind]?.hint}</span>
                    </div>
                  )
                })}
              </div>
            ) : null}
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={ids.about} className="text-[13px] text-foreground">
                What it returns and when to use it
                <span className="font-normal text-muted-foreground">The agent reads this to decide when to run the lookup.</span>
              </Label>
              <Textarea
                id={ids.about}
                rows={2}
                value={f.about}
                maxLength={LIMITS.about[1]}
                onChange={(e) => set('about', e.target.value)}
                placeholder="e.g. The latest switch legs for the client's account with their response codes, newest first. Use it when a card or ZIPIT transaction may have timed out."
              />
              <Counter value={f.about} limits={LIMITS.about} />
            </div>
          </>
        ) : null}
      </div>

      <ErrorNote error={teach.error ?? tryNote.error} />
      {missing.length ? (
        <ul className="m-0 flex list-disc flex-col gap-0.5 pl-5 text-xs text-muted-foreground" aria-label="Still to fill in">
          {missing.map((m) => (
            <li key={m}>{m}</li>
          ))}
        </ul>
      ) : null}

      <div className="flex flex-wrap items-center gap-2.5">
        <Button size="lg" variant="outline" onClick={attempt} disabled={missing.length > 0 || tryNote.isPending}>
          <FlaskConical aria-hidden />
          {tryNote.isPending ? 'Starting…' : 'Try it on this ticket'}
        </Button>
        <Button size="lg" onClick={propose} disabled={missing.length > 0 || teach.isPending}>
          <GraduationCap aria-hidden />
          {teach.isPending ? 'Proposing…' : 'Propose for future tickets'}
        </Button>
        <Button size="lg" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <span className="flex-[1_1_260px] text-xs text-muted-foreground">Trying re-checks this ticket with the desk’s agent in shadow, which costs one ticket’s worth of model calls. Nothing is posted, sent or logged as a decision.</span>
      </div>

      {trialId != null ? <TrialView desk={desk} id={trialId} decision={d} /> : <EarlierTries desk={desk} decision={d} onShow={setTrialId} />}
    </div>
  )
}

function problems(f: FormState, slots: number): string[] {
  const out: string[] = []
  const len = (s: string) => s.trim().length
  if (len(f.title) < LIMITS.title[0]) out.push('A short title (at least 4 characters).')
  if (len(f.body) < LIMITS.body[0]) out.push('What the agent should know (at least 20 characters).')
  if (f.withQuery) {
    if (!len(f.database)) out.push('The Metabase database the query runs on.')
    if (!len(f.sql)) out.push('The query (one SELECT with a row limit).')
    else if (!slots) out.push('At least one {{value}} in the query, so it looks up this ticket’s client.')
    if (len(f.about) < LIMITS.about[0]) out.push('What the lookup returns and when to use it (at least 40 characters).')
  }
  return out
}

function Counter({ value, limits, optional }: { value: string; limits: readonly [number, number]; optional?: boolean }) {
  const n = value.trim().length
  const short = !optional && n > 0 && n < limits[0]
  return (
    <span className={cn('text-right text-[11px]', short ? 'text-warn-fg' : 'text-muted-foreground')}>
      {count(n)} / {count(limits[1])}
      {short ? ` · at least ${limits[0]}` : ''}
    </span>
  )
}

function EarlierTries({ desk, decision: d, onShow }: { desk: ServiceDesk; decision: DecisionDetail; onShow: (id: number) => void }) {
  const tries = useDeskQuery<{ items: Trial[] }>(desk.key, 'knowledge/trials', { decision: d.id })
  const now = useNow()
  const items = tries.data?.items ?? []
  if (!items.length) return null
  return (
    <div className="flex flex-col gap-1.5 border-t border-border-soft pt-3">
      <span className="text-[13px] font-semibold">Earlier tries on this ticket</span>
      <ul className="m-0 flex list-none flex-col gap-1 p-0 text-[13px]">
        {items.map((t) => (
          <li key={t.id} className="flex flex-wrap items-center gap-2">
            <Button variant="link" className="min-w-0 shrink text-left text-[13px] whitespace-normal" onClick={() => onShow(t.id)}>
              {t.draft?.title ?? `Try ${t.id}`}
            </Button>
            <span className="text-xs text-muted-foreground">
              {t.created_by} · <Mono>{when(t.created_at, now)}</Mono> · {t.status === 'done' ? `would ${actionLabel(t.result?.final_action).toLowerCase()}` : t.status === 'failed' ? 'failed' : 'running'}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** The trial, polled while it runs, then side by side with the decision it re-checked. */
function TrialView({ desk, id, decision: d }: { desk: ServiceDesk; id: number; decision: DecisionDetail }) {
  const trial = useTrial(desk.key, id)
  const now = useNow(1_000)
  const t = trial.data
  if (!t) return trial.error ? <ErrorNote error={trial.error} /> : null
  if (t.status === 'running') {
    const seconds = Math.max(0, Math.round((now - new Date(t.created_at).getTime()) / 1000))
    return (
      <div role="status" className="flex items-center gap-2.5 rounded-lg border border-border bg-background px-3.5 py-3 text-[13px]">
        <Loader2 aria-hidden className="size-4 shrink-0 animate-spin text-muted-foreground" />
        <span>
          Re-checking #{t.ticket_number} with your note… <span className="text-muted-foreground">({seconds}s; about as long as one ticket in a pass)</span>
        </span>
      </div>
    )
  }
  if (t.status === 'failed' || !t.result) {
    return (
      <div role="alert" className="flex flex-col gap-1 rounded-lg border border-bad-line bg-bad-row px-3.5 py-3 text-[13px] text-bad-fg">
        <strong className="font-semibold">The try didn’t finish.</strong>
        <span>{t.error ?? 'No result came back.'}</span>
      </div>
    )
  }
  return <Comparison decision={d} result={t.result} trial={t} />
}

function Comparison({ decision: d, result: r, trial: t }: { decision: DecisionDetail; result: TrialResult; trial: Trial }) {
  const changed = d.final_action !== r.final_action
  const { service } = usePortal()
  return (
    <section aria-label="What the agent would do with your note" className="flex flex-col gap-3 border-t border-border-soft pt-3.5">
      <div role="status" className={cn('flex flex-col gap-1 rounded-lg px-3.5 py-3 text-[13px]', r.draft_used ? 'bg-ok-bg text-ok-fg' : 'bg-warn-panel text-warn-fg')}>
        <strong className="font-semibold">
          {changed
            ? `With your note the agent would ${actionLabel(r.final_action).toLowerCase()} instead of ${actionLabel(d.final_action).toLowerCase()}.`
            : `With your note the agent would still ${actionLabel(r.final_action).toLowerCase()}.`}
        </strong>
        <span>
          {r.draft_used
            ? `It used your note${r.tools_used.includes('k0_draft_lookup') ? ' and ran its lookup' : ''}.`
            : 'It didn’t use your note: it neither cited it nor ran its lookup. Check that the title and the text say when it applies.'}
          {r.demo ? ' Demo console: simulated, no model was called.' : ''}
        </span>
      </div>
      <div className="flex flex-wrap gap-3">
        <Side
          heading="When it decided"
          sub={d.prompt_version ? `prompt ${d.prompt_version}` : undefined}
          action={d.final_action}
          proposed={d.guardrail_changed ? d.proposed_action : null}
          confidence={d.confidence}
          reason={d.reason}
          evidence={d.evidence}
          reply={d.reply_text}
          notes={d.guardrail_notes ?? []}
        />
        <Side
          heading="With your note"
          sub={`tried by ${t.created_by === service.user ? 'you' : t.created_by}${r.prompt_version ? ` · prompt ${r.prompt_version}` : ''}`}
          action={r.final_action}
          proposed={r.proposed_action !== r.final_action ? r.proposed_action : null}
          confidence={r.confidence}
          reason={r.reason}
          evidence={r.evidence}
          reply={r.would_post ?? r.reply_text}
          notes={r.guardrail_notes}
          highlight
        />
      </div>
      <p className="m-0 text-xs text-muted-foreground">
        Lookups it ran: {r.tools_used.length ? r.tools_used.join(', ') : 'none'} · {count(r.input_tokens)} in / {count(r.output_tokens)} out tokens. Nothing was posted, e-mailed or
        added to the decision log.
      </p>
    </section>
  )
}

function Side({
  heading,
  sub,
  action,
  proposed,
  confidence,
  reason,
  evidence,
  reply,
  notes,
  highlight,
}: {
  heading: string
  sub?: string
  action: DecisionDetail['final_action']
  proposed: DecisionDetail['proposed_action'] | null
  confidence: number | null
  reason: string | null
  evidence: string[]
  reply: string | null
  notes: string[]
  highlight?: boolean
}) {
  return (
    <div className={cn('flex min-w-0 flex-[1_1_300px] flex-col gap-3 rounded-lg border px-3.5 py-3', highlight ? 'border-primary bg-card' : 'border-border bg-background')}>
      <div className="flex flex-col gap-0.5">
        <span className="text-[13px] font-semibold">{heading}</span>
        {sub ? <span className="text-xs text-muted-foreground">{sub}</span> : null}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {action ? <StatusPill status={ACTION_STATUS[action]} label={actionLabel(action)} /> : <span className="text-[13px] text-muted-foreground">No decision</span>}
        {proposed ? <span className="text-xs text-muted-foreground">proposed <s>{actionLabel(proposed)}</s>, changed by a rule</span> : null}
      </div>
      <Confidence value={confidence} />
      {reason ? <p className="m-0 text-[13px]">{reason}</p> : null}
      {evidence.length ? (
        <ul className="m-0 flex flex-col gap-0.5 pl-4 text-xs text-secondary-foreground">
          {evidence.slice(0, 6).map((e, i) => (
            <li key={i} className="break-words">
              {e}
            </li>
          ))}
        </ul>
      ) : null}
      {reply ? <div className="rounded-md border border-border bg-white px-3 py-2 text-[13px] whitespace-pre-line">{reply}</div> : null}
      {notes.length ? (
        <ul className="m-0 flex flex-col gap-0.5 pl-4 text-xs text-warn-fg">
          {notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
