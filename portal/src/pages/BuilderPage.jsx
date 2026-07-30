import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { useNavigate, useLocation, useParams } from 'react-router-dom'
import {
  Send, Sparkles, User, Paperclip, FileText, FileSpreadsheet, Presentation, X,
  CheckCircle2, XCircle, ExternalLink,
} from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import LivePreview from '../components/LivePreview'
import BuildProgress, { hasBuildNarrative } from '../components/chat/BuildProgress'
import { ToolActivityLine } from '../components/chat/ToolActivityLine'
import SessionBanners from '../components/chat/SessionBanners'
import AttachmentChips from '../components/AttachmentChips'
import AttachmentLightbox from '../components/AttachmentLightbox'
import ProjectBreadcrumb from '../components/projects/ProjectBreadcrumb'
import { listProjectConversations } from '../utils/conversationApi'
import { ApiError } from '../utils/apiError'
import { describeSaveFailure, describeModeSwitchFailure, isConversationGone } from '../utils/chatErrors'
import { readDraft, writeDraft, clearDraft } from '../utils/composerDraft'
import { notifyUsageChanged } from '../utils/usage.js'
import { createBuildLock, openBuildLockChannel } from '../utils/buildLock'
import { useDropTransientQuery } from '../hooks/useDropTransientQuery'
import { useBuildSession } from '../hooks/useBuildSession'
import { isActiveBuildStatus } from '../utils/buildSessionTypes'
import { usePendingAttachments } from '../hooks/usePendingAttachments'
import { startTurn, readTurnStream, buildFromPlan, switchMode, stopTurn, TurnStartError } from '../utils/turnStreamApi'
import { narrativeEnvelopes, narrativeStatus } from '../utils/turnNarrative'
import { fetchSaveState, saveProject } from '../utils/buildSessionApi'
import { PlanOptionsCard } from '../components/chat/PlanOptionsCard'
import { ModeSwitcher } from '../components/chat/ModeSwitcher'
import { wireMessageFromParts, buildUserParts, partsToText, attachmentsFromParts, countAttachments, releaseUploadedAttachments } from '../utils/attachmentStore'
import { ACCEPT_ATTR, validateConversationAttachmentCap, TEXT_MEDIA_TYPES, OFFICE_MEDIA_TYPES, DECK_MEDIA_TYPES, officeFormat } from '../utils/attachmentInput'
import { openPdf } from '../utils/attachmentViewer'
import { loadBuilds, createBuild, getBuild, deriveTitle } from '../utils/builderHistory'

// The from-scratch greeting (ephemeral — never persisted, and never sent to the model: it is
// chrome, not a turn, and replaying it as history would have the model answering its own hello).
const WELCOME_TEXT = "Hello! I'm Citizen Developer AI. Tell me what you'd like to build for BIAL operations."
const welcomeMessage = () => ({ id: 'welcome', ephemeral: true, role: 'assistant', parts: [{ type: 'text', text: WELCOME_TEXT }], createdAt: new Date().toISOString() })

// U7: the whole system prompt is server-owned now (`backend/src/api/v1/claude/prompts.py`,
// selected by the conversation's kind) — the thin client identity line moved there as
// ASSISTANT_IDENTITY_PROMPT, and the interview protocol keeps riding server-side.

// The three hardcoded "refinement chips" are GONE (user, 2026-07-30). They were a fixed list —
// dark mode, a real-time data table, a mobile layout — offered after every build regardless of
// what the app was, so a gate-cleaning log got advice about theming. A canned suggestion that
// cannot be about your app reads as filler, and it competed with the composer for the one
// decision the user was actually making. If per-app follow-ups are wanted later they have to
// come from the model, which is the only party that knows what was just built.

// The brief-card era is over (U11/U13): the plan streams as text, `present_plan_options`
// renders the card, and its resolution state derives from the STORED record — never from
// fence-parsing the transcript.

// The LIVE half of a build turn is now the BuildProgress bubble (U15) — headline, friendly
// steps, elapsed time, Stop/Force-end, and the raw output behind its Details expander. The
// TERMINALS stay deliberately absent from the live surface: a finished build appends a real
// `build`-part message (003-U5) that says the same thing permanently — live narrative while
// it runs; a record once it is done.

/**
 * The one-line summary persisted alongside a build part. It is the message's TEXT, so it is both
 * what a plain reader sees and what the model is shown as history on the next turn — which is why
 * it states the outcome plainly rather than decoratively.
 */
function outcomeSummary({ status, reason }) {
  if (status === 'failed') {
    return reason ? `The build failed: ${reason}` : 'The build failed.'
  }
  if (reason === 'quota_exceeded') return 'The build stopped: you reached your daily limit.'
  return 'Build finished.'
}

/** The persisted build outcome (003-U5) — a compact, permanent record of one build turn.
 * `live` is true ONLY while THIS build's exact preview is the currently-running one; the link is
 * gated on it so a per-session URL that died with its sandbox is never presented as clickable (#43,
 * F4). When it's dead, the "Relaunch preview" action lives in the live-preview pane, not on this
 * historical card (relaunch restores the LATEST snapshot, which a per-build card can't speak for). */
function BuildOutcome({ part, live = false }) {
  const failed = part.status === 'failed'
  return (
    <div
      data-testid="build-outcome"
      className={`mt-2 rounded-xl border px-3 py-2.5 ${failed ? 'border-danger/30 bg-danger/5' : 'border-bial-border bg-white'}`}
    >
      <div className="flex items-center gap-1.5">
        {failed ? <XCircle size={12} className="text-danger flex-shrink-0" /> : <CheckCircle2 size={12} className="text-green-600 flex-shrink-0" />}
        <p className="text-[11px] font-bold text-tertiary">{failed ? 'Build failed' : 'Build finished'}</p>
      </div>
      {failed && part.reason && (
        <p className="mt-1 text-[10px] leading-relaxed text-neutral break-words">{part.reason}</p>
      )}
      {part.previewUrl && live && (
        // Shown ONLY while this build's exact preview is the running one — a per-session sandbox
        // URL dies with its session, so once it's not live we drop the link entirely rather than
        // let a user click into a dead frame (F4). Relaunch lives in the preview pane.
        <a
          href={part.previewUrl}
          target="_blank"
          rel="noreferrer"
          className="mt-1.5 inline-flex items-center gap-1 text-[10px] font-semibold text-primary hover:underline break-all"
        >
          <ExternalLink size={9} className="flex-shrink-0" />
          Open the live preview
        </a>
      )}
      {!failed && part.snapshotCommitted === false && (
        // R7's whole point: a build that ran but did not save is NOT a success, and the user has
        // to know before they build again on top of it.
        <p className="mt-1 text-[10px] leading-relaxed text-warning-700">
          This build’s code wasn’t saved, so the next build won’t start from it.
        </p>
      )}
    </div>
  )
}

function MessageContent({ parts }) {
  // Render the prose from the parts model + any attachment chips. No jsx:preview fence-stripping
  // any more — the single-file preview is gone (U5); build turns carry no code fence.
  const text = partsToText(parts)
  const attachments = attachmentsFromParts(parts)
  if (!text && attachments.length === 0) return null
  const segments = text.split(/(\*\*[^*]+\*\*|\n)/g)
  return (
    <span>
      {attachments.length > 0 && <AttachmentChips attachments={attachments} />}
      {segments.map((part, i) => {
        if (part.startsWith('**') && part.endsWith('**')) return <strong key={i}>{part.slice(2, -2)}</strong>
        if (part === '\n') return <br key={i} />
        return <span key={i}>{part}</span>
      })}
    </span>
  )
}

/**
 * The PROJECT THREAD, rendered by ChatRoute at the flat `/chat/:chatId` — one conversation where
 * an app is specified, built, and iterated for its whole life (003-U4).
 *
 * THE ROUTING RULE (load-bearing — read before changing any send path). EVERY composer send is a
 * TURN on this conversation (`startTurn` + the frame stream); what differs by MODE is the toolset
 * the server hands the model, not the pipeline. An Ask or Plan send runs a READ-ONLY turn. A
 * Write send runs an ordinary turn that holds the write toolset and works directly against the
 * live sandbox — there is no card-confirm gate in front of it. Plan's `present_plan_options`
 * card is ONE route into Write — its Build-it runs the atomic `buildFromPlan` transition, which
 * flips the thread's mode server-side — but not the only one: a thread already in Write builds
 * straight from the composer.
 *
 * "Existing refine semantics" now means: a live LEGACY build session in this project is stopped
 * (`session.stop()`) before `buildFromPlan` fires the fresh build. `session.start()` is no
 * longer called anywhere in this file — the session half only reattaches (reload-mid-build) or
 * stops.
 *
 * THREE DISTINCT IDENTITIES (unchanged from the single-file era, KTD-8):
 *   conversationId — the thread      (`/chat/{id}`, PATCH /conversations/{id})
 *   projectId      — the container   (breadcrumb; the C3 build session is project-scoped)
 *   build session  — the C3 session  (project/user-scoped, one-per-user)
 *
 * WHAT THE BUILD READS. The refined brief travels in the start body's `prompt` string; the
 * thread's `conversationId` rides along so the server can materialize the attachments it already
 * persisted (R3 / plan 002-U3) — it reads FILE PARTS from the thread, not the conversation as
 * context. (An earlier comment here claimed BRAIN reads project/conversation context server-side
 * per "C3 §2.1". It does not, and never did; the persist-before-start ordering below is what makes
 * the RELAY's project-context lookup and the attachment materialization work.)
 *
 * SESSION ↔ THREAD IDENTITY: the session is project-scoped. The thread that confirmed the brief
 * ORIGINATES the session; a confirm in another chat of the SAME project RE-ATTACHES the live
 * session (409 → getStatus → projectId-compare → resubscribe); a DIFFERENT project is BLOCKED
 * (the 409 is not self-describing — the projectId comparison is the gate, not the bare 409).
 * `sessionChatRef`/`sessionProjectRef` record the originating chat/project.
 *
 * @param {{
 *   chatId?: string, projectId?: string | null, projectName?: string | null,
 *   buildSessionDeps?: { client?: import('../utils/buildSessionApi').BuildSessionClient,
 *                        eventSourceFactory?: import('../utils/buildSessionEvents').EventSourceFactory },
 * }} [props]
 */
export default function BuilderPage({ chatId: chatIdProp, projectId = null, projectName = null, projectHasSavedBuild = null, buildSessionDeps } = {}) {
  const navigate = useNavigate()
  const location = useLocation()
  const params = useParams()
  const buildId = chatIdProp ?? params.chatId
  const initialPrompt = location.state?.prompt || ''
  const contextRef = useRef({
    theme: location.state?.theme || 'bial',
    uploadedFiles: location.state?.uploadedFiles || [],
  })
  const dropTransientQuery = useDropTransientQuery()

  // The one build-session owner (feed + preview + status + keep-alive timers). Tests
  // inject a mock client + FakeEventSource via `buildSessionDeps`.
  const session = useBuildSession(buildSessionDeps ?? {})

  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [builds, setBuilds] = useState([])
  const [viewer, setViewer] = useState(null) // { name, src } for the pending-attachment lightbox
  // WHICH CHAT has a turn streaming, not merely whether one does (G2). One BuilderPage instance
  // survives a chat switch under flat routing, so the boolean form gated chat B's send on chat A's
  // turn — the same per-chat scoping `buildActiveHere` already applies to the build half.
  const [generatingChatId, setGeneratingChatId] = useState(null)
  // Has the adopt round-trip settled the question "is a build still running in this chat?" (G1).
  //
  //   'checking'    — the mount/adopt is still in flight. The honest opening state: the page
  //                   cannot yet say whether a send would be accepted.
  //   'resolved'    — the question was ANSWERED. Three arms reach it: no anchor in the transcript
  //                   (every ordinary chat), the reattach resolving, and the 404 retention lapse.
  //   'unreachable' — the question could not be ASKED. Send stays shut rather than guessing over a
  //                   possibly-live build, and a Retry renders, because a permanently shut gate
  //                   whose only explanation was a vanishing toast is the dead end this plan exists
  //                   to remove.
  const [gateCheck, setGateCheck] = useState('checking')
  // `Build it` was clicked and the atomic transition has not answered yet. A full server
  // round-trip (lock acquire + sandbox provision) lives in here — seconds, not a keystroke — and
  // the composer must be shut for ALL of it: the build has begun from the user's point of view,
  // and a send made in this window used to vanish without a word (the ref guard below is silent
  // by design, because it was only ever meant to absorb a double-Enter).
  // WHICH chat is mid-Build-it, not merely that one is. It was a bare boolean, which held
  // while `buildFromPlan` was in flight — a short window nobody noticed was unscoped. U5 made
  // the window the WHOLE BUILD (the turn watcher is awaited inside the same handler), and an
  // unscoped flag over that span gates every sibling chat in the tab: exactly the leak
  // `generatingChatId` and the per-chat `sendingRef` already exist to prevent.
  const [buildStartingChatId, setBuildStartingChatId] = useState(null)
  const [switchingMode, setSwitchingMode] = useState(false) // a server mode-switch is in flight
  // The conversation's SERVER-OWNED mode (U13): seeded from the handoff for a brand-new
  // chat, then from the saved header; the ModeToggle writes it through the atomic switch
  // endpoint and this state reflects the server's confirmed answer.
  const [chatMode, setChatMode] = useState(location.state?.mode ?? null)
  // The LIVE plan-options card (a `plan_options` frame mid-turn, before the row reaches a
  // reload's projection) + per-card local overrides so a Build-it outcome updates the card
  // instantly (the stored record catches up on the next hydration).
  const [livePlanOptions, setLivePlanOptions] = useState(null)
  const [planOverrides, setPlanOverrides] = useState({})
  const [planErrors, setPlanErrors] = useState({})
  // `turnError` covers the chat half (429 daily cap, refused turn, in-band failure);
  // `session.error` covers the build half. Distinct sources, both above the composer.
  const [turnError, setTurnError] = useState(null)

  // ── The Write turn's narrative, straight off the turn stream (U5) ─────────────────────
  // A build used to speak through the C7 session feed; it is a turn now, so these four
  // read from turn frames instead. Keyed state rather than arrays for steps, because a
  // step arrives twice (started, then finished) under one `toolCallId` and the second must
  // REPLACE the first in place — appending would stack a spinner and its own result.
  const [turnSteps, setTurnSteps] = useState({})
  const [turnWorkspace, setTurnWorkspace] = useState(null)
  const [turnPreview, setTurnPreview] = useState({ url: null, state: null })
  const [turnDiagnostics, setTurnDiagnostics] = useState([])
  // Read inside async callbacks that outlive their render (the build watcher's terminal),
  // where the closed-over state value would be whatever it was when the build STARTED.
  const turnPreviewRef = useRef({ url: null, state: null })
  const [turnQuota, setTurnQuota] = useState(null)
  // The save model (KTD-5e). `saveDirty` is TRI-STATE — null is UNKNOWN, not clean.
  const [saveDirty, setSaveDirty] = useState(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState(null)
  const [turnTerminal, setTurnTerminal] = useState(null)
  const [turnStartedAt, setTurnStartedAt] = useState(null)
  const [stoppingTurn, setStoppingTurn] = useState(false)
  // WHICH CHAT this narrative belongs to. Same scoping `sessionChatRef` gives the build half,
  // and for the same reason (CC3): the page does not remount on a chat switch, so without it a
  // sibling chat renders the neighbouring chat's build narrative — complete with a working Stop
  // button for a build its reader never started and cannot see the composer state of.
  const turnNarrativeChatRef = useRef(null)
  // The turn currently streaming, for Stop. A ref because the stop handler is created once
  // and would otherwise close over whichever turn was live at its first render.
  const liveTurnIdRef = useRef(null)

  /** Clear the previous turn's narrative before a new one starts — otherwise a second
   *  message renders the first build's steps and diagnostics as if they were its own. */
  useEffect(() => {
    turnPreviewRef.current = turnPreview
  }, [turnPreview])

  const turnNarrative = useMemo(
    () => ({
      steps: turnSteps,
      diagnostics: turnDiagnostics,
      quota: turnQuota,
      workspace: turnWorkspace,
      preview: turnPreview,
    }),
    [turnSteps, turnDiagnostics, turnQuota, turnWorkspace, turnPreview],
  )

  const turnEnvelopes = useMemo(() => narrativeEnvelopes(turnNarrative), [turnNarrative])
  const turnNarrativeIsThisChat = turnNarrativeChatRef.current === buildId
  const turnBuildStatus = useMemo(
    () =>
      turnNarrativeIsThisChat
        ? narrativeStatus(turnNarrative, {
            running: generatingChatId === buildId,
            terminal: turnTerminal,
            isBuild: chatMode === 'write',
          })
        : null,
    [turnNarrative, turnNarrativeIsThisChat, generatingChatId, buildId, turnTerminal, chatMode],
  )

  /** Stop the LIVE turn. Same endpoint an ordinary reply uses — a build has no separate
   *  stop any more, which is the point: one working indicator, one way to interrupt it. */
  const handleStopTurn = async () => {
    const activeId = buildIdRef.current
    const turnId = liveTurnIdRef.current
    if (!activeId || !turnId || stoppingTurn) return
    setStoppingTurn(true)
    try {
      await stopTurn(activeId, turnId)
    } catch {
      setTurnError('Could not stop this turn. Try again.')
    } finally {
      setStoppingTurn(false)
    }
  }

  /** Ask the SERVER whether there is unsaved work. Deliberately not a local flag: the
   *  comparison is container-HEAD vs saved-bundle-HEAD, which is the only one that survives a
   *  reload or a second tab — both of which lose in-memory state while the commits stay put. */
  const refreshSaveState = async (activeProjectId) => {
    if (!activeProjectId) return
    try {
      const state = await fetchSaveState(activeProjectId)
      if (projectIdRef.current === activeProjectId) setSaveDirty(state.dirty)
    } catch {
      // UNKNOWN, never "clean". A failed check must not report the work as safe.
      if (projectIdRef.current === activeProjectId) setSaveDirty(null)
    }
  }

  const handleSave = async () => {
    const activeProjectId = projectIdRef.current
    if (!activeProjectId || saving) return
    setSaving(true)
    setSaveError(null)
    try {
      await saveProject(activeProjectId)
      if (projectIdRef.current === activeProjectId) setSaveDirty(false)
    } catch (err) {
      // Surfaced, never swallowed: a Save that silently fails leaves the user believing their
      // work is stored. The 409 copy from the server already names the way out.
      if (projectIdRef.current === activeProjectId) {
        setSaveError(err?.message || 'Could not save your work. Try again.')
        setSaveDirty(null)
      }
    } finally {
      setSaving(false)
    }
  }

  const resetTurnNarrative = () => {
    turnNarrativeChatRef.current = buildIdRef.current
    setTurnSteps({})
    setTurnDiagnostics([])
    setTurnQuota(null)
    setTurnWorkspace(null)
    setTurnTerminal(null)
    setTurnStartedAt(Date.now())
    setStoppingTurn(false)
  }

  // The newest plan card in the transcript — the only actionable one (older render expired).
  const newestPlanCallId = useMemo(() => {
    if (livePlanOptions) return livePlanOptions.toolCallId
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const part = (messages[i].parts || []).find((pp) => pp?.type === 'plan_options')
      if (part) return part.item.toolCallId
    }
    return null
  }, [messages, livePlanOptions])

  const { pendingAttachments, handleFileSelect, removePending, clearPending, attachToast, showAttachToast } =
    usePendingAttachments()

  const bottomRef = useRef(null)
  const inputRef = useRef(null)
  const fileInputRef = useRef(null)
  // Build sessions whose outcome this instance has already appended. The in-memory half of the
  // dedupe; the transcript scan in `appendBuildOutcome` is the half that survives a reload.
  const outcomeWrittenRef = useRef(new Set())
  // The transcript, readable from async callbacks without a stale closure (the relay send
  // assembles the API messages after an await — mirrors ChatPage's `messagesRef`).
  const messagesRef = useRef(messages)
  messagesRef.current = messages
  const buildIdRef = useRef(null) // the active CONVERSATION being viewed/persisted — never a session id
  const streamAbortRef = useRef(null) // aborts the SUBSCRIPTION only — the turn runs on server-side
  const chatModeRef = useRef(null)
  const loadedBuildRef = useRef(null)
  const initFiredRef = useRef(null) // the chat id already seeded — fire-once per chat, not per mount
  const projectIdRef = useRef(projectId)
  projectIdRef.current = projectId
  chatModeRef.current = chatMode

  /**
   * The chat mode switch: SERVER-CONFIRMED. The switcher never moves optimistically — we
   * request the atomic switch and only reflect the mode the server hands back.
   *
   * A failure says what actually failed. It used to report every rejection as "Finish the
   * current step before switching modes" — a sentence that is true of the server's 409 and
   * false of a 401, a 500 and a dropped connection alike, and that named a step which in
   * those cases did not exist (N12). `switchingMode` clears on every arm, so the control is
   * never left inert.
   */
  const handleModeSelect = (nextMode) => {
    if (!buildId || nextMode === chatMode || switchingMode) return
    setSwitchingMode(true)
    switchMode(buildId, nextMode)
      .then((confirmed) => setChatMode(confirmed))
      .catch((err) => showAttachToast(describeModeSwitchFailure(err)))
      .finally(() => setSwitchingMode(false))
  }
  // The chat + project that ORIGINATED the live session (for attribution + the render gate). The
  // session is project-scoped, so its surfaces render only while viewing a chat of ITS project.
  const sessionChatRef = useRef(null)
  const sessionProjectRef = useRef(null)
  // The anchor arm (d) failed on, so its Retry can re-ask the same question. A ref, not state:
  // `gateCheck` is what the render reads, and this is only the argument that goes with it.
  const gateRetryRef = useRef(null)
  // The session's surfaces render only while viewing a chat of ITS project (it is project-scoped).
  // `blocked`/`error` come from attempts that FAILED to start (start()'s reset leaves sessionId
  // null), so they gate on the project stamp alone; the live surfaces also require a sessionId.
  //
  // Derived HERE, above every handler, and not down beside the JSX where the rest of the render
  // derivations live: `handleSend` reads `buildActive`, and a gate that depends on declaration
  // order is a gate one reorder away from silently opening.
  const sessionProjectMatches = sessionProjectRef.current === projectId
  const showSession = session.sessionId != null && sessionProjectMatches
  // TRUE for a build's whole duration (provisioning → building → ready) and false at its
  // terminal. PROJECT-SCOPED on purpose: one BuilderPage instance survives a project switch, so
  // the project-agnostic form would let project A's build lock project B's composer.
  const buildActive = showSession && isActiveBuildStatus(session.status)
  // The COMPOSER's half of that gate is per-CHAT, matching the server's own per-conversation
  // 409 (`live_session_for_conversation`). A sibling builder chat in the same project is NOT
  // the chat that is building: the server would accept its turn, so shutting its composer and
  // telling its reader "building your app" is a lie about someone else's build. `buildActive`
  // stays project-scoped — the cockpit, the live bubble and the delete gate all speak for the
  // project's one session, and one BuilderPage instance survives a project switch.
  const buildActiveHere = buildActive && sessionChatRef.current === buildId
  const generating = generatingChatId === buildId
  // THE ONE GATE, AND ITS ONLY TERM IS TURN STATE (KTD-1). Mode appears nowhere in it: a mode is a
  // tool-access level on this same conversation, not a thing that can shut the composer, and using
  // it as a gate is what produced the Write dead end.
  //
  // WHAT THE GATE WITHHOLDS IS *SENDING*, NOT TYPING (KTD-2). The text box and the attach button
  // stay live at all times, so the user can compose their next message while they wait; they
  // simply cannot submit it into a running turn. Not disabling the textarea IS the focus fix
  // (KTD-3) — `disabled` on the focused element blurs it to `document.body`, which is the
  // mechanism behind "it blurs mid-sentence and focus never comes back".
  const buildStarting = buildStartingChatId === buildId
  const turnInFlight = generating || buildActiveHere || buildStarting
  // Send additionally waits on the adopt round-trip: an unresolved gate means we do not yet know
  // whether a build is live here, and "probably fine" is not a thing to guess about a send that
  // the server would refuse.
  const sendUnavailable = turnInFlight || gateCheck !== 'resolved'
  // `sendingRef` records WHICH CHAT is mid-send, and it is stamped synchronously before the first
  // await so a second Enter — or the seeded prompt racing a manual send — cannot start a second
  // session (the one-per-user 409 collision). Per-chat for the same reason `generating` is (G2):
  // as a bare boolean it stayed set for as long as chat A's turn was in flight, so a send in the
  // sibling chat the user had switched to returned silently at the guard.
  const sendingRef = useRef(null)
  const seqRef = useRef(0) // next message sort key for the active build's persisted turns

  // One build at a time, per project — advisory (KTD-7): `blockedBy` is the instant cross-tab
  // pre-check; the authoritative barrier is C3 start's 409. A crashed tab's claim expires, so the
  // channel is the only way claims travel (factory, not a module singleton).
  const buildLockRef = useRef(null)
  if (buildLockRef.current === null) buildLockRef.current = createBuildLock({ channel: openBuildLockChannel() })

  useEffect(() => {
    const lock = buildLockRef.current
    return () => lock?.dispose()
  }, [])

  useEffect(() => {
    void refreshSaveState(projectId)
  }, [projectId])

  // The one warning the save model owes the user. Work that is never saved IS lost when the
  // container is reclaimed — that is the accepted product decision — so leaving with unsaved
  // work must not be silent. Armed ONLY on a definite `true`: on `null` (unknown) there is
  // nothing honest to claim, and a spurious prompt is how users learn to dismiss them.
  useEffect(() => {
    if (saveDirty !== true) return undefined
    const warn = (event) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [saveDirty])

  // A genuine unmount must cancel the in-flight turn-stream reader — a chat switch already
  // aborts it before resubscribing, but nothing did on unmount, leaking the reader (and its
  // fetch) past the component's life. The turn keeps running server-side; only the read stops.
  useEffect(() => () => streamAbortRef.current?.abort(), [])

  // Hold the advisory claim while THIS chat's session is live; retract it once the session is
  // GENUINELY over — a terminal status, or a fully-reset session — so another tab's `blockedBy`
  // pre-check clears (KTD-7). A refine's start() also passes through here (its reset() drops
  // sessionId transitionally), so beginOrRefineBuild RE-ACQUIRES the claim once start() resolves
  // 'started' (finding #23). The authoritative barrier is C3's 409; this is only the fast
  // cross-tab UX mirror.
  useEffect(() => {
    const chat = sessionChatRef.current
    if (!projectId || !chat) return
    const genuinelyEnded =
      session.sessionId == null || session.status === 'ended' || session.status === 'failed'
    if (genuinelyEnded) {
      buildLockRef.current?.release(chat)
      // The BUILD terminal signals the meter too (N4). A build is where the tokens actually go —
      // minutes of model steps against one chat turn's worth — so settling the meter only at the
      // chat terminal would leave the largest spend of all invisible until a reload.
      notifyUsageChanged()
    }
  }, [session.status, session.sessionId, projectId])

  // "Recent builds" lists THIS project's build chats.
  const refreshBuilds = useCallback(async () => {
    try {
      const list = projectId
        ? (await listProjectConversations(projectId)).filter((c) => c.kind === 'builder')
        : await loadBuilds()
      setBuilds(list.sort((a, b) => new Date(b.updatedAt) - new Date(a.updatedAt)))
    } catch {
      // Keep the current list on a transient error; the next refresh recovers.
    }
  }, [projectId])

  useEffect(() => {
    refreshBuilds()
  }, [refreshBuilds])

  // Adopt the routed chat. One effect owns both arms — a brand-new build chat and a saved one arrive
  // at the same URL shape, and only the server's answer tells them apart. The live build SESSION is
  // NOT reset here: it is project-scoped and outlives a chat switch (its surfaces are gated by
  // `sessionProjectRef` below), so switching chats never tears down an in-flight build.
  useEffect(() => {
    if (!buildId) {
      navigate('/projects', { replace: true })
      return undefined
    }
    if (loadedBuildRef.current === buildId) return undefined

    let alive = true
    buildIdRef.current = buildId

    // Drop every scrap of the PREVIOUS chat before a byte of this one arrives (no remount under flat
    // routing). The composer draft belongs to the OLD chat — a leaked draft would send into this
    // one — so restore THIS chat's saved draft rather than blanking unconditionally (G3).
    setMessages([])
    setInput(readDraft(buildId))
    clearPending()
    // Re-ask the live-build question for the chat we are arriving at. Carrying the previous chat's
    // answer forward would open send over a build this chat has not been checked for.
    setGateCheck('checking')
    gateRetryRef.current = null
    streamAbortRef.current?.abort()
    setLivePlanOptions(null)
    setPlanOverrides({})
    setPlanErrors({})
    setTurnError(null)

    getBuild(buildId)
      .then((saved) => {
        if (!alive || buildIdRef.current !== buildId) return
        loadedBuildRef.current = buildId
        if (saved?.context) contextRef.current = saved.context
        if (saved?.mode) setChatMode(saved.mode)
        const restored = saved?.messages ?? []
        if (restored.length > 0) {
          // Seed the next seq from the highest PERSISTED seq, not the array length: a transcript
          // with any gap (a failed append, a pruned turn) would otherwise mint a colliding seq.
          seqRef.current = Math.max(...restored.map((m) => m.seq ?? 0)) + 1
          setMessages(restored)
        } else {
          seqRef.current = 0
          setMessages([welcomeMessage()])
        }
        // R8's OTHER live clause — the build half. A reload (or a second tab) landing on a
        // thread whose build is STILL RUNNING has no session in memory, so nothing would gate
        // the composer: the textarea would be enabled over a live build, every send would be
        // refused by the server, the mode pill would offer a switch that 409s, and the
        // transcript would say a build "was running" in the past tense about one that is
        // running right now. The projection carries the session id on the newest
        // `build_in_progress` part — that is all a reattach needs.
        reattachToLiveBuild(buildId, restored, () => alive)
        // A HANDED-OFF PROMPT FIRES EITHER WAY. The thread is canonical and permanent now
        // (003-U1), so it is empty exactly once in its life — every "Generate App" after the
        // first arrives at a thread with turns. Consuming the prompt only on the empty branch
        // meant the second build onward silently swallowed the user's typed prompt AND their
        // attachments: the composer was already cleared above, and nothing else reads
        // `location.state.prompt`. Fire-once is `initFiredRef` within a mount, and stripping the
        // state from history across mounts; `restored` is handed over so the send cannot race the
        // render that restores it.
        const handedOff = fireHandoffPrompt(buildId, () => alive, restored)
        // R8's live clause: a turn still running server-side gets re-subscribed, so a reload
        // mid-reply keeps streaming instead of freezing on a half-written transcript (and the
        // next send stops 409ing against a turn this tab forgot about). Skipped when a handoff
        // prompt just fired — that path owns the socket, and two subscribers would abort each
        // other over one shared controller.
        if (!handedOff && saved?.activeTurn?.turnId) {
          void reattachToTurn(buildId, saved.activeTurn, () => alive)
        }
      })
      .catch(() => {
        if (alive) navigate('/projects', { replace: true })
      })
    return () => {
      alive = false
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [buildId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  /**
   * Send a prompt handed off from another surface (the project composer's Generate, or the
   * planning chat's Launch Builder) as a RELAY turn — never as a build. The interview runs
   * first; a build starts only from the brief card the model returns.
   *
   * Fire-once per chat (`initFiredRef`), mirroring ChatPage's `initialMessage` discipline: a
   * remount (StrictMode, a re-render) must not send the prompt twice. Called from BOTH adopt
   * branches, because the thread is only empty on its very first open and the handoff has to
   * work for the whole life of the project.
   */
  const fireHandoffPrompt = (id, isAlive, prior) => {
    if (!initialPrompt) return false
    if (initFiredRef.current === id) return false
    initFiredRef.current = id
    const attachments = location.state?.pendingAttachments || []
    // STRIP THE HANDOFF FROM HISTORY BEFORE FIRING. `initFiredRef` is a ref, so it only survives
    // within one mount — but a RELOAD is a fresh mount over the SAME history entry, and the
    // browser keeps router state across it. Left in place, every reload of a handed-off thread
    // re-sends the prompt: a duplicate turn, billed again, on a thread the user was only reading.
    window.history.replaceState({}, '', window.location.pathname + window.location.search)
    void fireRelayTurn(initialPrompt, attachments, id, { isAlive, prior })
    return true
  }

  /**
   * RE-ATTACH to a BUILD that is still running (the reload-mid-build clause).
   *
   * `buildActive` is derived from `sessionProjectRef`, which only `handleBuildIt` /
   * `handleRelaunch` stamp — so on a fresh mount NOTHING knows a build is in flight and the one
   * gate is simply absent, exactly in the window it matters most. The transcript knows: the
   * projection emits a `build_in_progress` part (carrying its session id) for a build that began
   * and whose outcome never landed. Stamp the session's chat/project from it and let
   * `session.reattach` settle the three cases it already handles — still live (subscribe +
   * keep-alive: the gate closes, the note shows, the mode pill freezes), already terminal
   * (settles to ended/failed, no subscription, the gate stays open), gone (404, below).
   *
   * The reattach also makes the live bubble supersede the anchor row, which is what stops the
   * past-tense "a build WAS running here" line from narrating a build that is still going.
   */
  const reattachToLiveBuild = (activeId, restored, isAlive) => {
    let anchor = null
    for (let i = restored.length - 1; i >= 0 && anchor === null; i -= 1) {
      anchor = (restored[i].parts || []).find((p) => p?.type === 'build_in_progress') ?? null
    }
    // ARM (a) — NO ANCHOR, which is EVERY ORDINARY CHAT. Spelling this arm out is the difference
    // between a gate and a brick: keying resolution solely on `session.reattach` settling would
    // leave send permanently unavailable in almost every conversation on the platform, because
    // almost none of them has ever had a build in flight.
    if (!anchor?.sessionId) {
      setGateCheck('resolved')
      return
    }
    attachToLiveSession(activeId, anchor.sessionId, isAlive)
  }

  /**
   * Ask the server about one specific live-build anchor, and settle the composer gate on the
   * answer. Split out of `reattachToLiveBuild` so arm (d)'s Retry can re-run exactly this round
   * trip rather than re-deriving the anchor from a transcript it no longer needs to re-read.
   */
  const attachToLiveSession = (activeId, sessionId, isAlive) => {
    // CC1 — CLASSIFY THE CURRENT SESSION BEFORE OVERWRITING THE REFS. This is a regression of a
    // class this repo already fixed once and wrote down: the build-session learning's rule is
    // "classify the current session before overwriting the refs", because stamping first makes
    // every same-session guard tautological. Here the cost is worse than a tautology —
    // `session.reattach()`'s first act is a synchronous `reset()`, so adopting a SIBLING chat
    // that happens to carry a stale `build_in_progress` anchor tore down the live build's
    // heartbeat and lock renewal for a build belonging to another chat entirely.
    const liveElsewhere =
      session.sessionId != null &&
      session.sessionId !== sessionId &&
      isActiveBuildStatus(session.status)
    if (liveElsewhere) {
      // Someone else's build is genuinely running on this page instance. Leave it alone — and
      // resolve the gate, because we DID get an answer about this chat: no build of its own.
      setGateCheck('resolved')
      gateRetryRef.current = null
      return
    }
    gateRetryRef.current = { activeId, sessionId }
    sessionChatRef.current = activeId
    sessionProjectRef.current = projectIdRef.current
    const settle = () => {
      setGateCheck('resolved')
      gateRetryRef.current = null
    }
    session
      .reattach(sessionId)
      // ARM (b) — the server answered, whichever of its three cases it answered with (still live,
      // already terminal, evicted). Either way the page now knows, so the gate can speak.
      .then(() => {
        if (isAlive() && buildIdRef.current === activeId) settle()
      })
      .catch((err) => {
        // The session is unreachable, so this page owns no session: drop the stamps or every
        // project-scoped surface would render for one that does not exist.
        if (!isAlive() || buildIdRef.current !== activeId) return
        sessionChatRef.current = null
        sessionProjectRef.current = null
        // ARM (c) — a 404 is the ORDINARY outcome (the server restarted, or the five-minute
        // retention lapsed). It is a real answer — there is no build — so it opens the gate and
        // needs no notice.
        if (err instanceof ApiError && err.status === 404) {
          settle()
          return
        }
        // ARM (d) — we genuinely could not ask. The gate stays SHUT rather than guessing over a
        // possibly-live build, and because a shut gate with only a vanishing toast is the dead-end
        // class this whole plan exists to remove, this state renders a persistent Retry.
        setGateCheck('unreachable')
      })
  }

  /**
   * THE TURN TERMINAL, for every path that has one.
   *
   * Clears the streaming flag — but ONLY if this chat still owns it. A turn that terminates after
   * the user has switched away and started a turn in the sibling chat must not clear the sibling's
   * flag, which would open send over a reply that is genuinely still running.
   *
   * And it signals the usage meter (N4). The turn transport never did: `notifyUsageChanged` had
   * exactly one caller, in the retiring relay hook, so the header's token count only ever moved
   * on a page load — a user could spend their whole daily budget watching a number that never
   * changed. Firing it HERE rather than at each of the three call sites is deliberate: every
   * terminal routes through this one function, including the failed and stopped ones, which are
   * billed too and would otherwise leave the meter stale exactly when it matters.
   */
  /** After every turn — a turn that wrote files is exactly when the answer changes, and the
   *  user should see Save light up without having to guess or reload. */
  const settleSaveState = () => void refreshSaveState(projectIdRef.current)

  const endGenerating = (activeId) => {
    setGeneratingChatId((prev) => (prev === activeId ? null : prev))
    notifyUsageChanged()
    settleSaveState()
  }

  /** Arm (d)'s way out: re-run the same adopt round-trip that could not be completed. */
  const retryGateCheck = () => {
    const pending = gateRetryRef.current
    if (!pending || pending.activeId !== buildIdRef.current) return
    setGateCheck('checking')
    attachToLiveSession(pending.activeId, pending.sessionId, () => true)
  }

  /**
   * The ONE turn-frame reducer, shared by both stream consumers — the send path below and the
   * mid-turn RE-ATTACH on reload. A frame must not be interpreted two different ways depending
   * on which consumer happened to open the socket; `sink` carries the two mutable accumulators
   * (`text` so far, the terminal status) back out to the caller.
   */
  const turnFrameHandler = (activeId, assistantId, sink) => {
    const paint = () =>
      setMessages((prev) => prev.map((m) => (m.id === assistantId ? { ...m, parts: [{ type: 'text', text: sink.text }] } : m)))
    return (frame) => {
      if (buildIdRef.current !== activeId) return // navigated away — drop the frame
      if (frame.type === 'text_delta') {
        sink.text += frame.text
        paint()
      } else if (frame.type === 'snapshot') {
        if (frame.textSoFar && frame.textSoFar.length > sink.text.length) {
          sink.text = frame.textSoFar
          paint()
        }
        // A snapshot of an ALREADY-SETTLED turn is itself terminal: the `error`/`turn_ended`
        // frames it consolidates may have been evicted from the ring, so treating only those
        // as terminal left a failed turn looking like it was still thinking, with no reason
        // shown. The snapshot carries the reason for exactly this case.
        if (frame.turnStatus === 'failed' || frame.turnStatus === 'stopped') {
          sink.terminal = frame.turnStatus
          if (frame.errorMessage) setTurnError(frame.errorMessage)
        } else if (frame.turnStatus === 'completed') {
          sink.terminal = 'completed'
        }
        // The catch-up half of the Write narrative. A `workspace`/`preview` frame that fired
        // before this tab connected is gone from the server's ring, so the snapshot is the
        // only thing left that can answer — which is what saves a mid-build reload from a
        // second REST round-trip, and from an empty preview pane over a running app.
        if (frame.turnId) liveTurnIdRef.current = frame.turnId
        if (frame.workspaceState) setTurnWorkspace({ state: frame.workspaceState, message: null })
        if (frame.previewUrl || frame.previewState) {
          setTurnPreview((prev) => ({
            url: frame.previewUrl ?? prev.url,
            state: frame.previewState ?? prev.state,
          }))
        }
        if (frame.steps?.length) {
          setTurnSteps((prev) => {
            const next = { ...prev }
            for (const step of frame.steps) next[`snap_${step.seq}_${step.tool}`] = step
            return next
          })
        }
      } else if (frame.type === 'plan_options') {
        setLivePlanOptions(frame.item)
      } else if (frame.type === 'error') {
        setTurnError(frame.message)
      } else if (frame.type === 'step') {
        // U5: the arm this handler used to DROP entirely. A Write turn's whole narrative —
        // every file written, every command run — arrives as step frames, so without this a
        // build streamed a blank bubble for several minutes and then a finished app.
        setTurnSteps((prev) => ({ ...prev, [frame.toolCallId]: frame.item }))
      } else if (frame.type === 'workspace') {
        setTurnWorkspace({ state: frame.state, message: frame.message ?? null })
      } else if (frame.type === 'preview') {
        // Keyed on the url: a NEW url remounts the iframe, which is how a restored sandbox
        // gets reloaded rather than left showing a dead frame.
        setTurnPreview((prev) => ({
          url: frame.previewUrl ?? prev.url,
          state: frame.state,
        }))
      } else if (frame.type === 'diagnostic') {
        // NOT `setTurnError`. The turn is not failing — a repair run follows — and showing
        // this as a failure would tell the user their build died four times on its way to
        // succeeding. It renders as an in-narrative alert row instead.
        setTurnDiagnostics((prev) => [...prev, frame])
      } else if (frame.type === 'quota') {
        setTurnQuota({ limit: frame.limit, used: frame.used, resetsAt: frame.resetsAt })
      } else if (frame.type === 'turn_ended') {
        sink.terminal = frame.status
        sink.reason = frame.reason ?? null
        sink.snapshotCommitted = frame.snapshotCommitted ?? null
        setTurnTerminal(frame.status)
        liveTurnIdRef.current = null
        if (frame.previewUrl) setTurnPreview({ url: frame.previewUrl, state: 'ready' })
      }
    }
  }

  /**
   * RE-ATTACH to a turn that is still running server-side (R8's live clause). A reload mid-turn
   * lands on a transcript whose newest row is the user's message: the reply is being generated,
   * but this tab has no socket, so the page would sit static and the next send would 409.
   *
   * Deliberately subscribes with NO cursor even though the server reports `lastSeq`. That seq
   * counts frames THIS tab never received — passing it would put the server in tail-only replay
   * and the already-streamed prefix would be lost. Cursor-0 gets the consolidating snapshot,
   * whose `textSoFar` is exactly the reply so far.
   */
  const reattachToTurn = async (activeId, activeTurn, isAlive) => {
    const stillHere = () => isAlive() && buildIdRef.current === activeId
    const assistantSeq = seqRef.current
    seqRef.current += 1
    const assistantId = `local_${Date.now()}_r`
    const sink = { text: '', terminal: null, reason: null, snapshotCommitted: null }
    setGeneratingChatId(activeId)
    setTurnError(null)
    resetTurnNarrative()
    setMessages((prev) => [...prev, { id: assistantId, role: 'assistant', parts: [{ type: 'text', text: '' }], seq: assistantSeq, createdAt: new Date().toISOString() }])

    streamAbortRef.current?.abort()
    const controller = new AbortController()
    streamAbortRef.current = controller
    const onFrame = turnFrameHandler(activeId, assistantId, sink)
    let outcome
    try {
      outcome = await readTurnStream({ conversationId: activeId, turnId: activeTurn.turnId, cursor: 0, signal: controller.signal, onFrame })
      // CC4 — RESUME ONCE, exactly as `fireRelayTurn` does. This path mapped any throw to
      // 'truncated' and stopped there, so a single dropped socket after a reload — the ordinary
      // case this function exists to serve — surfaced as "the connection dropped" on a turn that
      // was still happily running server-side. One resubscribe consolidates the turn so far via
      // the server snapshot then tails to the end; a SECOND truncation is a real drop, and the
      // reload advice below is then honest. (The streamed-reply learning's resume-once rule.)
      if (outcome === 'truncated' && !sink.terminal && !controller.signal.aborted) {
        outcome = await readTurnStream({ conversationId: activeId, turnId: activeTurn.turnId, cursor: 0, signal: controller.signal, onFrame })
      }
    } catch {
      outcome = 'truncated'
    }
    endGenerating(activeId)
    if (!stillHere()) return
    if (outcome === 'stalled') setTurnError('The reply stalled. Reload to catch up.')
    else if (outcome === 'truncated' && !sink.terminal) setTurnError('The connection dropped. Reload to catch up.')
    if (sink.terminal !== 'completed' && sink.text === '') {
      setMessages((prev) => prev.filter((m) => m.id !== assistantId))
      seqRef.current = assistantSeq
    }
    refreshBuilds()
  }

  /**
   * One relay turn (U7): create-or-confirm the thread → stream — the SERVER persists both
   * sides of the turn before its terminal [DONE] (write-before-DONE), so this page appends
   * nothing.
   *
   * THIS IS THE ONLY THING A SEND DOES. It never starts a build — the routing rule (KTD): every
   * composer send goes to the relay, and builds fire ONLY from a brief card's confirmation, first
   * build and iteration alike. The direct-fire send this page used to do is what made the agent
   * silently guess at a vague prompt.
   *
   * Create-before-stream is load-bearing: the stateless relay 404s an unknown conversation,
   * and the row is what carries the project parentage + context that ground the first turn.
   */
  const fireRelayTurn = async (rawText, attachments, activeId, { isAlive = () => true, onAbort, onSent, prior } = {}) => {
    const text = rawText.trim() || (attachments.length ? 'Please review the attached file(s).' : '')
    if (!text) return

    const stillHere = () => isAlive() && buildIdRef.current === activeId

    let parts
    try {
      parts = await buildUserParts(text, attachments)
    } catch (err) {
      // ABORT — never fall through to a turn that silently forgets the attachment (R3). The user
      // attached a spreadsheet; answering as if they hadn't is the wrong-build bug in miniature.
      showAttachToast(err?.message || 'Could not upload the attachment. Please try again.')
      if (stillHere()) onAbort?.()
      return
    }
    if (!stillHere()) return // switched chats mid-upload — abandon, don't clobber the new chat

    // `prior` is passed by the handoff, which fires in the same tick as the `setMessages` that
    // restores the transcript — `messagesRef` is only refreshed on the next render, so reading it
    // here would see the PRE-restore array and the handoff would overwrite the thread it just
    // loaded. Every other caller sends from a settled render and reads the ref.
    const priorMessages = (prior ?? messagesRef.current).filter((m) => !m.ephemeral)
    const userSeq = seqRef.current
    seqRef.current += 1
    const userMsg = { id: `local_${Date.now()}`, role: 'user', parts, seq: userSeq, createdAt: new Date().toISOString() }
    setMessages([...priorMessages, userMsg])

    // U7: the thread row must EXIST before the first turn (the relay 404s otherwise).
    // Idempotent per owner, so re-confirming after a reload costs one cheap 200.
    if (userSeq === 0) {
      try {
        await createBuild(activeId, {
          projectId,
          title: deriveTitle(partsToText(parts)),
          context: contextRef.current,
          mode: chatModeRef.current ?? 'plan',
        })
        onSent?.()
      } catch (err) {
        releaseUploadedAttachments(parts)
        showAttachToast(describeSaveFailure(err, 'Could not start this thread. Check your connection.'))
        // Roll back ONLY if we still own this chat — a mid-await switch means the snapshot/seq
        // describe the OTHER chat, and writing them here would clobber the current transcript.
        if (stillHere()) {
          setMessages(priorMessages)
          seqRef.current = userSeq
          onAbort?.()
        }
        // The thread was deleted out from under us (elsewhere, or in another tab): leave, rather
        // than sit on a page whose every send will fail the same way.
        if (isConversationGone(err)) navigate('/projects', { replace: true })
        return
      }
    } else {
      // The turn is about to stream (and the server persists it) — safe to clear the draft.
      onSent?.()
    }
    dropTransientQuery(activeId)
    refreshBuilds()

    const assistantSeq = seqRef.current
    seqRef.current += 1
    const assistantId = `local_${Date.now()}_a`
    const sink = { text: '', terminal: null, reason: null, snapshotCommitted: null }
    setGeneratingChatId(activeId)
    setTurnError(null)
    resetTurnNarrative()
    setMessages((prev) => [...prev, { id: assistantId, role: 'assistant', parts: [{ type: 'text', text: '' }], seq: assistantSeq, createdAt: new Date().toISOString() }])

    // U13: the turn API (U10). POST starts the turn DETACHED server-side; the subscription
    // below only observes — closing the tab never cancels the reply, and the server
    // persists both sides before the terminal (write-before-DONE).
    const wire = wireMessageFromParts(parts)
    // Did the server ACCEPT the turn? `startTurn` resolving (202) means the user's message is
    // persisted and the reply runs detached regardless of what this tab does next — so the
    // catch below must split on it: everything after the accept is subscription plumbing.
    let posted = false
    try {
      await startTurn(activeId, {
        text: wire.text ?? '',
        attachmentTexts: wire.attachmentTexts ?? [],
        attachmentIds: wire.attachmentIds ?? [],
      })
      posted = true
      streamAbortRef.current?.abort()
      const controller = new AbortController()
      streamAbortRef.current = controller
      const onFrame = turnFrameHandler(activeId, assistantId, sink)
      let outcome = await readTurnStream({ conversationId: activeId, signal: controller.signal, onFrame })
      if (outcome === 'truncated' && !sink.terminal && !controller.signal.aborted) {
        // A dropped socket before the terminal: one resubscribe consolidates the turn so far
        // via the server snapshot then tails to the end (resume-once). A second truncation is
        // a real drop — reload is the honest fallback.
        outcome = await readTurnStream({ conversationId: activeId, signal: controller.signal, onFrame })
      }
      if (outcome === 'stalled') setTurnError('The reply stalled. Reload to catch up.')
      else if (outcome === 'truncated' && !sink.terminal) setTurnError('The connection dropped. Reload to catch up.')
    } catch (err) {
      if (stillHere()) {
        if (posted) {
          // The turn was ACCEPTED — only the subscribe after it broke. The user's message is
          // persisted and the reply is being produced detached, so the N8 rollback below
          // would lie in the other direction: "could not be sent" over a message the server
          // already has invites a duplicate resend. Keep the user's bubble (it IS in the
          // database), drop only the empty reply placeholder, and point at reload — the
          // transcript there will have both sides of the turn.
          setTurnError('Your message was received, but this page lost the reply. Reload to catch up.')
          setMessages((prev) => prev.filter((m) => m.id !== assistantId))
          seqRef.current = assistantSeq
        } else {
          setTurnError(err instanceof TurnStartError ? err.message : 'The message could not be sent. Try again.')
          // N8 — ROLL BACK BOTH BUBBLES, not just the assistant's. A `startTurn` that threw
          // means the server persisted NOTHING: the user's message was refused at the door
          // (429 over the cap, 409 busy, 503 unconfigured). Leaving their bubble on screen
          // showed a transcript that disagreed with the database — the message looked sent,
          // survived until reload, and then vanished. Roll the whole optimistic pair back and
          // let the error banner be the only account of what happened.
          setMessages((prev) => prev.filter((m) => m.id !== assistantId && m.id !== userMsg.id))
          seqRef.current = userSeq
        }
      }
      endGenerating(activeId)
      return
    }
    endGenerating(activeId)

    if (stillHere()) {
      if (sink.terminal !== 'completed' && sink.text === '') {
        // Failed/stopped with nothing streamed — drop the empty bubble; the error banner
        // (or the stopped state) is the feedback.
        setMessages((prev) => prev.filter((m) => m.id !== assistantId))
        seqRef.current = assistantSeq
      }
      refreshBuilds()
    }
  }

  /**
   * Show what a build turn produced, the moment it ends.
   *
   * THE SERVER OWNS THE DURABLE WRITE (`services/build_sessions/outcome.py`), not this. Builds
   * take minutes and users close tabs, and an in-memory session is evicted five minutes after its
   * terminal — so a portal-written record would be missing for exactly the users a permanent
   * record serves. The thing that always knows a build finished is the thing that finished it.
   *
   * This renders the same outcome LOCALLY so the watching user sees it immediately rather than
   * waiting for a reload. On reload the server's row takes its place, identically.
   *
   * The local message is NOT persisted and its `seq` is display shape only — this page does not
   * try to predict which slot the server took. It cannot: the server writes while this tab may be
   * reloading, backgrounded, or closed, and a guess that is wrong is not a visible error but a
   * lost message. Allocation is the server's alone (`_free_seq` in the conversations router); this
   * page re-seeds `seqRef` from what each append reports it actually stored.
   */
  const showBuildOutcome = (outcome) => {
    // WHICH BUILD this describes. A build is a turn now, so the identity is `turnId`; a legacy
    // session outcome still keys on `sessionId`. Taking only `sessionId` — as this did — meant
    // every turn build deduped on `undefined`: the first one registered `undefined` in the Set,
    // and every build after it on the same page instance was silently swallowed. Worse, the
    // transcript scan below then matched ANY stored build part that lacked a session id, so a
    // genuinely new outcome could be suppressed by an unrelated older one.
    const buildKey = outcome.turnId ?? outcome.sessionId ?? null
    // No identity, no dedupe — and no card either. Rendering an outcome we cannot recognise
    // again guarantees a duplicate on the next terminal, which is the louder wrong.
    if (buildKey === null || outcomeWrittenRef.current.has(buildKey)) return
    // After a reload the transcript already holds the server's row, and a replayed terminal
    // would otherwise stack a second copy on top of it. `_id`/seq say nothing about WHICH build
    // a part describes; the turn (or session) id is the only thing that does.
    const already = messagesRef.current.some((m) =>
      (m.parts || []).some(
        (p) => p?.type === 'build' && (p.turnId ?? p.sessionId ?? null) === buildKey,
      ),
    )
    outcomeWrittenRef.current.add(buildKey)
    if (already) return

    // The summary text part mirrors what the server writes, so the local render and the reloaded
    // row read identically (`outcome.py::_summary` is the other half of this pair).
    const parts = [{ type: 'text', text: outcomeSummary(outcome) }, { type: 'build', ...outcome }]
    setMessages((prev) => [...prev, { id: `local_${Date.now()}_b`, role: 'assistant', parts, seq: seqRef.current, createdAt: new Date().toISOString() }])
    refreshBuilds()
  }

  /**
   * Watch the live session for its terminal and surface the outcome once.
   *
   * Reads the C7 `ended` envelope from the feed store for the authoritative detail
   * (`snapshot_committed` is only true on the SESSION-API frame — plan 002-U7 — so an
   * envelope-less terminal must not claim otherwise). Force-end and keep-alive reclaim reach a
   * terminal with NO `ended` envelope at all, so the status enum is the fallback.
   */
  useEffect(() => {
    const sid = session.sessionId
    const activeId = sessionChatRef.current
    if (!sid || !activeId) return
    if (session.status !== 'ended' && session.status !== 'failed') return
    // Only the thread that OWNS this session shows it, and only while we are viewing it.
    if (activeId !== buildIdRef.current || sessionProjectRef.current !== projectId) return

    const ended = session.envelopes.find((e) => e.type === 'ended')
    showBuildOutcome({
      status: session.status,
      sessionId: sid,
      previewUrl: session.previewUrl ?? null,
      endedAt: new Date().toISOString(),
      // UNKNOWN, not false. `finishSession('ended')` closes the feed the moment the stop HTTP call
      // resolves, so the real `ended` frame — which for a graceful stop says snapshot_committed:
      // true, because `_do_finalize` DID snapshot — may never be dispatched here. Collapsing that
      // into `false` warned the user their code wasn't saved about a build that saved it. The card
      // warns only on an explicit `false`, and the server's row (which always carries the real
      // value) replaces this one on reload.
      snapshotCommitted: ended?.snapshot_committed ?? null,
      reason: ended?.reason ?? null,
    })
    // The composer re-opens right here, so the mode pill above it has to be telling the truth.
    // `Build it` optimistically set it to Write; the end sequence has just put the thread BACK
    // into the mode it came from (`restore_conversation_mode`), and only the server knows which
    // one that was. Re-read the header rather than guess — a wrong guess would show the citizen
    // a mode their next send does not actually run in. Failure is a no-op: the pill stays as it
    // was and a reload corrects it.
    getBuild(activeId)
      .then((saved) => {
        if (saved?.mode && buildIdRef.current === activeId) setChatMode(saved.mode)
      })
      .catch(() => {
        // NOT swallowed (`.claude/rules/fail-first.md`). The composer has just re-opened; if we
        // could not learn the mode the server put the thread back into, the pill above it may be
        // showing a mode the next send does not run in — say so rather than let it lie silently.
        if (buildIdRef.current === activeId) showAttachToast('Reload to see the current chat mode.')
      })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.status, session.sessionId, projectId])

  /**
   * The advisory cross-tab pre-check message, or null when the coast is clear. Checked before a turn
   * is persisted so the user keeps their draft; the AUTHORITATIVE barrier is C3 start's 409 (KTD-7).
   */
  const buildBlockedMessage = (conversationId) => {
    if (!projectId) return null
    const blocker = buildLockRef.current?.blockedBy(projectId, conversationId)
    if (!blocker) return null
    const other = builds.find((b) => b.id === blocker.conversationId)
    const which = other?.title ? `“${other.title}”` : 'another build chat'
    return `${which} is already building this project. Only one build runs at a time — wait for it to finish, or stop it first.`
  }

  /**
   * A composer send is ALWAYS a chat turn — never a build.
   *
   * That is the routing rule: the model decides when there is enough to build and says so with a
   * brief card; the user confirms it. A post-build "add a chart" goes down this exact path too
   * (the protocol emits an updated brief immediately for a concrete change, so iteration costs one
   * model turn + one click).
   */
  const handleSend = async () => {
    const text = input.trim()
    const attachments = pendingAttachments
    if (!text && attachments.length === 0) return // nothing to send
    // THE ONE GATE, ENFORCED HERE AND ONLY HERE. The composer's attributes are the AFFORDANCE:
    // `aria-disabled` deliberately keeps Send focusable (KTD-2), the textarea is never disabled at
    // all (KTD-3), and Enter on it calls straight through to this function. So every arm of the
    // gate has to be re-checked at the one place that actually starts a turn.
    if (buildActiveHere || buildStarting) {
      showAttachToast('Your app is being built — send unlocks when it finishes. Keep typing meanwhile.')
      return
    }
    if (generating) {
      showAttachToast('Send unlocks when the current reply finishes. Keep typing meanwhile.')
      return
    }
    if (gateCheck !== 'resolved') {
      showAttachToast(
        gateCheck === 'unreachable'
          ? 'Still can’t tell whether a build is running here. Try the Retry above.'
          : 'Just checking whether a build is running here — one moment.',
      )
      return
    }
    // Synchronous, and a REF rather than state: the two keydowns of a fast double-Enter land in the
    // SAME tick, so `generating` — set after an await — is still false for the second one. The
    // draft is deliberately held until the server confirms the turn, so that second read sees the
    // very same text and fires a second relay turn: a duplicate persisted message, a duplicate
    // model call, and on a builder thread two brief cards for one request. Silent on purpose —
    // this is one keystroke burst, not a second intention worth a toast.
    // `handleConfirmBrief` holds this same ref, for the same reason, over the build trigger.
    if (sendingRef.current === buildIdRef.current) return
    // Project-first: a thread REQUIRES a project (no lazy Default — never reintroduce).
    if (!projectId) {
      showAttachToast('Open a project to start a build.')
      return
    }
    if (attachments.length > 0) {
      const cap = validateConversationAttachmentCap(countAttachments(messages), attachments.length)
      if (cap.error) {
        showAttachToast(cap.error)
        return
      }
    }

    const sendChatId = buildIdRef.current
    sendingRef.current = sendChatId
    try {
      // The draft is held until the turn is STORED, then cleared by `onSent`. Clearing it here —
      // optimistically, on the click — is what made every save failure unrecoverable: the toast
      // tells the user to send it again, and their text and staged files are already gone.
      await fireRelayTurn(text, attachments, sendChatId, {
        onSent: () => {
          setInput('')
          // Clear the PERSISTED draft with the in-memory one, or the next reload re-populates the
          // composer with the message that was just sent — which is easy to send twice by accident.
          clearDraft(sendChatId)
          clearPending()
        },
      })
    } finally {
      // Release only OUR claim: a send begun in the chat the user has since switched to must not
      // have its double-Enter guard cleared by this one settling late.
      if (sendingRef.current === sendChatId) sendingRef.current = null
    }
  }

  /**
   * Build it — the atomic U12 transition: ONE server call records the choice, flips the
   * conversation to Write, acquires the build lock, and starts the build. Every failure
   * is a typed outcome that re-arms the card; `started` hands back the session this page
   * attaches its cockpit to. Stale plans warn first — the user decides (force).
   */
  /**
   * Watch a build the server has already started, as the turn it now is.
   *
   * Structurally the same subscription an ordinary send makes — same frame handler, same
   * resume-once on a dropped socket — because after U5 there is genuinely no difference: a
   * build is a Write turn with more tools. What it does NOT do is `session.reattach`; there
   * is no build session to attach to, and reaching for one would re-open the C7 feed this
   * whole unit exists to retire.
   */
  const watchBuildTurn = async (activeId, turnId, toolCallId) => {
    const assistantSeq = seqRef.current
    seqRef.current += 1
    const assistantId = `local_${Date.now()}_b`
    const sink = { text: '', terminal: null, reason: null, snapshotCommitted: null }
    setGeneratingChatId(activeId)
    setTurnError(null)
    resetTurnNarrative()
    liveTurnIdRef.current = turnId
    // THE CROSS-TAB CLAIM. It used to ride the build SESSION, and a build has no session any
    // more — so without this a second chat in the same project gets no warning at all before
    // it starts a rival build. The claim-holding effect below keys on `session.*`, which a
    // Write turn never populates, so the acquire and the release both live here now.
    // Advisory only: the server's 409 is the real barrier, this is the fast UX mirror.
    sessionChatRef.current = activeId
    sessionProjectRef.current = projectId
    buildLockRef.current?.acquire(projectId, activeId)
    setMessages((prev) => [
      ...prev,
      { id: assistantId, role: 'assistant', parts: [{ type: 'text', text: '' }], seq: assistantSeq, createdAt: new Date().toISOString() },
    ])

    streamAbortRef.current?.abort()
    const controller = new AbortController()
    streamAbortRef.current = controller
    const onFrame = turnFrameHandler(activeId, assistantId, sink)
    try {
      // Cursor 0 deliberately: the build may have been running for seconds before this
      // subscribe landed, and the consolidating snapshot is what recovers those frames.
      let outcome = await readTurnStream({ conversationId: activeId, turnId, cursor: 0, signal: controller.signal, onFrame })
      if (outcome === 'truncated' && !sink.terminal && !controller.signal.aborted) {
        outcome = await readTurnStream({ conversationId: activeId, turnId, cursor: 0, signal: controller.signal, onFrame })
      }
      if (outcome === 'stalled') setTurnError('The build stalled. Reload to catch up.')
      else if (outcome === 'truncated' && !sink.terminal) {
        setTurnError('The connection dropped. Reload to catch up — the build is still running.')
      }
    } catch {
      setPlanErrors((prev) => ({
        ...prev,
        [toolCallId]: 'The build started but this page could not join it — reload to watch it.',
      }))
    } finally {
      endGenerating(activeId)
      // Release on EVERY exit, including the one where this tab lost the stream: the build may
      // still be running server-side, but this tab can no longer say anything true about it,
      // and a claim nobody retracts blocks the user's next build until the tab is closed.
      buildLockRef.current?.release(activeId)
      notifyUsageChanged()
    }

    if (buildIdRef.current === activeId && sink.terminal) {
      showBuildOutcome({
        status: sink.terminal === 'completed' ? 'ended' : 'failed',
        turnId,
        previewUrl: turnPreviewRef.current.url,
        endedAt: new Date().toISOString(),
        snapshotCommitted: sink.snapshotCommitted,
        reason: sink.reason,
      })
      refreshBuilds()
    }
  }

  const handleBuildIt = async (toolCallId) => {
    if (sendingRef.current === buildIdRef.current) return
    if (!projectId) {
      showAttachToast('Open a project to start a build.')
      return
    }
    const activeBuildId = buildIdRef.current
    const blocked = buildBlockedMessage(activeBuildId)
    if (blocked) {
      setPlanErrors((prev) => ({ ...prev, [toolCallId]: blocked }))
      return
    }
    sendingRef.current = activeBuildId
    // The gate closes on the CLICK, not on the server's answer: `buildFromPlan` is a full
    // round-trip (lock acquire + sandbox provision) and the build is already the user's
    // answer for every second of it.
    setBuildStartingChatId(activeBuildId)
    setPlanErrors((prev) => ({ ...prev, [toolCallId]: null }))
    try {
      const sessionLive = isActiveBuildStatus(session.status) && session.sessionId != null
      if (sessionLive && sessionProjectRef.current !== projectId) {
        setPlanErrors((prev) => ({
          ...prev,
          [toolCallId]: 'You already have a build running in another project. Stop it before starting one here.',
        }))
        return
      }
      if (sessionLive) {
        // The refine loop: end THIS project's live session gracefully before the fresh
        // build (the server would reap through it anyway; a courteous stop keeps its
        // snapshot + terminal clean).
        const stopped = await session.stop()
        if (!stopped) {
          setPlanErrors((prev) => ({
            ...prev,
            [toolCallId]: session.error || 'Could not stop the running build — try again.',
          }))
          return
        }
      }
      let outcome = await buildFromPlan(activeBuildId, toolCallId)
      if (outcome.outcome === 'stale_plan') {
        const proceed = window.confirm(
          'The app has changed since this plan was made. Build from this plan anyway?',
        )
        if (!proceed) return
        outcome = await buildFromPlan(activeBuildId, toolCallId, { force: true })
      }
      if (outcome.outcome === 'started' || outcome.outcome === 'already_started') {
        setPlanOverrides((prev) => ({ ...prev, [toolCallId]: 'build' }))
        // Now CORRECT rather than optimistic: the server flipped the mode atomically with the
        // record, and nothing hands it back at the terminal any more — Write is where this
        // thread stays until the user moves it.
        setChatMode('write')
        if (outcome.turnId) {
          // A build IS a turn. Subscribe to the turn stream exactly as an ordinary send does,
          // rather than attaching a build session — there is no session to attach to, and the
          // narrative (steps, workspace, preview, diagnostics) all arrives as turn frames.
          await watchBuildTurn(activeBuildId, outcome.turnId, toolCallId)
        }
      }
    } catch (err) {
      setPlanErrors((prev) => ({
        ...prev,
        [toolCallId]: err?.message || 'The build could not be started. Try again.',
      }))
    } finally {
      if (sendingRef.current === activeBuildId) sendingRef.current = null
      setBuildStartingChatId((prev) => (prev === activeBuildId ? null : prev))
    }
  }

  /** A card's effective item: the stored record, overlaid with this page's just-made choice. */
  const applyPlanOverride = (item) => {
    const override = planOverrides[item.toolCallId]
    if (!override) return item
    if (override === 'build') return { ...item, state: 'build' }
    if (override === 'refine') return { ...item, state: 'refine' }
    if (override.startsWith('build_failed:')) {
      return { ...item, state: 'build_failed', reason: override.slice('build_failed:'.length) }
    }
    return item
  }

  const handleRefined = (toolCallId) => {
    setPlanOverrides((prev) => ({ ...prev, [toolCallId]: 'refine' }))
    setLivePlanOptions((prev) => (prev && prev.toolCallId === toolCallId ? null : prev))
  }

  // Reset the terminal banners so the operator can start fresh (Start-again / Dismiss).
  const handleStartAgain = () => {
    session.reset()
    sessionChatRef.current = null
    sessionProjectRef.current = null
    inputRef.current?.focus()
  }

  const previewStatus = showSession ? session.status : null
  // U15: the live session's stored half. While the LIVE bubble below re-tells exactly this
  // session (reattach replays its envelopes), the hydrated step/in-progress rows that belong
  // to it are suppressed — one narrative, told once. Rows from OLDER builds always render.
  // The seq of the `build_in_progress` anchor belonging to the ATTACHED session, or null.
  // Rows at or after it describe the build the live bubble is currently speaking for.
  const attachedAnchorSeq = useMemo(() => {
    if (!showSession) return null
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const anchor = (messages[i].parts || []).find((p) => p?.type === 'build_in_progress')
      if (anchor) return anchor.sessionId === session.sessionId ? messages[i].seq : null
    }
    return null
  }, [messages, showSession, session.sessionId])
  // CC2 — STORED STEPS ARE SUPPRESSED ONLY WHEN THE LIVE BUBBLE CAN ACTUALLY RE-TELL THEM.
  //
  // The one-narrative rule is right for the tab that STARTED the build: its bubble holds every
  // envelope, so rendering the stored rows too would double-render each step. But the
  // justification in the old comment — "reattach replays its envelopes" — is false. `reattach()`
  // calls `reset()`, which CLEARS `envelopes`, then subscribes to the live feed from the current
  // point. It replays nothing. So a reload ten minutes into a build suppressed the entire visible
  // history and re-told none of it: the transcript went blank.
  //
  // The discriminator is whether THIS page instance has envelopes to show — not "is a session
  // attached" (the reloaded tab has one, holding nothing), and not the blunt "stop suppressing"
  // (which double-renders in the originating tab). A server-side replay would be better still,
  // but it needs a cursor on the build-session feed: backend work, outside this unit.
  const liveStoryAnchorSeq = session.envelopes.length > 0 ? attachedAnchorSeq : null
  // The #43 "come back later" journey: after a reload there is no live session, but a persisted
  // BuildOutcome part proves a build once ran — so the preview pane must offer the terminal
  // placeholder (with its Relaunch action), not the idle "submit a prompt" empty state. The
  // NEWEST outcome also drives the "Relaunch last saved version" label when that build failed
  // (paired with the server's restoredFromFailedBuild flag). Derived from the transcript so it
  // survives a reload; a live/reattached session (showSession) always wins below.
  const newestOutcome = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const part = (messages[i].parts || []).find((p) => p?.type === 'build')
      if (part) return part
    }
    return null
  }, [messages])
  // A relaunched preview (#43) is a framed URL with NO build lifecycle: it takes precedence over the
  // (ended) session's dead preview so the pane frames the RESTORED app — while `buildActive`/`previewStatus`
  // keep reading the session's own `ended` status, so Stop / delete-gate never light up on a relaunch.
  const relaunchedUrl = sessionProjectMatches ? session.relaunchedPreviewUrl : null
  // A live Write turn's preview outranks both: it is the app the user is watching being
  // built right now, and the session's url (when there is one at all) describes the previous
  // build. Keyed on the url so a NEW sandbox remounts the iframe rather than showing a frame
  // pointing at a container that no longer exists.
  const framedPreviewUrl = (turnNarrativeIsThisChat ? turnPreview.url : null) ?? relaunchedUrl ?? (showSession ? session.previewUrl : null)
  const framedStatus = turnBuildStatus ?? (relaunchedUrl ? 'ready' : (previewStatus ?? (newestOutcome ? 'ended' : null)))
  // The build bubble's two sources, resolved ONCE — the turn wins when it has something to say.
  // Naming them here is what lets the wrapper ask `hasBuildNarrative` the same question
  // BuildProgress asks itself, instead of rendering chrome around a `null`.
  const narratingTurn = turnBuildStatus !== null
  const buildEnvelopes = narratingTurn ? turnEnvelopes : session.envelopes
  const buildStatus = turnBuildStatus ?? session.status
  // #13/R2 — "done, preview live": this LIVE session completed, so the server pardoned its
  // container (idle lease) and the framed URL still serves. Gated on `showSession`
  // deliberately: a reloaded page (no live session, `framedStatus` synthesized from the
  // transcript's newest outcome) keeps the terminal placeholder + Relaunch — never coerce
  // "no live status" into a prior build's live-preview claim (the framedStatus lesson).
  // TWO HALVES OF ONE DECISION, and they must read the same source. `framedStatus` now comes
  // from the turn, so leaving this on the session meant a completed Write build showed
  // "The preview is no longer running" + Relaunch over an app the server had just PARDONED and
  // that was still serving — the framedStatus lesson, reintroduced from the other side.
  //
  // Keyed on `turnTerminal` (this tab watched the turn complete) rather than on `framedStatus`:
  // a reloaded page has no live turn and must keep the terminal placeholder, which is exactly
  // what the comment above defends.
  const completedLive =
    (turnNarrativeIsThisChat && turnTerminal === 'completed' && turnPreview.url != null) ||
    (showSession && session.status === 'ended' && session.endReason === 'completed')
  const handleRelaunch = () => {
    if (!projectId) return
    // Stamp the project so the relaunch surfaces (Restoring…, the framed URL, its errors) render:
    // on a fresh mount no session originated here, so the ref is unset and every
    // sessionProjectMatches gate would otherwise drop the relaunch state on the floor (#43).
    sessionProjectRef.current = projectId
    void session.relaunch(projectId)
  }

  return (
    <div className="h-screen flex flex-col font-manrope bg-bial-bg overflow-hidden">
      <Navbar />

      <div className="flex flex-1 overflow-hidden">
        {/* Chat panel */}
        <div className="w-72 xl:w-80 flex flex-col bg-white border-r border-bial-border flex-shrink-0">
          {/* Chat header: back-navigation + project name ONLY (F7/F10). The redundant in-rail
              usage meter (the Navbar shows real usage), the AI branding block + avatar, and the
              "Recent" builds dropdown are gone — past conversations live on the project page the
              breadcrumb links back to (ProjectPage lists every conversation). */}
          <div className="p-4 border-b border-bial-border">
            <ProjectBreadcrumb projectId={projectId} projectName={projectName} />
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto p-4 space-y-3 scrollbar-thin">
            {messages.map((msg) => {
              const buildPart = (msg.parts || []).find((p) => p?.type === 'build')
              const planPart = (msg.parts || []).find((p) => p?.type === 'plan_options')
              const stepPart = (msg.parts || []).find((p) => p?.type === 'step')
              const inProgressPart = (msg.parts || []).find((p) => p?.type === 'build_in_progress')
              // TWO DIFFERENT QUESTIONS, conflated until CC2 (and that is why fixing the step
              // half alone broke the anchor half):
              //
              //   STEP rows ask "is this story already being told?" — true only while the live
              //   bubble actually holds envelopes, so a reloaded tab renders its history.
              //
              //   The ANCHOR row ask "is this build over?" — it is the past-tense "a build was
              //   running here" line, and it must stay hidden for as long as a live session is
              //   ATTACHED, envelopes or not. A reloaded tab has no envelopes yet but the build
              //   is still running; showing the anchor there narrates a finished build over a
              //   live one.
              const stepSuperseded =
                liveStoryAnchorSeq != null && msg.seq != null && msg.seq >= liveStoryAnchorSeq
              const anchorSuperseded =
                attachedAnchorSeq != null && msg.seq != null && msg.seq >= attachedAnchorSeq
              if (stepPart && stepSuperseded) return null
              if (inProgressPart && anchorSuperseded) return null
              if (stepPart) {
                // A stored friendly step (U6 projection) — the reload half of the build narrative,
                // rendered through the SAME `ToolActivityLine` atom the live feed uses (F3/U3), so
                // live and reload can never diverge.
                return (
                  <div key={msg.id} className="ml-8" data-kind="stored-step" data-state={stepPart.step.state}>
                    <ToolActivityLine label={stepPart.step.label} state={stepPart.step.state} />
                  </div>
                )
              }
              if (inProgressPart) {
                // A build began here and no outcome closed it — and no live session is
                // re-telling it (that case returned above): reattach lost it or the server
                // crashed mid-build. State the durable truth rather than a dead spinner.
                return (
                  <div key={msg.id} className="ml-8 text-xs text-neutral" data-kind="build-in-progress">
                    A build was running here when this chat was last open.
                  </div>
                )
              }
              // A just-created assistant turn is empty until the first delta lands; a pure
              // card row has no prose. Render the bubble only when it would say something.
              const bodyParts = msg.parts
              const hasBody =
                partsToText(bodyParts) !== '' || attachmentsFromParts(msg.parts).length > 0
              return (
                <div key={msg.id}>
                  <div className={`flex gap-2 ${msg.role === 'user' ? 'flex-row-reverse' : ''}`}>
                    <div className={`w-6 h-6 rounded-full flex items-center justify-center flex-shrink-0 mt-0.5 ${msg.role === 'assistant' ? 'bg-primary/10' : 'bg-secondary/10'}`}>
                      {msg.role === 'assistant'
                        ? <Sparkles size={10} className="text-primary" />
                        : <User size={10} className="text-secondary" />
                      }
                    </div>
                    <div className="max-w-[85%] min-w-0">
                      {hasBody && (
                        <div className={`rounded-2xl px-3 py-2.5 text-xs leading-relaxed ${
                          msg.role === 'user'
                            ? 'bg-tertiary text-white rounded-tr-sm'
                            : 'bg-bial-bg text-tertiary rounded-tl-sm'
                        }`}>
                          <MessageContent parts={bodyParts} />
                          <p className="text-[10px] mt-1 opacity-40">
                            {msg.createdAt ? new Date(msg.createdAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}
                          </p>
                        </div>
                      )}
                      {buildPart && (
                        <BuildOutcome
                          part={buildPart}
                          live={
                            showSession &&
                            isActiveBuildStatus(session.status) &&
                            session.previewUrl === buildPart.previewUrl
                          }
                        />
                      )}
                      {planPart && (
                        <div className="mt-2" data-testid="plan-options-card">
                          <PlanOptionsCard
                            conversationId={buildId}
                            item={applyPlanOverride(planPart.item)}
                            expired={planPart.item.toolCallId !== newestPlanCallId}
                            onBuildIt={(id) => void handleBuildIt(id)}
                            onRefined={handleRefined}
                          />
                          {planErrors[planPart.item.toolCallId] && (
                            <p className="mt-1 text-[11px] text-danger" role="alert">
                              {planErrors[planPart.item.toolCallId]}
                            </p>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              )
            })}

            {/* The LIVE plan-options card: streamed by this turn's frames; on reload the
                projection row takes its place identically. Hidden once that row exists. */}
            {livePlanOptions &&
              !messages.some((m) =>
                (m.parts || []).some(
                  (pp) => pp?.type === 'plan_options' && pp.item.toolCallId === livePlanOptions.toolCallId,
                ),
              ) && (
                <div className="ml-8" data-testid="plan-options-card">
                  <PlanOptionsCard
                    conversationId={buildId}
                    item={applyPlanOverride(livePlanOptions)}
                    onBuildIt={(id) => void handleBuildIt(id)}
                    onRefined={handleRefined}
                  />
                  {planErrors[livePlanOptions.toolCallId] && (
                    <p className="mt-1 text-[11px] text-danger" role="alert">
                      {planErrors[livePlanOptions.toolCallId]}
                    </p>
                  )}
                </div>
              )}

            {/* The assistant is composing a reply. Shown only until the first delta lands —
                after that the streaming bubble is the feedback. */}
            {generating && partsToText(messages[messages.length - 1]?.parts) === '' && (
              <div className="flex gap-2 items-center">
                <div className="w-6 h-6 rounded-full bg-primary/10 flex items-center justify-center flex-shrink-0">
                  <Sparkles size={10} className="text-primary" />
                </div>
                <div className="bg-bial-bg rounded-2xl px-3 py-2.5 flex gap-1" role="status" aria-label="Thinking">
                  {[0, 1, 2].map((i) => (
                    <div key={i} className="w-1.5 h-1.5 bg-primary rounded-full animate-bounce" style={{ animationDelay: `${i * 0.15}s` }} />
                  ))}
                </div>
              </div>
            )}

            {/* The assistant's build turn — the whole live narrative in ONE bubble (U15):
                headline + friendly steps + elapsed time + Stop/Force-end, raw output behind
                Details. The right pane frames only the app now. */}
            {/* CC3 — scoped to the chat that OWNS the session, not merely to its project.
                `showSession` is project-scoped while the composer's gate is chat-scoped, so a
                sibling chat rendered another chat's build narrative complete with a WORKING Stop
                button: one click ended a build the reader had not started and could not see the
                composer state of. */}
            {/* AND the narrative's own emptiness check (2026-07-30): the wrapper used to render
                on state alone, so a terminal with nothing to say left an empty grey bubble
                sitting under the answer. */}
            {(narratingTurn || (showSession && sessionChatRef.current === buildId && (buildActive || session.envelopes.length > 0))) && hasBuildNarrative(buildStatus, buildEnvelopes) && (
              <div className="flex gap-2" data-testid="build-bubble">
                <div className="w-6 h-6 rounded-full bg-primary/10 flex items-center justify-center flex-shrink-0 mt-0.5">
                  <Sparkles size={10} className="text-primary" />
                </div>
                <div className="max-w-[85%] min-w-0 flex-1 rounded-2xl rounded-tl-sm px-3 py-2.5 leading-relaxed bg-bial-bg text-tertiary">
                  {/* ONE bubble, two sources. A Write turn narrates itself through turn frames
                      (U5); a legacy build session still narrates through the C7 feed until U5b
                      retires it. The turn wins when it has something to say, and the adapter
                      means both reach the SAME renderer — so the two can never tell different
                      stories about what a build looks like. */}
                  <BuildProgress
                    envelopes={buildEnvelopes}
                    status={buildStatus}
                    startedAt={turnBuildStatus !== null ? turnStartedAt : session.startedAt}
                    stopping={turnBuildStatus !== null ? stoppingTurn : session.stopping}
                    onStop={turnBuildStatus !== null ? handleStopTurn : () => session.stop()}
                    // undefined on a turn build: force-end has no turn equivalent, and
                    // BuildProgress hides the button rather than render a kill switch that
                    // confirms "this kills in-progress work" and then does nothing.
                    onForceEnd={turnBuildStatus === null ? () => session.forceEnd() : undefined}
                  />
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          {/* Input */}
          <div className="p-3 border-t border-bial-border space-y-2">
            {/* Session lifecycle banners (U15) — right where the operator is looking. */}
            <SessionBanners
              blocked={sessionProjectMatches ? session.blocked : null}
              reclaimed={showSession && session.reclaimed}
              feedDisconnected={showSession && session.feedDisconnected}
              quota={showSession ? session.quota : null}
              onForceEnd={(sid) => session.forceEnd(sid)}
              onReconnect={() => session.reconnect()}
              onStartAgain={handleStartAgain}
            />
            {turnError && (
              <div className="text-[11px] text-danger bg-danger/5 border border-danger/20 rounded-lg px-2.5 py-1.5">
                {turnError}
              </div>
            )}
            {sessionProjectMatches && session.error && (
              <div
                aria-live="assertive"
                className="text-[11px] text-danger bg-danger/5 border border-danger/20 rounded-lg px-2.5 py-1.5"
              >
                {session.error}
              </div>
            )}
            {/* Pending attachment preview row */}
            {pendingAttachments.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {pendingAttachments.map((a) => (
                  <div
                    key={a.id}
                    className="flex items-center gap-1 bg-bial-bg border border-bial-border rounded-lg px-1.5 py-1 text-[11px] text-tertiary"
                  >
                    {TEXT_MEDIA_TYPES.has(a.mediaType) ? (
                      <span className="flex-shrink-0 text-primary" title={a.name}>
                        {a.mediaType === 'text/csv' ? <FileSpreadsheet size={11} /> : <FileText size={11} />}
                      </span>
                    ) : OFFICE_MEDIA_TYPES.has(a.mediaType) ? (
                      <span className="flex-shrink-0 text-primary" title={a.name}>
                        {officeFormat(a.mediaType) === 'excel' ? <FileSpreadsheet size={11} /> : <FileText size={11} />}
                      </span>
                    ) : DECK_MEDIA_TYPES.has(a.mediaType) ? (
                      <span className="flex-shrink-0 text-primary" title={a.name}>
                        <Presentation size={11} />
                      </span>
                    ) : a.mediaType === 'application/pdf' ? (
                      <button
                        type="button"
                        onClick={() => openPdf(a.base64, a.name)}
                        title={`Open ${a.name}`}
                        className="flex-shrink-0 text-primary hover:opacity-80 transition"
                      >
                        <FileText size={11} />
                      </button>
                    ) : (
                      <img
                        src={`data:${a.mediaType};base64,${a.base64}`}
                        alt={a.name}
                        title={`View ${a.name}`}
                        onClick={() => setViewer({ name: a.name, src: `data:${a.mediaType};base64,${a.base64}` })}
                        className="h-6 w-6 object-cover rounded cursor-zoom-in hover:opacity-90 transition"
                      />
                    )}
                    <span className="truncate max-w-[7rem]">{a.name}</span>
                    <button onClick={() => removePending(a.id)} className="text-neutral hover:text-danger transition" title="Remove">
                      <X size={11} />
                    </button>
                  </div>
                ))}
              </div>
            )}

            {/* F5/U6: the compact in-composer mode switch. It STAYS disabled mid-turn, and for a
                better reason than the one it used to carry: the server stamps the running turn's
                rows with the conversation's mode and captures the build's entry_mode from it, so
                switching mid-run would retroactively mislabel work already in flight — it answers
                409, and this mirrors a real server rule (KTD-4). It is emphatically NOT a mode
                gate on the composer; mode never gates the composer at all. */}
            {chatMode && buildId && (
              <div className="flex items-center">
                <ModeSwitcher
                  value={chatMode}
                  onSelect={handleModeSelect}
                  disabled={turnInFlight || switchingMode}
                  composerRef={inputRef}
                />
              </div>
            )}
            {/* WHY SEND IS UNAVAILABLE — always stated, with a distinct reason per cause. Plain
                language, no product vocabulary: a citizen reading this has asked for an app and is
                watching it get made. Note what the copy no longer says — the chat does not "open
                back up", because it never closed; only sending waits. */}
            {sendUnavailable && (
              <p className="text-[11px] text-neutral" data-testid="composer-gate-note" role="status">
                {gateCheck === 'unreachable' ? (
                  <>
                    Couldn’t check whether a build is running here.{' '}
                    <button
                      type="button"
                      onClick={retryGateCheck}
                      className="underline underline-offset-2 text-primary hover:text-primary-600"
                    >
                      Retry
                    </button>
                  </>
                ) : gateCheck === 'checking' ? (
                  'Checking whether a build is still running here…'
                ) : buildActiveHere || buildStarting ? (
                  'Building your app — keep typing if you like; send unlocks when it’s done.'
                ) : (
                  'Replying — keep typing if you like; send unlocks when it’s done.'
                )}
              </p>
            )}
            <div className="flex gap-2 items-end">
              <input
                ref={fileInputRef}
                type="file"
                accept={ACCEPT_ATTR}
                multiple
                onChange={handleFileSelect}
                className="hidden"
              />
              {/* Attach NEVER disables. Staging a file is composing, not sending — and the
                  attachment rides the next send, which is exactly the message being composed. */}
              <button
                onClick={() => fileInputRef.current?.click()}
                title="Attach images, PDFs, Word, Excel, or text files (CSV, TXT)"
                className="flex-shrink-0 w-9 h-9 bg-bial-bg hover:bg-surface-muted text-neutral hover:text-primary border border-bial-border rounded-xl flex items-center justify-center transition"
              >
                <Paperclip size={13} />
              </button>
              {/* THE TEXTAREA IS NEVER DISABLED, and that is the whole focus fix (KTD-3):
                  `disabled` on the currently-focused element blurs it and drops focus to
                  `document.body`, which is the mechanism behind "it blurs mid-sentence and focus
                  never comes back". There is also no programmatic focus grab at the turn's start
                  or terminal — stealing focus at an async moment is its own bug class. */}
              <textarea
                ref={inputRef}
                value={input}
                onChange={(e) => {
                  setInput(e.target.value)
                  writeDraft(buildIdRef.current, e.target.value)
                }}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() } }}
                rows={2}
                placeholder="Describe what you need, or ask for a change…"
                className="flex-1 resize-none text-xs text-tertiary bg-bial-bg border border-bial-border rounded-xl px-3 py-2 focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20 transition placeholder:text-gray-300"
              />
              {/* `aria-disabled`, never `disabled` (KTD-2). A keyboard user who has tabbed to Send
                  when a turn starts would otherwise be blurred to `body` mid-compose — the same
                  mechanism as the textarea, on the control they are actually standing on.
                  `handleSend` is the enforcement; this is affordance only. */}
              <button
                onClick={handleSend}
                aria-disabled={sendUnavailable || (!input.trim() && pendingAttachments.length === 0)}
                className={`flex-shrink-0 w-9 h-9 bg-secondary text-white rounded-xl flex items-center justify-center transition ${
                  sendUnavailable || (!input.trim() && pendingAttachments.length === 0)
                    ? 'opacity-40 cursor-default'
                    : 'hover:bg-secondary-600'
                }`}
              >
                <Send size={13} />
              </button>
            </div>
          </div>
        </div>

        {/* The right pane is the APP (U15): LivePreview is its sole child. The build
            narrative lives in the chat's BuildProgress bubble; the lifecycle banners sit
            above the composer. The cockpit (ActivityFeed + SessionControls) is retired. */}
        <div className="flex-1 overflow-hidden">
          <LivePreview
            previewUrl={framedPreviewUrl}
            status={framedStatus}
            iterating={showSession && session.iterating}
            onRelaunch={handleRelaunch}
            relaunching={sessionProjectMatches && session.relaunching}
            relaunchError={sessionProjectMatches ? session.relaunchError : null}
            lastBuildFailed={newestOutcome?.status === 'failed'}
            restoredFromFailedBuild={relaunchedUrl != null && session.relaunchedFromFailedBuild}
            completedLive={completedLive}
            hasSavedBuild={projectHasSavedBuild}
            saveDirty={saveDirty}
            onSave={handleSave}
            saving={saving}
            saveError={saveError}
            reconnecting={(turnNarrativeIsThisChat && turnPreview.state === 'reconnecting') || (showSession && session.reconnecting)}
          />
        </div>
      </div>

      {/* Attachment validation / cap toast. Announced, because KTD-2 deliberately keeps the send
          button focusable — a screen-reader user who activates it gets no state change to hear,
          and this toast is the only feedback there is. */}
      {attachToast && (
        <div
          role="status"
          aria-live="polite"
          className="fixed bottom-6 left-6 z-50 bg-white border border-bial-border rounded-xl shadow-xl px-4 py-3 text-sm text-tertiary font-medium max-w-xs"
        >
          {attachToast}
        </div>
      )}

      {/* Pending-attachment image lightbox */}
      {viewer && (
        <AttachmentLightbox name={viewer.name} src={viewer.src} onClose={() => setViewer(null)} />
      )}
    </div>
  )
}
