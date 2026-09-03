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
 *  2. the pane's view — visibility and the pane's own pass-through props
 *  3. the reclaim dialog's open state
 *  4. the tri-state save state
 *  5. the app-revealed callback (R104's stop-clock)
 *  6. the rail mode, its collapse, and an opaque per-mode bag that outlives a chat
 *  7. what to SAY about the workspace — one computed value, and the handlers for its one action
 *  8. what the toolbar row NAMES, and the save control's values and its action (plan 002, U2)
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
 * PANE's re-render with a shallow comparator — its view is rebuilt by identity every render, with
 * fresh handler closures on it, so a comparator would buy nothing without memoising those too, and
 * that is a behaviour change this refactor is not making.
 *
 * THAT WARNING IS ABOUT THE PANE, AND ONLY THE PANE. `address`, `rail` and `workspace` are all
 * value-compared, because each carries plain data (plus, for `workspace`, handlers that are
 * provably interchangeable — see `sameReport`, which states the rule that keeps them so).
 *
 * The context carries the CHANNEL HANDLE, which is created once and never replaced. That handle is
 * stable for the life of the shell, so the context itself never re-renders anybody.
 */
import { createContext, useContext, useLayoutEffect, useRef, useSyncExternalStore, type ComponentProps } from 'react'
// TYPE-ONLY, so this stays a leaf at runtime: the import is erased and the channel keeps no
// dependency on the component it describes.
import type LivePreview from '../LivePreview'
import type { PreviewAddress } from '../../utils/previewAddress'
import type { CompileState } from '../../utils/compileState'
import type { PreviewLifeState, ReclaimBlocked } from '../../utils/buildSessionApi'
import type { RelaunchError } from '../../utils/buildSessionTypes'
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
  /* NO TOOLBAR SLOTS ANY MORE (plan 002, U2). `toolbarLeading` and `toolbarTrailing` existed so a
     surface could push chrome into the row `LivePreview` drew inside the pane. That row is gone —
     the boards draw one toolbar for the whole workspace, above both columns — so the slots have no
     row to fill and are removed rather than left pointing at nothing. Their two occupants (the
     publish chip and, before it, a chat-panel toggle) are drawn by the row itself.

     THE SAVE MODEL LEFT WITH THEM, for the same reason: Save is in the row now, reading the
     channel's own `save` cell. Keeping a second copy here would give one control two publishers
     that could disagree — and the pane spread would silently drop it, since JSX spread attributes
     are exempt from excess-property checking. `UnacceptedPaneProps` below is what caught exactly
     that when this field list was first trimmed. */
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
  /**
   * The project being STARTED — issue #161's framing half. The refusal carries only the incumbent,
   * so the name of the app the person is actually trying to open has to travel with the request:
   * the dialog leads with it, because "can I build THIS one?" is the question being asked.
   */
  startingProjectName: string | null
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
 * WHAT THE TOOLBAR ROW NAMES — the heading half (plan 002, U2).
 *
 * ═══ WHY THIS IS ITS OWN CELL, WHICH THE PLAN ASKED TO HAVE RECORDED ═══
 *
 * The row could have read the `pane` cell, which already carries chrome. It must not, for two
 * reasons that are both about the wrong lifetime rather than about tidiness:
 *
 *  1. THE PANE CELL IS REPUBLISHED ON EVERY KEYSTROKE. Its publisher is the conversation surface,
 *     which re-renders per character typed in the composer, and the cell holds React elements that
 *     cannot be value-compared. The row would re-render with the composer.
 *  2. THE PANE CELL IS CLEARED TO NOTHING ON UNMOUNT. The row has to name the project on a screen
 *     where no conversation is mounted at all.
 *
 * ═══ AND WHY THE ROUTES PUBLISH IT, NOT THE SURFACES ═══
 *
 * `ProjectPage` and `ChatRoute` are mounted for the whole life of an address INCLUDING their
 * loading and load-error branches; the surfaces below them are not. A cold open of `/chat/{id}`
 * spends its first frames with no conversation and no project resolved, and the row still has to
 * render its back control and hold its own height rather than appearing once the fetches land.
 * That is the "renders without a flash of empty space or a layout shift" property, and it is a
 * consequence of WHERE this is published rather than of anything the row does.
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
 * THE SAVE HALF OF THE ROW — its VALUES only. The action lives in `saveAction`, and the split is
 * the point.
 *
 * A handler on a value-compared cell is the hazard `sameReport` had to write a paragraph of rules
 * around: skip it in the comparator and a stale closure survives, compare it and every render of
 * the publisher wakes the subscriber. The row needs neither. It needs the latest handler AT THE
 * MOMENT OF A PRESS, which is not a render-time need at all — so the handler goes in its own cell
 * that nothing subscribes to and the row reads imperatively inside its `onClick`.
 */
export interface SaveSlot {
  /** TRI-STATE. `true` definitely dirty, `false` definitely clean, `null` "could not tell". */
  dirty: boolean | null
  saving: boolean
  error: string | null
  /**
   * WHETHER AN ACTION IS PUBLISHED AT ALL — derived from `saveAction` by `usePublishSave`, never
   * passed separately, so the two cannot disagree.
   *
   * The row needs this at RENDER time and the action itself only at press time, which is why one
   * is a compared value here and the other is a cell nothing subscribes to. Without it the row
   * cannot tell a pressable control from a status, and today's project screen — whose surface
   * deliberately publishes no `onSave` — would draw a button that does nothing.
   */
  canSave: boolean
}

export const NO_SAVE: SaveSlot = { dirty: null, saving: false, error: null, canSave: false }

/**
 * THE ROW'S HANDLERS, held apart from every compared value on purpose.
 *
 * Both are things a citizen PRESSES, so neither is needed at render time — which is what lets them
 * live in a cell nothing subscribes to. `rename` is here because the rail's header, which used to
 * own it, is replaced by the board's three sections; the capability had to move rather than be
 * dropped, and the row is where its name now lives.
 */
export interface WorkspaceActions {
  save: (() => void) | null
  rename: (() => void) | null
}

export const NO_ACTIONS: WorkspaceActions = { save: null, rename: null }

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

const sameHeading = (a: WorkspaceHeading, b: WorkspaceHeading) =>
  a.projectId === b.projectId &&
  a.projectName === b.projectName &&
  a.chatTitle === b.chatTitle &&
  a.chatKind === b.chatKind

const sameSave = (a: SaveSlot, b: SaveSlot) =>
  a.dirty === b.dirty && a.saving === b.saving && a.error === b.error && a.canSave === b.canSave

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
   * Without this, pressing the start control inside a Build chat did nothing visible: that surface
   * feeds the resolver's project-scoped arm with `null` (its own poll only runs over a framed URL),
   * and its `relaunchedUrl` arm was fed by a Relaunch button this plan retired — so the address had
   * no arm left that a fresh start could populate, and the app came up in a container nothing
   * framed. `previewAddress.ts`'s relaunched arm is exactly the right home for it: a restore has no
   * build lifecycle at all, which is why that arm resolves its own status to `ready`.
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
  /** `null` means NOBODY HAS REPORTED, which is the same "could not tell" as a failed check. */
  saveDirty: Cell<boolean | null>
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
 * Two reports that would render identically — SO THE HANDLERS ARE DELIBERATELY NOT COMPARED, and
 * that is the whole of the risk in this function.
 *
 * WHY IT HAS TO EXIST. This cell's subscriber is `WorkspaceShell` itself, so a publish re-renders
 * the shell, the navbar and both columns. Its two publishers both hand it a FRESH OBJECT on every
 * render — one an inline literal, the other a `useMemo` keyed on a value that is itself rebuilt
 * each call — so under `Object.is` every keystroke in a composer and every streamed frame woke the
 * entire page chrome. This is the same treatment `sameAddress` and `sameRail` already get, applied
 * to the cell with the widest blast radius of the three.
 *
 * WHAT MAKES SKIPPING THE HANDLERS SAFE, AND THE RULE A FUTURE EDITOR MUST KEEP. Holding the older
 * closures is only sound while they are interchangeable with the newer ones. Every handler at both
 * call sites is either a `useState` setter, a `useCallback([])`, or an arrow that touches nothing
 * but a ref, a functional `setState`, and `projectId` — which IS compared. None of them reads a
 * render-scoped value, so an older copy does exactly what a newer one would.
 *
 * PUBLISH A HANDLER THAT CLOSES OVER RENDER STATE AND THIS GOES WRONG SILENTLY: the pane would go
 * on calling a closure from an earlier render for as long as the state and project held still.
 * Such a handler must read that value through a ref, or this comparator must grow to compare it.
 * The narrower fix — memoising both publishers' objects — was not taken because it leaves the
 * default `Object.is` in place, so the next publisher added is one unmemoised literal away from
 * restoring the whole cost.
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
    saveDirty: createCell<boolean | null>(null),
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

/** What the toolbar row names. Every field is independently nullable — see `WorkspaceHeading`. */
export function useWorkspaceHeading(): WorkspaceHeading {
  return useCell(useWorkspaceChannel()?.heading, NO_HEADING)
}

/** The save control's VALUES. Its action is read at press time — see `useSaveAction`. */
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
 * Name the workspace for the toolbar row. PUBLISHED BY THE ROUTE, not by the surface below it —
 * see `WorkspaceHeading` for why that is what stops a cold open rendering an empty row.
 *
 * CLEARED ON UNMOUNT, unlike `project` and `saveDirty`. A heading describes an ADDRESS, and the two
 * routes that publish one swap in the same commit — the departing route's cleanup and the arriving
 * route's publish are both layout effects of that commit — so there is no frame in which the row is
 * blank. Keeping it instead would leave a chat's title standing over the project screen for as long
 * as `ProjectPage` spent loading, which is the one case this cell exists to get right.
 */
export function usePublishHeading(heading: WorkspaceHeading): void {
  usePublish(useWorkspaceChannel()?.heading, heading, NO_HEADING)
}

/**
 * Publish the save control's values, and its action.
 *
 * BOTH ARE CLEARED ON UNMOUNT, and for the same reason the reclaim request is: the action closes
 * over the publisher's own session, and a Save button left standing after that publisher died is a
 * button that does nothing. The tri-state on the SEPARATE `saveDirty` cell is the one that is KEPT,
 * because the unsaved work is in the container rather than in the component and the unload warning
 * has to stay armed across a navigation.
 */
export function usePublishSave(save: Omit<SaveSlot, 'canSave'>, actions: WorkspaceActions): void {
  // A FRESH OBJECT EVERY RENDER IS FREE HERE — the cell is value-compared, so an unchanged save
  // state wakes nobody however many times it is republished.
  usePublish(useWorkspaceChannel()?.save, { ...save, canSave: actions.save !== null }, NO_SAVE)
  usePublish(useWorkspaceChannel()?.actions, actions, NO_ACTIONS)
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
