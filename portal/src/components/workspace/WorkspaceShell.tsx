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
import { Outlet } from 'react-router-dom'
import { useEffect, useMemo, useState } from 'react'
import Navbar from '../layout/Navbar'
import ReclaimWorkspaceDialog from '../projects/ReclaimWorkspaceDialog'
import AppPaneHost from './AppPaneHost'
import {
  WorkspaceChannelProvider,
  createWorkspaceChannel,
  useRailSlot,
  useWorkspaceReclaim,
  useWorkspaceSaveState,
} from './workspaceChannel'

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
      onSaveAndSwitch={() => reclaim.resolve(true)}
      onSwitchAnyway={() => reclaim.resolve(false)}
      onCancel={reclaim.cancel}
    />
  )
}

/** Everything inside the provider, so it can read the channel it is mounted under. */
function ShellFrame() {
  useUnsavedWorkWarning()
  const rail = useRailSlot()

  return (
    <div className="h-screen flex flex-col font-manrope bg-bial-bg overflow-hidden">
      <ReclaimSlot />
      <Navbar />
      {/* THE TWO-COLUMN GRID, BUILT ONCE, ABOVE THE OUTLET. Plan F supplies the rail's contents
          and the threshold that flips `stacked`; it does not build a second two-column frame
          inside the project surface. This is also the container whose class changes at that
          threshold — the pane host is its SIBLING, so a direction swap cannot remount the frame. */}
      <div
        data-testid="workspace-grid"
        className={`flex flex-1 min-h-0 overflow-hidden ${rail.stacked ? 'flex-col' : 'flex-row'}`}
      >
        {/* The outlet column: the project surface or the chat surface. A flex child that may not
            overflow the frame — each surface declares its own scroller inside it. */}
        <div className="flex-1 min-w-0 min-h-0 flex flex-col overflow-hidden">
          <Outlet />
        </div>
        {/* The pane column — a SIBLING of the Outlet, which is what stops any route change from
            reaching it. This is the whole of R8's mechanism, in one line of JSX. */}
        <AppPaneHost />
      </div>
    </div>
  )
}

export default function WorkspaceShell() {
  // Created once and never replaced, so the context value is stable for the life of the shell and
  // the provider itself never re-renders anybody. Everything that moves lives in the cells.
  const [channel] = useState(createWorkspaceChannel)
  const value = useMemo(() => channel, [channel])
  return (
    <WorkspaceChannelProvider value={value}>
      <ShellFrame />
    </WorkspaceChannelProvider>
  )
}
