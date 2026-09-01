/**
 * THE WORKSPACE SHELL (Plan A, U3) — one page frame that no move inside a project destroys.
 *
 * ═══ WHY A LAYOUT ROUTE ═══
 *
 * The app pane used to exist because `BuilderPage` was the page that matched, and it was destroyed
 * because a different page matched next. That is the whole of what R8 forbids. A React Router
 * layout route renders the SAME element at the SAME position across a sibling route change, so
 * everything this component holds — above all the iframe — is preserved while only the outlet
 * content changes. The pane stops being rendered by the route and starts being rendered by the
 * address.
 *
 * Two constraints settled the shape, and both are load-bearing:
 *
 *  - THE AUTH WRAPPER GOES ABOVE THE SHELL, not inside each child. `RequireAuth` is a component
 *    re-run per `location.key`, not a route; wrapping each child would give one instance per
 *    address and re-run the guard's effect on every move between them.
 *  - `/apps/:appId` CANNOT JOIN. nginx proxies `/apps/` to the backend runner and the Vite dev
 *    proxy does not, so an SPA route there would work locally and 404 in the deployed container.
 *    The route table's own comment says this; it is repeated here because this is the file that
 *    would make adding it look natural.
 *
 * The URLs do NOT nest. `/projects/:projectId` and `/chat/:chatId` keep the flat addressing they
 * have — a chat has one stable address for its whole life and the project is a breadcrumb resolved
 * from it, never a path segment.
 *
 * ═══ WHAT THIS COMPONENT OWNS, AND WHAT IT DELIBERATELY DOES NOT ═══
 *
 * It owns four things: the page frame and its single height model; the two-column grid; the app
 * pane host; and the workspace channel, including the rail-mode slot Plan F fills.
 *
 * It STARTS NO FETCH AND HOLDS NO CONVERSATION. "The shell owns no chat state" is meant literally:
 * every request the product makes is still made by the surface below the Outlet. What the shell
 * holds is the channel those surfaces publish on, and — after Plan F — the rail mode.
 *
 * ═══ ONE HEIGHT MODEL REPLACES TWO ═══
 *
 * The two chat pages were `h-screen … overflow-hidden` roots, one of them with the codebase's only
 * `calc(100vh - 56px)` — a transcription of the navbar's `h-14` into a second file, one Tailwind
 * edit away from a scrollbar nobody could explain. The project page was a `min-h-screen` document
 * scroller. The shell takes the chat model: full height, navbar, no document scroll. Each surface
 * below then declares its OWN scroller, because sticky positioning and overflow both resolve
 * against the nearest scroll container and that container has moved.
 */
import { Outlet, useLocation } from 'react-router-dom'
import { useEffect, useLayoutEffect, useState } from 'react'
import Navbar from '../layout/Navbar'
import ReclaimWorkspaceDialog from '../projects/ReclaimWorkspaceDialog'
import AppPane from './AppPane'
import { HIDDEN_BUT_MOUNTED } from './hiddenSubtree'
import { WorkspaceExitProvider, useUnsavedWorkGuard } from './UnsavedWorkGuard'
import {
  WorkspaceChannelProvider,
  createWorkspaceChannel,
  useRailSlot,
  useWorkspaceChannel,
  useWorkspacePaneVisible,
  useWorkspaceReclaim,
  useWorkspaceReport,
  useWorkspaceSaveState,
} from './workspaceChannel'

/**
 * THE ID THE COLLAPSE CONTROL POINTS AT. The control that hides the rail is published into the
 * pane's toolbar — it has to be, because a collapsed rail is invisible and untabbable and a toggle
 * inside it would be a one-way door — so `aria-controls` is the only thing tying the two together
 * for anyone reading the markup or navigating it. One constant so the two ends cannot drift.
 */
export const WORKSPACE_RAIL_ID = 'workspace-rail'

/**
 * WHICH RAIL IS SHOWING, DERIVED FROM THE ADDRESS AND FROM NOTHING ELSE (Plan F, U1).
 *
 * A chat address means the rail IS the conversation; anything else in the workspace is the
 * project's own details. There is no third mode — chat history was withheld — and there is no
 * route and no `?rail=` query behind this, deliberately: a query param would make a rail mode a
 * shareable link, which is a different feature from the one R9 asks for ("leaving a chat returns
 * to the mode it was opened from, as it was"), and shell state gives that for free.
 */
export type RailMode = 'details' | 'conversation'

function railModeFor(pathname: string): RailMode {
  return pathname.startsWith('/chat/') ? 'conversation' : 'details'
}

/**
 * THE RAIL'S WIDTH IS A CLASS ON ONE PERSISTENT ELEMENT — never a conditional render of two trees.
 *
 * Two settled widths and a collapse, taken from the canvas's 400px and 520px. The conversation
 * gets the wider one because it holds a transcript and a composer; the project's details do not.
 *
 * TWO THINGS THIS DELIBERATELY IS NOT. It is not a draggable divider — two settled widths plus one
 * collapse is the whole requirement, and the panel library the obvious path reaches for has renamed
 * its exports. And it is not a measured breakpoint: the stacked crossing below is a responsive
 * class on the same container, so no `matchMedia` and no `ResizeObserver` enters this plan and
 * AE37 holds by construction.
 *
 * WHEN NOTHING WANTS THE PANE the rail is the whole surface and takes the remaining space — a
 * planning conversation, which has no pane at all, must not be pinned to 520px with a void beside
 * it. That is why `paneVisible` is read before either width.
 */
function railWidthClass(mode: RailMode, collapsed: boolean, paneVisible: boolean): string {
  // Zero width AND out of reach. Width alone would only clip it, leaving its composer, its links
  // and its menus in the tab order — the WCAG 4.1.2 violation `hiddenSubtree.ts` records. The
  // subtree stays MOUNTED, so a draft and a scroll position survive a hide/show cycle.
  if (collapsed) return `w-0 flex-shrink-0 border-r-0 overflow-hidden ${HIDDEN_BUT_MOUNTED}`
  if (!paneVisible) return 'flex-1'
  // Stacked below the threshold (`flex-1`, sharing the column), settled beside the pane above it.
  return mode === 'conversation'
    ? 'flex-1 lg:flex-none lg:w-[520px]'
    : 'flex-1 lg:flex-none lg:w-[400px]'
}

/**
 * THE ONE WARNING THE SAVE MODEL OWES THE CITIZEN (Plan A, U7).
 *
 * Work that is never saved IS lost when the container is reclaimed — that is the accepted product
 * decision — so leaving with unsaved work must not be silent.
 *
 * WHAT THE EXTRACTION FORCED, AND THE WHOLE OF WHAT CHANGED. The handler lived on the builder page,
 * which is now an outlet child that unmounts on every move to the project screen; hoisting it here
 * keeps it armed while the citizen is anywhere in the workspace instead of only while a builder
 * chat is open. Nothing else about the guard is built here: no in-app dialog, no guarded exit
 * function, no enumeration of departing controls. Every exit from the workspace after this plan is
 * still a route change, exactly as before it — the plan adds no rail and no in-place transition —
 * so there is nothing yet for an in-place guard to catch. Plan F introduces those transitions and
 * owns the dialog that covers them, and it has nothing here to delete.
 *
 * ARMED ONLY ON A DEFINITE `true`, AND THE `null` SILENCE IS DELIBERATE. The save state is
 * tri-state and its `null` means "could not check", never "clean". R62 says the platform must SAY
 * when it could not tell — but the browser's unload prompt renders fixed text the page cannot
 * supply a "we could not check" sentence to, so arming on `null` produces a prompt with nothing
 * answerable behind it, which is how people learn to dismiss prompts. That sentence lands in Plan
 * F's in-app dialog instead. Nothing here reports "nothing unsaved" from an unknown state either:
 * the pane's Save block already renders only when the state is known.
 *
 * NO NEW PRODUCER AND NO NEW TRAFFIC. The shell reads whatever the mounted surface last published
 * and treats "nobody has published" as `null`. The check costs two `git` executions inside the
 * container and compares container-HEAD against saved-bundle-HEAD, so a project screen with no
 * conversation mounted has nothing to compare anyway — and pays for nothing.
 */
function useUnsavedWorkWarning(): void {
  const saveDirty = useWorkspaceSaveState()
  useEffect(() => {
    if (saveDirty !== true) return undefined
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [saveDirty])
}

/**
 * The cross-project reclaim dialog, mounted at shell level.
 *
 * Its open state travels on the channel; the CLASSIFICATION stays exactly where it is, on the
 * surface that made the call that was refused. Nothing here inspects a refusal or classifies one —
 * a bare 409 is not self-describing, and a second competing classifier is how the reclaim path
 * loses its one authority.
 *
 * Being honest about why it is here rather than on the page: today no 409 can be raised from the
 * project surface at all, because the only producers are the builder surface's own calls. It is
 * here so that Plan F's start control, which will raise one, has somewhere for a refusal to appear
 * — not because this plan creates a case the page could not have handled.
 */
function ReclaimSlot() {
  const reclaim = useWorkspaceReclaim()
  if (!reclaim) return null
  return (
    <ReclaimWorkspaceDialog
      blocked={reclaim.blocked}
      startingProjectName={reclaim.startingProjectName}
      onSaveAndSwitch={() => reclaim.resolve(true)}
      onSwitchAnyway={() => reclaim.resolve(false)}
      onCancel={reclaim.cancel}
    />
  )
}

/**
 * THE RAIL IS THE SHELL'S FOURTH RESPONSIBILITY, ADDED BY PLAN F — and it has ONE writer.
 *
 * Plan A's docblock says the shell holds "no data fetching and no chat state", and that is still
 * true: a rail mode and a collapse are state about the SHELL'S OWN CHROME — one derived from the
 * address the router already resolved, the other a toggle on the shell's own grid — and neither is
 * about any conversation. They are published downward so a surface can READ which rail it is
 * rendering into without re-deriving the same predicate from `useLocation` in three places, and
 * they are the alternative to the `?rail=` query param this plan rejected.
 *
 * WHY THE SHELL OWNS THE COLLAPSE RATHER THAN THE OUTLET CHILD. The control that undoes a collapse
 * cannot live in the rail — a collapsed rail is `w-0` and `invisible`, so a toggle inside it is a
 * one-way door. It has to live on the pane side, and the pane is the shell's SIBLING of the Outlet.
 * An Outlet child owning the state would have to publish both the flag and its toggle upward
 * through the channel, which puts a function on a value-compared cell and gives one piece of chrome
 * two writers. Holding it here is one `useState` and a prop.
 *
 * A LAYOUT effect, matching every other publisher on this channel: the pane host is a sibling
 * reading from the store, so a passive publish would leave it one committed frame behind.
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
  useUnsavedWorkWarning()
  const rail = useRailSlot()
  const mode = railModeFor(useLocation().pathname)
  const [collapsed, setCollapsed] = useState(false)
  usePublishRail(mode, collapsed)
  // WHICH COLUMN GROWS, and it is not a cosmetic choice. The two columns are the conversation and
  // the app, and the conversation is the SIZED one whenever the app is on screen: the builder
  // surface's chat panel sets its own 288px and the pane takes everything left over, which is
  // exactly the split the product has today. Leaving the outlet column at `flex-1` alongside a
  // `flex-1` pane splits the workspace in half and strands the panel in a column twice its width.
  //
  // When nothing wants the pane — the project screen before Plan F, and every planning
  // conversation — the outlet column grows instead, because then it IS the whole surface.
  //
  // Plan F's rail supplies its own two settled widths the same way, so this stays one rule rather
  // than becoming a per-mode table here.
  const paneVisible = useWorkspacePaneVisible()

  // THE IN-PLACE GUARD (U8), MOUNTED HERE AND NOT IN THE OUTLET CHILD. The exits it exists for —
  // the navbar's links, the breadcrumb — sit ABOVE the Outlet, so a guard mounted below it would
  // lose coverage of exactly the departing controls it was written for.
  //
  // `workspaceIsAlive` comes from the one computed state rather than from a second read: a `null`
  // save state means "could not tell" only while the workspace is running, and means "nobody
  // asked" otherwise. Conflating them fires a warning on every exit from every stopped project.
  const report = useWorkspaceReport()
  const { guard, dialog: unsavedWorkDialog } = useUnsavedWorkGuard({
    saveDirty: useWorkspaceSaveState(),
    workspaceIsAlive: report?.state.name === 'running',
    projectId: report?.projectId ?? null,
  })

  // A RAIL COLLAPSED BESIDE A PANE MUST NOT SURVIVE THE PANE GOING AWAY. The control that restores
  // it lives on the pane side, so a planning conversation — which has no pane at all — would
  // inherit a hidden rail with nothing on screen and no way back. Reset when the pane leaves,
  // rather than trying to keep a toggle reachable on a surface that has nowhere to put one.
  useEffect(() => {
    if (!paneVisible) setCollapsed(false)
  }, [paneVisible])

  return (
    <WorkspaceExitProvider value={guard}>
    <div className="h-screen flex flex-col font-manrope bg-bial-bg overflow-hidden">
      <ReclaimSlot />
      {unsavedWorkDialog}
      <Navbar />
      {/* THE TWO-COLUMN GRID, BUILT ONCE, ABOVE THE OUTLET. Plan F supplies the rail's contents
          and the threshold that flips `stacked`; it does not build a second two-column frame
          inside the project surface. This is also the container whose class changes at that
          threshold — the pane host is its SIBLING, so a direction swap cannot remount the frame. */}
      <div
        data-testid="workspace-grid"
        /* R13's crossing, AS A RESPONSIVE CLASS ON THIS ONE ELEMENT. Below the threshold the pane
           stacks under the rail; above it they sit side by side. Never a conditional render of a
           stacked tree and a side-by-side tree — that is a remount, and AE37 forbids it. Because
           the pane host is this element's SIBLING and the rail's contents are its Outlet child,
           the direction swap happens on their COMMON parent, which is the whole reason the grid
           has to be in one place. `rail.stacked` stays a deliberate force-stack override. */
        className={`flex flex-1 min-h-0 overflow-hidden ${rail.stacked ? 'flex-col' : 'flex-col lg:flex-row'}`}
      >
        {/* The outlet column — THE RAIL. The project surface or the chat surface renders inside
            it, and its width is a class on this one persistent element: the details width, the
            conversation width, or collapsed. The element is never conditionally rendered, so a
            width change and a collapse both leave every descendant mounted. */}
        <div
          id={WORKSPACE_RAIL_ID}
          data-testid="workspace-outlet"
          data-rail-mode={mode}
          className={`min-w-0 min-h-0 flex flex-col overflow-hidden ${railWidthClass(mode, collapsed, paneVisible)}`}
        >
          <Outlet />
        </div>
        {/* The pane column — a SIBLING of the Outlet, which is what stops any route change from
            reaching it. This is the whole of R8's mechanism, in one line of JSX.
            `AppPane` (Plan F, U4) wraps the host with the region label, the skip control and the
            sentence for when there is nothing to frame; the iframe and its identity stay in the
            host, because a second mount of it is the remount AE4 and AE37 exist to forbid. */}
        <AppPane collapsed={collapsed} onToggleCollapsed={() => setCollapsed((was) => !was)} />
      </div>
    </div>
    </WorkspaceExitProvider>
  )
}

export default function WorkspaceShell() {
  // Created once and never replaced, so the context value is stable for the life of the shell and
  // the provider itself never re-renders anybody. Everything that moves lives in the cells, which
  // is what lets a save-state publish reach the unload warning without touching the pane.
  const [channel] = useState(createWorkspaceChannel)
  return (
    <WorkspaceChannelProvider value={channel}>
      <ShellFrame />
    </WorkspaceChannelProvider>
  )
}
