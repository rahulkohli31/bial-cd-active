import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { useNavigate, useLocation, useParams } from 'react-router-dom'
import {
  Send, Sparkles, User, Paperclip, FileText, FileSpreadsheet, Presentation, History, Trash2, X,
  CheckCircle2, XCircle, ExternalLink,
} from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import LivePreview from '../components/LivePreview'
import BuildProgress from '../components/chat/BuildProgress'
import SessionBanners from '../components/chat/SessionBanners'
import AttachmentChips from '../components/AttachmentChips'
import AttachmentLightbox from '../components/AttachmentLightbox'
import ProjectBreadcrumb from '../components/projects/ProjectBreadcrumb'
import { listProjectConversations } from '../utils/conversationApi'
import { describeSaveFailure, isConversationGone } from '../utils/chatErrors'
import { createBuildLock, openBuildLockChannel } from '../utils/buildLock'
import { useDropTransientQuery } from '../hooks/useDropTransientQuery'
import { useBuildSession } from '../hooks/useBuildSession'
import { isActiveBuildStatus } from '../utils/buildSessionTypes'
import { usePendingAttachments } from '../hooks/usePendingAttachments'
import { startTurn, readTurnStream, buildFromPlan, TurnStartError } from '../utils/turnStreamApi'
import { PlanOptionsCard } from '../components/chat/PlanOptionsCard'
import { ModeToggle } from '../components/chat/ModeToggle'
import { UsageMeter } from '../components/chat/UsageMeter'
import { wireMessageFromParts, buildUserParts, partsToText, attachmentsFromParts, countAttachments, releaseUploadedAttachments } from '../utils/attachmentStore'
import { ACCEPT_ATTR, validateConversationAttachmentCap, TEXT_MEDIA_TYPES, OFFICE_MEDIA_TYPES, DECK_MEDIA_TYPES, officeFormat } from '../utils/attachmentInput'
import { openPdf } from '../utils/attachmentViewer'
import { loadBuilds, createBuild, getBuild, deleteBuild, deriveTitle } from '../utils/builderHistory'
import { relativeTime } from '../utils/chatHistory'

// The from-scratch greeting (ephemeral — never persisted, and never sent to the model: it is
// chrome, not a turn, and replaying it as history would have the model answering its own hello).
const WELCOME_TEXT = "Hello! I'm Citizen Developer AI. Tell me what you'd like to build for BIAL operations."
const welcomeMessage = () => ({ id: 'welcome', ephemeral: true, role: 'assistant', parts: [{ type: 'text', text: WELCOME_TEXT }], createdAt: new Date().toISOString() })

// U7: the whole system prompt is server-owned now (`backend/src/api/v1/claude/prompts.py`,
// selected by the conversation's kind) — the thin client identity line moved there as
// ASSISTANT_IDENTITY_PROMPT, and the interview protocol keeps riding server-side.

const REFINEMENT_CHIPS = [
  'Change the theme to dark mode',
  'Add a real-time data table',
  'Switch to mobile layout',
]

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
 * THE ROUTING RULE (load-bearing — read before changing any send path). EVERY composer send goes
 * to the chat relay. A build starts ONLY when the user confirms a brief card. That holds for the
 * first build AND for iteration ("add a chart" is a send, which returns an updated brief, which
 * the user confirms). The page used to fire a build directly on send, which is exactly what let
 * the agent silently guess at a vague prompt and build the wrong app; the interview protocol
 * (server-side, `api/v1/claude/prompts.py`) is what asks instead, and the card is what makes the
 * user's confirmation the trigger.
 *
 * "Existing refine semantics" now means the SESSION MECHANICS behind the card — stop()+start() on
 * a live session — not a direct-fire send.
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
export default function BuilderPage({ chatId: chatIdProp, projectId = null, projectName = null, projectAppId = null, buildSessionDeps } = {}) {
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
  const [showBuilds, setShowBuilds] = useState(false)
  const [viewer, setViewer] = useState(null) // { name, src } for the pending-attachment lightbox
  const [generating, setGenerating] = useState(false) // a turn is streaming
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
  // The chat + project that ORIGINATED the live session (for attribution + the render gate). The
  // session is project-scoped, so its surfaces render only while viewing a chat of ITS project.
  const sessionChatRef = useRef(null)
  const sessionProjectRef = useRef(null)
  // `sendingRef` flips synchronously before the first await so a second Enter — or the seeded prompt
  // racing a manual send — cannot start a second session (the one-per-user 409 collision).
  const sendingRef = useRef(false)
  const seqRef = useRef(0) // next message sort key for the active build's persisted turns
  const deletedRef = useRef(new Set()) // builds deleted mid-run

  // One build at a time, per project — advisory (KTD-7): `blockedBy` is the instant cross-tab
  // pre-check; the authoritative barrier is C3 start's 409. A crashed tab's claim expires, so the
  // channel is the only way claims travel (factory, not a module singleton).
  const buildLockRef = useRef(null)
  if (buildLockRef.current === null) buildLockRef.current = createBuildLock({ channel: openBuildLockChannel() })

  useEffect(() => {
    const lock = buildLockRef.current
    return () => lock?.dispose()
  }, [])

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
    if (genuinelyEnded) buildLockRef.current?.release(chat)
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
    // routing). The composer draft belongs to the OLD chat — a leaked draft would send into this one.
    setMessages([])
    setInput('')
    clearPending()
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
        // A HANDED-OFF PROMPT FIRES EITHER WAY. The thread is canonical and permanent now
        // (003-U1), so it is empty exactly once in its life — every "Generate App" after the
        // first arrives at a thread with turns. Consuming the prompt only on the empty branch
        // meant the second build onward silently swallowed the user's typed prompt AND their
        // attachments: the composer was already cleared above, and nothing else reads
        // `location.state.prompt`. Fire-once is `initFiredRef` within a mount, and stripping the
        // state from history across mounts; `restored` is handed over so the send cannot race the
        // render that restores it.
        fireHandoffPrompt(buildId, () => alive, restored)
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
    if (!initialPrompt) return
    if (initFiredRef.current === id) return
    initFiredRef.current = id
    const attachments = location.state?.pendingAttachments || []
    // STRIP THE HANDOFF FROM HISTORY BEFORE FIRING. `initFiredRef` is a ref, so it only survives
    // within one mount — but a RELOAD is a fresh mount over the SAME history entry, and the
    // browser keeps router state across it. Left in place, every reload of a handed-off thread
    // re-sends the prompt: a duplicate turn, billed again, on a thread the user was only reading.
    window.history.replaceState({}, '', window.location.pathname + window.location.search)
    void fireRelayTurn(initialPrompt, attachments, id, { isAlive, prior })
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
    let assistantText = ''
    setGenerating(true)
    setTurnError(null)
    setMessages((prev) => [...prev, { id: assistantId, role: 'assistant', parts: [{ type: 'text', text: '' }], seq: assistantSeq, createdAt: new Date().toISOString() }])

    // U13: the turn API (U10). POST starts the turn DETACHED server-side; the subscription
    // below only observes — closing the tab never cancels the reply, and the server
    // persists both sides before the terminal (write-before-DONE).
    const wire = wireMessageFromParts(parts)
    let sawTerminal = null
    try {
      await startTurn(activeId, {
        text: wire.text ?? '',
        attachmentTexts: wire.attachmentTexts ?? [],
        attachmentIds: wire.attachmentIds ?? [],
      })
      streamAbortRef.current?.abort()
      const controller = new AbortController()
      streamAbortRef.current = controller
      const onFrame = (frame) => {
        if (buildIdRef.current !== activeId) return // navigated away — drop the frame
        if (frame.type === 'text_delta') {
          assistantText += frame.text
          setMessages((prev) => prev.map((m) => (m.id === assistantId ? { ...m, parts: [{ type: 'text', text: assistantText }] } : m)))
        } else if (frame.type === 'snapshot' && frame.textSoFar && frame.textSoFar.length > assistantText.length) {
          assistantText = frame.textSoFar
          setMessages((prev) => prev.map((m) => (m.id === assistantId ? { ...m, parts: [{ type: 'text', text: assistantText }] } : m)))
        } else if (frame.type === 'plan_options') {
          setLivePlanOptions(frame.item)
        } else if (frame.type === 'error') {
          setTurnError(frame.message)
        } else if (frame.type === 'turn_ended') {
          sawTerminal = frame.status
        }
      }
      let outcome = await readTurnStream({ conversationId: activeId, signal: controller.signal, onFrame })
      if (outcome === 'truncated' && !sawTerminal && !controller.signal.aborted) {
        // A dropped socket before the terminal: one resubscribe consolidates the turn so far
        // via the server snapshot then tails to the end (mirrors useConversationStream's
        // resume-once). A second truncation is a real drop — reload is the honest fallback.
        outcome = await readTurnStream({ conversationId: activeId, signal: controller.signal, onFrame })
      }
      if (outcome === 'stalled') setTurnError('The reply stalled. Reload to catch up.')
      else if (outcome === 'truncated' && !sawTerminal) setTurnError('The connection dropped. Reload to catch up.')
    } catch (err) {
      if (stillHere()) {
        setTurnError(err instanceof TurnStartError ? err.message : 'The message could not be sent. Try again.')
        setMessages((prev) => prev.filter((m) => m.id !== assistantId))
        seqRef.current = assistantSeq
      }
      setGenerating(false)
      return
    }
    setGenerating(false)

    if (stillHere()) {
      if (sawTerminal !== 'completed' && assistantText === '') {
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
    if (outcomeWrittenRef.current.has(outcome.sessionId)) return
    // Dedupe on sessionId: after a reload the transcript already holds the server's row, and a
    // replayed terminal would otherwise stack a second copy on top of it. `_id`/seq say nothing
    // about WHICH build a part describes; the session is the only thing that does.
    const already = messagesRef.current.some((m) =>
      (m.parts || []).some((p) => p?.type === 'build' && p.sessionId === outcome.sessionId),
    )
    outcomeWrittenRef.current.add(outcome.sessionId)
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
    // A reply already streaming is a state the user can SEE, so it earns an explanation. This must
    // stay ahead of the ref guard below, which answers a different question silently.
    if (generating) {
      showAttachToast('Please wait for the current reply to finish.')
      return
    }
    // Synchronous, and a REF rather than state: the two keydowns of a fast double-Enter land in the
    // SAME tick, so `generating` — set after an await — is still false for the second one. The
    // draft is deliberately held until the server confirms the turn, so that second read sees the
    // very same text and fires a second relay turn: a duplicate persisted message, a duplicate
    // model call, and on a builder thread two brief cards for one request. Silent on purpose —
    // this is one keystroke burst, not a second intention worth a toast.
    // `handleConfirmBrief` holds this same ref, for the same reason, over the build trigger.
    if (sendingRef.current) return
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

    sendingRef.current = true
    try {
      // The draft is held until the turn is STORED, then cleared by `onSent`. Clearing it here —
      // optimistically, on the click — is what made every save failure unrecoverable: the toast
      // tells the user to send it again, and their text and staged files are already gone.
      await fireRelayTurn(text, attachments, buildIdRef.current, {
        onSent: () => {
          setInput('')
          clearPending()
        },
      })
    } finally {
      sendingRef.current = false
    }
  }

  /**
   * Build it — the atomic U12 transition: ONE server call records the choice, flips the
   * conversation to Write, acquires the build lock, and starts the build. Every failure
   * is a typed outcome that re-arms the card; `started` hands back the session this page
   * attaches its cockpit to. Stale plans warn first — the user decides (force).
   */
  const handleBuildIt = async (toolCallId) => {
    if (sendingRef.current) return
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
    sendingRef.current = true
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
      if (outcome.outcome === 'started' || outcome.outcome === 'already_built') {
        setPlanOverrides((prev) => ({ ...prev, [toolCallId]: 'build' }))
        setChatMode('write') // the server flipped it atomically with the record
        if (outcome.sessionId) {
          sessionChatRef.current = activeBuildId
          sessionProjectRef.current = projectId
          buildLockRef.current?.acquire(projectId, activeBuildId)
          try {
            await session.reattach(outcome.sessionId)
            buildLockRef.current?.acquire(projectId, activeBuildId)
          } catch {
            buildLockRef.current?.release(activeBuildId)
            setPlanErrors((prev) => ({
              ...prev,
              [toolCallId]: 'The build started but this page could not join it — reload to watch it.',
            }))
          }
        }
      } else if (outcome.outcome === 'build_failed') {
        setPlanOverrides((prev) => ({
          ...prev,
          [toolCallId]: `build_failed:${outcome.reason ?? 'unknown'}`,
        }))
      }
    } catch (err) {
      setPlanErrors((prev) => ({
        ...prev,
        [toolCallId]: err?.message || 'The build could not be started. Try again.',
      }))
    } finally {
      sendingRef.current = false
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

  const handleSelectBuild = (id) => {
    setShowBuilds(false)
    if (id === buildIdRef.current) return
    setViewer(null)
    navigate(`/chat/${id}`)
  }

  const handleDeleteBuild = async (e, id) => {
    e.stopPropagation()
    deletedRef.current.add(id)
    if (id === buildIdRef.current) {
      setShowBuilds(false)
      setViewer(null)
      navigate(projectId ? `/projects/${projectId}` : '/projects', { replace: true })
    }
    setBuilds((prev) => prev.filter((b) => b.id !== id)) // optimistic removal
    try {
      await deleteBuild(id)
    } catch {
      deletedRef.current.delete(id)
      refreshBuilds()
      return
    }
    refreshBuilds()
  }

  // Reset the terminal banners so the operator can start fresh (Start-again / Dismiss).
  const handleStartAgain = () => {
    session.reset()
    sessionChatRef.current = null
    sessionProjectRef.current = null
    inputRef.current?.focus()
  }

  // The session's surfaces render only while viewing a chat of ITS project (it is project-scoped).
  // `blocked`/`error` come from attempts that FAILED to start (start()'s reset leaves sessionId
  // null), so they gate on the project stamp alone; the live surfaces also require a sessionId.
  const sessionProjectMatches = sessionProjectRef.current === projectId
  const showSession = session.sessionId != null && sessionProjectMatches
  const previewStatus = showSession ? session.status : null
  // A build chat's delete is gated while ITS session is live (deleting the chat that owns a running
  // build would strand it); a different chat deletes freely.
  const buildActive = showSession && isActiveBuildStatus(session.status)
  // U15: the live session's stored half. While the LIVE bubble below re-tells exactly this
  // session (reattach replays its envelopes), the hydrated step/in-progress rows that belong
  // to it are suppressed — one narrative, told once. Rows from OLDER builds always render.
  const liveStoryAnchorSeq = useMemo(() => {
    if (!showSession) return null
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const anchor = (messages[i].parts || []).find((p) => p?.type === 'build_in_progress')
      if (anchor) return anchor.sessionId === session.sessionId ? messages[i].seq : null
    }
    return null
  }, [messages, showSession, session.sessionId])
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
  const framedPreviewUrl = relaunchedUrl ?? (showSession ? session.previewUrl : null)
  const framedStatus = relaunchedUrl ? 'ready' : (previewStatus ?? (newestOutcome ? 'ended' : null))
  // #13/R2 — "done, preview live": this LIVE session completed, so the server pardoned its
  // container (idle lease) and the framed URL still serves. Gated on `showSession`
  // deliberately: a reloaded page (no live session, `framedStatus` synthesized from the
  // transcript's newest outcome) keeps the terminal placeholder + Relaunch — never coerce
  // "no live status" into a prior build's live-preview claim (the framedStatus lesson).
  const completedLive = showSession && session.status === 'ended' && session.endReason === 'completed'
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
          {/* Agent header */}
          <div className="p-4 border-b border-bial-border relative">
            <div className="flex items-center justify-between gap-2 mb-3">
              <ProjectBreadcrumb projectId={projectId} projectName={projectName} />
              <button
                onClick={() => { refreshBuilds(); setShowBuilds((s) => !s) }}
                title="Recent builds"
                className="flex items-center gap-1 p-1.5 rounded-lg text-neutral hover:text-primary hover:bg-bial-bg transition"
              >
                <History size={15} />
                <span className="text-[11px] font-semibold">Recent</span>
              </button>
            </div>

            {showBuilds && (
              <div className="absolute right-3 top-12 z-30 w-64 max-h-80 overflow-y-auto scrollbar-thin bg-white rounded-xl border border-bial-border shadow-xl py-2">
                {/* No "+ New" here. A project has ONE build thread (003-U1), so "a new build
                    chat" is not a thing you can make any more — and minting one would have done
                    real damage rather than nothing: under newest-wins the fresh empty row becomes
                    the project's canonical thread, orphaning the transcript that holds the app's
                    whole design history. This list is READ-ONLY history now (the plan's wording:
                    older builder chats stay reachable; only the canonical thread takes new work). */}
                <div className="px-3 py-1.5">
                  <p className="text-[10px] font-bold uppercase tracking-wider text-neutral">Recent builds</p>
                </div>
                {builds.length === 0 ? (
                  <p className="px-3 py-3 text-xs text-neutral text-center">No saved builds yet</p>
                ) : (
                  builds.map((b) => {
                    const gated = buildActive && b.id === sessionChatRef.current
                    return (
                      <div
                        key={b.id}
                        onClick={() => handleSelectBuild(b.id)}
                        className={`group relative mx-1.5 my-0.5 rounded-lg px-2.5 py-2 cursor-pointer transition ${
                          b.id === buildIdRef.current ? 'bg-bial-bg' : 'hover:bg-surface-muted'
                        }`}
                      >
                        <p className="text-xs font-semibold text-tertiary truncate pr-6">{b.title}</p>
                        <p className="text-[10px] text-neutral">{relativeTime(b.updatedAt)}</p>
                        <button
                          onClick={(e) => handleDeleteBuild(e, b.id)}
                          disabled={gated}
                          aria-label={`Delete ${b.title || 'build'}`}
                          title={gated ? 'Finishing a build — stop it first to delete this chat' : 'Delete build'}
                          className={`absolute right-1.5 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 transition p-1 ${
                            gated ? 'text-neutral/40 cursor-not-allowed' : 'text-neutral hover:text-danger'
                          }`}
                        >
                          <Trash2 size={11} />
                        </button>
                      </div>
                    )
                  })
                )}
              </div>
            )}

            <div className="flex items-center gap-3">
              <div className="relative">
                <div className="w-10 h-10 rounded-full bg-primary flex items-center justify-center">
                  <Sparkles size={17} className="text-white" />
                </div>
                <span className="absolute -bottom-0.5 -right-0.5 w-3 h-3 bg-green-400 rounded-full border-2 border-white" />
              </div>
              <div>
                <p className="text-sm font-bold text-tertiary">Citizen Developer AI</p>
                <p className="text-xs text-neutral">powered by Anthropic</p>
              </div>
            </div>

            {/* U13: the server-owned mode + the daily-usage meter. The switch is disabled
                while a reply streams or a build runs (between turns only). */}
            {chatMode && buildId && (
              <div className="mt-3 flex items-center justify-between gap-2">
                <ModeToggle
                  conversationId={buildId}
                  mode={chatMode}
                  disabled={generating || buildActive}
                  onSwitched={setChatMode}
                  onRefused={(message) => showAttachToast(message)}
                />
              </div>
            )}
            <div className="mt-2">
              <UsageMeter />
            </div>
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto p-4 space-y-3 scrollbar-thin">
            {messages.map((msg) => {
              const buildPart = (msg.parts || []).find((p) => p?.type === 'build')
              const planPart = (msg.parts || []).find((p) => p?.type === 'plan_options')
              const stepPart = (msg.parts || []).find((p) => p?.type === 'step')
              const inProgressPart = (msg.parts || []).find((p) => p?.type === 'build_in_progress')
              // U15: the live bubble below re-tells the attached session's story — its stored
              // rows stay silent until the session ends (older builds' rows always render).
              const supersededByLive =
                liveStoryAnchorSeq != null && msg.seq != null && msg.seq >= liveStoryAnchorSeq
              if ((stepPart || inProgressPart) && supersededByLive) return null
              if (stepPart) {
                // A stored friendly step (U6 projection) — the reload half of the build
                // narrative, compact and avatar-less like its live counterpart.
                return (
                  <div key={msg.id} className="ml-8 flex items-center gap-2 text-xs text-tertiary" data-kind="stored-step" data-state={stepPart.step.state}>
                    {stepPart.step.state === 'failed'
                      ? <XCircle size={13} className="text-danger flex-shrink-0" />
                      : <CheckCircle2 size={13} className={`flex-shrink-0 ${stepPart.step.state === 'ok' ? 'text-green-600' : 'text-neutral/40'}`} />}
                    <span className={stepPart.step.state === 'failed' ? 'text-danger' : ''}>{stepPart.step.label}</span>
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
            {showSession && (buildActive || session.envelopes.length > 0) && (
              <div className="flex gap-2">
                <div className="w-6 h-6 rounded-full bg-primary/10 flex items-center justify-center flex-shrink-0 mt-0.5">
                  <Sparkles size={10} className="text-primary" />
                </div>
                <div className="max-w-[85%] min-w-0 flex-1 rounded-2xl rounded-tl-sm px-3 py-2.5 leading-relaxed bg-bial-bg text-tertiary">
                  <BuildProgress
                    envelopes={session.envelopes}
                    status={session.status}
                    startedAt={session.startedAt}
                    stopping={session.stopping}
                    onStop={() => session.stop()}
                    onForceEnd={() => session.forceEnd()}
                  />
                  {session.status === 'ready' && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {REFINEMENT_CHIPS.map((chip) => (
                        <button
                          key={chip}
                          onClick={() => { setInput(chip); inputRef.current?.focus() }}
                          className="text-[10px] font-worksans text-neutral bg-white border border-bial-border rounded-full px-2.5 py-1 hover:border-primary hover:text-primary transition"
                        >
                          {chip}
                        </button>
                      ))}
                    </div>
                  )}
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

            <div className="flex gap-2 items-end">
              <input
                ref={fileInputRef}
                type="file"
                accept={ACCEPT_ATTR}
                multiple
                onChange={handleFileSelect}
                className="hidden"
              />
              <button
                onClick={() => fileInputRef.current?.click()}
                title="Attach images, PDFs, Word, Excel, or text files (CSV, TXT)"
                className="flex-shrink-0 w-9 h-9 bg-bial-bg hover:bg-surface-muted text-neutral hover:text-primary border border-bial-border rounded-xl flex items-center justify-center transition"
              >
                <Paperclip size={13} />
              </button>
              <textarea
                ref={inputRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() } }}
                rows={2}
                placeholder="Describe what you need, or ask for a change…"
                className="flex-1 resize-none text-xs text-tertiary bg-bial-bg border border-bial-border rounded-xl px-3 py-2 focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20 transition placeholder:text-gray-300"
              />
              <button
                onClick={handleSend}
                // Deliberately NOT disabled while a build runs: talking to the assistant during a
                // build is how the next change gets specified. Only an in-flight REPLY gates it.
                disabled={generating || (!input.trim() && pendingAttachments.length === 0)}
                className="flex-shrink-0 w-9 h-9 bg-secondary hover:bg-secondary-600 disabled:opacity-40 text-white rounded-xl flex items-center justify-center transition"
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
            projectHasApp={projectAppId != null}
          />
        </div>
      </div>

      {/* Attachment validation / cap toast */}
      {attachToast && (
        <div className="fixed bottom-6 left-6 z-50 bg-white border border-bial-border rounded-xl shadow-xl px-4 py-3 text-sm text-tertiary font-medium max-w-xs">
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
