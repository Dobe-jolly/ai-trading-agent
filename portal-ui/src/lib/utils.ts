import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** Read/write sessionStorage without ever throwing (private windows, blocked storage). */
export const safeSession = {
  get<T>(key: string, fallback: T): T {
    try {
      const raw = window.sessionStorage.getItem(key)
      return raw ? (JSON.parse(raw) as T) : fallback
    } catch {
      return fallback
    }
  },
  set(key: string, value: unknown) {
    try {
      window.sessionStorage.setItem(key, JSON.stringify(value))
    } catch {
      /* storage unavailable — state stays in memory */
    }
  },
}
