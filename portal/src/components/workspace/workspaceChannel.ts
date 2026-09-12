/**
 * THE UPWARD CHANNEL between the mounted surface and the shell.
 *
 * WHY THIS MODULE EXISTS AT ALL. The app pane host is a SIBLING of the `<Outlet/>`, not a
 * descendant of it — `AppPaneHost` owns that rule. Everything it needs is produced below that
 * Outlet — the resolved address, the pane's toolbar slots, whether the surface wants the pane
 * visible, the reclaim dialog's state, the save-state reading — and a sibling cannot read any
 * of it by props. So the mechanism has to be named once, in one place, or three implementers
 * will pick three and the seam will have three shapes.
 *
 * WHAT TRAVELS ON IT, AND NOTHING ELSE:
 *
 *  1. the resolved preview address, its status and its liveness  (`utils/previewAddress.ts`)
 *  2. the pane's view — visibility and the pane's own pass-through props
 *  3. the reclaim dialog's open state
 *  4. one save-state reading — the tri-state flag and the recovery instant, together
 *  5. the app-revealed callback, the reveal stop-clock
 *  6. the rail mode, its collapse, and an opaque per-mode bag that outlives a chat
 *  7. what to SAY about the workspace — one computed value, and the handlers for its one action
 *  8. what the toolbar row NAMES, and the save control's values and its action
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
 *  - THE CHANNEL CARRIES NO FETCHING. The surface below still owns every request it makes. "The
 *    shell owns no chat state" means it starts no fetch and holds no conversation; it does hold
 *    this channel, and it also holds the rail mode.
 *
 * THE SHAPE, AND WHY IT IS CELLS RATHER THAN A CONTEXT VALUE. A plain context whose value is an
 * object re-renders EVERY consumer whenever ANY field changes. Each payload is therefore its own
 * cell with its own listener set, read through `useSyncExternalStore`.
 *
 * BE PRECISE ABOUT WHAT THAT BUYS, because the tempting sentence is not true. A save-state publish
 * reaches only the shell's unload effect. A keystroke touches neither the address nor the save
 * state nor the visibility — but it DOES republish the pane view, because that view is rebuilt by
 * identity every render (its toolbar nodes and its handlers are fresh closures), so the pane host
 * re-renders once per character exactly as `LivePreview` did when the page rendered it directly.
 * What the split protects is the thing that matters: the address is the VALUE-compared cell and
 * the frame's identity input, so no amount of typing can move what is framed. Do not "fix" the
 * PANE's re-render with a shallow comparator — its view is rebuilt by identity every render, with
 * fresh handler closures on it, so a comparator would buy nothing without memoising those too.
 *
 * THAT WARNING IS ABOUT THE PANE, AND ONLY THE PANE. `address`, `rail` and `workspace` are all
 * value-compared, because each carries plain data (plus, for `workspace`, handlers that are
 * provably interchangeable — see `sameReport`, which states the rule that keeps them so).
 *
 * The context carries the CHANNEL HANDLE, which is created once and never replaced. That handle
 * is stable for the life of the shell, so the context itself never re-renders anybody.
 */
import { createContext, useContext, useLayoutEffect, useRef, useSyncExternalStore, type ComponentProps } from 'react'
// TYPE-ONLY, so this stays a leaf at runtime: the import is erased and the channel keeps no
// dependency on the component it describes.
import type LivePreview from '../LivePreview'
import type { PreviewAddress } from '../../utils/previewAddress'
import type { CompileState } from '../../utils/compileState'
import type { HandoverStep, PreviewLifeState, ReclaimBlocked } from '../../utils/buildSessionApi'
import { sameWorkspaceState } from './workspaceState'
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
  a.url === b.url && a.status === b.status && a.serving === b.serving && a.projectId === b.projectId

/**
 * VALUE-COMPARED, for the same reason the address is. The rail's flags are rebuilt on every render
 * of the surface that publishes them, and that surface re-renders on every keystroke in its
 * composer — so an identity comparison here would wake the shell's grid once per character and
 * recompute the width class each time. `state` is compared by identity deliberately: it is opaque,
 * so there is nothing here that could compare it by value, and its owner is expected to hold it
 * stable across renders that did not change it.
 */
const sameRail = (a: RailSlot, b: RailSlot) =>
  a.mode === b.mode && a.stacked === b.stacked && a.collapsed === b.collapsed

/**
 * What the mounted surface asks the pane to SHOW — its visibility declaration and the pane's own
 * props.
 *
 * THE PASS-THROUGH PROPS KEEP THEIR SCOPES, which is why they are listed one by one rather than
 * collapsed into a bag. Narrowing an APP-scoped one to the open conversation "for consistency" is
 * what blanks the compile signal and leaves an error screen uncovered.
 */
export interface PaneView {
  /* NO TOOLBAR SLOTS AND NO SAVE MODEL HERE. The boards draw one toolbar for the whole workspace,
     above both columns, and Save is in that row, reading the channel's own `save` cell. Keeping a
     second copy here would give one control two publishers that could disagree — and the pane
     spread would silently drop it, since JSX spread attributes are exempt from excess-property
     checking. `UnacceptedPaneProps` below is what catches that. */
  /** Chat-scoped: this conversation's own turn. */
  iterating: boolean
  reconnecting: boolean
  /* THE RELAUNCH FOUR ARE GONE — `onRelaunch`, `relaunching`, `relaunchError`, `lastBuildFailed`.
     `LivePreview` accepted the callback and never read it, so `ConversationSurface.handleRelaunch`
     could never fire and the session state above it could never move: three of the four were
     published as constants on both surfaces already. `lastBuildFailed` had to leave here and there
     in ONE change, which is exactly what `UnacceptedPaneProps` below exists to force. */
  /* `restoredFromFailedBuild` IS GONE, and so is its renderer. It fed one chip drawn over the
     framed app's own navigation, both publishers hardcoded it `false`, and the chip is deleted —
     so the field described a claim nothing could make to a renderer that no longer exists. That
     notice still needs a NEW home (the toolbar row, or a transcript line); when it gets one, the
     field comes back beside it rather than here. */
  /* `completedLive` HAS MOVED ONTO THE ADDRESS, as `serving`. It was the one field on this view
     that decided whether the frame stayed MOUNTED, which is why the host had to hold its last
     value across an unmount — a pane field cleared on a leave was tearing down an app the server
     was still serving. Liveness is a fact about what is framed, so it rides on the address cell,
     which is KEPT across an unmount by design; the hold, and the hazard it was written against,
     are both gone with it. Nothing on THIS view can unmount the frame any more. */
  /* `hasSavedBuild` AND `occupyingProjectName` LEFT WITH THE CARDS THAT READ THEM. Both existed
     only to fill in a sentence LivePreview used to write about the WORKSPACE — "Nothing has been
     built here yet", "Baggage Reconciliation is using your workspace" — and the pane is no longer
     an author of workspace sentences: `workspaceState` owns every one of them, resolved from the
     preview reading it already holds. Neither field was ever read by `AppPane`, `AppPaneHost` or
     `WorkspaceShell`; they travelled this channel purely to reach a component that has stopped
     asking. The subset assertion below is what caught them still being here. */
  /** Project-scoped: the project's one workspace. */
  previewState: PreviewLifeState | null
  turnRunning: boolean
  /** App-scoped: about the project's ONE app, deliberately NOT narrowed to the open chat. */
  compileState: CompileState | null
  workspaceLost: boolean
  /** The framed app's own error reporter, scoped to the framed URL by its caller. */
  onFrameMessage?: (data: unknown) => void
  /**
   * The reveal stop-clock. It travels on the channel rather than as a prop, so re-hosting the
   * pane cannot silently drop it. Without it the number stops being produced and nothing
   * announces that, which is the one failure a measurement cannot detect.
   */
  onRevealed?: () => void
}

/**
 * THE SUBSET CLAIM `AppPaneHost`'S SPREAD RESTS ON, pinned by the compiler rather than by a comment.
 *
 * The host spreads a `PaneView` straight into `<LivePreview/>`, and JSX spread attributes are EXEMPT
 * from excess-property checking — so a field added here that the pane has no prop for would compile
 * clean and go nowhere at runtime. `never` means every field is a real prop; add one the pane does
 * not accept and the assertion below turns it into a compile error at this declaration site.
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
  /**
   * The project being STARTED. The refusal carries only the incumbent,
   * so the name of the app the person is actually trying to open has to travel with the request:
   * the dialog leads with it, because "can I build THIS one?" is the question being asked.
   */
  startingProjectName: string | null
  /** `true` saves the other project before releasing it; `false` releases without saving. */
  resolve: (save: boolean) => Promise<void>
  cancel: () => void
  /**
   * WHICH STEP THE HAND-OVER HAS REACHED, or `null` before one starts.
   *
   * It travels with the request rather than being derived by the dialog, because the SEQUENCE is
   * the publisher's: stop the other project, wait for that to finish, save, release, start this
   * one, then open the chat. Those take real time, and a dialog left spinning through them is
   * indistinguishable from one that has hung.
   */
  step: HandoverStep | null
}

/**
 * The rail's slot — WHICH RAIL IS SHOWING, and how the shell is laying it out.
 *
 * `WorkspaceShell` derives the mode from the address and publishes it here; `WorkspaceRail` reads
 * it. Nothing writes it from below.
 */
export interface RailSlot {
  /**
   * WHICH RAIL IS SHOWING, and it is DERIVED FROM THE ADDRESS rather than chosen by anybody:
   * `details` on a project address, `conversation` on a chat one.
   *
   * There is no route for it and no `?rail=` query param, deliberately — a query param would make
   * a rail mode a shareable link, which is a different feature.
   */
  mode: string | null
  /**
   * Below the stacking threshold the two columns stack instead of sitting side by side.
   *
   * A FORCE-STACK OVERRIDE rather than the threshold itself: the crossing is a responsive class on
   * the shell's own grid container, so it costs no `matchMedia`, no `ResizeObserver` and no state.
   * This flag stays because a caller that knows it wants one column should be able to say so.
   */
  stacked: boolean
  /**
   * THE RAIL IS HIDDEN, NOT UNMOUNTED. Zero width plus `HIDDEN_BUT_MOUNTED` on a subtree that
   * stays in the document, so a draft and a scroll position survive a hide/show cycle and the
   * collapsed subtree leaves the tab order. The control that undoes it cannot live inside the
   * rail — a collapsed rail is invisible and untabbable, so a toggle in it would be a one-way
   * door. It lives on the pane side, which is what remains on screen.
   */
  collapsed: boolean
}

/**
 * WHAT THE TOOLBAR ROW NAMES — the heading half, in ITS OWN CELL rather than a read of `pane`:
 * that cell is republished on every keystroke, holds React elements no comparator can
 * value-compare, and is cleared on unmount, while the row has to name the project on a screen
 * where no conversation is mounted. PUBLISHED BY THE ROUTES, which are mounted for the whole life
 * of an address including their loading branches, so the row holds its height and its back control
 * through a cold open instead of appearing once the fetches land.
 */
export interface WorkspaceHeading {
  projectId: string | null
  /** `null` until the project's own fetch lands — a cold open of a chat address, or a project
   *  whose row was deleted out from under it. The row renders a stable fallback, never a gap. */
  projectName: string | null
  /** Set only on a chat address. `null` on the project screen, and `null` for a freshly minted
   *  chat whose row does not exist yet — its title is derived from the first message it sends. */
  chatTitle: string | null
  /** The stored wire value (`plan` / `build`), presented through `utils/chatKind.ts`. */
  chatKind: string | null
}

export const NO_HEADING: WorkspaceHeading = {
  projectId: null,
  projectName: null,
  chatTitle: null,
  chatKind: null,
}

/**
 * THE SAVE HALF OF THE ROW — its VALUES only. The action lives in `actions`, and the split is the
 * point. A handler on a value-compared cell is a hazard either way: skip it in the comparator and
 * a stale closure survives, compare it and every render of the publisher wakes the subscriber. The
 * row needs the latest handler AT THE MOMENT OF A PRESS, which is not a render-time need — so it
 * goes in its own cell that nothing subscribes to and the row reads it inside its `onClick`.
 */
export interface SaveSlot {
  /** TRI-STATE. `true` definitely dirty, `false` definitely clean, `null` "could not tell". */
  dirty: boolean | null
  saving: boolean
  error: string | null
  /**
   * WHETHER AN ACTION IS PUBLISHED AT ALL — derived from `actions` by `usePublishSave`, never
   * passed separately, so the two cannot disagree.
   *
   * The row needs this at RENDER time and the action itself only at press time. Without it the row
   * cannot tell a pressable control from a status, and a surface that publishes no `onSave` would
   * draw a button that does nothing.
   */
  canSave: boolean
}

export const NO_SAVE: SaveSlot = { dirty: null, saving: false, error: null, canSave: false }

/**
 * ONE READING OF THE SAVE STATE — TWO FACTS THAT TRAVEL TOGETHER OR NOT AT ALL.
 *
 * This used to be a bare `boolean | null` on the channel, and the second fact is here because a
 * bare `dirty` cannot tell the platform's two very different `true`s apart. `dirty` answers "is
 * there a saved VERSION of this tree?", so THE BUILD ITSELF makes it true: a citizen who described
 * an app, watched it get built and touched nothing arrives at `dirty: true, savedHead: null`, and
 * every surface reading that flag alone announced unsaved changes and blocked their exit over work
 * they had never done. `recoveryAt` is the fact that separates them — see
 * `SaveState.recoveryAt` in `utils/buildSessionApi.ts`, and `SessionManager.newest_restore_source`
 * behind it, for why a non-null instant means "the platform can put this back".
 *
 * WHY ONE CELL AND NOT TWO. The pair comes from a single `GET save-state` response, and every rule
 * written against it — the rail's sentence, the exit dialog, the unload prompt — is only sound if
 * both halves describe THE SAME reading. Two independently published cells would let a consumer
 * combine a dirty flag from one moment with a recovery instant from another, which is exactly how
 * a "safe to leave" gets computed from a recovery copy that no longer covers the current tree.
 * Publishing them together in one `set` makes that arithmetic impossible rather than merely
 * unlikely.
 *
 * IT IS NOT A CLAIM THAT ANYTHING WAS SAVED. A recovery copy is the platform's doing; a version is
 * the citizen's, and Save stays MANUAL. `dirty` stays true beside a non-null instant.
 */
export interface SaveReading {
  /** TRI-STATE. `true` definitely dirty, `false` definitely clean, `null` "could not tell". */
  dirty: boolean | null
  /** When the platform last wrote a recovery copy of this tree (ISO-8601), or `null` for none. */
  recoveryAt: string | null
}

/**
 * NOBODY HAS REPORTED, which reads as the same "could not tell" a failed check produces — and
 * carries NO recovery instant, because the reading that would have named one never happened.
 * Fail toward warning: an absent fact must never be the reason somebody loses work.
 */
export const NO_SAVE_READING: SaveReading = { dirty: null, recoveryAt: null }

/**
 * THE ROW'S HANDLERS, held apart from every compared value on purpose.
 *
 * Both are things a citizen PRESSES, so neither is needed at render time — which is what lets them
 * live in a cell nothing subscribes to. `rename` is here because the toolbar row is where a
 * workspace's name is shown, so it is where renaming it belongs.
 */
export interface WorkspaceActions {
  save: (() => void) | null
  rename: (() => void) | null
  /** Open the share panel (#198) — `null` wherever nothing on screen can share (a chat, or
   *  a shared viewer's own restricted screen, which never registers this channel at all). */
  share: (() => void) | null
}

export const NO_ACTIONS: WorkspaceActions = { save: null, rename: null, share: null }

/**
 * The address, plus the ONE thing that can invalidate it after its publisher is gone.
 *
 * An address outlives its publisher and is bounded by the project instead — `AppPaneHost` owns
 * that rule; this type is what carries the project id alongside the address.
 */
export interface WorkspaceAddress extends PreviewAddress {
  projectId: string | null
}

export const NO_ADDRESS: WorkspaceAddress = { url: null, status: null, serving: false, projectId: null }

export const NO_RAIL: RailSlot = { mode: null, stacked: false, collapsed: false }

const sameHeading = (a: WorkspaceHeading, b: WorkspaceHeading) =>
  a.projectId === b.projectId &&
  a.projectName === b.projectName &&
  a.chatTitle === b.chatTitle &&
  a.chatKind === b.chatKind

const sameSave = (a: SaveSlot, b: SaveSlot) =>
  a.dirty === b.dirty && a.saving === b.saving && a.error === b.error && a.canSave === b.canSave

/** BOTH FIELDS, and the second one is not optional: a comparator blind to `recoveryAt` would
 *  hold the first reading forever and freeze every sentence and every guard decision derived
 *  from it at whatever the first poll happened to say. */
const sameReading = (a: SaveReading, b: SaveReading) =>
  a.dirty === b.dirty && a.recoveryAt === b.recoveryAt

/**
 * WHAT THE PANE NEEDS IN ORDER TO SAY WHAT THE WORKSPACE IS DOING. The `state` is the one computed
 * value — a sentence and at most one action, with no destructive verb in its type — and it travels
 * on the channel for the same reason the address does. THE HANDLERS TRAVEL WITH IT because they
 * are the publisher's, exactly as the reclaim request's are: a shell that re-derived them would be
 * a second authority on a question that already has one. `null` MEANS NOBODY HAS COMPUTED ONE, and
 * the pane then renders nothing.
 */
export interface WorkspaceReport {
  state: WorkspaceState
  /** The project the state describes. `null` while a route is still resolving one. */
  projectId: string | null
  /** Record how a start attempt ended; `null` clears it (a start that reached the app). */
  onStartOutcome: (outcome: StartOutcome | null) => void
  /**
   * A PRESS HAS BEGUN, OR FINISHED — and the pane needs to know before the server does.
   *
   * The server's `starting` state is the honest answer and it arrives on the NEXT read, which is up
   * to a full poll cadence away. Without this the pane went on saying "Your app is saved." for that
   * whole window after somebody pressed the button: true, but not an acknowledgement, and the only
   * feedback was a spinner inside the control itself.
   */
  onStartPending: (pending: boolean) => void
  /**
   * THE URL A SUCCESSFUL START JUST PRODUCED — and the publisher decides what to do with it.
   *
   * Without it a start inside a Build chat has no arm of the address resolver it can populate, and
   * the app comes up in a container nothing frames. `previewAddress.ts`'s relaunched arm is its
   * home: a restore has no build lifecycle, which is why that arm resolves straight to `ready`.
   */
  onStarted: (previewUrl: string) => void
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
  /** One save-state reading — see `SaveReading`. `NO_SAVE_READING` means nobody has reported. */
  saveReading: Cell<SaveReading>
  rail: Cell<RailSlot>
  /** What the toolbar row NAMES. Published by the routes — see `WorkspaceHeading`. */
  heading: Cell<WorkspaceHeading>
  /** The save control's values. Its ACTION is the next cell, deliberately. */
  save: Cell<SaveSlot>
  /**
   * THE ROW'S ACTIONS, AND NOTHING SUBSCRIBES TO THEM. Republished on every render of whichever
   * surface owns them, compared by identity, and read imperatively by the row at press time. That
   * is what makes a changing handler free: it wakes nobody, and it can never be stale, because the
   * read happens after the press rather than during a render.
   */
  actions: Cell<WorkspaceActions>
  /** What to SAY about the workspace, and the handlers for the one thing that may be pressed. */
  workspace: Cell<WorkspaceReport | null>
}

/**
 * Two reports that would render identically — SO THE HANDLERS ARE DELIBERATELY NOT COMPARED. The
 * subscriber is the shell itself and both publishers hand it a fresh object every render, so under
 * `Object.is` every keystroke woke the whole page chrome. Holding older closures is sound only
 * while they are interchangeable: every handler at both call sites is a `useState` setter, a
 * `useCallback([])`, or an arrow over a ref and `projectId`, which IS compared. A HANDLER THAT
 * CLOSES OVER RENDER STATE GOES WRONG SILENTLY — read it through a ref, or grow this comparator.
 */
const sameReport = (a: WorkspaceReport | null, b: WorkspaceReport | null): boolean =>
  a === b || (a !== null && b !== null && a.projectId === b.projectId && sameWorkspaceState(a.state, b.state))

export function createWorkspaceChannel(): WorkspaceChannel {
  return {
    address: createCell<WorkspaceAddress>(NO_ADDRESS, sameAddress),
    project: createCell<string | null>(null),
    pane: createCell<PaneView | null>(null),
    visible: createCell<boolean>(false),
    reclaim: createCell<ReclaimRequest | null>(null),
    saveReading: createCell<SaveReading>(NO_SAVE_READING, sameReading),
    rail: createCell<RailSlot>(NO_RAIL, sameRail),
    heading: createCell<WorkspaceHeading>(NO_HEADING, sameHeading),
    save: createCell<SaveSlot>(NO_SAVE, sameSave),
    actions: createCell<WorkspaceActions>(NO_ACTIONS),
    workspace: createCell<WorkspaceReport | null>(null, sameReport),
  }
}

/**
 * `null` outside a shell, and that is not an error condition.
 *
 * Every publisher below no-ops when there is no channel, because the surfaces are mounted without
 * a shell in fifteen existing test suites and could legitimately be rendered anywhere. A surface
 * that cannot reach a pane simply does not get one; it must never throw, because the thing it
 * would take down is the conversation.
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
 * WHAT THE PANE SHOULD FRAME, with a stale address already discarded — the held address stops
 * being this workspace's when a surface declares a different project.
 *
 * `null` IS NOT A DIFFERENT PROJECT. A surface that has not resolved its project yet — every cold
 * open of a chat address, since `ChatRoute` learns the project from a fetch — claims nothing, and
 * a claim of nothing must not tear down a running app.
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

/**
 * THE WHOLE READING, not just its tri-state. `dirty` is `true` definitely dirty, `false`
 * definitely clean, `null` "could not tell"; `recoveryAt` says whether the platform is holding a
 * copy it can put back. Both, from one read — see `SaveReading` for why they are never separated.
 */
export function useWorkspaceSaveState(): SaveReading {
  return useCell(useWorkspaceChannel()?.saveReading, NO_SAVE_READING)
}

export function useRailSlot(): RailSlot {
  return useCell(useWorkspaceChannel()?.rail, NO_RAIL)
}

/** What the workspace is doing, and what may be pressed about it. `null` = nobody has said. */
export function useWorkspaceReport(): WorkspaceReport | null {
  return useCell(useWorkspaceChannel()?.workspace, null)
}

/** What the toolbar row names. Every field is independently nullable — see `WorkspaceHeading`. */
export function useWorkspaceHeading(): WorkspaceHeading {
  return useCell(useWorkspaceChannel()?.heading, NO_HEADING)
}

/** The save control's VALUES. Its action is read at press time — see `useWorkspaceActions`. */
export function useWorkspaceSave(): SaveSlot {
  return useCell(useWorkspaceChannel()?.save, NO_SAVE)
}

/**
 * A READER, NOT A VALUE — and that is the whole design of this pair.
 *
 * The returned function reads the currently published handlers when it is CALLED, which is after a
 * press. So the row never re-renders because a handler's identity changed, and it can never hold a
 * closure from an earlier render: there is no render in between the read and the call.
 */
export function useWorkspaceActions(): () => WorkspaceActions {
  const channel = useWorkspaceChannel()
  return () => channel?.actions.get() ?? NO_ACTIONS
}

// ─── Publishing: what a mounted surface says upward ────────────────────────────────────────────
//
// WHETHER A PAYLOAD IS CLEARED WHEN ITS PUBLISHER UNMOUNTS IS A PER-PAYLOAD DECISION, and each
// one has a different reason. Getting this uniform in either direction breaks something:
//
//   address    KEPT     — the router unmounts the conversation on a move to the project screen,
//                         and clearing here would destroy the running app with it. Bounded by
//                         the project instead (see `useWorkspaceAddress`). This is also why
//                         LIVENESS belongs on this cell rather than on the pane view: a fact that
//                         decides whether the frame stays mounted has to survive the same leave
//                         the URL does, or the two would disagree on exactly this transition.
//   project    KEPT     — the cell must not go blank between an unmounting surface and the one
//                         replacing it, because the next publisher's address is judged against
//                         it. Note what KEPT does NOT buy: after a move to a surface that
//                         declares nothing, the cell still names the departed project, so
//                         `belongsElsewhere` cannot fire. Every surface the shell mounts
//                         declares one for exactly that reason, and any surface that SHOWS
//                         the pane must keep doing so — declaring the project
//                         before publishing an address is what stops it framing the previous
//                         project's app with nothing able to detect it.
//   pane       CLEARED  — chrome and props belonging to a surface that is gone. The frame needs
//                         only the address to keep running, so dropping these costs nothing and
//                         keeping them would render a departed conversation's toolbar.
//   visible    CLEARED  — a surface that is gone is not asking for anything to be shown.
//   reclaim    CLEARED  — its buttons close over the publisher's own save/release/retry handlers.
//                         A dialog left standing after they died is a dialog whose buttons do
//                         nothing, which is precisely the dead end the reclaim flow exists to
//                         remove.
//   saveReading KEPT    — the unsaved work is in the CONTAINER, not in the component. Clearing on
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
 * Name the workspace for the toolbar row. PUBLISHED BY THE ROUTE, not by the surface below it —
 * see `WorkspaceHeading`.
 *
 * CLEARED ON UNMOUNT, unlike `project` and `saveReading`. A heading describes an ADDRESS, and the two
 * routes that publish one swap within a single commit, so there is no frame in which the row is
 * blank. Keeping it would leave a chat's title standing over the project screen while it loads.
 */
export function usePublishHeading(heading: WorkspaceHeading): void {
  usePublish(useWorkspaceChannel()?.heading, heading, NO_HEADING)
}

/**
 * Publish the save control's values, and its action.
 *
 * BOTH ARE CLEARED ON UNMOUNT, for the same reason the reclaim request is: the action closes over
 * the publisher's own session, and a Save button left standing after that publisher died does
 * nothing. The reading on the SEPARATE `saveReading` cell is the one that is KEPT.
 */
export function usePublishSave(save: Omit<SaveSlot, 'canSave'>, actions: WorkspaceActions): void {
  // A FRESH OBJECT EVERY RENDER IS FREE HERE — the cell is value-compared, so an unchanged save
  // state wakes nobody however many times it is republished.
  usePublish(useWorkspaceChannel()?.save, { ...save, canSave: actions.save !== null }, NO_SAVE)
  usePublish(useWorkspaceChannel()?.actions, actions, NO_ACTIONS)
}

/**
 * Publish what to frame. Survives this surface's unmount — see the table above.
 * "I HAVE NOTHING YET" IS NOT "THERE IS NOTHING". A surface's address arms are cold on its first
 * commit and `usePublish` runs on every render, so without this rule that commit would retire the
 * held address; a publisher that has said nothing yet abstains. THE RETIRE PATH STAYS OPEN — once
 * it HAS resolved a URL or a status, every later publish lands, `{url: null}` included. A ref
 * assigned during render, because the layout effect reads it on that same first commit.
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
    { url: address.url, status: address.status, serving: address.serving, projectId },
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
 * The pane ELEMENT is rendered by the address; what a citizen SEES is decided by the mounted
 * surface declaring it here, and exactly one surface does that declaring.
 */
export function useAppPaneVisible(visible: boolean): void {
  usePublish(useWorkspaceChannel()?.visible, visible, false)
}

/** Publish the reclaim dialog's open state. The CLASSIFICATION stays with its publisher. */
export function usePublishReclaim(request: ReclaimRequest | null): void {
  usePublish(useWorkspaceChannel()?.reclaim, request, null)
}

/**
 * Publish ONE save-state reading. Survives this surface's unmount — see the table above.
 *
 * ADDS NO PRODUCER AND NO TRAFFIC. Whoever calls this already knows the answer; the shell reads
 * whatever was last published and treats "nobody has published" as `NO_SAVE_READING`. A project
 * screen with no conversation mounted therefore costs no container round trip and warns about
 * nothing.
 *
 * TAKES THE PAIR, NOT A FLAG PLUS AN OPTIONAL EXTRA. The two facts have to reach the cell in one
 * `set` for a consumer to be allowed to reason across them — `SaveReading` records why — so the
 * call site names both or it does not compile. A FRESH OBJECT EVERY RENDER IS FREE: the cell is
 * value-compared, so an unchanged reading wakes nobody however often it is republished.
 */
export function usePublishSaveState(reading: SaveReading): void {
  usePublish(useWorkspaceChannel()?.saveReading, reading)
}


/**
 * Publish what to say about the workspace. CLEARED ON UNMOUNT, like the pane view and for the same
 * reason: its handlers close over the departing surface's own read, its outcome slot and its
 * refusal routing, so a state left standing would render a sentence whose one button calls into a
 * component that no longer exists.
 */
export function usePublishWorkspaceReport(report: WorkspaceReport | null): void {
  usePublish(useWorkspaceChannel()?.workspace, report, null)
}
