/**
 * WHAT GETS FRAMED, decided once. The app pane is rendered by the address, not the route: the
 * shell mounts one iframe for the whole workspace and the element exists whenever this module
 * returns a URL, which only works if the decision is made from ABOVE the chat. PURE — signals in,
 * an address and a status out; no hooks, fetches or refs. `null` when no source qualifies, never a
 * fallback: a pane framing nothing is correct, one framing the wrong app is not.
 *
 * THE PRECEDENCE, AND TWO PREDICATES THAT ARE NOT THE SAME PREDICATE:
 *   1. the live turn's preview      — CHAT-scoped   (`narratingChatIsOpenChat`)
 *   2. the project's live preview   — PROJECT-scoped, ranked last
 *
 * WHY THIS EXISTS — THE ASYMMETRY IS LOAD-BEARING, not a tidy-up target. The turn arm is gated by
 * the chat predicate ALONE, the project arm by the project predicate alone. Merging them into
 * one "is this ours" test breaks both directions at once: it stops a live turn framing in the case
 * that matters most (a chat whose project the page was never stamped with), and it lets one
 * project's build frame into another project's pane. `previewAddress.test.ts` and
 * `ConversationSurface-previewaddress.test.tsx` each carry a scenario, and
 * `ConversationSurface-session.test.jsx` pins the second caller's precedence over a transcript's
 * own terminal read.
 *
 * A THIRD SCOPE IS NAMED HERE AND KEPT OUT: APP-scoped facts (the compile state, whether the
 * workspace was lost) are about the project's ONE app and deliberately NOT narrowed to the open
 * chat, since their producer outlives the turn — blanking them on a chat switch leaves an error
 * screen uncovered. They stay ordinary pass-through props with their reasons beside them: the
 * app-scoped facts answer *what to say about the app* rather than *what to frame*, and pulling
 * them in here "for consistency" is how the compile signal gets narrowed to a chat.
 *
 * WHAT THIS MODULE WILL NOT DO: it is PURE — identities and raw signals in, an address, a status
 * and its liveness out. No hooks, no fetches, no refs (a ref's current value is passed as an
 * argument, never read in here).
 */
import type { BuildSessionStatus } from './buildSessionTypes'

export interface PreviewAddressInputs {
  // ── chat-scoped ───────────────────────────────────────────────────────────────────────────
  /** The URL the live turn last named, whichever chat it was narrating. */
  turnPreviewUrl: string | null
  /**
   * The live turn's build status — top of the STATUS precedence, deliberately independent of
   * which arm won the URL: a provisioning build has a status and no URL yet, which is what
   * renders the loading state instead of an empty pane.
   *
   * Gated by the chat predicate IN HERE even though its only caller hands it in already gated —
   * an arm carries its predicate INTO the module rather than relying on the caller having
   * derived one above the JSX, because a gate that depends on where it was declared is one
   * reorder away from silently opening.
   */
  turnStatus: BuildSessionStatus | null
  /** THE CHAT PREDICATE. Is the turn that produced the signals above narrating the OPEN chat? */
  narratingChatIsOpenChat: boolean

  // ── project-scoped ────────────────────────────────────────────────────────────────────────
  /**
   * The project's own live preview, from the preview-state read (`alive`: "a container is
   * serving this project; `previewUrl` is framable" — `buildSessionApi.ts`). RANKED LAST, and
   * the only arm that needs no chat: the turn arm above it needs a live turn. It is also where a
   * started app arrives — the start answers with no address, and this read is the one that has it.
   *
   * IT HAS TWO CALLERS. `components/workspace/ProjectWorkspace.tsx` is the project-scoped
   * publisher this arm was written for, and `components/chat/ConversationSurface.tsx` joined it:
   * a chat opened cold — a hard load, a bookmark, a browser restart — has neither of those, so it
   * needs the same arm or it says the app is running over an empty frame. Both feed only the
   * `alive` case, the one state whose `previewUrl` the wire's own contract calls framable. Until
   * the first caller landed, this arm had no caller at all and the bare project screen published
   * nothing, so the pane host hit its "no pane and no address" early return and rendered nothing
   * on a fresh load.
   *
   * THE PRECEDENCE BELOW IS LOAD-BEARING FOR THE SECOND CALLER: this arm outranks
   * `transcriptHasBuildOutcome ? 'ended'`, so once the chat route feeds it, a transcript that
   * ended is no longer allowed to declare the preview gone while a container is demonstrably
   * serving the project.
   */
  projectPreviewUrl: string | null
  /** THE PROJECT PREDICATE. Do the project-scoped signals above belong to the OPEN project? */
  belongsToOpenProject: boolean

  // ── transcript-derived ────────────────────────────────────────────────────────────────────
  /**
   * The open chat's transcript records a build that finished. The BOTTOM of the status
   * precedence and nothing more: it says a build once ran here, so a reloaded tab shows the
   * terminal placeholder and its Relaunch rather than the idle "submit a prompt" empty state.
   * It never contributes a URL — a persisted outcome's URL names a container that is long gone.
   */
  transcriptHasBuildOutcome: boolean
}

export interface PreviewAddress {
  /** What to frame, or `null` for "nothing qualifies" — never a guess and never a fallback. */
  url: string | null
  /**
   * What the pane should say about it. `null` means nothing is framed and nothing ended — it is
   * the idle state, and it must never be read as a terminal.
   */
  status: BuildSessionStatus | null
  /**
   * IS A CONTAINER STILL SERVING WHAT `url` NAMES? The whole of what `completedLive` used to be,
   * renamed to the question it actually answers and moved onto the address.
   *
   * ITS ONE JOB is to outrank a terminal `status`. A turn ending does not take the app down — the backend pardons the container unconditionally — so `ended` plus a serving
   * container means "the build is over and your app is still there", and the pane keeps framing it
   * instead of collapsing to "The preview is no longer running". Without this, pressing Stop, or
   * simply sending a second message after a build, pulled a running app off the screen.
   *
   * IT MAKES NO CLAIM ABOUT THE BUILD. "Alive" and "worth framing" are two questions and they stay
   * two: this one is answered by what is serving, and what the pane is allowed to SAY about the
   * newest build is answered by the compile state, whose `unknown` asserts nothing in either
   * direction. Reading this as "the build succeeded" is the exact conflation that put an unearned
   * "Build complete" on a screen where no build ever ran.
   */
  serving: boolean
}

/**
 * Resolve what the app pane frames, and what it says about it.
 *
 * The two results are computed independently on purpose, and that is not an oversight to be
 * refactored away: a build that is provisioning has a status and no URL (which is the loading
 * state), and a build that ended still has a status after its URL has stopped qualifying (which
 * is the terminal placeholder). Tying the status to whichever arm won the URL collapses both.
 */
export function resolvePreviewAddress(inputs: PreviewAddressInputs): PreviewAddress {
  const {
    turnPreviewUrl, turnStatus, narratingChatIsOpenChat,
    projectPreviewUrl, belongsToOpenProject,
    transcriptHasBuildOutcome,
  } = inputs

  // The chat predicate, and ONLY the chat predicate. See the asymmetry note above.
  const fromTurn = narratingChatIsOpenChat ? turnPreviewUrl : null
  // The project predicate, and only it.
  const fromProject = belongsToOpenProject ? projectPreviewUrl : null

  const url = fromTurn ?? fromProject ?? null

  // A live turn's own status outranks everything — it is the only source describing what is
  // happening RIGHT NOW. Below it, the project arm has no lifecycle of its own and resolves to
  // `ready` because that is what it is: an app that is up.
  const status =
    (narratingChatIsOpenChat ? turnStatus : null) ??
    (fromProject ? 'ready' : null) ??
    (transcriptHasBuildOutcome ? 'ended' : null)

  // LIVENESS, AND WHY IT IS TWO SOURCES RATHER THAN ONE.
  //
  // The preview-state read is the BEST authority — it asks the server what is actually serving
  // this project, independent of any turn's history — but it is not the only one, because it is a
  // poll and a poll has not always answered yet. The disjunct beside it covers the moment it has
  // not: the instant a turn ends over a live preview. That is a fact this render already holds,
  // and dropping it would make a citizen watch their app disappear for one poll interval every
  // time a build finished.
  //
  // A TURN THAT PUBLISHED A PREVIEW COUNTS AS SERVING UNLESS IT FAILED, and that clause covers
  // two separate moments.
  //
  //   HOWEVER THE TURN ENDED — `turnStatus` is `'ended'` for a turn that COMPLETED and for one
  //           the citizen STOPPED, because the backend pardons the container either way. It is
  //           `'failed'` only for a turn that genuinely failed or lost its workspace. So the
  //           pardon rides on the phase rather than on a terminal reason string this module
  //           never sees.
  //   THE NEXT MESSAGE — the moment a citizen SENDS a second message, the surface resets the
  //           turn narrative and `turnStatus` drops to `null` while `turnPreviewUrl` keeps the
  //           URL the last `preview_ready` named. Requiring `'ended'` here would make liveness
  //           blink off for exactly that render — and the status, falling through to the
  //           transcript's own `'ended'`, would collapse `frameContext` and REMOUNT the iframe.
  //           That is the reload on every message: the app re-requests its document and throws
  //           away the citizen's form entries, their scroll position and their selected tab.
  //
  // SO `null` IS TREATED AS "STILL SERVING", AND THAT IS NOT A GUESS. `fromTurn` is non-null only
  // because a turn told this tab, on this chat, that an app was answering at that URL, and the
  // container behind it is not torn down by anything a turn does. `failed` is the one phase that
  // says otherwise, and it is excluded. A page that RELOADS starts with no turn preview at all, so
  // none of this can resurrect a stale claim — the terminal placeholder still wins there, which is
  // the `framedStatus` lesson this module already keeps.
  //
  // NO URL, NOTHING SERVING. Liveness describes what is framed; with nothing framed it is not
  // "false because the app is down", it is simply not a question, and `false` is the answer that
  // makes every reader (`keepFramed`, the terminal placeholder) behave as it did before.
  //
  // AND THE PROBE NEEDS NO INPUT OF ITS OWN. `fromProject` is already exactly "the preview-state
  // read answered `alive` for THIS project" — the arm's own docblock quotes the wire contract
  // saying so — and it already carries the project predicate. A second `containerAlive: boolean`
  // beside it would be the same fact spelled twice, from the same read, with nothing forcing the
  // two spellings to agree: a caller could feed a URL on one and `false` on the other and the
  // resolver would believe both. One expression, one source.
  //
  // Note it is `fromProject`, NOT "fromProject won the URL". A live turn's preview outranks it for
  // what to FRAME while describing the same container, so a project that is demonstrably serving
  // says so whichever arm supplied the address.
  const serving =
    url !== null && (fromProject !== null || (fromTurn !== null && turnStatus !== 'failed'))

  return { url, status, serving }
}
