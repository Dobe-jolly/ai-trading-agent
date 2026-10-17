/**
 * Who is signed in, which desks the service runs, and which one the top bar has selected.
 * The role comes from the console's sign-in; there is no switch for it in the page.
 */
import * as React from 'react'

import { useService } from './api/hooks'
import type { Service, ServiceDesk } from './api/types'
import { safeSession } from './utils'

export type DeskFilter = 'all' | string

export type DeskState = 'running' | 'paused' | 'stale' | 'neverRan'

interface PortalState {
  service: Service
  desks: ServiceDesk[]
  /** The desks the top bar's selection covers (all of them, or one). */
  selected: ServiceDesk[]
  desk: DeskFilter
  setDesk: (desk: DeskFilter) => void
  isAdmin: boolean
  deskByKey: (key: string) => ServiceDesk | undefined
  /** Updated time of the desk list, for "Updated hh:mm:ss". */
  updatedAt: number
}

const PortalContext = React.createContext<PortalState | null>(null)
const STORAGE_KEY = 'nmbz-portal:desk'

export function PortalProvider({ children, fallback }: { children: React.ReactNode; fallback: (state: { error: Error | null }) => React.ReactNode }) {
  const query = useService()
  const [desk, setDeskState] = React.useState<DeskFilter>(() => safeSession.get<DeskFilter>(STORAGE_KEY, 'all'))

  const setDesk = React.useCallback((next: DeskFilter) => {
    setDeskState(next)
    safeSession.set(STORAGE_KEY, next)
  }, [])

  const value = React.useMemo<PortalState | null>(() => {
    const service = query.data
    if (!service) return null
    const known = desk === 'all' || service.desks.some((d) => d.key === desk)
    const current = known ? desk : 'all'
    return {
      service,
      desks: service.desks,
      selected: current === 'all' ? service.desks : service.desks.filter((d) => d.key === current),
      desk: current,
      setDesk,
      isAdmin: service.role === 'admin',
      deskByKey: (key) => service.desks.find((d) => d.key === key),
      updatedAt: query.dataUpdatedAt,
    }
  }, [query.data, query.dataUpdatedAt, desk, setDesk])

  if (!value) return <>{fallback({ error: query.error })}</>
  return <PortalContext.Provider value={value}>{children}</PortalContext.Provider>
}

export function usePortal() {
  const ctx = React.useContext(PortalContext)
  if (!ctx) throw new Error('usePortal must be used inside <PortalProvider>')
  return ctx
}

/** Paused (on purpose, by a person) beats stale (should be running but has not reported). */
export function deskState(desk: ServiceDesk, servicePaused: boolean): DeskState {
  if (desk.kill_switch || servicePaused) return 'paused'
  if (desk.health.minutes_since_last_run == null) return 'neverRan'
  if (desk.health.stale) return 'stale'
  return 'running'
}

/** Is the mode a hold put on from the portal (more careful than the configured mode)? */
export const isHeld = (desk: Pick<ServiceDesk, 'configured_mode' | 'effective_mode'>) => desk.effective_mode !== desk.configured_mode
