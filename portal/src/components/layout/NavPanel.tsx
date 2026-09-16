import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Pin, PinOff } from 'lucide-react'
import { useWorkspaceExit } from '../workspace/UnsavedWorkGuard'
import { isAuthenticated } from '../../utils/auth'
import { fetchUsageToday, onUsageChanged } from '../../utils/usage'
import type { UsageToday } from '../../utils/usage'
import { projectsListHref } from '../../utils/projectsListMemory'
import BIALLogo from '../BIALLogo'
import NavItems from './NavItems'
import ProfileCluster from './ProfileCluster'
import TokenRing from './TokenRing'
import { NAV_WIDTH_PX } from '../../lib/motion'

/**
 * The navigation panel — ONE WIDTH, TWO POSITIONS. It is 248px docked into the layout on the list
 * routes, or floating over the content inside an application. There is no icon rail: a third,
 * narrower form is a third set of states to draw, test and keep honest, for a saving the hidden
 * state already beats.
 *
 * THE LOGO RUNS THE WORKSPACE-EXIT ROUTINE BEFORE NAVIGATING, exactly as the header's did, and
 * for the same reason: a single-page navigation is not an unload, so `beforeunload` cannot cover
 * it and leaving by the brand link used to discard unsaved work in silence. It lands on the
 * remembered list rather than page one.
 *
 * THE USAGE READ LIVES HERE because the ring does. It is gated on `isAuthenticated` so it never
 * fires during logout, and a null read hides the meter without collapsing the foot — the nav's
 * own structure must not depend on whether a fetch succeeded.
 */

interface Props {
  /** Shown only where the panel floats: docking it is meaningless when it is already docked. */
  pinned?: boolean
  onTogglePin?: () => void
  onNavigate?: () => void
  onItemFocus?: () => void
  onOpenIntegrations: () => void
}

export default function NavPanel({
  pinned,
  onTogglePin,
  onNavigate,
  onItemFocus,
  onOpenIntegrations,
}: Props) {
  const navigate = useNavigate()
  const exit = useWorkspaceExit()
  const [usage, setUsage] = useState<UsageToday | null>(null)

  useEffect(() => {
    let active = true
    const load = async () => {
      if (!isAuthenticated()) {
        if (active) setUsage(null)
        return
      }
      const data = await fetchUsageToday()
      if (active) setUsage(data)
    }
    void load()
    const off = onUsageChanged(() => void load())
    return () => {
      active = false
      off()
    }
  }, [])

  return (
    <div
      className="flex h-full flex-col overflow-y-auto bg-white"
      style={{ width: NAV_WIDTH_PX }}
      data-testid="nav-panel"
    >
      <div className="flex items-center gap-2.5 px-4 pb-4 pt-[18px]">
        <button
          type="button"
          onClick={() => exit(() => navigate(projectsListHref()))}
          className="flex items-center gap-2.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 rounded-lg"
          aria-label="BIAL Citizen Developer — go to My Applications"
        >
          <BIALLogo />
          <span className="text-[15px] font-extrabold leading-tight tracking-[-0.2px] text-primary">
            BIAL Citizen
            <br />
            Developer
          </span>
        </button>
        {onTogglePin && (
          <button
            type="button"
            onClick={onTogglePin}
            data-testid="nav-pin"
            aria-pressed={pinned}
            title={pinned ? 'Unpin the navigation' : 'Pin the navigation open'}
            className="ml-auto shrink-0 rounded-lg p-1.5 text-neutral transition hover:bg-surface-muted hover:text-primary-900 outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            {pinned ? <PinOff size={15} /> : <Pin size={15} />}
          </button>
        )}
      </div>

      <NavItems
        onNavigate={onNavigate}
        onItemFocus={onItemFocus}
        onOpenIntegrations={onOpenIntegrations}
      />

      <div className="mt-auto flex flex-col">
        {usage && <TokenRing usage={usage} />}
        <ProfileCluster />
      </div>
    </div>
  )
}
