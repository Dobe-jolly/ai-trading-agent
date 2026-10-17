/**
 * The console API's responses (nmbz.console and agentcore.portal). Field names are the server's, unchanged.
 * Every text that can carry customer data arrives masked (<NUMBER_1>, <MOBILE_1>, …).
 */

export type Role = 'admin' | 'viewer'
export type Mode = 'shadow' | 'notes' | 'live'
export type Hold = 'shadow' | 'notes'
/** What a decision does to the ticket (agentcore.portal.data.ACTIONS). */
export type Action = 'reply_and_close' | 'request_info' | 'escalate' | 'no_action' | 'move'
/** Where a reply held for approval stands. approved = posts on the desk's next pass; escalated = rejected, note posted. */
export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'posted' | 'escalated' | 'dropped'
export type Severity = 'ok' | 'watch' | 'incident'

/** A check status's tone in the desk's vocabulary. */
export type VocabTone = 'ok' | 'warn' | 'crit' | 'idle' | 'ask' | string

export interface DeskVocab {
  title?: string
  agent?: string
  status?: Record<string, [VocabTone, string]>
  template_label?: string
  queue_sub?: string
  queue_empty?: string
  pause_placeholder?: string
  anomaly?: { unavailable?: string; intro?: string }
}

export interface DeskHealth {
  minutes_since_last_run: number | null
  stale: boolean
  stale_after_minutes: number
  running: boolean
  last_run_at: string | null
  last_run_errors: number | null
}

export interface ServiceDesk {
  key: string
  title: string
  blurb: string
  vocab: DeskVocab
  configured_mode: Mode
  effective_mode: Mode
  mode_override: Hold | null
  kill_switch: boolean
  approvals_pending: number
  approval_mode: 'off' | 'unscripted' | 'all'
  /** Link to a ticket in osTicket, with {ticket_id} and {number} to fill in; null when not configured. */
  osticket_ticket_url: string | null
  anomaly_watch: boolean
  anomaly: { severity: Severity; headline: string; created_at: string } | null
  health: DeskHealth
  emails_failed: number
  /** Notes staff proposed that wait for an Admin who did not write them. */
  knowledge_pending: number
}

export interface Service {
  user: string
  role: Role
  /** Every desk paused at once (the service stop file). */
  paused: boolean
  can_edit: boolean
  desks: ServiceDesk[]
}

export interface Run {
  run_id: string
  started_at: string
  finished_at: string | null
  mode: Mode | 'audit'
  model: string
  tickets_seen: number
  decisions: number
  errors: number
  duration_s?: number | null
}

export interface DecisionSummary {
  id: number
  created_at: string
  mode: Mode | 'audit'
  run_id: string
  ticket_id: number
  ticket_number: string
  proposed_action: Action | null
  final_action: Action | null
  confidence: number | null
  category: string | null
  statuses: Record<string, string>
  guardrail_notes: string[]
  guardrail_changed: boolean
  executed: string[]
  /** Still waiting: pending in the console, or approved and posted on the next pass. */
  held: boolean
  approval: ApprovalStatus | null
  deferred: boolean
  error: string | null
  review: 'correct' | 'incorrect' | null
}

export interface Approval {
  id: number
  decision_id: number
  created_at: string
  ticket_id: number
  ticket_number: string
  action: Action
  why: string | null
  reply_masked: string | null
  status: ApprovalStatus
  decided_by: string | null
  decided_at: string | null
  comment: string | null
  edited: number
  finished_at: string | null
  result: string | null
}

export interface ApprovalRow extends Approval {
  confidence: number | null
  category: string | null
  reason: string | null
  statuses: Record<string, string>
  mode: Mode
  proposed_action: Action | null
}

export interface Approvals {
  pending: ApprovalRow[]
  decided: ApprovalRow[]
  counts: Partial<Record<ApprovalStatus, number>>
  can_decide: boolean
  approval_mode: string
}

export interface Review {
  id: number
  decision_id: number
  created_at: string
  reviewer: string
  verdict: 'correct' | 'incorrect'
  expected_action: Action | null
  comment: string | null
}

export interface Notification {
  id: number
  created_at: string
  decision_id: number | null
  ticket_id: number | null
  ticket_number: string | null
  kind: string
  channel: string
  recipient: string | null
  cc: string | null
  subject: string
  status: 'pending' | 'sent' | 'failed' | 'skipped'
  attempts: number
  last_error: string | null
  sent_at: string | null
}

export type ContentBlock =
  | { type: 'text'; text: string }
  | { type: 'tool_use'; id: string; name: string; input: Record<string, unknown> }
  | { type: 'tool_result'; tool_use_id: string; content: string; is_error?: boolean }
  | { type: string; [key: string]: unknown }

export interface TranscriptMessage {
  role: 'user' | 'assistant'
  content: string | ContentBlock[]
}

export interface DecisionDetail extends Omit<DecisionSummary, 'approval'> {
  reason: string | null
  evidence: string[]
  reply_text: string | null
  /** The standard wording for this outcome, when the desk has one. */
  template_reply: string | null
  /** What was actually posted (audit replays and posted replies). */
  actual_reply: string | null
  tools_used: string[]
  input_tokens: number | null
  output_tokens: number | null
  transcript: TranscriptMessage[]
  attachments: { name?: string; kind?: string; status?: string; summary?: string; note?: string }[]
  reviews: Review[]
  notifications: Notification[]
  history: DecisionSummary[]
  approval_note: string | null
  approval: Approval | null
  /** First 12 characters of a hash of the prompt and tools that produced this decision. */
  prompt_version?: string | null
  /** Staff notes switched on when it decided, and the ones it used (called their lookup or cited them). */
  knowledge_active?: number[]
  knowledge_used?: { id: number; title: string | null; status: NoteStatus | null }[]
  /** When osTicket showed the ticket reopened after the agent closed it. */
  reopened_at?: string | null
}

export interface Overview {
  days: number
  health: Omit<DeskHealth, 'last_run_at' | 'last_run_errors'> & { last_run: Run | null }
  kpis: {
    decided: number
    closed: number
    escalated: number
    request_info: number
    no_action: number
    moved: number
    held: number
    deferred: number
    errors: number
    guardrail_changed: number
    approvals_pending: number
    avg_confidence: number | null
    emails_sent: number
    emails_pending: number
    emails_failed: number
  }
  by_day: ({ day: string } & Record<Action, number>)[]
  recent: DecisionSummary[]
}

export interface QueueItem {
  ticket_id: number
  ticket_number: string
  help_topic: string
  opened: string | null
  run_id: string
  updated_at: string
  /** An action the agent took this pass, or skipped / error / not reached. */
  state: Action | 'skipped' | 'error' | 'not reached' | string
  detail: string | null
  decision_id: number | null
  final_action: Action | null
  confidence: number | null
  statuses: Record<string, string>
}

export interface Queue {
  as_of: Run | null
  items: QueueItem[]
  counts: Record<string, number>
}

export interface Runs {
  items: Run[]
}

export interface Live {
  running: boolean
  run_id?: string
  started_at?: string
  updated_at?: string
  ticket_number?: string | null
  help_topic?: string | null
  steps?: { at: string; text: string }[]
}

export interface Quality {
  days: number
  reviewed: number
  correct: number
  incorrect: number
  accuracy: number | null
  by_action: { action: Action; reviewed: number; correct: number }[]
  corrections: { agent: Action; expected: Action; count: number }[]
  needs_review: DecisionSummary[]
  /** Of the closes people reviewed, how many were right. */
  close_precision?: { reviewed: number; correct: number; precision: number | null }
  /** Of the tickets it really closed, how many were reopened since (checked on each pass). */
  reopens?: { closed: number; checked: number; reopened: number; rate: number | null }
}

export interface ControlEvent {
  id: number
  created_at: string
  actor: string
  action: string
  detail: string | null
}

export interface Controls {
  kill_switch: boolean
  mode_override: Hold | null
  configured_mode: Mode
  effective_mode: Mode
  can_edit: boolean
  events: ControlEvent[]
}

export interface Notifications {
  items: Notification[]
  counts: Partial<Record<Notification['status'], number>>
  retryable: number
  can_retry: boolean
  transport: string
}

export interface AnomalySignal {
  key: string
  label: string
  severity: Severity
  value: string | number | null
  usual: string | number | null
  detail: string
}

export interface AnomalyReport {
  id: number
  created_at: string
  severity: Severity
  headline: string
  alert: string | null
  summary: {
    headline?: string
    summary?: string
    likely_causes?: string[]
    actions?: string[]
    branch_message?: string
    by?: string
  }
  analysis: { severity?: Severity; signals?: AnomalySignal[] }
}

export interface Anomalies {
  report: AnomalyReport | null
  history: { id: number; created_at: string; severity: Severity; incident_key: string; headline: string; alert: string | null }[]
  state: { active: number; incident_key: string | null; started_at: string | null; last_alert_at: string | null } | null
}

export interface DecisionList {
  total: number
  limit: number
  offset: number
  items: DecisionSummary[]
}

// ---- what staff teach a desk (agentcore.knowledge) ------------------------------------------------------------

export type NoteStatus = 'proposed' | 'active' | 'rejected' | 'retired' | 'withdrawn'
export type SlotKind = 'account' | 'mobile' | 'national_id' | 'number' | 'code' | 'date' | 'amount'

export interface NoteQuery {
  database: string
  sql: string
  /** {{slot}} in the SQL → what kind of value fills it. */
  slots: Record<string, SlotKind | string>
  about: string
}

/** What a person writes on the ticket page (sent as is; the console masks it). */
export interface NoteDraft {
  title: string
  body: string
  example?: string | null
  topic?: string | null
  query?: NoteQuery | null
}

export interface Note {
  id: number
  created_at: string
  created_by: string
  status: NoteStatus
  title: string
  /** Masked: account, card and phone numbers are tokens. */
  body: string
  example: string | null
  topic: string | null
  query_db: string | null
  query_sql: string | null
  query_slots: Record<string, string>
  query_about: string | null
  /** The lookup the agent calls, k<id>_<title>. */
  tool_name: string | null
  source_decision_id: number | null
  source_ticket: string | null
  decided_by: string | null
  decided_at: string | null
  decision_note: string | null
  retired_by: string | null
  retired_at: string | null
  retire_note: string | null
  uses: number
  last_used_at: string | null
}

export interface Knowledge {
  items: Note[]
  pending: number
  user: string
  can_approve: boolean
  /** How much of the system prompt the switched-on notes take; the newest approved go in first. */
  prompt: { max_chars: number; chars: number; in_prompt: number[]; left_out: number[] }
  slot_kinds: Record<string, { label: string; hint: string }>
  /** Metabase databases the desk already reads (suggestions). */
  databases: string[]
  max_query_rows: number
  rows_to_model: number
}

/** What the agent would do now on a ticket with a draft note (masked). Nothing was posted or sent. */
export interface TrialResult {
  proposed_action: Action
  final_action: Action
  confidence: number | null
  category: string | null
  reason: string | null
  evidence: string[]
  reply_text: string | null
  would_post: string | null
  guardrail_notes: string[]
  deferred: boolean
  tools_used: string[]
  draft_used: boolean
  knowledge_used: number[]
  prompt_version: string | null
  input_tokens: number
  output_tokens: number
  transcript: TranscriptMessage[]
  /** The demo console simulates trials: no model was called. */
  demo?: boolean
}

export interface Trial {
  id: number
  created_at: string
  created_by: string
  decision_id: number | null
  ticket_number: string
  draft: { title: string; body: string; example: string | null; topic: string | null; query?: NoteQuery } | null
  status: 'running' | 'done' | 'failed'
  finished_at: string | null
  error: string | null
  result: TrialResult | null
}
