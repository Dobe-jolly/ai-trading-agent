/**
 * Talks to the console (python -m nmbz portal) on the same origin. The browser's sign-in (HTTP Basic) is sent
 * with every request; POSTs carry the header the console requires against cross-site requests.
 */

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, { credentials: 'same-origin', ...init, headers: { Accept: 'application/json', ...init?.headers } })
  } catch {
    throw new ApiError(0, 'Could not reach the console. Check that it is running and the network is up.')
  }
  const text = await res.text()
  let body: unknown = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = null
  }
  if (!res.ok) {
    const message = (body as { error?: string } | null)?.error ?? `The console answered ${res.status}.`
    throw new ApiError(res.status, message)
  }
  return body as T
}

export function apiGet<T>(path: string, params?: Record<string, string | number | undefined | null>): Promise<T> {
  const query = params
    ? Object.entries(params)
        .filter(([, v]) => v !== undefined && v !== null && v !== '')
        .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
        .join('&')
    : ''
  return request<T>(query ? `${path}?${query}` : path)
}

export function apiPost<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Portal-Request': '1' },
    body: JSON.stringify(body),
  })
}

/** A desk's API path: /api/d/<desk>/<rest>. */
export const deskPath = (desk: string, rest: string) => `/api/d/${encodeURIComponent(desk)}/${rest.replace(/^\//, '')}`
