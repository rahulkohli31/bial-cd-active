import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { motion } from 'motion/react'
import { LayoutGrid, Users, Store, Database, ShieldCheck } from 'lucide-react'
import { useWorkspaceExit } from '../workspace/UnsavedWorkGuard'
import { getStoredUser, isAuthenticated } from '../../utils/auth'
import { fetchAppStatusCounts } from '../../utils/appRegistryApi'
import { projectsListHref } from '../../utils/projectsListMemory'
import WaitingCountBadge from '../admin/WaitingCountBadge'
import { highlightTransition } from '../../lib/motion'

/**
 * The five destinations, and the one thing about them that is not cosmetic: EVERY ONE OF THEM
 * LEAVES THE WORKSPACE THROUGH ITS GUARD.
 *
 * The header this replaces wired `useWorkspaceExit()` onto the logo AND onto each of its links,
 * separately. Wiring it once here and missing one would change behaviour by omission — the
 * unsaved-work dialog, the save offer and the failed-save refusal would simply stop happening on
 * that route, silently, on a plan whose scope explicitly excludes touching that path.
 * `AppShell.test.tsx` asserts it per destination for that reason.
 *
 * THE LIST LINK CARRIES THE LIST BACK. `projectsListHref()` is read at CLICK time, not memoised
 * at render, so a search typed a moment ago on the list is what this lands on rather than a
 * page-one reset nobody asked for.
 *
 * THE ADMIN ENTRY IS GATED ON `isAdmin` AND SO IS ITS COUNT. The count route is superadmin-only
 * server-side, so asking for anyone else spends a request to earn a 403 in every citizen's
 * console. A failed read leaves the count null and draws no badge: a badge that guessed would be
 * worse than none on the one surface whose job is to be trusted.
 */

export interface NavDestination {
  label: string
  to: string
  Icon: typeof LayoutGrid
}

export const NAV_DESTINATIONS: readonly NavDestination[] = [
  { label: 'My Applications', to: '/projects', Icon: LayoutGrid },
  { label: 'Shared Applications', to: '/shared-applications', Icon: Users },
  { label: 'App Marketplace', to: '/marketplace', Icon: Store },
  { label: 'Integrations', to: '/integrations', Icon: Database },
]

export const ADMIN_DESTINATION: NavDestination = { label: 'Admin', to: '/admin', Icon: ShieldCheck }

interface Props {
  /** Called after a destination is chosen, so a floating panel can leave behind the new page. */
  onNavigate?: () => void
  /** Opens the panel when focus lands on an item — a panel that vanishes under the keyboard is
   *  a trap. Held open by the panel itself for as long as focus is inside it. */
  onItemFocus?: () => void
}

export default function NavItems({ onNavigate, onItemFocus }: Props) {
  const navigate = useNavigate()
  const exit = useWorkspaceExit()
  const { pathname } = useLocation()
  const user = getStoredUser()
  const isAdmin = user?.isAdmin === true
  const [waiting, setWaiting] = useState<number | null>(null)

  useEffect(() => {
    if (!isAdmin || !isAuthenticated()) return undefined
    let active = true
    const read = () => {
      void fetchAppStatusCounts()
        .then((counts) => { if (active) setWaiting(counts.pending) })
        .catch(() => { if (active) setWaiting(null) })
    }
    read()
    // RE-READ WHEN THE TAB COMES BACK rather than polling. The queue changes underneath this
    // badge — an administrator approves, a citizen withdraws, a pipeline routes a drifted
    // version — and a fetch-once badge sits on a number the admin panel has already corrected.
    // Nothing here is urgent enough to wake an idle tab for.
    const refresh = () => { if (document.visibilityState === 'visible') read() }
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      active = false
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [isAdmin])

  const destinations = isAdmin ? [...NAV_DESTINATIONS, ADMIN_DESTINATION] : NAV_DESTINATIONS

  const go = (to: string) => {
    const href = to === '/projects' ? projectsListHref() : to
    exit(() => {
      navigate(href)
      onNavigate?.()
    })
  }

  return (
    <nav aria-label="Primary" className="flex flex-col gap-0.5 px-2.5 py-1">
      {destinations.map(({ label, to, Icon }) => {
        const active = pathname === to || pathname.startsWith(`${to}/`)
        return (
          <button
            key={to}
            type="button"
            onClick={() => go(to)}
            onFocus={onItemFocus}
            aria-current={active ? 'page' : undefined}
            data-testid={`nav-${to.slice(1)}`}
            // FOCUS MUST NOT LOOK LIKE ACTIVE. The active item carries a tinted ground; focus
            // carries a ring. Without the distinction a keyboard user on a non-active item sees
            // two highlighted rows and cannot tell which one Enter will open.
            className={`relative flex items-center gap-2.5 rounded-lg px-2.5 py-2.5 text-left text-[13.5px] transition-colors outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 ${
              active ? 'font-semibold text-primary' : 'font-medium text-neutral hover:text-primary-900'
            }`}
          >
            {active && (
              // ONE ELEMENT WITH A SHARED `layoutId`, NOT A CLASS PER ITEM: the tinted pill
              // travels to the newly active row instead of blinking off one and on at another.
              <motion.span
                layoutId="nav-active-pill"
                transition={highlightTransition}
                aria-hidden="true"
                className="absolute inset-0 rounded-lg bg-primary/10 ring-1 ring-inset ring-primary/30"
              />
            )}
            <Icon size={17} className="relative shrink-0" />
            <span className="relative">{label}</span>
            {to === ADMIN_DESTINATION.to && (
              <span className="relative ml-auto">
                <WaitingCountBadge count={waiting} where="nav" />
              </span>
            )}
          </button>
        )
      })}
    </nav>
  )
}
