import { useNavigate } from 'react-router-dom'
import { Pin, PinOff } from 'lucide-react'
import { useWorkspaceExit } from '../workspace/UnsavedWorkGuard'
import { useUsageToday } from '../../hooks/useUsageToday'
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
 * THE USAGE READ IS SHARED WITH THE WORKSPACE TOOLBAR, which draws the same figures compactly
 * while this panel is hidden. A null read hides the meter without collapsing the foot — the nav's
 * own structure must not depend on whether a fetch succeeded.
 */

interface Props {
  /** Shown only where the panel floats: docking it is meaningless when it is already docked. */
  pinned?: boolean
  onTogglePin?: () => void
  onNavigate?: () => void
  onItemFocus?: () => void
}

export default function NavPanel({
  pinned,
  onTogglePin,
  onNavigate,
  onItemFocus,
}: Props) {
  const navigate = useNavigate()
  const exit = useWorkspaceExit()
  const usage = useUsageToday()

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
          {/* The mark CARRIES the wordmark — a second copy beside it drew the brand twice, and
              at 248px the two overlapped into "Develope" on top of "Developer" with the pin
              glyph across them. It wraps to two lines here on its own. */}
          <BIALLogo />
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
      />

      <div className="mt-auto flex flex-col">
        {usage && <TokenRing usage={usage} />}
        <ProfileCluster />
      </div>
    </div>
  )
}
