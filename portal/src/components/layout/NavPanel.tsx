import { useId } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, LayoutGroup } from 'motion/react'
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import { useWorkspaceExit } from '../workspace/UnsavedWorkGuard'
import { useUsageToday } from '../../hooks/useUsageToday'
import { projectsListHref } from '../../utils/projectsListMemory'
import BIALLogo from '../BIALLogo'
import NavItems from './NavItems'
import ProfileCluster from './ProfileCluster'
import TokenRing from './TokenRing'
import { NAV_PANEL_PX, NAV_RAIL_PX } from './useNavRail'

/**
 * The navigation panel — ONE COLUMN, TWO WIDTHS. It rests as a 56px rail of icons and grows to
 * 248px of labelled destinations when the pointer arrives or the pin is set. There is no third
 * form: a separate drawer would be a third set of states to draw, test and keep honest.
 *
 * THE WIDTH IS ANIMATED, THE CONTENT IS NOT SWAPPED. Both states render the same elements — the
 * labels fold to zero width rather than unmounting — so the collapse cannot change what a screen
 * reader finds, and React has nothing to reconcile across the transition.
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
  /** Icons only, at rail width. */
  collapsed?: boolean
  pinned?: boolean
  onTogglePin?: () => void
  onNavigate?: () => void
  onItemFocus?: () => void
  /** Told when a menu this panel owns opens, so the rail does not close under it. */
  onMenuOpenChange?: (open: boolean) => void
}

export default function NavPanel({
  collapsed = false,
  pinned = false,
  onTogglePin,
  onNavigate,
  onItemFocus,
  onMenuOpenChange,
}: Props) {
  const navigate = useNavigate()
  const exit = useWorkspaceExit()
  const usage = useUsageToday()
  const scope = useId()

  return (
    <motion.div
      className="flex h-full flex-col overflow-x-hidden overflow-y-auto bg-white"
      initial={false}
      animate={{ width: collapsed ? NAV_RAIL_PX : NAV_PANEL_PX }}
      transition={{ type: 'tween', ease: 'easeOut', duration: 0.2 }}
      data-testid="nav-panel"
      data-collapsed={collapsed ? 'true' : 'false'}
    >
      <div
        className={`flex shrink-0 items-center pb-4 pt-[18px] ${collapsed ? 'justify-center px-2' : 'gap-2 px-4'}`}
      >
        <button
          type="button"
          onClick={() => exit(() => navigate(projectsListHref()))}
          className="flex min-w-0 items-center gap-2.5 rounded-lg text-left outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1"
          aria-label="BIAL Citizen Developer — go to My Applications"
        >
          <BIALLogo compact={collapsed} />
        </button>
        {onTogglePin && !collapsed && (
          <button
            type="button"
            onClick={onTogglePin}
            data-testid="nav-pin"
            aria-pressed={pinned}
            title={pinned ? 'Let the navigation collapse' : 'Keep the navigation open'}
            className="ml-auto shrink-0 rounded-lg p-1.5 text-neutral outline-none transition hover:bg-surface-muted hover:text-primary-900 focus-visible:ring-2 focus-visible:ring-primary"
          >
            {pinned ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
          </button>
        )}
      </div>

      {/* ONE NAMESPACE PER PANEL. The active-destination pill travels between ROWS by sharing a
          `layoutId`, and two panels mounted at once would pair them ACROSS the two and leave an
          exit unfinished, so the floating copy never goes away. */}
      <LayoutGroup id={scope}>
        <NavItems collapsed={collapsed} onNavigate={onNavigate} onItemFocus={onItemFocus} />
      </LayoutGroup>

      <div className="mt-auto flex flex-col">
        {usage && <TokenRing usage={usage} rail={collapsed} />}
        <ProfileCluster collapsed={collapsed} onMenuOpenChange={onMenuOpenChange} />
      </div>
    </motion.div>
  )
}
