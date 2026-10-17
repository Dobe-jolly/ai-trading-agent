/** Words and small helpers for what staff teach a desk (notes, their lookups and trials). */
import type { NoteStatus, SlotKind } from './api/types'
import type { StatusKey } from './status'

export const NOTE_STATUS: Record<NoteStatus, { status: StatusKey; label: string }> = {
  proposed: { status: 'awaitingApproval', label: 'Waiting for an Admin' },
  active: { status: 'posted', label: 'In use' },
  rejected: { status: 'rejected', label: 'Turned down' },
  retired: { status: 'noAction', label: 'Switched off' },
  withdrawn: { status: 'noAction', label: 'Withdrawn' },
}

export const noteStatus = (s: string | null | undefined) => NOTE_STATUS[s as NoteStatus] ?? { status: 'skipped' as StatusKey, label: s ?? 'Unknown' }

export const noteId = (id: number) => `K${id}`

/** The {{slots}} a query uses, in order of first use. */
export function slotsIn(sql: string): string[] {
  const seen: string[] = []
  for (const m of sql.matchAll(/\{\{\s*([A-Za-z][A-Za-z0-9_]{0,30})\s*\}\}/g)) {
    if (!seen.includes(m[1])) seen.push(m[1])
  }
  return seen
}

/** A first guess at what a slot holds, from its name; the person can change it. */
export function guessKind(name: string): SlotKind {
  const n = name.toLowerCase()
  if (/acc|acct|account/.test(n)) return 'account'
  if (/mobile|phone|msisdn|cell/.test(n)) return 'mobile'
  if (/national|nid|id_?no/.test(n)) return 'national_id'
  if (/date|day|from|to$|since|until/.test(n)) return 'date'
  if (/amount|amt|value/.test(n)) return 'amount'
  if (/card|pan/.test(n)) return 'number'
  return 'code'
}

/** The same lengths the console checks, so the form can say what's missing before sending. */
export const LIMITS = { title: [4, 120], body: [20, 4000], example: [0, 6000], about: [40, 600], sql: [1, 4000] } as const
