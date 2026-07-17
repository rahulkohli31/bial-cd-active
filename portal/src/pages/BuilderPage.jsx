import { useState, useEffect, useRef, useCallback } from 'react'
import { useNavigate, useLocation, useParams } from 'react-router-dom'
import {
  Send, Sparkles, User, Paperclip, FileText, FileSpreadsheet, Presentation, History, Trash2, X,
  CheckCircle2, XCircle, ExternalLink,
} from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import LivePreview from '../components/LivePreview'
import ActivityFeed from '../components/ActivityFeed'
import SessionControls from '../components/SessionControls'
import AttachmentChips from '../components/AttachmentChips'
import AttachmentLightbox from '../components/AttachmentLightbox'
import ProjectBreadcrumb from '../components/projects/ProjectBreadcrumb'
import { listProjectConversations } from '../utils/conversationApi'
import { describeSaveFailure, isConversationGone } from '../utils/chatErrors'
import { createBuildLock, openBuildLockChannel } from '../utils/buildLock'
import { useDropTransientQuery } from '../hooks/useDropTransientQuery'
import { useBuildSession } from '../hooks/useBuildSession'
import { buildSessionClient } from '../utils/buildSessionApi'
import { isActiveBuildStatus } from '../utils/buildSessionTypes'
import { usePendingAttachments } from '../hooks/usePendingAttachments'
import { useClaudeAPI } from '../hooks/useClaudeAPI'
import { parseBuildBrief } from '../utils/buildBrief'
import BuildBriefCard from '../components/chat/BuildBriefCard'
import { assembleApiMessages, buildUserParts, partsToText, attachmentsFromParts, countAttachments, releaseUploadedAttachments } from '../utils/attachmentStore'
import { ACCEPT_ATTR, validateConversationAttachmentCap, TEXT_MEDIA_TYPES, OFFICE_MEDIA_TYPES, DECK_MEDIA_TYPES, officeFormat } from '../utils/attachmentInput'
import { openPdf } from '../utils/attachmentViewer'
import { loadBuilds, newBuild, appendBuilderMessage, getBuild, deleteBuild, deriveTitle } from '../utils/builderHistory'
import { relativeTime } from '../utils/chatHistory'

// The from-scratch greeting (ephemeral — never persisted, and never sent to the model: it is
// chrome, not a turn, and replaying it as history would have the model answering its own hello).
const WELCOME_TEXT = "Hello! I'm Citizen Developer AI. Tell me what you'd like to build for BIAL operations."
const welcomeMessage = () => ({ id: 'welcome', ephemeral: true, role: 'assistant', parts: [{ type: 'text', text: WELCOME_TEXT }], createdAt: new Date().toISOString() })

/**
 * The client half of the thread's system prompt. Deliberately thin: the INTERVIEW PROTOCOL — the
 * part that actually governs the conversation — is appended server-side for builder-kind
 * conversations (`backend/src/api/v1/claude/prompts.py`), where it is tamper-proof and cannot
 * drift from the fence the parser expects. Anything load-bearing belongs there, not here.
 */
const THREAD_SYSTEM_PROMPT = `You are Citizen Developer AI, the assistant for the Bengaluru International Airport (BIAL) Citizen Developer Portal, powered by Anthropic Claude. You are talking to airport staff who are not developers. Keep replies short, concrete, and free of jargon — they are busy.`

const REFINEMENT_CHIPS = [
  'Change the theme to dark mode',
  'Add a real-time data table',
  'Switch to mobile layout',
]

// The LIVE half of a build turn — a single, non-persisted status line derived from the session
// (the activity feed on the right carries the detail). KTD-8: the feed IS the build narrative, and
// none of it is worth persisting.
//
// The two TERMINALS are deliberately absent. They used to render here, but a finished build now
// appends a real `build`-part message (003-U5) that says the same thing permanently — so keeping
// the ephemeral line would print the outcome twice, once in a bubble that vanishes on reload and
// once in one that does not. Live status while it runs; a record once it is done.
function assistantStatusLine(status) {
  switch (status) {
    case 'provisioning':
    case 'building':
      return 'Building your app — watch the progress and preview on the right.'
    case 'ready':
      return 'Your app preview is live on the right. Tell me what to change.'
    default:
      return null
  }
}

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

/** The persisted build outcome (003-U5) — a compact, permanent record of one build turn. */
function BuildOutcome({ part }) {
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
      {part.previewUrl && (
        // The preview is a per-session sandbox that dies with the session, so this link is a
        // record of what ran, not a promise it is still up. Say that, rather than let a user
        // click into a dead frame expecting their app.
        <a
          href={part.previewUrl}
          target="_blank"
          rel="noreferrer"
          className="mt-1.5 inline-flex items-center gap-1 text-[10px] font-semibold text-primary hover:underline break-all"
        >
          <ExternalLink size={9} className="flex-shrink-0" />
          Open the preview from this build
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
export default function BuilderPage({ chatId: chatIdProp, projectId = null, projectName = null, buildSessionDeps } = {}) {
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

  // The one build-session owner (feed + preview + status + keep-alive timers). The client is also
  // held directly for the 409 → getStatus reattach/block decision (the hook's reattach can't know
  // the "current project"). Tests inject a mock client + FakeEventSource via `buildSessionDeps`.
  const session = useBuildSession(buildSessionDeps ?? {})
  const sessionClient = buildSessionDeps?.client ?? buildSessionClient

  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [builds, setBuilds] = useState([])
  const [showBuilds, setShowBuilds] = useState(false)
  const [viewer, setViewer] = useState(null) // { name, src } for the pending-attachment lightbox
  const [generating, setGenerating] = useState(false) // a relay turn is streaming
  // Which brief cards have already fired a build, keyed by their message id, and any start error
  // to surface ON the card. A card stays in the transcript forever, so without this a user could
  // scroll up and re-fire an old brief over a live build.
  const [startedCards, setStartedCards] = useState(() => new Set())
  const [cardErrors, setCardErrors] = useState({})

  // `relayError` covers the chat half (429 daily cap, expired session, upstream failure);
  // `session.error` covers the build half. Distinct sources, both surfaced above the composer.
  const { sendMessage, error: relayError } = useClaudeAPI()

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
  const loadedBuildRef = useRef(null)
  const initFiredRef = useRef(null) // the chat id already seeded — fire-once per chat, not per mount
  const projectIdRef = useRef(projectId)
  projectIdRef.current = projectId
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

    getBuild(buildId)
      .then((saved) => {
        if (!alive || buildIdRef.current !== buildId) return
        loadedBuildRef.current = buildId
        // A thread with turns → restore it. An EMPTY thread is treated as fresh: the canonical
        // thread now exists from the moment the project is first opened (003-U1), so `saved` is
        // truthy with zero messages on a first visit — the case the old `if (saved)` arm would
        // have silently swallowed the handed-off prompt in.
        if (saved && saved.messages.length > 0) {
          // Seed the next seq from the highest PERSISTED seq, not the array length: a transcript
          // with any gap (a failed append, a pruned turn) would otherwise mint a colliding seq.
          seqRef.current = Math.max(...saved.messages.map((m) => m.seq ?? 0)) + 1
          if (saved.context) contextRef.current = saved.context
          setMessages(saved.messages)
          return
        }
        if (saved?.context) contextRef.current = saved.context
        seedFreshThread(buildId, () => alive)
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
   * First visit to a thread with a handed-off prompt (the project composer's Generate, or the
   * planning chat's Launch Builder): fire it as this thread's first RELAY turn — never as a
   * build. The interview runs first; a build starts only from the brief card the model returns.
   *
   * Fire-once per chat (`initFiredRef`), mirroring ChatPage's `initialMessage` discipline: a
   * remount (StrictMode, a re-render) must not send the prompt twice.
   */
  const seedFreshThread = (id, isAlive) => {
    if (initFiredRef.current === id) return
    initFiredRef.current = id
    seqRef.current = 0

    if (!initialPrompt) {
      setMessages([welcomeMessage()])
      return
    }
    void fireRelayTurn(initialPrompt, location.state?.pendingAttachments || [], id, {
      isAlive,
      onAbort: () => setMessages([welcomeMessage()]),
    })
  }

  /**
   * One relay turn: persist the user turn → stream the assistant's reply → persist it.
   *
   * THIS IS THE ONLY THING A SEND DOES. It never starts a build — the routing rule (KTD): every
   * composer send goes to the relay, and builds fire ONLY from a brief card's confirmation, first
   * build and iteration alike. The direct-fire send this page used to do is what made the agent
   * silently guess at a vague prompt.
   *
   * Persist-before-stream is load-bearing: the single append call upserts the header AND inserts
   * the message, so the conversation row exists by the time `POST /v1/claude` looks it up to fold
   * in the project's description + the interview protocol. That ordering is why the FIRST turn of
   * a thread gets its context at all.
   */
  const fireRelayTurn = async (rawText, attachments, activeId, { isAlive = () => true, onAbort } = {}) => {
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

    const priorMessages = messagesRef.current.filter((m) => !m.ephemeral)
    const userSeq = seqRef.current
    seqRef.current += 1
    const userMsg = { id: `local_${Date.now()}`, role: 'user', parts, seq: userSeq, createdAt: new Date().toISOString() }
    setMessages([...priorMessages, userMsg])

    try {
      await appendBuilderMessage(
        activeId,
        { role: 'user', parts, seq: userSeq },
        userSeq === 0
          ? { title: deriveTitle(partsToText(parts)), context: contextRef.current, projectId }
          : { projectId },
      )
    } catch (err) {
      releaseUploadedAttachments(parts)
      showAttachToast(describeSaveFailure(err, 'Could not save this message. Check your connection.'))
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
    dropTransientQuery(activeId)
    refreshBuilds()

    // Only the NEWEST turn's binaries are inflated from the composer's in-memory bytes;
    // historical binaries are dropped by the assembler (they cost tokens the model already spent).
    const byteMap = new Map(attachments.map((a) => [a.id, a.base64]))
    const apiMessages = assembleApiMessages([...priorMessages, userMsg], (id) => byteMap.get(id))

    const assistantSeq = seqRef.current
    seqRef.current += 1
    const assistantId = `local_${Date.now()}_a`
    let assistantText = ''
    setGenerating(true)
    setMessages((prev) => [...prev, { id: assistantId, role: 'assistant', parts: [{ type: 'text', text: '' }], seq: assistantSeq, createdAt: new Date().toISOString() }])

    const result = await sendMessage(
      apiMessages,
      (delta) => {
        if (buildIdRef.current !== activeId) return // navigated away mid-stream — drop the delta
        assistantText += delta
        setMessages((prev) => prev.map((m) => (m.id === assistantId ? { ...m, parts: [{ type: 'text', text: assistantText }] } : m)))
      },
      { systemPrompt: THREAD_SYSTEM_PROMPT },
      activeId, // the server folds in this project's description + the interview protocol
    )
    setGenerating(false)

    // Falsy = failed / aborted / streamed nothing. Drop the empty bubble; useClaudeAPI's own
    // error surfaces the reason.
    if (!result) {
      if (stillHere()) {
        setMessages((prev) => prev.filter((m) => m.id !== assistantId))
        seqRef.current = assistantSeq
      }
      return
    }
    // NO-OP if the user navigated away or deleted the thread mid-stream, so an in-flight reply
    // can never resurrect a deleted conversation or land on the wrong one.
    if (!stillHere()) return
    try {
      await appendBuilderMessage(activeId, { role: 'assistant', parts: [{ type: 'text', text: assistantText }], seq: assistantSeq }, { projectId })
      refreshBuilds()
    } catch {
      // The turn is on screen and usable (the brief card renders from state, so the build still
      // works) — it just will not survive a reload. Say so rather than silently losing it.
      showAttachToast('Your reply could not be saved, so it may disappear on reload.')
    }
  }

  /**
   * Record what a build turn produced, permanently, as a `build` part (003-U5).
   *
   * The live narrative (status line + activity feed) is deliberately NOT persisted — it is a lot
   * of noise with a short shelf life. The OUTCOME is the part worth keeping: reopening a finished
   * thread should tell the whole story (prompt → questions → brief → what happened).
   *
   * DEDUPE IS ON `sessionId`, NOT seq/_id. The server's append idempotency only covers a replay of
   * the same `_id`, or a race on the same seq. Neither catches the real duplicate: after a reload
   * the portal mints a FRESH `_id` and a fresh seq, so a replayed terminal would append a clean
   * SECOND outcome for a build that ran once. Scanning the transcript for this session's part is
   * what actually closes it — and it works across reloads, which an in-memory guard cannot.
   */
  const appendBuildOutcome = async (activeId, outcome) => {
    if (outcomeWrittenRef.current.has(outcome.sessionId)) return
    const already = messagesRef.current.some((m) =>
      (m.parts || []).some((p) => p?.type === 'build' && p.sessionId === outcome.sessionId),
    )
    if (already) {
      outcomeWrittenRef.current.add(outcome.sessionId)
      return
    }
    outcomeWrittenRef.current.add(outcome.sessionId)

    const seq = seqRef.current
    seqRef.current += 1
    // A summary text part rides alongside the build part: the transcript needs something to read,
    // and the relay assembles a turn from its TEXT parts — a build-part-only message would replay
    // to the model as an empty assistant turn on the user's next send.
    const parts = [{ type: 'text', text: outcomeSummary(outcome) }, { type: 'build', ...outcome }]
    const message = { id: `local_${Date.now()}_b`, role: 'assistant', parts, seq, createdAt: new Date().toISOString() }
    setMessages((prev) => [...prev, message])
    try {
      await appendBuilderMessage(activeId, { role: 'assistant', parts, seq }, { projectId })
      refreshBuilds()
    } catch {
      // It is on screen; it just will not survive a reload. Re-arm the guard so a later terminal
      // (or a retry) can try again rather than being permanently deduped against a write that
      // never landed.
      outcomeWrittenRef.current.delete(outcome.sessionId)
      seqRef.current = seq
    }
  }

  /**
   * Watch the live session for its terminal and record the outcome once.
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
    // Only the thread that OWNS this session records it, and only while we are viewing it — the
    // transcript being written belongs to that thread.
    if (activeId !== buildIdRef.current || sessionProjectRef.current !== projectId) return

    const ended = session.envelopes.find((e) => e.type === 'ended')
    void appendBuildOutcome(activeId, {
      status: session.status,
      sessionId: sid,
      previewUrl: session.previewUrl ?? null,
      endedAt: new Date().toISOString(),
      snapshotCommitted: ended?.snapshot_committed ?? false,
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
   * Start (or refine) the project's build session for `activeBuildId`. Called ONLY from a brief
   * card's confirmation (`handleConfirmBrief`) — this is the page's single build trigger.
   *
   * Refine = stop()+start() (no C3 refine verb — default (a); the frame reloads). On a 409 the
   * client cannot tell reattach from block from the code alone, so it `getStatus`es the existing
   * session and RE-ATTACHES only when its projectId equals this chat's project; otherwise the block
   * banner stands (cross-project). The projectId comparison is the gate, NOT the bare 409.
   *
   * @returns `{failed: true, message}` when no build is running as a result — the caller re-arms
   *   the card so the retry sits where the user is looking. Reattach counts as SUCCESS (a build IS
   *   running; it just isn't a new one).
   */
  const beginOrRefineBuild = async (activeBuildId, prompt) => {
    if (!projectId) return { failed: true, message: 'Open a project to start a build.' }
    // Classify the CURRENT session BEFORE overwriting the refs (else the same-project test would be
    // tautological). One BuilderPage instance persists across project switches, so `session` may be
    // a live build belonging to ANOTHER project.
    const sessionLive = isActiveBuildStatus(session.status) && session.sessionId != null
    const sameProject = sessionProjectRef.current === projectId
    if (sessionLive && !sameProject) {
      // A build is live for a DIFFERENT project in this same tab. Do NOT call start() — its reset()
      // would drop that build's heartbeat and orphan it (the server would 409 anyway). Block here.
      return {
        failed: true,
        message: 'You already have a build running in another project. Stop it before starting one here.',
      }
    }

    // Stamp the originating chat/project NOW (refs — no re-render). The render gate ALSO requires
    // `session.sessionId != null`, so a cross-project block (start fails → no sessionId) stays
    // hidden even with these set; but a same-project reattach's async state updates land AFTER
    // these, so the gate is already satisfied when the framed preview appears (avoids a lost frame).
    const prevChat = sessionChatRef.current
    const prevProject = sessionProjectRef.current
    sessionChatRef.current = activeBuildId
    sessionProjectRef.current = projectId
    // Advisory claim so other tabs see this project as building (the real lock is server-side).
    buildLockRef.current?.acquire(projectId, activeBuildId)

    if (sessionLive && sameProject) {
      // A refine turn (no C3 refine verb): end THIS project's live session before starting a fresh
      // one, so the second start never 409s the user's own live session.
      const stopped = await session.stop()
      if (!stopped) {
        // The stop FAILED and the old session is STILL LIVE (finding #19): starting now would
        // 409 against our own session, and start()'s reset() would wipe the surfaced stop
        // error. Abort the refine — restore the live session's attribution and drop only the
        // claim this refine added (never the live session's own claim).
        sessionChatRef.current = prevChat
        sessionProjectRef.current = prevProject
        if (activeBuildId !== prevChat) buildLockRef.current?.release(activeBuildId)
        return { failed: true, message: session.error || 'Could not stop the running build — try again.' }
      }
    }

    // Pass the chat id so the server can ground the build in this thread's attachments (R3): the
    // user turn — with its file parts — was persisted before we got here, so the server reads the
    // image/PDF/office bytes it already holds instead of the browser re-uploading them.
    const outcome = await session.start(projectId, prompt, activeBuildId)
    if (outcome.kind === 'started') {
      // Re-acquire the advisory claim: a refine's start() passes through reset(), whose
      // transitional no-session state the release effect (rightly) treats as ended — so the
      // claim must be re-asserted once the fresh session is genuinely live (finding #23).
      // Advisory only; the server 409 stays authoritative. The terminal effect releases it.
      buildLockRef.current?.acquire(projectId, activeBuildId)
      return { failed: false }
    }
    if (outcome.kind === 'blocked' && outcome.existingSessionId) {
      const existing = outcome.existingSessionId
      const st = await sessionClient.getStatus(existing).catch(() => null)
      if (st && st.projectId === projectId) {
        // Same project → join the live build (reattach's reset() clears the 409 block; it seeds the
        // preview from getStatus, KTD-1). If reattach's own getStatus seed rejects, DON'T leave a silent
        // blank cockpit: release the advisory claim (else it wedges other tabs) and surface a retry
        // (fail-first — no swallowed error). The composer stays live for the retry.
        try {
          await session.reattach(existing)
          // Reattach passes through reset() too — its transitional no-session state releases
          // the claim (the terminal effect), so re-assert it once the joined session is live,
          // mirroring the 'started' path above (finding #23). Advisory only.
          buildLockRef.current?.acquire(projectId, activeBuildId)
          // NOT a failure: a build for this project IS running and the cockpit is now showing it.
          // Re-arming the card here would invite the user to start a second one.
          return { failed: false }
        } catch {
          buildLockRef.current?.release(activeBuildId)
          return { failed: true, message: 'Could not rejoin your running build — try again.' }
        }
      }
      // else cross-project → `session.blocked` stays set (SessionControls renders the block banner);
      // `session.sessionId` is null (start failed) so the feed/preview stay hidden for this chat.
    }
    // No session started for this chat (cross-project block, or an error surfaced via `session.error`):
    // drop the optimistic advisory claim so it never wedges another tab.
    buildLockRef.current?.release(activeBuildId)
    // A cross-project block returns NO message: SessionControls already renders that state — with
    // the force-end affordance the user actually needs — and repeating it on the card would say
    // the same thing twice in two places. The card still re-arms, so the retry works once they
    // have dealt with the other session.
    return { failed: true, message: outcome.kind === 'error' ? outcome.message : null }
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
    if (generating) {
      showAttachToast('Please wait for the current reply to finish.')
      return
    }
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

    setInput('')
    clearPending()
    await fireRelayTurn(text, attachments, buildIdRef.current)
  }

  /**
   * Confirming a brief card — the ONE place a build starts.
   *
   * The advisory cross-tab pre-check runs HERE rather than at send: a send is now just a chat
   * turn, and blocking someone from talking to the assistant because another tab is building
   * would be nonsense. Blocking them from starting a SECOND build is the actual rule (KTD-7).
   */
  const handleConfirmBrief = async (cardId, brief) => {
    if (sendingRef.current || startedCards.has(cardId)) return
    if (!projectId) {
      showAttachToast('Open a project to start a build.')
      return
    }
    const activeBuildId = buildIdRef.current
    const blocked = buildBlockedMessage(activeBuildId)
    if (blocked) {
      setCardErrors((prev) => ({ ...prev, [cardId]: blocked }))
      return
    }

    sendingRef.current = true // synchronous: no second start may begin in this thread
    setCardErrors((prev) => ({ ...prev, [cardId]: null }))
    setStartedCards((prev) => new Set(prev).add(cardId))
    try {
      const outcome = await beginOrRefineBuild(activeBuildId, brief)
      if (outcome?.failed && buildIdRef.current === activeBuildId) {
        // The start did not take. Re-arm the card so the retry is right where the user is
        // looking — a dead card here would strand them with a brief and no way to build it.
        setStartedCards((prev) => {
          const next = new Set(prev)
          next.delete(cardId)
          return next
        })
        setCardErrors((prev) => ({ ...prev, [cardId]: outcome.message }))
      }
    } finally {
      sendingRef.current = false
    }
  }

  const handleSelectBuild = (id) => {
    setShowBuilds(false)
    if (id === buildIdRef.current) return
    setViewer(null)
    navigate(`/chat/${id}`)
  }

  const handleNewBuild = () => {
    setShowBuilds(false)
    setViewer(null)
    if (!projectId) {
      navigate('/projects')
      return
    }
    navigate(`/chat/${newBuild()}?projectId=${encodeURIComponent(projectId)}&kind=builder`, { state: {} })
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
  const statusLine = showSession ? assistantStatusLine(session.status) : null
  // A build chat's delete is gated while ITS session is live (deleting the chat that owns a running
  // build would strand it); a different chat deletes freely.
  const buildActive = showSession && isActiveBuildStatus(session.status)

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
                <div className="px-3 py-1.5 flex items-center justify-between">
                  <p className="text-[10px] font-bold uppercase tracking-wider text-neutral">Recent builds</p>
                  <button onClick={handleNewBuild} className="text-[11px] font-semibold text-primary hover:underline">
                    + New
                  </button>
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
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto p-4 space-y-3 scrollbar-thin">
            {messages.map((msg) => {
              // An assistant turn may carry a build brief. Parsing at RENDER (not at receipt) is
              // what makes a restored transcript behave identically to a live one: the fence lives
              // in the persisted text, so a reloaded thread re-renders its card — and its build
              // button — with no extra persisted state to keep in sync.
              const buildPart = (msg.parts || []).find((p) => p?.type === 'build')
              // An outcome message is a record, not a proposal: never parse it for a fence (its
              // summary text carries none, and a build part must not sprout a build button).
              const proposal = msg.role === 'assistant' && !msg.ephemeral && !buildPart
                ? parseBuildBrief(partsToText(msg.parts))
                : { kind: 'none' }
              const hasCard = proposal.kind === 'brief' || proposal.kind === 'degraded'
              // With the fence lifted out, an assistant turn can be pure brief — and a
              // just-created assistant turn is empty until the first delta lands. Render the
              // bubble only when it would actually say something, so neither leaves an empty
              // bubble (or a lone timestamp) on screen.
              const bodyParts = hasCard ? [{ type: 'text', text: proposal.text }] : msg.parts
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
                      {buildPart && <BuildOutcome part={buildPart} />}
                      {hasCard && (
                        <BuildBriefCard
                          brief={proposal.brief}
                          degraded={proposal.kind === 'degraded'}
                          busy={generating}
                          started={startedCards.has(msg.id)}
                          refine={sessionChatRef.current === buildIdRef.current && session.sessionId != null}
                          error={cardErrors[msg.id] ?? null}
                          onBuild={() => void handleConfirmBrief(msg.id, proposal.brief)}
                        />
                      )}
                    </div>
                  </div>
                </div>
              )
            })}

            {/* The assistant is composing a reply (an interview question, or a brief). Shown only
                until the first delta lands — after that the streaming bubble is the feedback. */}
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

            {/* The assistant's build turn — a single, non-persisted status line driven by the live
                session (the activity feed on the right carries the detail). */}
            {statusLine && (
              <div>
                <div className="flex gap-2">
                  <div className="w-6 h-6 rounded-full bg-primary/10 flex items-center justify-center flex-shrink-0 mt-0.5">
                    <Sparkles size={10} className="text-primary" />
                  </div>
                  <div className="max-w-[85%] rounded-2xl rounded-tl-sm px-3 py-2.5 text-xs leading-relaxed bg-bial-bg text-tertiary">
                    {statusLine}
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
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          {/* Input */}
          <div className="p-3 border-t border-bial-border space-y-2">
            {relayError && (
              <div className="text-[11px] text-danger bg-danger/5 border border-danger/20 rounded-lg px-2.5 py-1.5">
                {relayError}
              </div>
            )}
            {sessionProjectMatches && session.error && (
              <div className="text-[11px] text-danger bg-danger/5 border border-danger/20 rounded-lg px-2.5 py-1.5">
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

        {/* Build cockpit: controls, activity feed, and the framed live preview */}
        <div className="flex-1 flex flex-col overflow-hidden">
          <div className="px-4 py-2 border-b border-bial-border bg-white flex-shrink-0">
            <SessionControls
              status={previewStatus}
              stopping={session.stopping}
              blocked={sessionProjectMatches ? session.blocked : null}
              reclaimed={showSession && session.reclaimed}
              feedDisconnected={showSession && session.feedDisconnected}
              quota={showSession ? session.quota : null}
              startedAt={session.startedAt}
              onStop={() => session.stop()}
              onForceEnd={(sid) => session.forceEnd(sid)}
              onReconnect={() => session.reconnect()}
              onStartAgain={handleStartAgain}
            />
          </div>
          <div className="flex-1 flex flex-col overflow-hidden">
            {/* The activity feed takes the top; the framed preview fills the rest. During
                provisioning/building the feed is the story; once ready the framed app leads. */}
            <div className="h-[38%] min-h-[8rem] border-b border-bial-border bg-white overflow-hidden flex flex-col">
              <ActivityFeed envelopes={showSession ? session.envelopes : []} />
            </div>
            <div className="flex-1 overflow-hidden">
              <LivePreview previewUrl={showSession ? session.previewUrl : null} status={previewStatus} iterating={showSession && session.iterating} />
            </div>
          </div>
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
