import * as React from 'react'
import { Check, CircleX, Lock } from 'lucide-react'

import { ErrorNote } from '@/components/data-state'
import { isPlainKey, Kbd } from '@/components/shell/go-to'
import { StepEyebrow } from '@/components/page-header'
import { StatusPill } from '@/components/status-pill'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input, Label, NativeSelect, Textarea } from '@/components/ui/input'
import { useDecideApproval, useReview } from '@/lib/api/hooks'
import type { Action, DecisionDetail, ServiceDesk } from '@/lib/api/types'
import { useNow, when } from '@/lib/format'
import { usePortal } from '@/lib/portal'
import { ACTIONS, ACTION_LABEL, actionLabel } from '@/lib/vocab'

export function ActionBar({ decision: d, desk }: { decision: DecisionDetail; desk: ServiceDesk }) {
  const { isAdmin } = usePortal()
  const pending = d.approval?.status === 'pending'
  const title = pending ? (isAdmin ? 'Approve, edit or reject the held reply' : 'A reply is waiting for an Admin') : 'Was the agent right?'
  return (
    <Card className="gap-3.5 border-input px-5 pt-4 pb-5">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div className="flex flex-col gap-1.5">
          <StepEyebrow n={6}>Your action</StepEyebrow>
          <h2 className="m-0 text-base font-semibold">{title}</h2>
        </div>
        <span className="text-xs text-muted-foreground">{isAdmin ? 'Signed in as an Admin' : 'Signed in as a Viewer: read and review only'}</span>
      </div>
      {d.approval ? <ApprovalBlock decision={d} desk={desk} /> : null}
      {d.mode !== 'audit' ? <ReviewBlock decision={d} desk={desk} /> : null}
    </Card>
  )
}

function ApprovalBlock({ decision: d, desk }: { decision: DecisionDetail; desk: ServiceDesk }) {
  const { isAdmin } = usePortal()
  const now = useNow()
  const a = d.approval!
  const decide = useDecideApproval()
  const [editing, setEditing] = React.useState(false)
  const [text, setText] = React.useState(a.reply_masked ?? '')
  const [comment, setComment] = React.useState('')
  const [confirm, setConfirm] = React.useState<null | 'approve' | 'reject'>(null)
  const commentId = React.useId()
  const canDecide = isAdmin && a.status === 'pending'

  // The classic console's keys: A approve, E edit, X reject (Enter confirms: the Yes button takes focus), Esc backs out.
  React.useEffect(() => {
    if (!canDecide) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if ((e.target as HTMLElement | null)?.closest?.('[role="dialog"]')) return
        if (confirm) setConfirm(null)
        else if (editing && !(e.target instanceof HTMLTextAreaElement)) {
          setEditing(false)
          setText(a.reply_masked ?? '')
        }
        return
      }
      if (!isPlainKey(e) || decide.isPending) return
      const k = e.key.toLowerCase()
      if (k === 'a' && !(editing && !text.trim())) {
        e.preventDefault()
        setConfirm('approve')
      } else if (k === 'x') {
        e.preventDefault()
        setConfirm('reject')
      } else if (k === 'e' && a.reply_masked && !editing) {
        e.preventDefault()
        setEditing(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [canDecide, confirm, editing, text, a.reply_masked, decide.isPending])
  // The Yes button takes focus when the confirmation opens, so Enter confirms and Tab reaches Cancel.
  const yesRef = React.useRef<HTMLButtonElement>(null)
  React.useEffect(() => {
    if (confirm) yesRef.current?.focus()
  }, [confirm])

  if (a.status !== 'pending') {
    const who = a.decided_by ? `${a.decided_by} at ${when(a.decided_at, now)}` : null
    const lines: Record<string, { tone: string; text: string }> = {
      approved: { tone: 'bg-ok-bg text-ok-fg', text: `Approved${a.edited ? ' with edits' : ''} by ${who}. Not posted yet — it goes out on ${desk.title}’s next pass.` },
      posted: { tone: 'bg-ok-bg text-ok-fg', text: `Approved${a.edited ? ' with edits' : ''} by ${who}, and posted ${when(a.finished_at, now)}.` },
      rejected: { tone: 'bg-esc-bg text-esc-fg', text: `Rejected by ${who}. Nothing goes to the branch; the agent leaves an escalation note on the next pass.` },
      escalated: { tone: 'bg-esc-bg text-esc-fg', text: `Rejected by ${who}. The escalation note is on the ticket.` },
      dropped: { tone: 'bg-muted text-secondary-foreground', text: `Dropped ${when(a.finished_at, now)}: ${a.result ?? 'the ticket changed before anyone decided'}. The agent decides again.` },
    }
    const line = lines[a.status] ?? { tone: 'bg-muted', text: a.status }
    return (
      <div role="status" className={`flex flex-col gap-1 rounded-lg px-3.5 py-3 text-[13px] ${line.tone}`}>
        <strong className="font-semibold">{line.text}</strong>
        {a.comment ? <span>Comment: “{a.comment}”</span> : null}
        {a.status === 'approved' || a.status === 'rejected' ? <span className="text-xs opacity-80">A decision can’t be undone from the portal.</span> : null}
      </div>
    )
  }

  if (!isAdmin) {
    return (
      <div className="flex items-start gap-2.5 rounded-lg bg-muted px-3.5 py-3 text-[13px] text-secondary-foreground">
        <Lock aria-hidden className="mt-0.5 size-4 shrink-0" />
        <span>Only Admins can approve or reject replies. You can still say below whether the agent got this right; it feeds the Review accuracy figures.</span>
      </div>
    )
  }

  const submit = (verdict: 'approve' | 'reject') =>
    decide.mutate(
      { desk: desk.key, id: a.id, verdict, reply: verdict === 'approve' && editing ? text : undefined, comment: comment.trim() || undefined },
      { onSuccess: () => { setConfirm(null); setEditing(false) }, onError: () => setConfirm(null) },
    )

  return (
    <div className="flex flex-col gap-3">
      {editing ? (
        <label className="flex flex-col gap-1.5 text-[13px] font-semibold">
          Edit the reply before approving
          <Textarea rows={7} value={text} onChange={(e) => setText(e.target.value)} className="font-normal" />
          <span className="text-xs font-normal text-muted-foreground">
            Keep masked values like &lt;MOBILE_1&gt; as they are; they are filled in when the reply is posted. New account or phone numbers can’t be typed in.
          </span>
        </label>
      ) : null}

      <Label htmlFor={commentId} className="text-[13px] text-foreground">
        Comment <span className="font-normal text-muted-foreground">(kept in the log; on reject it goes into the escalation note)</span>
      </Label>
      <Input id={commentId} value={comment} maxLength={1000} onChange={(e) => setComment(e.target.value)} className="h-10" />

      <ErrorNote error={decide.error} />

      {confirm === 'approve' ? (
        <div className="flex flex-wrap items-center gap-2.5 rounded-lg border border-primary bg-ok-bg px-3.5 py-3">
          <strong className="flex-[1_1_280px] text-[13px] font-semibold text-ok-fg">
            Post this {editing ? 'edited ' : ''}reply on {desk.title}’s next pass{a.action === 'reply_and_close' ? ' and close the ticket' : ''}?
          </strong>
          <Button size="lg" ref={yesRef} onClick={() => submit('approve')} disabled={decide.isPending}>
            <Check aria-hidden strokeWidth={2.4} />
            {decide.isPending ? 'Approving…' : 'Yes, approve'}
          </Button>
          <Button size="lg" variant="secondary" onClick={() => setConfirm(null)} disabled={decide.isPending}>
            Cancel
          </Button>
        </div>
      ) : confirm === 'reject' ? (
        <div className="flex flex-wrap items-center gap-2.5 rounded-lg border border-destructive bg-bad-row px-3.5 py-3">
          <strong className="flex-[1_1_280px] text-[13px] font-semibold text-bad-fg">
            Reject it? Nothing is sent to the branch; the agent leaves an escalation note{comment.trim() ? ' with your comment' : ''} so a person takes the ticket.
          </strong>
          <Button size="lg" variant="destructive" ref={yesRef} onClick={() => submit('reject')} disabled={decide.isPending}>
            {decide.isPending ? 'Rejecting…' : 'Yes, reject'}
          </Button>
          <Button size="lg" variant="secondary" onClick={() => setConfirm(null)} disabled={decide.isPending}>
            Cancel
          </Button>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2.5">
          <Button size="lg" onClick={() => setConfirm('approve')} disabled={editing && !text.trim()}>
            <Check aria-hidden strokeWidth={2.4} />
            {editing ? 'Approve edited reply' : 'Approve'}
          </Button>
          {editing ? (
            <Button size="lg" variant="secondary" onClick={() => { setEditing(false); setText(a.reply_masked ?? '') }}>
              Stop editing
            </Button>
          ) : (
            <Button size="lg" variant="outline" onClick={() => setEditing(true)} disabled={!a.reply_masked}>
              Edit &amp; approve
            </Button>
          )}
          <Button size="lg" variant="destructive" onClick={() => setConfirm('reject')}>
            Reject
          </Button>
          <span className="flex-[1_1_260px] text-[13px] text-muted-foreground">Approving queues the reply; it goes out on the desk’s next pass, not instantly.</span>
          <span className="hidden basis-full gap-3 text-xs text-muted-foreground lg:flex" aria-hidden>
            <span>
              <Kbd>A</Kbd> approve
            </span>
            <span>
              <Kbd>E</Kbd> edit
            </span>
            <span>
              <Kbd>X</Kbd> reject
            </span>
            <span>
              <Kbd>J</Kbd> <Kbd>K</Kbd> next / previous
            </span>
            <span>
              <Kbd>?</Kbd> all keys
            </span>
          </span>
        </div>
      )}
    </div>
  )
}

function ReviewBlock({ decision: d, desk }: { decision: DecisionDetail; desk: ServiceDesk }) {
  const { service } = usePortal()
  const now = useNow()
  const review = useReview()
  const [choosing, setChoosing] = React.useState(false)
  const [expected, setExpected] = React.useState<Action>(d.final_action === 'escalate' ? 'reply_and_close' : 'escalate')
  const [comment, setComment] = React.useState('')
  const mine = d.reviews.find((r) => r.reviewer === service.user)
  const others = d.reviews.filter((r) => r !== mine)

  const save = (verdict: 'correct' | 'incorrect') =>
    review.mutate(
      { desk: desk.key, decisionId: d.id, verdict, expected: verdict === 'incorrect' ? expected : null, comment: comment.trim() || undefined },
      { onSuccess: () => { setChoosing(false); setComment('') } },
    )

  return (
    <div className="flex flex-col gap-2.5 border-t border-border-soft pt-3.5">
      <span className="text-[13px] font-semibold">
        Review <span className="font-normal text-muted-foreground">· mark what the agent did, not what the branch did next</span>
      </span>

      {d.reviews.length ? (
        <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-[13px]">
          {[...(mine ? [mine] : []), ...others].map((r) => (
            <li key={r.id} className="flex flex-wrap items-center gap-2">
              <StatusPill status={r.verdict === 'correct' ? 'reviewCorrect' : 'reviewIncorrect'} label={r.verdict === 'correct' ? 'Correct' : `Incorrect — expected ${actionLabel(r.expected_action).toLowerCase()}`} />
              <span className="text-muted-foreground">
                {r.reviewer === service.user ? 'You' : r.reviewer} · {when(r.created_at, now)}
              </span>
              {r.comment ? <span className="text-secondary-foreground">“{r.comment}”</span> : null}
            </li>
          ))}
        </ul>
      ) : null}

      <ErrorNote error={review.error} />

      {choosing ? (
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault()
            save('incorrect')
          }}
        >
          <Label className="flex-[0_1_240px] text-[13px] text-foreground">
            What should it have done?
            <NativeSelect value={expected} onChange={(e) => setExpected(e.target.value as Action)} className="h-11 text-sm font-normal">
              {ACTIONS.filter((a) => a !== d.final_action).map((a) => (
                <option key={a} value={a}>
                  {ACTION_LABEL[a]}
                </option>
              ))}
            </NativeSelect>
          </Label>
          <Label className="flex-[1_1_300px] text-[13px] text-foreground">
            Note for the team (optional)
            <Input value={comment} onChange={(e) => setComment(e.target.value)} maxLength={2000} className="h-11 text-sm font-normal" placeholder="e.g. the reversal shows in core banking at 08:10" />
          </Label>
          <Button size="lg" type="submit" disabled={review.isPending}>
            {review.isPending ? 'Saving…' : 'Save review'}
          </Button>
          <Button size="lg" variant="secondary" type="button" onClick={() => setChoosing(false)}>
            Cancel
          </Button>
        </form>
      ) : (
        <div className="flex flex-wrap items-center gap-2.5">
          <Button size="lg" variant="outline" onClick={() => save('correct')} disabled={review.isPending}>
            <Check aria-hidden strokeWidth={2.4} />
            {mine ? 'Mark correct again' : 'Mark correct'}
          </Button>
          <Button size="lg" variant="destructive" onClick={() => setChoosing(true)} disabled={review.isPending}>
            <CircleX aria-hidden />
            Mark incorrect
          </Button>
          {d.reviews.length ? <span className="text-xs text-muted-foreground">The latest review is the one the accuracy figures count.</span> : null}
        </div>
      )}
    </div>
  )
}
