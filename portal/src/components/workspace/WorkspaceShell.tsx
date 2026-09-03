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
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { memo, useCallback, useEffect, useLayoutEffect, useState, type CSSProperties } from 'react'
import Navbar from '../layout/Navbar'
import ReclaimWorkspaceDialog from '../projects/ReclaimWorkspaceDialog'
import AppPane from './AppPane'
import RailResizeHandle from './RailResizeHandle'
import WorkspaceToolbar from './WorkspaceToolbar'
import { clampRailWidth, openingWidth, readRailWidth, writeRailWidth } from './railWidth'
import type { DeviceName } from './WorkspaceToolbar'
import { HIDDEN_BUT_MOUNTED } from './hiddenSubtree'
import { WorkspaceExitProvider, useUnsavedWorkGuard } from './UnsavedWorkGuard'
import {
  WorkspaceChannelProvider,
  createWorkspaceChannel,
  useRailSlot,
  useWorkspaceChannel,
  useWorkspaceHeading,
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
 * ═══ THE WIDTH IS A CUSTOM PROPERTY NOW, AND THE MECHANISM IS THE POINT (plan 002, U7) ═══
 *
 * The boundary is draggable, which the earlier decision here refused. That refusal is amended: the
 * canvas devotes an artboard to the stops, and "someone who wants the app at full width already
 * has a control for it" is an argument for bounded resizing, not against it.
 *
 * WHAT IT IS NOT IS A PANEL LIBRARY, and that is the load-bearing half. `react-resizable-panels` —
 * which the board itself names — takes its direction as a VALUE rather than as a class and applies
 * sizes inline, which reintroduces the measured breakpoint this shell was built without and fights
 * the width classes. Worse, a plan chat has no pane, and a conditionally rendered second panel
 * would remount the group's children — the one thing the pane host forbids, because it reloads the
 * citizen's app.
 *
 * So: keep the responsive class for stacking, keep ONE element, and drive its width from a custom
 * property that is consumed ONLY above the stacking threshold. The stacked arm keeps its flexible
 * width untouched, no `matchMedia` and no `ResizeObserver` enters the shell, and AE37 — crossing
 * the threshold is a layout change, not a remount — still holds by construction.
 *
 * COLLAPSE MUST ZERO THE PROPERTY, not merely add a zero-width class. The hide treatment keeps the
 * element's layout box, so a leftover `--rail-w` leaves an invisible 400px gap where the rail was.
 *
 * WHEN NOTHING WANTS THE PANE the rail is the whole surface and takes the remaining space — a
 * planning conversation, which has no pane at all, must not be pinned to 520px with a void beside
 * it. That is why `paneVisible` is read before the width at all.
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
      step={reclaim.step}
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
  // THE DEVICE WIDTH AND THE RELOAD NONCE ARE THE SHELL'S NOW (plan 002, U2) — both were private
  // state inside `LivePreview`, chosen there because their controls lived in that component's own
  // toolbar. Their controls are in the row above the grid, so the state comes up here with them,
  // and the pane receives both as props down a chain of shell-owned siblings. Holding them here
  // also means the chosen width survives a route change from the project screen to a chat, which
  // it could not while it lived inside a component the pane host re-mounts around.
  const [device, setDevice] = useState<DeviceName>('Desktop')
  const [reloadNonce, setReloadNonce] = useState(0)
  const heading = useWorkspaceHeading()

  /**
   * THE BOUNDARY THE CITIZEN CAN MOVE (plan 002, U7).
   *
   * REMEMBERED ONCE, READ ONCE. The stored preference is read on the first render and not watched
   * afterwards: it is a per-person setting, so nothing else can change it while a workspace is
   * open, and subscribing to storage would be a listener with no writer.
   *
   * `null` FROM STORAGE IS NOT A WIDTH. It means the citizen has never dragged one, and the two
   * opening widths differ — 400px for a project's details, 520px for a conversation, because a
   * transcript needs more room than a status panel. Substituting a number here would pick one of
   * them for both. Once they HAVE dragged, their width replaces both, which is the board's own
   * sentence: "drag it once and every project opens there".
   */
  const [remembered, setRemembered] = useState<number | null>(readRailWidth)
  const railWidth = remembered ?? openingWidth(mode)
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
    // WHOSE WORK IS AT RISK. The heading already carries the name for the toolbar row, and it is
    // the same fact — so the dialog names the project rather than saying "this app" to somebody
    // who is, by definition, in the middle of leaving it for another one.
    projectName: heading.projectName,
  })

  // A RAIL COLLAPSED BESIDE A PANE MUST NOT SURVIVE THE PANE GOING AWAY. The control that restores
  // it lives on the pane side, so a planning conversation — which has no pane at all — would
  // inherit a hidden rail with nothing on screen and no way back. Reset when the pane leaves,
  // rather than trying to keep a toggle reachable on a surface that has nowhere to put one.
  useEffect(() => {
    if (!paneVisible) setCollapsed(false)
  }, [paneVisible])

  // THE BACK CONTROL IS DERIVED FROM THE ADDRESS, and it goes through the same guard every other
  // navigation in the workspace goes through. Two of the three exits a citizen actually uses — the
  // navbar and this one — used to leave unsaved work behind in silence; the navbar was routed
  // through the guard first, and this is the other one.
  const navigate = useNavigate()
  const back = useCallback(() => {
    const to = heading.chatKind !== null && heading.projectId ? `/projects/${heading.projectId}` : '/projects'
    guard(() => navigate(to))
  }, [guard, navigate, heading.chatKind, heading.projectId])

  return (
    <WorkspaceExitProvider value={guard}>
    <div className="h-screen flex flex-col font-manrope bg-bial-bg overflow-hidden">
      <ReclaimSlot />
      {unsavedWorkDialog}
      <Navbar />
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
        {/* The pane column — a SIBLING of the Outlet, which is what stops any route change from
            reaching it. This is the whole of R8's mechanism, in one line of JSX.
            `AppPane` (Plan F, U4) wraps the host with the region label, the skip control and the
            sentence for when there is nothing to frame; the iframe and its identity stay in the
            host, because a second mount of it is the remount AE4 and AE37 exist to forbid. */}
        <AppPane device={device} reloadNonce={reloadNonce} />
      </div>
    </div>
    </WorkspaceExitProvider>
  )
}

/**
 * THE RAIL'S ROUTE CONTENT, MEMOISED — the other half of `AppPane`'s fix.
 *
 * `RailResizeHandle` reports every pointer move into the shell's width state, and BOTH columns of
 * the grid are siblings of that state. `AppPane` was given `React.memo` for exactly this; the
 * outlet column is the same distance from the same cause and renders the far heavier subtree —
 * `ChatRoute` → `ConversationSlot` → `ConversationSurface`, with the transcript, the composer and
 * the assistant-ui runtime under it. Without this, a drag re-invokes all of it at pointer
 * frequency for a width that belongs to the wrapper, not to the route.
 *
 * It takes NO props, so the memo can never go stale: routing changes reach `Outlet` through
 * context, which memo does not block. The wrapper div's `--rail-w` still updates every move —
 * that is a cheap style write, and it is what actually moves the boundary.
 */
const RailOutlet = memo(function RailOutlet() {
  return <Outlet />
})

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
