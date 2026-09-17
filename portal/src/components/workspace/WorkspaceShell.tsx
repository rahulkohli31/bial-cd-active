/**
 * THE WORKSPACE SHELL — one page frame that no move inside a project destroys.
 *
 * A layout route renders the SAME element at the SAME position across a sibling route change, so
 * everything this component holds survives while only the outlet content changes; the pane it
 * holds is framed by the address rather than by the route, which `AppPaneHost` owns.
 * `RequireAuth` wraps the shell, not each child, because it re-runs per `location.key`.
 * `/apps/:appId` cannot join the table: `/apps/` goes to the control plane at both edges and is
 * shadowed before React Router sees it. The URLs do not nest — a chat keeps one flat address.
 * It owns the frame and its one height model, the grid, the pane host and the channel; it starts
 * no fetch and holds no conversation, and every surface below declares its own scroller.
 */
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { memo, useCallback, useEffect, useLayoutEffect, useState, type CSSProperties } from 'react'
import { useNavReveal } from '../layout/NavReveal'
import ReclaimWorkspaceDialog from '../projects/ReclaimWorkspaceDialog'
import AppPane from './AppPane'
import RailResizeHandle from './RailResizeHandle'
import WorkspaceToolbar from './WorkspaceToolbar'
import { clampRailWidth, openingWidth, readRailWidth, writeRailWidth } from './railWidth'
import { projectsListHref } from '../../utils/projectsListMemory'
import type { DeviceName } from './devices'
import { WORKSPACE_RAIL_ID } from './railId'
import { HIDDEN_BUT_MOUNTED } from './hiddenSubtree'
import {
  WorkspaceChannelProvider,
  createWorkspaceChannel,
  useRailSlot,
  useWorkspaceChannel,
  useWorkspaceHeading,
  useWorkspacePaneVisible,
  useWorkspaceReclaim,
} from './workspaceChannel'

/**
 * WHICH RAIL IS SHOWING, DERIVED FROM THE ADDRESS AND FROM NOTHING ELSE.
 *
 * A chat address means the rail IS the conversation; anything else is the project's own details.
 * There is no third mode, and no route and no `?rail=` query behind this: a query param would make
 * a rail mode a shareable link, which is a different feature, and shell state gives this for free.
 */
export type RailMode = 'details' | 'conversation'

function railModeFor(pathname: string): RailMode {
  return pathname.startsWith('/chat/') ? 'conversation' : 'details'
}

/**
 * THE RAIL'S WIDTH IS A CLASS ON ONE PERSISTENT ELEMENT — never a conditional render of two trees,
 * and never a panel library: `react-resizable-panels` sizes inline, and its conditionally rendered
 * second panel would remount the group's children, which the pane host forbids. The responsive
 * class stays for stacking, and the width itself is a custom property consumed only above the
 * stacking threshold, so no `matchMedia` and no `ResizeObserver` enters the shell. `paneVisible`
 * is read before the width, because a planning conversation has no pane at all.
 */
function railWidthClass(collapsed: boolean, paneVisible: boolean): string {
  // Zero width AND out of reach. Width alone would only clip it, leaving its composer, its links
  // and its menus in the tab order — the WCAG 4.1.2 violation `hiddenSubtree.ts` records. The
  // subtree stays MOUNTED, so a draft and a scroll position survive a hide/show cycle.
  //
  // ZERO IN BOTH DIRECTIONS, and the height is not belt-and-braces — it is the whole of the fix
  // below the stacking threshold. This element is a child of a flex ROW above the threshold and a
  // flex COLUMN below it. In the column, `w-0` constrains nothing and `flex-shrink-0` pins the rail
  // at its full CONTENT height, so Hide details on a narrow window left an invisible 1,586px band
  // where the rail had been and pushed the app pane to y=1697 with a height of 0 — the citizen
  // presses "give the app the screen" and every pixel of the workspace goes blank. Measured in a
  // browser at 1024px; no suite saw it, because jsdom lays nothing out and the class was read as a
  // string. Above the threshold a zero height is equally correct: the element is already zero-width
  // and hidden, so nothing is left for a height to stretch.
  if (collapsed) return `w-0 h-0 flex-shrink-0 border-r-0 overflow-hidden ${HIDDEN_BUT_MOUNTED}`
  // WHEN THE RAIL IS THE WHOLE WINDOW IT IS THE PAGE, AND THE PAGE IS WHITE. `#F0F4F8` is the
  // ground the boards paint BEHIND THE APP; with no app beside it there is nothing for that grey to
  // be behind, and `PlanChat` draws its root and its chat region both `#FFFFFF` with the 760px
  // column centred on one unbroken white surface. Without this the centred column read as a white
  // card floating between two 336px grey margins — a card the board does not draw.
  if (!paneVisible) return 'flex-1 bg-white'
  // Stacked below the threshold (`flex-1`, sharing the column), the citizen's own width above it.
  return 'flex-1 wide:flex-none wide:w-[var(--rail-w)]'
}

/**
 * The cross-project reclaim dialog, mounted at shell level.
 *
 * Its open state travels on the channel; the CLASSIFICATION stays exactly where it is, on the
 * surface that made the call that was refused. Nothing here inspects a refusal or classifies one —
 * a bare 409 is not self-describing, and a second competing classifier is how the reclaim path
 * loses its one authority.
 */
function ReclaimSlot() {
  const reclaim = useWorkspaceReclaim()
  if (!reclaim) return null
  return (
    <ReclaimWorkspaceDialog
      blocked={reclaim.blocked}
      startingProjectName={reclaim.startingProjectName}
      step={reclaim.step}
      onSaveAndSwitch={() => reclaim.resolve(true)}
      onSwitchAnyway={() => reclaim.resolve(false)}
      onCancel={reclaim.cancel}
    />
  )
}

/**
 * THE RAIL IS THE SHELL'S FOURTH RESPONSIBILITY, AND IT HAS ONE WRITER. A rail mode and a collapse
 * are state about the shell's own chrome, published downward so a surface can read which rail it
 * is rendering into without re-deriving the predicate from `useLocation` in three places. The
 * shell owns the collapse because the control that undoes one cannot live inside a collapsed rail;
 * it lives on the pane side, which is a sibling of the Outlet. A LAYOUT effect, matching every
 * other publisher on this channel: a passive publish would leave the pane host a frame behind.
 */
function usePublishRail(mode: RailMode, collapsed: boolean): void {
  const channel = useWorkspaceChannel()
  useLayoutEffect(() => {
    const held = channel?.rail.get()
    if (!held || (held.mode === mode && held.collapsed === collapsed)) return
    channel?.rail.set({ ...held, mode, collapsed })
  })
}

/** Everything inside the provider, so it can read the channel it is mounted under. */
function ShellFrame() {
  const rail = useRailSlot()
  const mode = railModeFor(useLocation().pathname)
  const [collapsed, setCollapsed] = useState(false)
  usePublishRail(mode, collapsed)
  // THE REVEAL NEEDS TO KNOW WHEN THE CHAT IS AWAY, because that is the one layout where the
  // left edge belongs entirely to the application: the hover zone is not installed at all there,
  // and a panel sliding over the app on a stray pointer is what that layout exists to prevent.
  // The shell is the only writer of `collapsed`, so it is the only honest reporter of it.
  const reveal = useNavReveal()
  const reportChatHidden = reveal?.setChatHidden
  useEffect(() => {
    reportChatHidden?.(collapsed)
  }, [reportChatHidden, collapsed])
  // THE DEVICE WIDTH AND THE RELOAD NONCE ARE THE SHELL'S. Their controls are in the row above
  // the grid, so the state comes up here with them, and the pane receives both as props down a
  // chain of shell-owned siblings. Holding them here also means the chosen width survives a route
  // change from the project screen to a chat, which it could not while it lived inside a
  // component the pane host re-mounts around.
  const [device, setDevice] = useState<DeviceName>('Desktop')
  const [reloadNonce, setReloadNonce] = useState(0)
  const heading = useWorkspaceHeading()

  /**
   * THE BOUNDARY THE CITIZEN CAN MOVE. Read once on the first render and not watched afterwards:
   * it is a per-person setting, so subscribing to storage would be a listener with no writer.
   *
   * `null` FROM STORAGE IS NOT A WIDTH. It means nobody has dragged one, and the two opening
   * widths differ — a transcript needs more room than a status panel — so substituting a number
   * here would pick one of them for both. Once dragged, their width replaces both.
   */
  const [remembered, setRemembered] = useState<number | null>(readRailWidth)
  const railWidth = remembered ?? openingWidth(mode)
  // WHICH COLUMN GROWS, and it is not a cosmetic choice. The two columns are the conversation and
  // the app, and the conversation is the SIZED one whenever the app is on screen: the builder
  // surface's chat panel sets its own 288px and the pane takes everything left over, which is
  // exactly the split the product has today. Leaving the outlet column at `flex-1` alongside a
  // `flex-1` pane splits the workspace in half and strands the panel in a column twice its width.
  //
  // When nothing wants the pane — every planning conversation — the outlet column grows
  // instead, because then it IS the whole surface.
  //
  // The rail supplies its own two settled widths the same way, so this stays one rule rather
  // than becoming a per-mode table here.
  const paneVisible = useWorkspacePaneVisible()

  // A RAIL COLLAPSED BESIDE A PANE MUST NOT SURVIVE THE PANE GOING AWAY. The control that restores
  // it lives on the pane side, so a planning conversation — which has no pane at all — would
  // inherit a hidden rail with nothing on screen and no way back. Reset when the pane leaves,
  // rather than trying to keep a toggle reachable on a surface that has nowhere to put one.
  useEffect(() => {
    if (!paneVisible) setCollapsed(false)
  }, [paneVisible])

  // THE BACK CONTROL IS DERIVED FROM THE ADDRESS.
  const navigate = useNavigate()
  const back = useCallback(() => {
    // THE CHAT'S OWN PROJECT WHENEVER THERE IS ONE TO GO TO. Keyed on the rail mode rather than on
    // the heading's kind: the kind comes from the conversation fetch, so for the length of a cold
    // chat open this control used to send a citizen who pressed back out to the projects list —
    // out of the project they were working in — and the row's label said "Back to projects" while
    // it did. The mode is derived from the pathname, so it is right from the first frame.
    //
    // THE LIST ADDRESS CARRIES ITS OWN STATE BACK. Read fresh at press time, not memoised at
    // render — this control can sit for minutes before it is pressed, and the list's page,
    // search and page size can all have changed on `/projects` in the meantime.
    const to =
      mode === 'conversation' && heading.projectId
        ? `/projects/${heading.projectId}`
        : projectsListHref()
    navigate(to)
  }, [navigate, mode, heading.projectId])

  return (
    <div className="h-screen flex flex-col font-manrope bg-bial-bg overflow-hidden">
      <ReclaimSlot />
      {/* ONE TOOLBAR ROW, DRAWN ONCE, ABOVE THE GRID — so it survives a collapse of the rail it
          used to live inside, and so it is a single element across a project↔chat move rather
          than three headers that appear and disappear. */}
      <WorkspaceToolbar
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed((was) => !was)}
        device={device}
        onDevice={setDevice}
        onReload={() => setReloadNonce((n) => n + 1)}
        onBack={back}
      />
      {/* THE TWO-COLUMN GRID, BUILT ONCE, ABOVE THE OUTLET — never a second two-column frame
          inside a surface. It is also the container whose class changes at the stacking
          threshold. */}
      <div
        data-testid="workspace-grid"
        /* THE STACKING CROSSING, AS A RESPONSIVE CLASS ON THIS ONE ELEMENT. Below the threshold
           the pane stacks under the rail; above it they sit side by side — never a conditional
           render of a stacked tree and a side-by-side tree, which would remount. The swap happens
           on the two columns' COMMON parent, which is why the grid has to be in one place.
           `rail.stacked` stays a deliberate force-stack override. */
        className={`flex flex-1 min-h-0 overflow-hidden ${rail.stacked ? 'flex-col' : 'flex-col wide:flex-row'}`}
      >
        {/* The outlet column — THE RAIL. The project surface or the chat surface renders inside
            it, and its width is a class on this one persistent element: the details width, the
            conversation width, or collapsed. The element is never conditionally rendered, so a
            width change and a collapse both leave every descendant mounted. */}
        <div
          id={WORKSPACE_RAIL_ID}
          data-testid="workspace-outlet"
          data-rail-mode={mode}
          // COLLAPSE ZEROES THE PROPERTY, it does not merely stop consuming it. The hide treatment
          // keeps the element's layout box, so a leftover width would leave an invisible gap
          // exactly where the rail was — a strip of nothing the citizen cannot see and cannot
          // click past.
          style={{ '--rail-w': `${collapsed ? 0 : railWidth}px` } as CSSProperties}
          className={`min-w-0 min-h-0 flex flex-col overflow-hidden ${railWidthClass(collapsed, paneVisible)}`}
        >
          <RailOutlet />
        </div>
        {/* THE 26px STUB THE CHAT LEAVES BEHIND, and the second way back from a collapse.
            Until now the toolbar control was the only route, which makes one control the single
            point of failure for a state that hides the whole left column. The stub is the same
            rule the toolbar control follows — a toggle may never live inside the thing it hides
            — applied once more: it sits OUTSIDE the collapsed column, as its own sibling, so it
            is visible and tabbable precisely when the column is neither. */}
        {paneVisible && collapsed && (
          <div
            data-testid="chat-stub"
            className="flex w-[26px] flex-shrink-0 items-start justify-center border-r border-bial-border bg-white pt-2"
          >
            <button
              type="button"
              onClick={() => setCollapsed(false)}
              aria-expanded={false}
              aria-controls={WORKSPACE_RAIL_ID}
              aria-label="Show the chat"
              title="Show the chat"
              className="inline-flex h-6 w-6 items-center justify-center rounded text-neutral transition hover:bg-bial-bg hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
            >
              <ChevronRight size={14} />
            </button>
          </div>
        )}
        {/* THE HANDLE, BETWEEN THE TWO COLUMNS. Rendered only when there are two: a collapsed rail
            has no boundary to move, and a surface that declares no pane — every plan chat — is the
            whole window, so a divider in it would divide nothing. Its own class hides it below the
            stacking threshold, where the board says it must disappear rather than become a control
            that cannot help. */}
        {paneVisible && !collapsed && (
          <RailResizeHandle
            width={railWidth}
            controls={WORKSPACE_RAIL_ID}
            onResize={(next) => setRemembered(clampRailWidth(next))}
            onCommit={writeRailWidth}
          />
        )}
        {/* The pane column — a SIBLING of the Outlet, which is what stops a route change from
            reaching it; `AppPaneHost` owns that rule. `AppPane` wraps the host with the region
            label, the skip control and the sentence for when there is nothing to frame. */}
        <AppPane device={device} reloadNonce={reloadNonce} />
      </div>
    </div>
  )
}

/**
 * THE RAIL'S ROUTE CONTENT, MEMOISED — the other half of `AppPane`'s fix.
 *
 * `RailResizeHandle` reports every pointer move into the shell's width state, and both columns of
 * the grid are siblings of that state, so without this a drag re-invokes the whole route subtree
 * at pointer frequency for a width that belongs to the wrapper. It takes NO props, so the memo can
 * never go stale: routing changes reach `Outlet` through context, which memo does not block.
 */
const RailOutlet = memo(function RailOutlet() {
  return <Outlet />
})

export default function WorkspaceShell() {
  // Created once and never replaced, so the context value is stable for the life of the shell and
  // the provider itself never re-renders anybody. Everything that moves lives in the cells.
  const [channel] = useState(createWorkspaceChannel)
  return (
    <WorkspaceChannelProvider value={channel}>
      <ShellFrame />
    </WorkspaceChannelProvider>
  )
}
