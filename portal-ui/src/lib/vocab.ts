/** Turning the service's values (actions, approval states, queue states, check statuses) into words and pills. */
import type { Action, ApprovalStatus, DecisionSummary, DeskVocab, Mode, QueueItem, VocabTone } from './api/types'
import type { StatusKey } from './status'

export const ACTIONS: Action[] = ['reply_and_close', 'request_info', 'escalate', 'no_action', 'move']

export const ACTION_LABEL: Record<Action, string> = {
  reply_and_close: 'Reply & close',
  request_info: 'Ask branch',
  escalate: 'Escalate',
  no_action: 'No action',
  move: 'Move topic',
}

export const ACTION_STATUS: Record<Action, StatusKey> = {
  reply_and_close: 'actClose',
  request_info: 'actAskBranch',
  escalate: 'actEscalate',
  no_action: 'actNone',
  move: 'actRoute',
}

export const actionLabel = (a: Action | null | undefined) => (a ? ACTION_LABEL[a] ?? a.replaceAll('_', ' ') : '—')

export const APPROVAL_STATUS: Record<ApprovalStatus, StatusKey> = {
  pending: 'awaitingApproval',
  approved: 'approved',
  rejected: 'rejected',
  posted: 'posted',
  escalated: 'rejectedPosted',
  dropped: 'dropped',
}

export const approvalStatus = (s: string | null | undefined): StatusKey => APPROVAL_STATUS[s as ApprovalStatus] ?? 'skipped'

/** What a decision came to: the pill shown in lists and on the ticket. */
export function decisionOutcome(d: Pick<DecisionSummary, 'error' | 'deferred' | 'approval' | 'mode' | 'final_action' | 'executed'>): {
  status: StatusKey
  label?: string
} {
  if (d.error) return { status: 'failed' }
  if (d.deferred) return { status: 'deferred', label: 'Deferred · retried next pass' }
  if (d.approval) return { status: approvalStatus(d.approval) }
  const action = d.final_action
  if (d.mode === 'shadow') return { status: 'shadowLogged', label: `Shadow · would ${action ? ACTION_LABEL[action].toLowerCase() : 'act'}` }
  if (d.mode === 'notes') return { status: 'noteWritten', label: `Note · proposes ${action ? ACTION_LABEL[action].toLowerCase() : 'an action'}` }
  if (d.mode === 'audit') return { status: 'skipped', label: 'Audit replay' }
  switch (action) {
    case 'reply_and_close':
      return { status: d.executed.includes('close') ? 'closed' : 'proposedClosure', label: d.executed.includes('close') ? undefined : 'Replied' }
    case 'request_info':
      return { status: 'awaitingBranch', label: 'Asked branch' }
    case 'escalate':
      return { status: 'escalated' }
    case 'move':
      return { status: 'routed' }
    case 'no_action':
      return { status: 'noAction' }
    default:
      return { status: 'skipped', label: 'No decision' }
  }
}

/** Where an open ticket stood at the desk's last pass. */
const QUEUE_DETAIL: [string, string][] = [
  ['already escalated', 'Escalated on an earlier pass; left with the team'],
  ['waiting on ticket owner', 'Asked earlier; waiting on the ticket owner'],
  ['waiting on the branch', 'Asked earlier; waiting on the branch'],
  ['unchanged since last decision', 'Nothing new since the last decision'],
  ['awaiting approval', 'Reply held for an Admin'],
]

/** The queue's detail in words a supervisor reads, or a note that the desk decided it on this pass. */
export function queueDetail(item: Pick<QueueItem, 'state' | 'detail'>): string {
  const detail = (item.detail ?? '').trim()
  const known = QUEUE_DETAIL.find(([k]) => detail.toLowerCase().startsWith(k))
  if (known) return known[1]
  if (detail) return detail.charAt(0).toUpperCase() + detail.slice(1)
  return item.state === 'not reached' ? 'The pass ended before this ticket' : 'Decided on this pass'
}

export function queueState(item: Pick<QueueItem, 'state' | 'detail'>): { status: StatusKey; label?: string } {
  const detail = (item.detail ?? '').toLowerCase()
  switch (item.state) {
    case 'reply_and_close':
      return { status: 'closed' }
    case 'request_info':
      return { status: 'awaitingBranch', label: 'Asked branch' }
    case 'escalate':
      return { status: 'escalated' }
    case 'move':
      return { status: 'routed' }
    case 'no_action':
      return { status: 'noAction' }
    case 'error':
      return { status: 'failed' }
    case 'not reached':
      return { status: 'notReached' }
    case 'skipped':
      if (detail.startsWith('awaiting approval')) return { status: 'awaitingApproval' }
      if (detail.includes('waiting on ticket owner') || detail.includes('waiting on the branch')) return { status: 'awaitingBranch' }
      if (detail.includes('already escalated')) return { status: 'escalated', label: 'Escalated earlier' }
      if (detail.includes('approved') || detail.includes('rejected')) return { status: 'approved', label: 'Decided in the console' }
      return { status: 'skipped' }
    default:
      return { status: 'skipped', label: item.state }
  }
}

export const MODE_LABEL: Record<Mode, string> = { live: 'Live', notes: 'Notes', shadow: 'Shadow' }

/** A desk's check status in its own words, with a tone. Unknown statuses read as themselves. */
export function checkStatus(vocab: DeskVocab | undefined, status: string): { tone: VocabTone; label: string } {
  const entry = vocab?.status?.[status]
  return entry ? { tone: entry[0], label: entry[1] } : { tone: 'idle', label: status.replaceAll('_', ' ') }
}

export const TONE_CLASS: Record<string, string> = {
  ok: 'bg-ok-bg text-ok-fg border-ok-bg',
  warn: 'bg-warn-bg text-warn-fg border-warn-line',
  crit: 'bg-bad-bg text-bad-fg border-bad-line',
  ask: 'bg-info-bg text-info-fg border-info-bg',
  idle: 'bg-muted text-secondary-foreground border-muted',
}

/** Turns lookup names like find_switch_transactions into "Find switch transactions". */
export const humanize = (name: string) => {
  const s = name.replaceAll('_', ' ').trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/** Notification kinds as people say them. */
export const EMAIL_KIND: Record<string, string> = {
  reply_and_close: 'Resolved',
  request_info: 'Information request',
  escalate: 'Escalation',
  admin_update: 'Service alert',
  admin_alert: 'Service alert',
  admin_recovered: 'Service recovered',
}
export const emailKind = (k: string) => EMAIL_KIND[k] ?? (k.startsWith('admin_') ? 'Service alert' : humanize(k))
