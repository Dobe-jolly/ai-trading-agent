/** Times and numbers as the portal shows them. The server sends UTC ISO times; they are shown in local time. */
import * as React from 'react'

export const toDate = (iso: string | null | undefined) => (iso ? new Date(iso) : null)

export function minutesSince(iso: string | null | undefined, now: number): number | null {
  const d = toDate(iso)
  return d ? Math.max(0, (now - d.getTime()) / 60000) : null
}

/** "just now", "4 min", "2 h 10 min", "3 days" — a span of minutes. */
export function span(minutes: number | null | undefined): string {
  if (minutes == null) return '—'
  if (minutes < 1) return 'under a minute'
  if (minutes < 60) return `${Math.floor(minutes)} min`
  const h = Math.floor(minutes / 60)
  const m = Math.floor(minutes % 60)
  if (h < 48) return m ? `${h} h ${String(m).padStart(2, '0')} min` : `${h} h`
  return `${Math.floor(h / 24)} days`
}

export function ago(iso: string | null | undefined, now: number): string {
  const m = minutesSince(iso, now)
  if (m == null) return '—'
  return m < 1 ? 'just now' : `${span(m)} ago`
}

const clockFmt = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit' })
const dayFmt = new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: 'numeric', month: 'short' })
const fullFmt = new Intl.DateTimeFormat('en-GB', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })

export const clock = (iso: string | null | undefined) => (iso ? clockFmt.format(new Date(iso)) : '—')

const sameDay = (a: Date, b: Date) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()

/** "11:42" today, "Mon 12 Oct 18:44" otherwise. */
export function when(iso: string | null | undefined, now: number = Date.now()): string {
  const d = toDate(iso)
  if (!d) return '—'
  return sameDay(d, new Date(now)) ? clockFmt.format(d) : `${dayFmt.format(d)} ${clockFmt.format(d)}`
}

export const longDate = (now: number) => fullFmt.format(new Date(now))

export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  if (seconds < 60) return `${Math.round(seconds)} s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  if (m < 60) return s ? `${m} min ${s} s` : `${m} min`
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`
}

export const pct = (x: number | null | undefined) => (x == null ? '—' : `${Math.round(x * 100)}%`)

/** Reasons from the service start lower-case (they are written to follow "because"); capitalise them when they stand alone. */
export const sentence = (text: string | null | undefined) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : '')

export const count = (n: number | null | undefined) => (n == null ? '—' : n.toLocaleString('en-GB'))

/** The current time, ticking every 30 s so "N min ago" stays right without a refetch. */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = React.useState(() => Date.now())
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(id)
  }, [intervalMs])
  return now
}
