import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { motion } from 'motion/react'
import { LayoutGrid, Users, Store, Database, ShieldCheck } from 'lucide-react'
import { getStoredUser, isAuthenticated } from '../../utils/auth'
import { fetchAppStatusCounts } from '../../utils/appRegistryApi'
import { projectsListHref } from '../../utils/projectsListMemory'
import WaitingCountBadge from '../admin/WaitingCountBadge'
import { highlightTransition } from '../../lib/motion'

/**
 * The five destinations.
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
  /**
   * ADDRESSES THAT BELONG HERE WITHOUT SITTING UNDER THIS PATH. Opening one row of a list can
   * land a reader somewhere whose address shares no prefix with the list's — a shared
   * application is at `/shared/{id}` while its list is `/shared-applications` — and a plain
   * prefix test then marks nothing at all, so the one thing the navigation is for, saying where
   * you are, is exactly absent on the screens a person reached by using it.
   */
  owns?: readonly string[]
}

export const NAV_DESTINATIONS: readonly NavDestination[] = [
  { label: 'My Applications', to: '/projects', Icon: LayoutGrid, owns: ['/chat/'] },
  { label: 'Shared Applications', to: '/shared-applications', Icon: Users, owns: ['/shared/'] },
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
  /** Icons only. The label still renders — it is what `title` and the accessible name read —
   *  but it is folded to zero width so the row centres on its icon. */
  collapsed?: boolean
}

export default function NavItems({ onNavigate, onItemFocus, collapsed = false }: Props) {
  const navigate = useNavigate()
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
    navigate(href)
    onNavigate?.()
  }

  return (
    <nav aria-label="Primary" className="flex flex-col gap-0.5 px-2.5 py-1">
      {destinations.map(({ label, to, Icon, owns }) => {
        const active =
          pathname === to ||
          pathname.startsWith(`${to}/`) ||
          (owns?.some((prefix) => pathname.startsWith(prefix)) ?? false)
        return (
          <button
            key={to}
            type="button"
            onClick={() => go(to)}
            onFocus={onItemFocus}
            aria-current={active ? 'page' : undefined}
            data-testid={`nav-${to.slice(1)}`}
            // The rail's only label. A collapsed row is an icon, and an icon alone is a guess
            // for anyone who has not already learned this navigation.
            title={collapsed ? label : undefined}
            // FOCUS MUST NOT LOOK LIKE ACTIVE. The active item carries a tinted ground; focus
            // carries a ring. Without the distinction a keyboard user on a non-active item sees
            // two highlighted rows and cannot tell which one Enter will open.
            className={`relative flex items-center gap-2.5 rounded-lg py-2.5 text-left text-[13.5px] transition-colors outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 ${
              collapsed ? 'justify-center px-0' : 'px-2.5'
            } ${
              active ? 'font-semibold text-primary' : 'font-medium text-neutral hover:text-primary-900'
            }`}
          >
            {active && (
              // ONE ELEMENT WITH A SHARED `layoutId`, NOT A CLASS PER ITEM: the tinted pill
              // travels to the newly active row instead of blinking off one and on at another.
              //
              // THE SHARED LAYOUT IS DROPPED AT RAIL WIDTH, and that is a correctness fix rather
              // than a taste one. A `layoutId` element is measured against its ancestors, and in
              // the rail its ancestor is the panel ANIMATING ITS OWN WIDTH — so the pill was
              // positioned from a box that no longer existed by the time it painted, and landed
              // up and to the left of the icon it is supposed to sit behind. Without the shared
              // id it is an ordinary absolutely-positioned span on `inset-0`, which is exactly
              // the icon's own box. The travel is worth having between labelled rows; it is worth
              // nothing in a 56px column where every row is the same square.
              <motion.span
                layoutId={collapsed ? undefined : 'nav-active-pill'}
                transition={highlightTransition}
                aria-hidden="true"
                className="absolute inset-0 rounded-lg bg-primary/10 ring-1 ring-inset ring-primary/30"
              />
            )}
            <Icon size={18} className="relative shrink-0" />
            {/* FOLDED, NOT UNMOUNTED. Removing the text would take the button's accessible name
                with it, so a screen reader on the rail would read five unnamed buttons. It is
                clipped to zero width instead, and the name survives the collapse. */}
            <span
              className={`relative overflow-hidden whitespace-nowrap transition-[opacity,max-width] duration-200 ${
                collapsed ? 'max-w-0 opacity-0' : 'max-w-[160px] opacity-100'
              }`}
            >
              {label}
            </span>
            {to === ADMIN_DESTINATION.to && (
              // THE COUNT SURVIVES THE COLLAPSE, because it is the one thing in this navigation
              // that is not a destination but a summons — an administrator with applications
              // waiting must not lose the signal by leaving the pointer elsewhere. At rail width
              // the number has nowhere to sit, so it becomes a dot pinned to the icon.
              <span
                className={
                  collapsed
                    ? 'pointer-events-none absolute right-1.5 top-1.5'
                    : 'relative ml-auto'
                }
              >
                <WaitingCountBadge count={waiting} where="nav" compact={collapsed} />
              </span>
            )}
          </button>
        )
      })}
    </nav>
  )
}
