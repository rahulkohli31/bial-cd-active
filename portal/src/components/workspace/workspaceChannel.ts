/**
 * THE UPWARD CHANNEL between the mounted surface and the shell (Plan A, U3).
 *
 * ═══ WHY THIS MODULE EXISTS AT ALL ═══
 *
 * The app pane host is a SIBLING of the `<Outlet/>`, not a descendant of it. Everything it needs
 * is produced below that Outlet — the resolved address, the pane's toolbar slots, whether the
 * surface wants the pane visible, the reclaim dialog's state, the tri-state save state — and a
 * sibling cannot read any of it by props. So the mechanism has to be named once, in one place, or
 * three implementers will pick three and the seam will have three shapes.
 *
 * ═══ WHAT TRAVELS ON IT, AND NOTHING ELSE ═══
 *
 *  1. the resolved preview address and its status      (`utils/previewAddress.ts`)
 *  2. the pane's view — visibility, toolbar slots, and the pane's own pass-through props
 *  3. the reclaim dialog's open state
 *  4. the tri-state save state
 *  5. the app-revealed callback (R104's stop-clock)
 *  6. the rail mode, plus an opaque per-mode bag that outlives a chat
 *
 * TWO RULES MAKE IT SAFE, and they are the whole contract:
 *
 *  - A PUBLISH MUST NOT CHANGE THE PANE'S IDENTITY INPUTS unless the address genuinely changed.
 *    That is why the address is its own cell with a VALUE comparison rather than a field on the
 *    pane view: the surface re-renders on every keystroke, so a channel that republished one
 *    object would hand the host a new address object per character typed. The iframe's key is
 *    the URL plus its reload nonce, so a new object with the same URL would not actually remount
 *    it — but relying on that is relying on a coincidence, and `AppPaneHost.test.tsx`'s identity
 *    scenarios are what enforce this rule.
 *  - THE CHANNEL CARRIES NO FETCHING. The surface below still owns every request it makes today.
 *    "The shell owns no chat state" means it starts no fetch and holds no conversation; it does
 *    hold this channel, and after Plan F it also holds the rail mode.
 *
 * ═══ THE SHAPE, AND WHY IT IS CELLS RATHER THAN A CONTEXT VALUE ═══
 *
 * A plain context whose value is an object re-renders EVERY consumer whenever ANY field changes.
 * Here that would mean the pane host re-rendering on every character typed into the composer, and
 * the save state's publish re-rendering the pane. Each payload is therefore its own cell with its
 * own listener set, read through `useSyncExternalStore` — so a save-state publish reaches only the
 * shell's unload effect, and a keystroke reaches nothing at all.
 *
 * The context carries the CHANNEL HANDLE, which is created once and never replaced. That handle is
 * stable for the life of the shell, so the context itself never re-renders anybody.
 */
import { createContext, useContext, useLayoutEffect, useSyncExternalStore, type ReactNode } from 'react'
import type { PreviewAddress } from '../../utils/previewAddress'
import type { CompileState } from '../../utils/compileState'
import type { PreviewLifeState, ReclaimBlocked } from '../../utils/buildSessionApi'
import type { RelaunchError } from '../../utils/buildSessionTypes'

type Listener = () => void

/** One payload, one listener set. `equals` is what keeps a republish from waking a subscriber. */
interface Cell<T> {
  get: () => T
  set: (next: T) => void
  subscribe: (listener: Listener) => () => void
}

function createCell<T>(initial: T, equals: (a: T, b: T) => boolean = Object.is): Cell<T> {
  let value = initial
  const listeners = new Set<Listener>()
  return {
    get: () => value,
    set: (next) => {
      if (equals(value, next)) return
      value = next
      for (const listener of listeners) listener()
    },
    subscribe: (listener) => {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
  }
}

const sameAddress = (a: WorkspaceAddress, b: WorkspaceAddress) =>
  a.url === b.url && a.status === b.status && a.projectId === b.projectId

/**
 * What the mounted surface asks the pane to SHOW — its visibility declaration, its toolbar slots,
 * and the pane's own props.
 *
 * THE PASS-THROUGH PROPS KEEP THEIR SCOPES, which is the whole reason they are listed one by one
 * rather than collapsed into a bag. Three of them are APP-scoped — facts about the project's one
 * app, whose producer outlives the turn — and narrowing them to the open conversation "for
 * consistency" is what blanks the compile signal and leaves an error screen uncovered. The
 * reasoning lives beside each one at the publishing site, where it can be read next to what it
 * describes.
 */
export interface PaneView {
  toolbarLeading: ReactNode
  toolbarTrailing: ReactNode
  /** Chat-scoped: this conversation's own turn. */
  iterating: boolean
  reconnecting: boolean
  /** Project-scoped: the project's one workspace and its restore path. */
  onRelaunch?: () => void
  relaunching: boolean
  relaunchError: RelaunchError | null
  lastBuildFailed: boolean
  restoredFromFailedBuild: boolean
  completedLive: boolean
  hasSavedBuild: boolean | null
  previewState: PreviewLifeState | null
  occupyingProjectName: string | null
  turnRunning: boolean
  /** App-scoped: about the project's ONE app, deliberately NOT narrowed to the open chat. */
  compileState: CompileState | null
  workspaceLost: boolean
  /** The save model (KTD-5e). `saveDirty` is TRI-STATE — `null` is UNKNOWN, never clean. */
  saveDirty: boolean | null
  saving: boolean
  saveError: string | null
  onSave?: () => void
  /** The framed app's own error reporter, scoped to the framed URL by its caller. */
  onFrameMessage?: (data: unknown) => void
  /**
   * R104's stop-clock. Plan E shipped the reveal mark in v1.6.20 and the mount it was passed at
   * is the one this plan replaces — the in-file comment there says "THIS MOUNT IS LOAD-BEARING …
   * Whoever re-hosts this pane carries the prop forward". It travels here rather than as a prop
   * so it survives Plan D's deletion of the page that publishes it. Without it the number stops
   * being produced and nothing announces that, which is the one failure a measurement cannot
   * detect.
   */
  onRevealed?: () => void
}

/**
 * The reclaim dialog's open state. The CLASSIFICATION stays where it is — this is only the slot.
 *
 * The handlers travel with it because they are the publisher's: stopping the other project's
 * build, saving it, releasing it and retrying the refused call are all things the surface that
 * made that call knows how to do, and a shell that re-derived them would be a second authority on
 * a refusal that already has one.
 */
export interface ReclaimRequest {
  blocked: ReclaimBlocked
  /** `true` saves the other project before releasing it; `false` releases without saving. */
  resolve: (save: boolean) => Promise<void>
  cancel: () => void
}

/**
 * The rail's slot, provided now and filled by Plan F.
 *
 * Plan F needs the rail mode and an opaque per-mode bag to outlive a chat (R9, R63), and has
 * already rejected a `?rail=` query param on the strength of shell-held state. Providing the slot
 * here costs one cell and saves F from amending a shipped component. This plan renders no rail and
 * reads no mode.
 */
export interface RailSlot {
  mode: string | null
  /** Opaque to the shell; the Outlet child owns the contents. */
  state: Record<string, unknown>
  /**
   * Below R13's threshold the two columns stack instead of sitting side by side. Plan F owns the
   * threshold that flips this; THIS PLAN OWNS THE CONTAINER whose class it changes, which is what
   * makes the "a layout change does not remount the frame" claim assertable against the shell's
   * own grid rather than against an arbitrary test wrapper.
   */
  stacked: boolean
}

/**
 * The address, plus the ONE thing that can invalidate it after its publisher is gone.
 *
 * An address OUTLIVES the surface that published it — that is the whole mechanism, and it is why
 * leaving a build chat for the project screen no longer destroys the running app. But an address
 * that outlives its publisher needs something other than the publisher's lifetime to bound it, or
 * a stale one lives for as long as the tab does. That something is the project: a different
 * project is a different app, so a different address.
 */
export interface WorkspaceAddress extends PreviewAddress {
  projectId: string | null
}

export const NO_ADDRESS: WorkspaceAddress = { url: null, status: null, projectId: null }

export const NO_RAIL: RailSlot = { mode: null, state: {}, stacked: false }

export interface WorkspaceChannel {
  address: Cell<WorkspaceAddress>
  /**
   * Which project the workspace is showing. Declared by every mounted surface, and separate from
   * the address because "I have no address" and "I am a different project" are different claims
   * and only the second one invalidates what is already framed.
   */
  project: Cell<string | null>
  pane: Cell<PaneView | null>
  visible: Cell<boolean>
  reclaim: Cell<ReclaimRequest | null>
  /** `null` means NOBODY HAS REPORTED, which is the same "could not tell" as a failed check. */
  saveDirty: Cell<boolean | null>
  rail: Cell<RailSlot>
}

export function createWorkspaceChannel(): WorkspaceChannel {
  return {
    address: createCell<WorkspaceAddress>(NO_ADDRESS, sameAddress),
    project: createCell<string | null>(null),
    pane: createCell<PaneView | null>(null),
    visible: createCell<boolean>(false),
    reclaim: createCell<ReclaimRequest | null>(null),
    saveDirty: createCell<boolean | null>(null),
    rail: createCell<RailSlot>(NO_RAIL),
  }
}

/**
 * `null` outside a shell, and that is not an error condition.
 *
 * Every publisher below no-ops when there is no channel, because the surfaces are mounted without
 * a shell in fifteen existing test suites and — until Plan D collapses them — could legitimately
 * be rendered anywhere. A surface that cannot reach a pane simply does not get one; it must never
 * throw, because the thing it would take down is the conversation.
 */
const WorkspaceChannelContext = createContext<WorkspaceChannel | null>(null)

export const WorkspaceChannelProvider = WorkspaceChannelContext.Provider

export function useWorkspaceChannel(): WorkspaceChannel | null {
  return useContext(WorkspaceChannelContext)
}

function useCell<T>(cell: Cell<T> | undefined, fallback: T): T {
  return useSyncExternalStore(
    cell?.subscribe ?? (() => () => {}),
    cell ? cell.get : () => fallback,
    cell ? cell.get : () => fallback,
  )
}

// ─── Subscribing: what the shell and the pane host read ───────────────────────────────────────

/**
 * WHAT THE PANE SHOULD FRAME, with a stale address already discarded.
 *
 * The rule is the one the resolver's project predicate already states, applied one layer up where
 * the address now outlives its publisher: an address belongs to a project, and it stops being this
 * workspace's address when the workspace is showing a different one.
 *
 * `null` IS NOT A DIFFERENT PROJECT. A surface that has not resolved its project yet — which is
 * every cold open of a chat address, since `ChatRoute` learns the project from a fetch — claims
 * nothing, and a claim of nothing must not tear down a running app. Reading an unresolved project
 * as "some other project" would break R8 in exactly the round trip it is about: leave a build
 * chat for the project screen, come back, and watch the app reload while the route resolves.
 */
export function useWorkspaceAddress(): WorkspaceAddress {
  const held = useCell(useWorkspaceChannel()?.address, NO_ADDRESS)
  const project = useCell(useWorkspaceChannel()?.project, null)
  const belongsElsewhere = held.projectId !== null && project !== null && held.projectId !== project
  return belongsElsewhere ? NO_ADDRESS : held
}

export function useWorkspacePane(): PaneView | null {
  return useCell(useWorkspaceChannel()?.pane, null)
}

/** Whether any mounted surface is asking for the pane to be SEEN. Absent means no. */
export function useWorkspacePaneVisible(): boolean {
  return useCell(useWorkspaceChannel()?.visible, false)
}

export function useWorkspaceReclaim(): ReclaimRequest | null {
  return useCell(useWorkspaceChannel()?.reclaim, null)
}

/** TRI-STATE. `true` is definitely dirty, `false` definitely clean, `null` "could not tell". */
export function useWorkspaceSaveState(): boolean | null {
  return useCell(useWorkspaceChannel()?.saveDirty, null)
}

export function useRailSlot(): RailSlot {
  return useCell(useWorkspaceChannel()?.rail, NO_RAIL)
}

// ─── Publishing: what a mounted surface says upward ────────────────────────────────────────────
//
// WHETHER A PAYLOAD IS CLEARED WHEN ITS PUBLISHER UNMOUNTS IS A PER-PAYLOAD DECISION, and each
// one has a different reason. Getting this uniform in either direction breaks something:
//
//   address    KEPT     — R8. The router unmounts the conversation on a move to the project
//                         screen; clearing here would destroy the running app on the one
//                         transition the requirement most obviously covers. Bounded by the
//                         project instead (see `useWorkspaceAddress`).
//   project    KEPT     — every surface declares its own on mount, so there is no window where
//                         nobody has; clearing would blank it for a frame and, with it, the
//                         address it validates.
//   pane       CLEARED  — chrome and props belonging to a surface that is gone. The frame needs
//                         only the address to keep running, so dropping these costs nothing and
//                         keeping them would render a departed conversation's toolbar.
//   visible    CLEARED  — a surface that is gone is not asking for anything to be shown.
//   reclaim    CLEARED  — its buttons close over the publisher's own save/release/retry handlers.
//                         A dialog left standing after they died is a dialog whose buttons do
//                         nothing, which is precisely the dead end the reclaim flow exists to
//                         remove. (Plan F's start control gives the project surface its own
//                         producer; until then only the builder surface can raise one at all.)
//   saveDirty  KEPT     — the unsaved work is in the CONTAINER, not in the component. Clearing on
//                         unmount would disarm the unload warning the moment the user navigated
//                         from the chat to the project screen, which is the exact coverage the
//                         hoist to the shell exists to add.

function usePublish<T>(cell: Cell<T> | undefined, value: T, onUnmount?: T): void {
  // LAYOUT effect, not a passive one. The host is a sibling that re-renders from the store, so a
  // passive publish would leave it one committed frame behind its surface — visible on mount as a
  // pane that appears hidden and then shows itself.
  const publish = () => cell?.set(value)
  useLayoutEffect(publish)
  useLayoutEffect(
    () => () => {
      if (onUnmount !== undefined) cell?.set(onUnmount)
    },
    // Unmount only. `cell` is stable for the life of the shell and `onUnmount` is a constant at
    // every call site; listing them would re-run this cleanup on a re-render and clear a payload
    // its publisher is still standing behind.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  )
}

/** Declare which project the workspace is showing. `null` while a route is still resolving one. */
export function useWorkspaceProject(projectId: string | null): void {
  usePublish(useWorkspaceChannel()?.project, projectId)
}

/** Publish what to frame. Survives this surface's unmount — see the table above. */
export function usePublishAddress(address: PreviewAddress, projectId: string | null): void {
  const channel = useWorkspaceChannel()
  usePublish(channel?.address, { url: address.url, status: address.status, projectId })
}

/** Publish the pane's chrome and its props. Cleared on unmount. */
export function usePublishPaneView(view: PaneView): void {
  usePublish(useWorkspaceChannel()?.pane, view, null)
}

/**
 * THE ONE NAMED CALL by which a mounted surface declares it wants the pane VISIBLE.
 *
 * One call, greppable, and the call Plan D must preserve when it rewrites both surfaces' render
 * bodies. Saying it plainly, because the register of the claim matters: after this plan the pane
 * ELEMENT is rendered by the address, but what a citizen SEES is still decided by which surface
 * mounted — and until Plan F, the surface that declares visibility is still the one `ChatRoute`
 * picked by kind.
 */
export function useAppPaneVisible(visible: boolean): void {
  usePublish(useWorkspaceChannel()?.visible, visible, false)
}

/** Publish the reclaim dialog's open state. The CLASSIFICATION stays with its publisher. */
export function usePublishReclaim(request: ReclaimRequest | null): void {
  usePublish(useWorkspaceChannel()?.reclaim, request, null)
}

/**
 * Publish the tri-state save state. Survives this surface's unmount — see the table above.
 *
 * ADDS NO PRODUCER AND NO TRAFFIC. Whoever calls this already knows the answer; the shell reads
 * whatever was last published and treats "nobody has published" as `null`. A project screen with
 * no conversation mounted therefore costs no container round trip and warns about nothing.
 */
export function usePublishSaveState(dirty: boolean | null): void {
  usePublish(useWorkspaceChannel()?.saveDirty, dirty)
}
