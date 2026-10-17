import { HeadContent, Link, Outlet, createRootRouteWithContext } from '@tanstack/react-router'
import type { QueryClient } from '@tanstack/react-query'
import { PlugZap } from 'lucide-react'

import { Loading } from '@/components/data-state'
import { AppSidebar } from '@/components/shell/app-sidebar'
import { GoTo } from '@/components/shell/go-to'
import { TopBar } from '@/components/shell/top-bar'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ApiError } from '@/lib/api/client'
import { PortalProvider } from '@/lib/portal'

export const Route = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  head: () => ({ meta: [{ title: 'Agent Supervision · NMBZ ICT Helpdesk' }] }),
  component: RootLayout,
  notFoundComponent: NotFound,
})

function RootLayout() {
  return (
    <TooltipProvider delayDuration={200}>
      <HeadContent />
      <PortalProvider fallback={({ error }) => <ConsoleUnavailable error={error} />}>
        <div className="flex min-h-screen flex-col bg-background lg:flex-row">
          <AppSidebar />
          <div className="flex min-w-0 flex-1 flex-col">
            <TopBar />
            <main className="mx-auto flex w-full max-w-[1320px] flex-col gap-5 px-4 pt-6 pb-14 sm:px-8 sm:pt-7">
              <Outlet />
            </main>
          </div>
        </div>
        <GoTo />
      </PortalProvider>
    </TooltipProvider>
  )
}

/** Before the desk list arrives: loading, or why it could not be read. */
function ConsoleUnavailable({ error }: { error: Error | null }) {
  if (!error) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <Loading label="Connecting to the console…" />
      </div>
    )
  }
  const status = error instanceof ApiError ? error.status : 0
  const signIn = status === 401
  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6">
      <div className="flex max-w-[520px] flex-col items-start gap-3 rounded-xl border border-border bg-card p-6">
        <span aria-hidden className="flex size-10 items-center justify-center rounded-lg bg-bad-bg text-bad-fg">
          <PlugZap className="size-5" />
        </span>
        <h1 className="m-0 text-xl font-semibold">{signIn ? 'Sign in to the console' : 'The console is not answering'}</h1>
        <p className="m-0 text-[13px] text-secondary-foreground">
          {signIn
            ? 'The console needs your user name and password. Reload the page to get the sign-in prompt again.'
            : 'The supervision portal reads everything from the agents’ console service (python -m nmbz portal). Check that it is running and reachable, then reload.'}
        </p>
        <p className="m-0 font-mono text-xs text-muted-foreground">{error.message}</p>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="mt-1 inline-flex h-10 items-center rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground hover:bg-primary-strong"
        >
          Reload
        </button>
      </div>
    </div>
  )
}

function NotFound() {
  return (
    <div className="flex flex-col items-start gap-2 py-12">
      <h1 className="m-0 text-2xl font-semibold">Page not found</h1>
      <p className="m-0 text-muted-foreground">That address is not part of the supervision portal.</p>
      <Link to="/" className="font-medium">
        Back to Overview
      </Link>
    </div>
  )
}
