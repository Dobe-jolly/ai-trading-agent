/**
 * Reading a decision's transcript: the ticket as the agent saw it (first message) and each lookup it ran
 * (tool_use paired with its tool_result). Everything in it is already masked by the service.
 */
import type { ContentBlock, TranscriptMessage } from './api/types'

export interface TicketHeader {
  number?: string
  topic?: string
  branch?: string
  subject?: string
  opened?: string
  /** The whole first message, as the agent read it. */
  text: string
}

const textOf = (m: TranscriptMessage | undefined): string =>
  !m ? '' : typeof m.content === 'string' ? m.content : m.content.map((b) => ('text' in b && typeof b.text === 'string' ? b.text : '')).join('\n')

const field = (text: string, re: RegExp) => re.exec(text)?.[1]?.trim() || undefined

export function ticketHeader(transcript: TranscriptMessage[] | undefined): TicketHeader {
  const text = textOf(transcript?.[0])
  return {
    text,
    number: field(text, /Ticket #(\S+)/),
    topic: field(text, /(?:Help topic|Filed under): (.+?)(?:\s{2,}|\n|$)/),
    branch: field(text, /Raised by: (.+?)(?:\s{2,}|\n|$)/),
    subject: field(text, /^Subject: (.+)$/m),
    opened: field(text, /Opened: (.+?)(?:\s{2,}|\n|$)/),
  }
}

/** The ticket owner's first message from the thread, for a short quote. */
export function firstMessage(text: string): string | undefined {
  const thread = text.split(/Thread, oldest first:\n/)[1]
  if (!thread) return undefined
  const body = thread.split(/\n--- /)[0].replace(/^--- [^\n]*\n/, '').trim()
  return body.split(/\n\n(?=[A-Z][a-z]+ )/)[0].trim() || undefined
}

export type LookupStatus = 'found' | 'noMatch' | 'lookupFailed'

export interface Lookup {
  id: string
  name: string
  input: Record<string, unknown>
  /** The masked text that came back; undefined if the transcript has no result for it. */
  result?: string
  status: LookupStatus
  /** One line on what came back. */
  summary: string
}

function parse(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return undefined
  }
}

/**
 * No matching record: the lookup worked and found nothing (found: 0 / false, exists: false, or every list in the
 * answer empty). Anything that returned a status, a record or a figure counts as found. A tool error is a failed
 * lookup, never "nothing there".
 */
export function lookupStatus(content: string | undefined, isError: boolean | undefined): LookupStatus {
  if (isError) return 'lookupFailed'
  const data = parse(content ?? '')
  if (!data || typeof data !== 'object' || Array.isArray(data)) return Array.isArray(data) && data.length === 0 ? 'noMatch' : 'found'
  const o = data as Record<string, unknown>
  if (o.found === 0 || o.found === false || o.exists === false || o.closed_tickets_found === 0) return 'noMatch'
  const lists = Object.values(o).filter(Array.isArray) as unknown[][]
  const hasOther = Object.entries(o).some(
    ([, v]) => !Array.isArray(v) && v !== null && typeof v !== 'object' && v !== 0 && v !== '' && v !== false,
  )
  if (lists.length && lists.every((l) => l.length === 0) && !hasOther) return 'noMatch'
  return 'found'
}

const label = (key: string) => {
  const words = key.replaceAll('_', ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** A result object in a line a person can read: its plain fields first, then how many items each list holds. */
function describe(o: Record<string, unknown>): string {
  const parts: string[] = []
  for (const [k, v] of Object.entries(o)) {
    if (v == null || v === '') continue
    if (typeof v === 'boolean') parts.push(`${label(k)}: ${v ? 'yes' : 'no'}`)
    else if (typeof v === 'string' || typeof v === 'number') parts.push(`${label(k)}: ${String(v).length > 60 ? `${String(v).slice(0, 60)}…` : v}`)
  }
  for (const [k, v] of Object.entries(o)) {
    if (Array.isArray(v)) parts.push(`${label(k)}: ${v.length} ${v.length === 1 ? 'item' : 'items'}`)
    else if (v && typeof v === 'object') {
      const n = Object.keys(v).length
      parts.push(`${label(k)}: ${n} ${n === 1 ? 'field' : 'fields'}`)
    }
  }
  const line = parts.slice(0, 5).join(' · ')
  return parts.length > 5 ? `${line} · …` : line || 'Empty result.'
}

function summarise(content: string | undefined, status: LookupStatus): string {
  if (content == null) return 'No result recorded.'
  if (status === 'lookupFailed') return content.length > 220 ? `${content.slice(0, 220)}…` : content
  const data = parse(content)
  if (data && typeof data === 'object' && !Array.isArray(data)) {
    const o = data as Record<string, unknown>
    const s = o.summary
    if (typeof s === 'string') return s
    if (s && typeof s === 'object' && typeof (s as Record<string, unknown>).status === 'string') return `Status: ${(s as Record<string, string>).status.replaceAll('_', ' ')}`
    if (typeof o.found === 'number') return `${o.found} found`
    if (typeof o.status === 'string') return `Status: ${o.status.replaceAll('_', ' ')}`
    if (typeof o.meaning === 'string') return o.meaning
    return describe(o)
  }
  return content.length > 160 ? `${content.slice(0, 160)}…` : content
}

export function lookups(transcript: TranscriptMessage[] | undefined): Lookup[] {
  const results = new Map<string, { content: string; is_error?: boolean }>()
  const uses: { id: string; name: string; input: Record<string, unknown> }[] = []
  for (const m of transcript ?? []) {
    if (typeof m.content === 'string') continue
    for (const b of m.content as ContentBlock[]) {
      if (b.type === 'tool_result') {
        const r = b as Extract<ContentBlock, { type: 'tool_result' }>
        results.set(r.tool_use_id, { content: typeof r.content === 'string' ? r.content : JSON.stringify(r.content), is_error: r.is_error })
      }
      if (b.type === 'tool_use' && m.role === 'assistant') {
        const u = b as Extract<ContentBlock, { type: 'tool_use' }>
        if (u.name !== 'submit_decision') uses.push({ id: u.id, name: u.name, input: u.input ?? {} })
      }
    }
  }
  return uses.map((u) => {
    const r = results.get(u.id)
    const status = lookupStatus(r?.content, r?.is_error)
    return { ...u, result: r?.content, status, summary: summarise(r?.content, status) }
  })
}

/** Pretty JSON when the text is JSON, the text as it is otherwise. */
export function pretty(text: string | undefined): string {
  if (text == null) return '—'
  const data = parse(text)
  return data === undefined ? text : JSON.stringify(data, null, 2)
}

export const argsLine = (input: Record<string, unknown>) =>
  Object.values(input)
    .map((v) => (typeof v === 'string' ? v : JSON.stringify(v)))
    .join(', ')
