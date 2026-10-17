/**
 * The outcome timeline of one decision: decided → checked → (held → decided by a person) → what reached the ticket.
 * "Approved" and "Posted" are separate steps on purpose: an approved reply goes out on the desk's next pass.
 */
import type { DecisionDetail, ServiceDesk } from './api/types'
import { span } from './format'
import { actionLabel } from './vocab'

export type TimelineKind = 'done' | 'warn' | 'current' | 'queued' | 'bad' | 'pending'

export interface TimelineStep {
  label: string
  at: string | null
  note: string
  kind: TimelineKind
}

const executedWords: Record<string, string> = {
  reply: 'reply posted',
  close: 'ticket closed',
  answered: 'marked answered',
  note: 'internal note',
  moved: 'moved to its topic',
  'email queued': 'email queued',
}

export function buildTimeline(d: DecisionDetail, desk: ServiceDesk | undefined, now: number): { steps: TimelineStep[]; note: string } {
  const steps: TimelineStep[] = []
  const deskName = desk?.title ?? 'the desk'
  steps.push({
    label: 'Decided',
    at: d.created_at,
    note: `${d.mode === 'live' ? 'Live' : d.mode === 'notes' ? 'Notes' : d.mode === 'shadow' ? 'Shadow' : 'Audit'} pass · proposed ${actionLabel(d.proposed_action).toLowerCase()}`,
    kind: 'done',
  })

  if (d.error) {
    steps.push({ label: 'Failed', at: d.created_at, note: d.error, kind: 'bad' })
    return { steps, note: 'The agent stopped on this ticket. Nothing was sent to the branch; it tries again on the next pass.' }
  }

  if (d.guardrail_changed) {
    steps.push({ label: 'Checked', at: d.created_at, note: `A rule changed it to ${actionLabel(d.final_action).toLowerCase()}`, kind: 'warn' })
  } else {
    steps.push({ label: 'Checked', at: d.created_at, note: d.guardrail_notes.length ? 'Rules noted something' : 'All rules passed', kind: 'done' })
  }

  const a = d.approval
  if (a) {
    steps.push({ label: 'Held', at: a.created_at, note: 'Waiting for a person', kind: 'warn' })
    if (a.status === 'pending') {
      steps.push({ label: 'Approved', at: null, note: `Waiting for an Admin · ${span((now - new Date(a.created_at).getTime()) / 60000)}`, kind: 'current' })
      steps.push({ label: 'Posted', at: null, note: 'On the next pass after approval', kind: 'pending' })
      return { steps, note: 'Nothing has been sent to the branch yet. An approved reply goes out on the desk’s next pass, not at once.' }
    }
    if (a.status === 'approved' || a.status === 'posted') {
      steps.push({ label: 'Approved', at: a.decided_at, note: `By ${a.decided_by ?? 'an Admin'}${a.edited ? ', with edits' : ''}`, kind: 'done' })
      if (a.status === 'approved') {
        steps.push({ label: 'Posted', at: null, note: `On ${deskName}’s next pass`, kind: 'queued' })
        return {
          steps,
          note: `Approved is not Posted. The reply waits for ${deskName}’s next pass; this step turns to Posted once it is on the ticket.${
            d.mode === 'live' ? '' : ' The desk is not live, so it goes out when it runs live again.'
          }`,
        }
      }
      steps.push({ label: 'Posted', at: a.finished_at, note: a.result ?? 'On the ticket', kind: 'done' })
      return { steps, note: 'The reply is on the ticket.' }
    }
    if (a.status === 'rejected' || a.status === 'escalated') {
      steps.push({ label: 'Rejected', at: a.decided_at, note: `By ${a.decided_by ?? 'an Admin'}`, kind: 'bad' })
      if (a.status === 'rejected') {
        steps.push({ label: 'Escalation note', at: null, note: `On ${deskName}’s next pass`, kind: 'queued' })
        return { steps, note: 'Nothing goes to the branch. On the next pass the agent leaves an escalation note with the comment, so a person takes the ticket.' }
      }
      steps.push({ label: 'Escalation note', at: a.finished_at, note: a.result ?? 'Posted', kind: 'done' })
      return { steps, note: 'The reply was not sent. An escalation note with the comment is on the ticket.' }
    }
    if (a.status === 'dropped') {
      steps.push({ label: 'Dropped', at: a.finished_at, note: a.result ?? 'The ticket changed', kind: 'pending' })
      return { steps, note: 'The ticket changed before anyone decided, so the held reply was dropped and the agent decides again.' }
    }
  }

  if (d.deferred) {
    steps.push({ label: 'Deferred', at: d.created_at, note: 'Retried on a later pass', kind: 'current' })
    return { steps, note: 'The agent will look at this ticket again.' }
  }
  if (d.mode === 'shadow') {
    steps.push({ label: 'Logged', at: d.created_at, note: 'Shadow: nothing written to osTicket', kind: 'done' })
    return { steps, note: 'In shadow the agent only logs what it would do, to compare with what staff do.' }
  }
  if (d.mode === 'notes') {
    steps.push({ label: 'Note written', at: d.created_at, note: 'Internal note for staff', kind: 'done' })
    return { steps, note: 'In notes mode the decision is an internal note; staff act on it.' }
  }
  const did = d.executed.map((e) => executedWords[e] ?? e)
  steps.push({ label: 'On the ticket', at: d.created_at, note: did.length ? did.join(' · ') : 'Nothing written', kind: did.length ? 'done' : 'pending' })
  return { steps, note: did.length ? `Done on the ticket: ${did.join(', ')}.` : 'Nothing was written to the ticket.' }
}
