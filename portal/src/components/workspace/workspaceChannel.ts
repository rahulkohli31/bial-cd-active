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
 *  6. the rail mode, its collapse, and an opaque per-mode bag that outlives a chat
 *  7. what to SAY about the workspace — one computed value, and the handlers for its one action
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
 * Each payload is therefore its own cell with its own listener set, read through
 * `useSyncExternalStore`.
 *
 * BE PRECISE ABOUT WHAT THAT BUYS, because the tempting sentence is not true. A save-state publish
 * reaches only the shell's unload effect. A keystroke touches neither the address nor the save
 * state nor the visibility — but it DOES republish the pane view, because that view is rebuilt by
 * identity every render (its toolbar nodes and its handlers are fresh closures), so the pane host
 * re-renders once per character exactly as `LivePreview` did when the page rendered it directly.
 * What the split protects is the thing that matters: the address is the VALUE-compared cell and
 * the frame's identity input, so no amount of typing can move what is framed. Do not "fix" the
 * re-render with a shallow comparator — the handlers are unstable, so it would buy nothing without
 * memoising them too, and that is a behaviour change this refactor is not making.
 *
 * The context carries the CHANNEL HANDLE, which is created once and never replaced. That handle is
 * stable for the life of the shell, so the context itself never re-renders anybody.
 */
import { createContext, useContext, useLayoutEffect, useRef, useSyncExternalStore, type ComponentProps, type ReactNode } from 'react'
// TYPE-ONLY, so this stays a leaf at runtime: the import is erased and the channel keeps no
// dependency on the component it describes.
import type LivePreview from '../LivePreview'
import type { PreviewAddress } from '../../utils/previewAddress'
import type { CompileState } from '../../utils/compileState'
import type { PreviewLifeState, ReclaimBlocked } from '../../utils/buildSessionApi'
import type { RelaunchError } from '../../utils/buildSessionTypes'
import type { StartOutcome, WorkspaceState } from './workspaceState'

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
 * VALUE-COMPARED, for the same reason the address is. The rail's flags are rebuilt on every render
 * of the surface that publishes them, and that surface re-renders on every keystroke in its
 * composer — so an identity comparison here would wake the shell's grid once per character and
 * recompute the width class each time. `state` is compared by identity deliberately: it is opaque,
 * so there is nothing here that could compare it by value, and its owner is expected to hold it
 * stable across renders that did not change it.
 */
const sameRail = (a: RailSlot, b: RailSlot) =>
  a.mode === b.mode && a.stacked === b.stacked && a.collapsed === b.collapsed && a.state === b.state

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
   * so it survived Plan D's deletion of the page that used to publish it. Without it the number stops
   * being produced and nothing announces that, which is the one failure a measurement cannot
   * detect.
   */
  onRevealed?: () => void
}

/**
 * THE SUBSET CLAIM `AppPaneHost`'S SPREAD RESTS ON, pinned by the compiler rather than by a comment.
 *
 * The host spreads a `PaneView` straight into `<LivePreview/>`. JSX spread attributes are EXEMPT from
 * excess-property checking — only fresh object literals get it — so a field added here that the pane
 * has no prop for would compile clean and go nowhere at runtime, silently. That is the one failure a
 * reader would reasonably assume the types already prevent.
 *
 * `never` means every field is a real prop. Add a field the pane does not accept and this alias stops
 * being `never`, which the assertion below turns into a compile error at the declaration site — where
 * the mistake is, rather than at the spread that would have swallowed it.
 */
export type UnacceptedPaneProps = Exclude<keyof PaneView, keyof ComponentProps<typeof LivePreview>>

const _paneViewIsASubsetOfLivePreviewProps: UnacceptedPaneProps extends never ? true : never = true
void _paneViewIsASubsetOfLivePreviewProps

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
  /**
   * WHICH RAIL IS SHOWING, and it is DERIVED FROM THE ADDRESS rather than chosen by anybody.
   * `details` on a project address, `conversation` on a chat one. The shell computes it and
   * publishes it here so surfaces below can read it; nothing writes it from below.
   *
   * There is no route for it and no `?rail=` query param, deliberately: a query param would make
   * a rail mode a shareable link, and a link that reopens somebody else's view of a screen is a
   * different feature from the one R9 asks for.
   */
  mode: string | null
  /** Opaque to the shell; the Outlet child owns the contents. */
  state: Record<string, unknown>
  /**
   * Below R13's threshold the two columns stack instead of sitting side by side. Plan F owns the
   * threshold that flips this; THIS PLAN OWNS THE CONTAINER whose class it changes, which is what
   * makes the "a layout change does not remount the frame" claim assertable against the shell's
   * own grid rather than against an arbitrary test wrapper.
   *
   * PLAN F LEFT IT AS A FORCE-STACK OVERRIDE rather than as the threshold itself. The crossing is
   * expressed as a responsive class on the same container — `flex-col lg:flex-row` — so it costs
   * no `matchMedia`, no `ResizeObserver` and no state, and AE37 ("crossing the threshold is a
   * layout change, not a remount") is true by construction rather than by a test. This flag stays
   * because a caller that genuinely knows it wants one column should be able to say so.
   */
  stacked: boolean
  /**
   * THE RAIL IS HIDDEN, NOT UNMOUNTED. Zero width plus `HIDDEN_BUT_MOUNTED` on a subtree that
   * stays in the document, so a draft and a scroll position survive a hide/show cycle and the
   * collapsed subtree leaves the tab order.
   *
   * WHERE THE CONTROL THAT UNDOES THIS LIVES IS THE WHOLE DESIGN. It cannot be inside the rail:
   * a collapsed rail is invisible and untabbable, so a toggle in it would be a one-way door. The
   * project surface publishes it into the pane's leading toolbar slot instead — the same place the
   * conversation surface already puts its own chat-panel toggle — where it stays reachable
   * precisely because the pane is what remains on screen.
   */
  collapsed: boolean
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

export const NO_RAIL: RailSlot = { mode: null, state: {}, stacked: false, collapsed: false }

/**
 * WHAT THE PANE NEEDS IN ORDER TO SAY WHAT THE WORKSPACE IS DOING (Plan F, U2/U3/U4).
 *
 * The `state` is the one computed value — a sentence and at most one action, with no destructive
 * verb in its type. It travels on the channel for the same reason the address does: the pane host
 * is a SIBLING of the Outlet, and the surface that made the read is below it.
 *
 * THE HANDLERS TRAVEL WITH IT because they are the publisher's, exactly as the reclaim request's
 * are. Recording how a start ended, asking the platform again, and routing a refusal to the one
 * dialog are all things the surface that owns the read knows how to do; a shell that re-derived
 * them would be a second authority on a question that already has one.
 *
 * `null` MEANS NOBODY HAS COMPUTED ONE — a surface mounted outside a workspace, or one that has
 * not resolved a project. The pane renders nothing rather than inventing a state to describe.
 */
export interface WorkspaceReport {
  state: WorkspaceState
  /** The project the state describes. `null` while a route is still resolving one. */
  projectId: string | null
  /** Record how a start attempt ended; `null` clears it (a start that reached the app). */
  onStartOutcome: (outcome: StartOutcome | null) => void
  /** Ask the platform again, now. A retry press, or a start that just finished. */
  onRefresh: () => void
  /**
   * Route a reclaim refusal to the one dialog, carrying the retry that resumes what was refused.
   * The CLASSIFICATION already happened at the call site — this is the slot, not a second
   * classifier, and a bare 409 is not self-describing enough to have two of those.
   */
  onReclaimRefusal: (blocked: ReclaimBlocked, retry: () => Promise<void>) => void
}

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
  /** What to SAY about the workspace, and the handlers for the one thing that may be pressed. */
  workspace: Cell<WorkspaceReport | null>
}

export function createWorkspaceChannel(): WorkspaceChannel {
  return {
    address: createCell<WorkspaceAddress>(NO_ADDRESS, sameAddress),
    project: createCell<string | null>(null),
    pane: createCell<PaneView | null>(null),
    visible: createCell<boolean>(false),
    reclaim: createCell<ReclaimRequest | null>(null),
    saveDirty: createCell<boolean | null>(null),
    rail: createCell<RailSlot>(NO_RAIL, sameRail),
    workspace: createCell<WorkspaceReport | null>(null),
  }
}

/**
 * `null` outside a shell, and that is not an error condition.
 *
 * Every publisher below no-ops when there is no channel, because the surfaces are mounted without
 * a shell in fifteen existing test suites and could legitimately be rendered anywhere. A surface that cannot reach a pane simply does not get one; it must never
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

/** What the workspace is doing, and what may be pressed about it. `null` = nobody has said. */
export function useWorkspaceReport(): WorkspaceReport | null {
  return useCell(useWorkspaceChannel()?.workspace, null)
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
//   project    KEPT     — the cell must not go blank between an unmounting surface and the one
//                         replacing it, because the next publisher's address is judged against
//                         it. Note what KEPT does NOT buy: after a move to a surface that
//                         declares nothing, the cell still names the departed project, so
//                         `belongsElsewhere` cannot fire. Every surface the shell mounts
//                         declares one for exactly that reason, and whichever surface Plan F
//                         teaches to SHOW the pane must keep doing so — declaring the project
//                         before publishing an address is what stops it framing the previous
//                         project's app with nothing able to detect it.
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

function usePublish<T>(cell: Cell<T> | undefined, value: T, onUnmount?: T, abstain = false): void {
  // LAYOUT effect, not a passive one. The host is a sibling that re-renders from the store, so a
  // passive publish would leave it one committed frame behind its surface — visible on mount as a
  // pane that appears hidden and then shows itself.
  const publish = () => {
    if (!abstain) cell?.set(value)
  }
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

/**
 * Publish what to frame. Survives this surface's unmount — see the table above.
 *
 * ═══ "I HAVE NOTHING YET" IS NOT "THERE IS NOTHING" ═══
 *
 * Keeping the address across an unmount only buys R8 the OUTBOUND leg. The return leg mounts a
 * BRAND NEW surface, and a surface's address arms are all cold on its first commit — a session
 * hook starts at `null`, a turn narrative ref starts unset, a transcript starts empty, and the
 * real URL only arrives after a hydrate/reattach round trip. `usePublish` runs on every render
 * with no dependency list, so without this rule that first commit would hand the cell a bare
 * `{url: null}` and retire the very address the outbound leg went to such lengths to keep. The
 * citizen would watch their running app reload on the way BACK into the chat — the exact failure
 * the shell was extracted to remove, arriving through the other door.
 *
 * So a publisher that has said nothing yet says nothing at all: it abstains, and the held address
 * stands until this publisher has an answer of its own.
 *
 * THE RETIRE PATH STAYS OPEN, and that is the half a blanket "ignore nulls" would break. Once a
 * publisher HAS resolved something — a URL, or a status with no URL yet, which is the provisioning
 * case and a real claim — it has standing, and every later publish lands, `{url: null}` included.
 * That is how a container that dies, a relaunch that fails, or a workspace that is lost still
 * clears the frame while its surface stays mounted.
 *
 * A ref rather than state, and assigned during render rather than in an effect: `usePublish`'s
 * layout effect reads this on the SAME commit that first carries an address, so a value that only
 * became true in a later effect would abstain one commit too long and drop the first real publish.
 */
export function usePublishAddress(address: PreviewAddress, projectId: string | null): void {
  const channel = useWorkspaceChannel()
  const hasStanding = useRef(false)
  // Both null is the only shape that means "not resolved yet". A status with no URL is the
  // loading state — a publisher saying "a build is coming up here" — and must not be swallowed.
  const saysNothing = address.url === null && address.status === null
  if (!saysNothing) hasStanding.current = true
  usePublish(
    channel?.address,
    { url: address.url, status: address.status, projectId },
    undefined,
    saysNothing && !hasStanding.current,
  )
}

/** Publish the pane's chrome and its props. Cleared on unmount. */
export function usePublishPaneView(view: PaneView): void {
  usePublish(useWorkspaceChannel()?.pane, view, null)
}

/**
 * THE ONE NAMED CALL by which a mounted surface declares it wants the pane VISIBLE.
 *
 * One call, greppable, and the call Plan D carried across when it collapsed the two surfaces into
 * one. Saying it plainly, because the register of the claim matters: the pane ELEMENT is rendered
 * by the address, but what a citizen SEES is still decided by the mounted surface declaring it —
 * there is now one surface doing that declaring rather than two.
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

/**
 * Publish the rail's slot — its mode, its collapse and its opaque per-mode bag (Plan F, U1).
 *
 * CLEARED ON UNMOUNT, and the reason is the opposite of the address's. An address survives its
 * publisher because the app it names is still running and R8 forbids destroying it on a
 * navigation. A rail slot describes chrome that has just been unmounted: leaving `collapsed: true`
 * standing after the surface that collapsed it is gone would hand the NEXT surface a rail already
 * hidden, with the control that would restore it published by a component that no longer exists.
 *
 * ONE WRITER AT A TIME, by construction rather than by convention: the shell mounts exactly one
 * Outlet child, and only that child publishes here.
 */
export function usePublishRailSlot(slot: RailSlot): void {
  usePublish(useWorkspaceChannel()?.rail, slot, NO_RAIL)
}

/**
 * Publish what to say about the workspace. CLEARED ON UNMOUNT, like the pane view and for the same
 * reason: its handlers close over the departing surface's own read, its outcome slot and its
 * refusal routing. A state left standing after they died would render a sentence whose one button
 * calls into a component that no longer exists — the dead end the reclaim flow exists to remove,
 * rebuilt one layer up.
 */
export function usePublishWorkspaceReport(report: WorkspaceReport | null): void {
  usePublish(useWorkspaceChannel()?.workspace, report, null)
}
