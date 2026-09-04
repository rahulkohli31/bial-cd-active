/**
 * WHAT GETS FRAMED, decided once (Plan A, U2).
 *
 * The app pane is about to stop being rendered by the route and start being rendered by the
 * address: the shell mounts one iframe for the whole workspace, and the *element exists* whenever
 * this module returns a URL. That only works if the decision can be made from ABOVE the chat —
 * which is precisely what the builder page's render body could not do, because the precedence was
 * spelled inline at the framing sites and its two scoping predicates were free variables derived
 * further up the same function.
 *
 * ═══ THE PRECEDENCE, AND THE TWO PREDICATES THAT ARE NOT THE SAME PREDICATE ═══
 *
 *   1. the live turn's preview      — CHAT-scoped   (`narratingChatIsOpenChat`)
 *   2. a relaunched URL             — PROJECT-scoped
 *   3. the live session's URL       — PROJECT-scoped, and additionally needs a session to exist
 *   4. the project's live preview   — PROJECT-scoped, ranked last
 *
 * THE ASYMMETRY IS LOAD-BEARING AND IT IS NOT A TIDY-UP TARGET. The turn arm is gated by the chat
 * predicate ALONE; the three below it are gated by the project predicate alone. Merging the two
 * into one "is this ours" test breaks it in both directions at once: it stops a live turn framing
 * in the one case that matters most (the citizen watching their build in a chat whose project the
 * page's session was never stamped with), and it lets one project's build frame into another
 * project's pane. `previewAddress.test.ts` has a scenario for each half, and
 * `ConversationSurface-previewaddress.test.tsx` has the same asymmetry at the page level.
 *
 * ═══ THREE SCOPES, AND ONLY TWO OF THEM LIVE HERE ═══
 *
 * There are three scopes in play on this pane, not two:
 *
 *  - CHAT-scoped   — the live turn's preview and its reconnecting flag. About the open conversation.
 *  - PROJECT-scoped — the relaunched URL, the session's URL, whether a turn is running anywhere in
 *                     this project. About the project, not the chat.
 *  - APP-scoped    — the compile state, and whether the workspace was lost. Facts about the
 *                     project's ONE app, deliberately NOT narrowed to the open conversation,
 *                     because their producer outlives the turn and blanking them on a chat switch
 *                     is what leaves an error screen uncovered.
 *
 * The third scope is named here so it is documented somewhere, and then deliberately kept OUT of
 * this module: the app-scoped facts are not address sources, they answer *what to say about the
 * app* rather than *what to frame*, and pulling them in "for consistency" is how the compile signal
 * gets narrowed to a chat. They stay ordinary pass-through props with their reasons beside them.
 *
 * ═══ WHAT THIS MODULE WILL NOT DO ═══
 *
 * It is PURE — identities and raw signals in, an address and a status out. No hooks, no fetches,
 * no refs (a ref's current value is passed as an argument, never read in here). It returns `null`
 * for the address when no source qualifies: it never invents a fallback and it never widens a
 * scope to produce one. A pane framing nothing is a correct answer; a pane framing the wrong app
 * is not.
 */
import type { BuildSessionStatus } from './buildSessionTypes'

export interface PreviewAddressInputs {
  // ── chat-scoped ───────────────────────────────────────────────────────────────────────────
  /** The URL the live turn last named, whichever chat it was narrating. */
  turnPreviewUrl: string | null
  /**
   * The live turn's build status — the top of the STATUS precedence, and deliberately independent
   * of which arm won the URL: a build that is provisioning has a status and no URL yet, and that
   * pair is what renders the loading state instead of an empty pane.
   *
   * Gated by the chat predicate in here even though its only caller today hands it in already
   * gated. The point of this module is that an arm carries its predicate INTO it rather than
   * relying on the caller having derived one above the JSX — a gate that depends on where it was
   * declared is one reorder away from silently opening, which is the note the builder page's own
   * comments already carry about this exact pair of predicates.
   */
  turnStatus: BuildSessionStatus | null
  /** THE CHAT PREDICATE. Is the turn that produced the signals above narrating the OPEN chat? */
  narratingChatIsOpenChat: boolean

  // ── project-scoped ────────────────────────────────────────────────────────────────────────
  /**
   * A restored app (#43). It has no build lifecycle at all — no feed, no keep-alive, no lock —
   * which is why it resolves the status to `ready` on its own rather than reading one.
   */
  relaunchedUrl: string | null
  /** The live session's framed URL, and its status. Both additionally require `sessionId`. */
  sessionUrl: string | null
  sessionStatus: BuildSessionStatus | null
  /**
   * The live session's id, or `null` when this page owns no session.
   *
   * A SEPARATE INPUT FROM THE PROJECT PREDICATE, because the two lower arms do not gate the same
   * way and a merge would lose the difference: a relaunch resolves on the project predicate alone
   * (there may be no session at all — that is the ordinary "come back later" case), while the
   * session's URL and status additionally need a session to exist.
   */
  sessionId: string | null
  /**
   * The project's own live preview, from the preview-state read, whose contract says exactly when
   * this is framable: `alive` — "a container is serving this project; `previewUrl` is framable"
   * (`buildSessionApi.ts`). RANKED LAST, and it is the only arm that does not require a chat:
   * the three above it all need a live turn, a relaunch performed in this session, or a session.
   * At a bare project address on a fresh load there is none of that, so without this arm the
   * project screen frames nothing.
   *
   * ITS CALLER EXISTS. `components/workspace/ProjectWorkspace.tsx` is the project-scoped publisher
   * this arm was written for, and it feeds only the `alive` case — the one state whose `previewUrl`
   * the wire's own contract calls framable. Until it landed, this arm had no caller at all and the
   * bare project screen published nothing, so the pane host hit its "no pane and no address" early
   * return and rendered nothing on a fresh load.
   */
  projectPreviewUrl: string | null
  /** THE PROJECT PREDICATE. Do the project-scoped signals above belong to the OPEN project? */
  sessionBelongsToOpenProject: boolean

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
}

/**
 * Resolve what the app pane frames, and what it says about it.
 *
 * The two results are computed independently on purpose, and that is not an oversight to be
 * refactored away: a build that is provisioning has a status and no URL (which is the loading
 * state), and a session that ended still has a status after its URL has stopped qualifying (which
 * is the terminal placeholder). Tying the status to whichever arm won the URL collapses both.
 */
export function resolvePreviewAddress(inputs: PreviewAddressInputs): PreviewAddress {
  const {
    turnPreviewUrl, turnStatus, narratingChatIsOpenChat,
    relaunchedUrl, sessionUrl, sessionStatus, sessionId, projectPreviewUrl,
    sessionBelongsToOpenProject, transcriptHasBuildOutcome,
  } = inputs

  // The chat predicate, and ONLY the chat predicate. See the asymmetry note above.
  const fromTurn = narratingChatIsOpenChat ? turnPreviewUrl : null
  // The project predicate, and only it. A relaunch is a restore, not a build: it needs no session.
  const fromRelaunch = sessionBelongsToOpenProject ? relaunchedUrl : null
  // …and this one needs a session to exist as well, which is the distinction a naive merge loses.
  const hasLiveSession = sessionId != null && sessionBelongsToOpenProject
  const fromSession = hasLiveSession ? sessionUrl : null
  const fromProject = sessionBelongsToOpenProject ? projectPreviewUrl : null

  const url = fromTurn ?? fromRelaunch ?? fromSession ?? fromProject ?? null

  // A live turn's own status outranks everything — it is the only source describing what is
  // happening RIGHT NOW. Below it, the two arms with no lifecycle of their own resolve to `ready`
  // because that is what they are: an app that is up. The session's status sits between them so a
  // session that ended still renders its terminal placeholder rather than being overwritten by a
  // stale "the container is alive" read.
  const status =
    (narratingChatIsOpenChat ? turnStatus : null) ??
    (fromRelaunch ? 'ready' : null) ??
    (hasLiveSession ? sessionStatus : null) ??
    (fromProject ? 'ready' : null) ??
    (transcriptHasBuildOutcome ? 'ended' : null)

  return { url, status }
}
