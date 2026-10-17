/**
 * Shared status vocabulary. Every pill in the portal comes from here so that look-alike
 * pairs stay visually distinct everywhere:
 *   intent (proposed, approved, queued) is outlined or dashed; facts (closed, posted) are filled.
 */

export type StatusKey =
  // what happened to a ticket
  | 'new'
  | 'proposedClosure'
  | 'closed'
  | 'awaitingApproval'
  | 'awaitingBranch'
  | 'deferred'
  | 'escalated'
  | 'failed'
  | 'noAction'
  | 'routed'
  | 'notReached'
  | 'skipped'
  // replies held for approval
  | 'approved'
  | 'posted'
  | 'rejected'
  | 'rejectedPosted'
  | 'dropped'
  // evidence lookups
  | 'found'
  | 'noMatch'
  | 'lookupFailed'
  // desk health
  | 'running'
  | 'paused'
  | 'stale'
  | 'neverRan'
  // rule outcomes
  | 'ruleChanged'
  | 'ruleHeld'
  | 'ruleNote'
  // non-live modes
  | 'noteWritten'
  | 'shadowLogged'
  // outbound messages
  | 'sent'
  | 'sendFailed'
  | 'queued'
  | 'sendSkipped'
  // runs
  | 'runRunning'
  | 'runCompleted'
  | 'runWithErrors'
  | 'runUnfinished'
  // reviews
  | 'reviewCorrect'
  | 'reviewIncorrect'
  // proposed actions
  | 'actClose'
  | 'actEscalate'
  | 'actAskBranch'
  | 'actNone'
  | 'actRoute'

interface StatusStyle {
  label: string
  /** background, text and border classes */
  className: string
  dot: string
}

export const STATUS: Record<StatusKey, StatusStyle> = {
  new: { label: 'Not decided yet', className: 'bg-card text-secondary-foreground border-input', dot: 'bg-neutral-dot' },
  proposedClosure: { label: 'Proposed closure', className: 'bg-card text-ok-fg border-primary border-dashed', dot: 'bg-primary' },
  closed: { label: 'Replied & closed', className: 'bg-ok-bg text-ok-fg border-ok-bg', dot: 'bg-primary' },
  awaitingApproval: { label: 'Awaiting approval', className: 'bg-warn-bg text-warn-fg border-warn-dot', dot: 'bg-warn-dot' },
  awaitingBranch: { label: 'Awaiting branch', className: 'bg-info-bg text-info-fg border-info-bg', dot: 'bg-info-dot' },
  deferred: { label: 'Deferred', className: 'bg-plum-bg text-plum-fg border-plum-bg', dot: 'bg-plum-dot' },
  escalated: { label: 'Escalated', className: 'bg-esc-bg text-esc-fg border-esc-bg', dot: 'bg-esc-dot' },
  failed: { label: 'Failed', className: 'bg-bad-fg text-white border-bad-fg', dot: 'bg-white' },
  noAction: { label: 'No action', className: 'bg-muted text-secondary-foreground border-muted', dot: 'bg-neutral-dot' },
  routed: { label: 'Moved to its topic', className: 'bg-ok-bg text-ok-fg border-ok-bg', dot: 'bg-ok-dot' },
  notReached: { label: 'Not reached', className: 'bg-card text-secondary-foreground border-shadow-line border-dashed', dot: 'bg-neutral-dot' },
  skipped: { label: 'Skipped', className: 'bg-muted text-secondary-foreground border-muted', dot: 'bg-neutral-dot' },

  approved: { label: 'Approved · posts next pass', className: 'bg-card text-ok-fg border-primary', dot: 'bg-primary' },
  posted: { label: 'Posted', className: 'bg-primary text-white border-primary', dot: 'bg-white' },
  rejected: { label: 'Rejected · note next pass', className: 'bg-card text-esc-fg border-esc-dot', dot: 'bg-esc-dot' },
  rejectedPosted: { label: 'Rejected · escalation note posted', className: 'bg-esc-bg text-esc-fg border-esc-bg', dot: 'bg-esc-dot' },
  dropped: { label: 'Dropped · ticket changed', className: 'bg-card text-secondary-foreground border-shadow-line border-dashed', dot: 'bg-neutral-dot' },

  found: { label: 'Found', className: 'bg-ok-bg text-ok-fg border-ok-bg', dot: 'bg-ok-dot' },
  noMatch: { label: 'No matching record', className: 'bg-muted text-secondary-foreground border-muted', dot: 'bg-neutral-dot' },
  lookupFailed: { label: 'Lookup failed', className: 'bg-bad-bg text-bad-fg border-bad-line', dot: 'bg-bad-dot' },

  running: { label: 'Running', className: 'bg-ok-bg text-ok-fg border-ok-bg', dot: 'bg-ok-dot' },
  paused: { label: 'Paused', className: 'bg-paused-bg text-paused-fg border-paused-bg', dot: 'bg-paused-dot' },
  stale: { label: 'Stale', className: 'bg-warn-bg text-warn-fg border-warn-line', dot: 'bg-warn-dot' },
  neverRan: { label: 'Never ran', className: 'bg-warn-bg text-warn-fg border-warn-line', dot: 'bg-warn-dot' },

  ruleChanged: { label: 'Changed the action', className: 'bg-warn-bg text-warn-fg border-warn-line', dot: 'bg-warn-dot' },
  ruleHeld: { label: 'Held for approval', className: 'bg-card text-warn-fg border-warn-dot', dot: 'bg-warn-dot' },
  ruleNote: { label: 'Noted', className: 'bg-muted text-secondary-foreground border-muted', dot: 'bg-neutral-dot' },

  noteWritten: { label: 'Note written', className: 'bg-info-bg text-info-fg border-info-bg', dot: 'bg-info-dot' },
  shadowLogged: { label: 'Shadow — not written', className: 'bg-card text-secondary-foreground border-shadow-line border-dashed', dot: 'bg-neutral-dot' },

  sent: { label: 'Sent', className: 'bg-ok-bg text-ok-fg border-ok-bg', dot: 'bg-ok-dot' },
  sendFailed: { label: 'Failed to send', className: 'bg-bad-bg text-bad-fg border-bad-line', dot: 'bg-bad-dot' },
  queued: { label: 'Queued · next pass', className: 'bg-card text-secondary-foreground border-shadow-line border-dashed', dot: 'bg-neutral-dot' },
  sendSkipped: { label: 'Not sent', className: 'bg-muted text-secondary-foreground border-muted', dot: 'bg-neutral-dot' },

  runRunning: { label: 'Running', className: 'bg-info-bg text-info-fg border-info-bg', dot: 'bg-info-dot' },
  runCompleted: { label: 'Completed', className: 'bg-ok-bg text-ok-fg border-ok-bg', dot: 'bg-ok-dot' },
  runWithErrors: { label: 'Completed with errors', className: 'bg-warn-bg text-warn-fg border-warn-line', dot: 'bg-warn-dot' },
  runUnfinished: { label: 'Did not finish', className: 'bg-card text-warn-fg border-warn-dot border-dashed', dot: 'bg-warn-dot' },

  reviewCorrect: { label: 'Correct', className: 'bg-ok-bg text-ok-fg border-ok-bg', dot: 'bg-ok-dot' },
  reviewIncorrect: { label: 'Incorrect', className: 'bg-bad-bg text-bad-fg border-bad-line', dot: 'bg-bad-dot' },

  actClose: { label: 'Reply & close', className: 'bg-card text-ok-fg border-primary border-dashed', dot: 'bg-primary' },
  actEscalate: { label: 'Escalate', className: 'bg-esc-bg text-esc-fg border-esc-bg', dot: 'bg-esc-dot' },
  actAskBranch: { label: 'Ask branch', className: 'bg-info-bg text-info-fg border-info-bg', dot: 'bg-info-dot' },
  actNone: { label: 'No action', className: 'bg-muted text-secondary-foreground border-muted', dot: 'bg-neutral-dot' },
  actRoute: { label: 'Move topic', className: 'bg-muted text-secondary-foreground border-muted', dot: 'bg-neutral-dot' },
}

/** Operating mode chips. Live is set in server configuration; Notes and Shadow can also be holds from the portal. */
export const MODE = {
  live: { label: 'Live', className: 'bg-ok-bg text-ok-fg border-ok-line', dot: 'bg-ok-dot', tip: 'Replies, closes and escalates within the rules. Off-script replies wait for approval.' },
  notes: { label: 'Notes', className: 'bg-info-bg text-info-fg border-info-line', dot: 'bg-info-dot', tip: 'Writes its decision as an internal note. Staff act on it.' },
  shadow: { label: 'Shadow', className: 'bg-card text-secondary-foreground border-shadow-line border-dashed', dot: 'bg-neutral-dot', tip: 'Decides and logs only. Writes nothing to osTicket.' },
} as const
