/**
 * Comparing the agent's reply with the standard wording for the same outcome, sentence by sentence.
 * A sentence is "added" when no sentence of the standard wording says the same thing (ignoring case and spacing).
 */

const split = (text: string) =>
  text
    .split(/(?<=[.!?])\s+(?=[A-Z<"(])/)
    .map((s) => s.trim())
    .filter(Boolean)

const norm = (s: string) => s.toLowerCase().replace(/\s+/g, ' ').replace(/[.!?]+$/, '').trim()

export interface ReplyPart {
  text: string
  added: boolean
}

export function compareReply(reply: string, standard: string | null | undefined): { parts: ReplyPart[]; same: boolean } {
  const sentences = split(reply)
  if (!standard) return { parts: sentences.map((text) => ({ text, added: false })), same: false }
  const known = new Set(split(standard).map(norm))
  const parts = sentences.map((text) => ({ text, added: !known.has(norm(text)) }))
  return { parts, same: norm(reply) === norm(standard) }
}

export const sentences = split
