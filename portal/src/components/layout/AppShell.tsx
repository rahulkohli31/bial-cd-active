import { useEffect, useState, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import IntegrationsDialog from '../connectors/IntegrationsDialog'
import { rememberProjectsSearch } from '../../utils/projectsListMemory'
import NavPanel from './NavPanel'
import NavReveal, { NavMenuButton, useStackedViewport } from './NavReveal'

/**
 * The frame every route renders inside — the navigation on the left, the page beside it.
 *
 * THE SHELL IS A LAYOUT, NOT A PER-PAGE IMPORT. Four pages used to draw their own header, which
 * is how a product ends up with chrome that differs by screen. Pages draw pages now.
 *
 * TWO POSITIONS, ONE WIDTH. Docked into the row on the list routes; absent on the routes inside
 * an application, where `NavReveal` brings it back over the content on demand. Which routes are
 * which is decided HERE, by address, because the shell is the only thing that sees every route —
 * a page deciding for itself is how two of them eventually disagree.
 *
 * NOTHING BECOMES UNREACHABLE AT 360px, which is this product's standing promise. Below the
 * stacking threshold there is no room for a docked column beside the work, so every route — list
 * routes included — reaches the navigation as a drawer, and the shell draws the button that opens
 * it. A narrow screen loses the permanence, never the destinations.
 *
 * INTEGRATIONS IS AN INTERIM DOOR, DELIBERATELY. The connector dialog has no route of its own
 * yet, and both its existing doors — the profile menu and the workspace rail — are being removed.
 * Hosting it here keeps the capability reachable from its navigation entry for the commit range
 * where the page does not exist, rather than shipping an entry that leads nowhere. It goes when
 * the page lands.
 */

/** The addresses inside an application: the navigation is not on screen on these. */
function isApplicationRoute(pathname: string): boolean {
  return pathname.startsWith('/projects/') || pathname.startsWith('/chat/')
}

export default function AppShell({ children }: { children: ReactNode }) {
  const { pathname, search } = useLocation()
  const stacked = useStackedViewport()

  // THE HALF THE ADDRESS BAR CANNOT DO BY ITSELF, and the shell is now the one place positioned
  // to do it: mounted on every route, so it sees every time the address reads the list and can
  // remember what it carried. The list entry and the workspace's back control read it back at
  // click time, which is what makes leaving and returning land on the same search and page
  // rather than on a page-one reset nobody asked for. Without a writer, `projectsListHref()`
  // silently degrades to a bare `/projects` and the whole module becomes dead.
  useEffect(() => {
    if (pathname === '/projects') rememberProjectsSearch(search)
  }, [pathname, search])
  const [integrationsOpen, setIntegrationsOpen] = useState(false)
  const openIntegrations = () => setIntegrationsOpen(true)

  const dialog = integrationsOpen ? (
    <IntegrationsDialog onClose={() => setIntegrationsOpen(false)} />
  ) : null

  // Inside an application the workspace toolbar carries the menu button, so the shell adds no
  // bar of its own there — the whole point of that layout is that the platform's chrome gets out
  // of the application's way.
  if (isApplicationRoute(pathname)) {
    return (
      <NavReveal hideable onOpenIntegrations={openIntegrations}>
        {children}
        {dialog}
      </NavReveal>
    )
  }

  if (stacked) {
    return (
      <NavReveal hideable onOpenIntegrations={openIntegrations}>
        <div className="flex h-screen flex-col overflow-hidden bg-bial-bg font-manrope">
          <div className="flex h-12 shrink-0 items-center gap-2 border-b border-bial-border bg-white px-3">
            <NavMenuButton />
          </div>
          <div className="min-w-0 flex-1 overflow-y-auto">{children}</div>
        </div>
        {dialog}
      </NavReveal>
    )
  }

  return (
    <div className="flex h-screen overflow-hidden bg-bial-bg font-manrope">
      <aside className="h-full shrink-0 border-r border-bial-border" data-testid="nav-docked">
        <NavPanel onOpenIntegrations={openIntegrations} />
      </aside>
      <div className="min-w-0 flex-1 overflow-y-auto">{children}</div>
      {dialog}
    </div>
  )
}
