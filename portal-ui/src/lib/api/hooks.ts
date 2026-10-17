/** Data from the console, cached and refreshed every 30 s (TanStack Query). */
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiGet, apiPost, deskPath } from './client'
import type { Action, Approval, Controls, Hold, Note, NoteDraft, Review, Service, ServiceDesk, Trial } from './types'

export const REFRESH_MS = 30_000

type Params = Record<string, string | number | undefined>

export function useService() {
  return useQuery({ queryKey: ['service'], queryFn: () => apiGet<Service>('/api/service'), refetchInterval: REFRESH_MS })
}

export function useDeskQuery<T>(desk: string | undefined, rest: string, params?: Params, enabled = true) {
  return useQuery({
    queryKey: ['desk', desk, rest, params ?? {}],
    queryFn: () => apiGet<T>(deskPath(desk!, rest), params),
    enabled: Boolean(desk) && enabled,
    refetchInterval: REFRESH_MS,
  })
}

export interface DeskResult<T> {
  desk: ServiceDesk
  data: T | undefined
  error: Error | null
}

/** The same read from several desks at once, e.g. every desk's approvals. */
export function useDeskQueries<T>(desks: ServiceDesk[], rest: string, params?: Params) {
  return useQueries({
    queries: desks.map((desk) => ({
      queryKey: ['desk', desk.key, rest, params ?? {}],
      queryFn: () => apiGet<T>(deskPath(desk.key, rest), params),
      refetchInterval: REFRESH_MS,
    })),
    combine: (results) => ({
      results: results.map((r, i) => ({ desk: desks[i], data: r.data, error: r.error }) as DeskResult<T>),
      isLoading: results.some((r) => r.isPending),
      errors: results.flatMap((r, i) => (r.error ? [{ desk: desks[i], error: r.error }] : [])),
      updatedAt: Math.max(0, ...results.map((r) => r.dataUpdatedAt)),
    }),
  })
}

/** After a change, everything about that desk and the desk list is read again. */
function useInvalidate() {
  const qc = useQueryClient()
  return (desk?: string) =>
    Promise.all([
      qc.invalidateQueries({ queryKey: ['service'] }),
      desk ? qc.invalidateQueries({ queryKey: ['desk', desk] }) : qc.invalidateQueries({ queryKey: ['desk'] }),
    ])
}

export function useDecideApproval() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (v: { desk: string; id: number; verdict: 'approve' | 'reject'; reply?: string; comment?: string }) =>
      apiPost<Approval>(deskPath(v.desk, `approvals/${v.id}`), { verdict: v.verdict, reply: v.reply, comment: v.comment }),
    onSettled: (_d, _e, v) => invalidate(v.desk),
  })
}

export function useReview() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (v: { desk: string; decisionId: number; verdict: 'correct' | 'incorrect'; expected?: Action | null; comment?: string }) =>
      apiPost<Review>(deskPath(v.desk, `decisions/${v.decisionId}/reviews`), {
        verdict: v.verdict,
        expected_action: v.expected ?? null,
        comment: v.comment,
      }),
    onSettled: (_d, _e, v) => invalidate(v.desk),
  })
}

export function useDeskControls() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (v: { desk: string; kill_switch?: boolean; mode_override?: Hold | null; note?: string }) => {
      const { desk, ...body } = v
      return apiPost<Controls>(deskPath(desk, 'controls'), body)
    },
    onSettled: (_d, _e, v) => invalidate(v.desk),
  })
}

export function usePauseService() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (v: { pause: boolean; note?: string }) => apiPost<Service>('/api/service/controls', v),
    onSettled: () => invalidate(),
  })
}

export function useRetryEmails() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (v: { desk: string }) => apiPost<{ requeued: number }>(deskPath(v.desk, 'notifications/retry'), {}),
    onSettled: (_d, _e, v) => invalidate(v.desk),
  })
}

// ---- what staff teach a desk -------------------------------------------------------------------------------

/** Propose a note (anyone signed in). It is used on future tickets once an Admin who did not write it approves. */
export function useTeach() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (v: { desk: string; decisionId?: number; draft: NoteDraft }) =>
      apiPost<Note>(deskPath(v.desk, 'knowledge'), { ...v.draft, decision_id: v.decisionId }),
    onSettled: (_d, _e, v) => invalidate(v.desk),
  })
}

export type NoteVerdict = 'approve' | 'reject' | 'retire' | 'withdraw'

export function useDecideNote() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: (v: { desk: string; id: number; verdict: NoteVerdict; note?: string }) =>
      apiPost<Note>(deskPath(v.desk, `knowledge/${v.id}`), { verdict: v.verdict, note: v.note }),
    onSettled: (_d, _e, v) => invalidate(v.desk),
  })
}

/** Re-check one ticket with a draft note: the desk's agent runs in shadow and nothing is posted or sent. */
export function useTryNote() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { desk: string; decisionId: number; draft: NoteDraft }) =>
      apiPost<Trial>(deskPath(v.desk, 'knowledge/trials'), { decision_id: v.decisionId, draft: v.draft }),
    onSuccess: (trial, v) => {
      qc.setQueryData(['trial', v.desk, trial.id], trial)
      return qc.invalidateQueries({ queryKey: ['desk', v.desk, 'knowledge/trials'] })
    },
  })
}

/** One trial, read every 2 s while it runs. */
export function useTrial(desk: string, id: number | null) {
  return useQuery({
    queryKey: ['trial', desk, id],
    queryFn: () => apiGet<Trial>(deskPath(desk, `knowledge/trials/${id}`)),
    enabled: id != null,
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 2_000 : false),
  })
}
