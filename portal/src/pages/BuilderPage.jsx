import { useState, useEffect, useRef, useCallback } from 'react'
import { useNavigate, useLocation, useParams } from 'react-router-dom'
import {
  Send, Sparkles, User, Paperclip, FileText, FileSpreadsheet, Presentation, History, Trash2, X,
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
import { buildUserParts, partsToText, attachmentsFromParts, countAttachments, releaseUploadedAttachments } from '../utils/attachmentStore'
import { ACCEPT_ATTR, validateConversationAttachmentCap, TEXT_MEDIA_TYPES, OFFICE_MEDIA_TYPES, DECK_MEDIA_TYPES, officeFormat } from '../utils/attachmentInput'
import { openPdf } from '../utils/attachmentViewer'
import { loadBuilds, newBuild, appendBuilderMessage, getBuild, deleteBuild, deriveTitle } from '../utils/builderHistory'
import { relativeTime } from '../utils/chatHistory'

// The from-scratch greeting (ephemeral — never persisted).
const WELCOME_TEXT = "Hello! I'm Citizen Developer AI. Tell me what you'd like to build for BIAL operations."
const welcomeMessage = () => ({ id: 'welcome', role: 'assistant', parts: [{ type: 'text', text: WELCOME_TEXT }], createdAt: new Date().toISOString() })

const REFINEMENT_CHIPS = [
  'Change the theme to dark mode',
  'Add a real-time data table',
  'Switch to mobile layout',
]

// The assistant's side of a build turn is NOT persisted (KTD-8 / U5): the activity feed IS the
// build narrative, and the ended C7 envelope its conclusion. This derives a single, non-persisted
// status line for the chat transcript from the live session — optimistic-visible-state up front.
function assistantStatusLine(status) {
  switch (status) {
    case 'provisioning':
    case 'building':
      return 'Building your app — watch the progress and preview on the right.'
    case 'ready':
      return 'Your app preview is live on the right. Tell me what to change.'
    case 'ended':
      return 'Build finished — your app preview is on the right.'
    case 'failed':
      return 'The build ran into a problem — see the activity feed for details.'
    default:
      return null
  }
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
 * The build chat, rendered by ChatRoute at the flat `/chat/:chatId`.
 *
 * THREE DISTINCT IDENTITIES (unchanged from the single-file era, KTD-8):
 *   conversationId — the chat        (`/chat/{id}`, PATCH /conversations/{id})
 *   projectId      — the container   (breadcrumb; the C3 build session is project-scoped)
 *   build session  — the C3 session  (project/user-scoped, one-per-user; NO conversationId on the wire)
 *
 * WHAT CHANGED IN PHASE-2 (U5): Send no longer streams a single-file component. It drives a C3
 * BUILD SESSION — the agent builds a real Next.js app in a per-user sandbox; the cockpit frames
 * its cross-origin `preview_url`, streams the C7 progress as an activity feed, and controls the
 * session (stop / force-end). The app gets its data credentials server-side at provision (C9), so
 * the portal feeds the app nothing (no `previewCode`/`config`/`accessToken` — all gone).
 *
 * SESSION ↔ CONVERSATION IDENTITY (specified): the session is project-scoped, but the builder's
 * URL / transcript / Recent-builds stay conversation(`buildId`)-keyed. The active build chat
 * ORIGINATES the session; a Send in another chat of the SAME project RE-ATTACHES the live session
 * (via the 409 → getStatus → projectId-compare → resubscribe path); a Send in a DIFFERENT project
 * is BLOCKED (the 409 is not self-describing — the projectId comparison is the gate, not the bare
 * 409). `sessionChatRef`/`sessionProjectRef` record the originating chat/project.
 *
 * REFINE TURNS (no C3 refine verb — cross-track confirm item, default (a)): a Send while a session
 * is live for this project `stop()`s then `start()`s a fresh session (the frame goes dark then
 * reloads — a documented cost). The build-intent gate (`sendingRef`) blocks a double-start.
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

  const { pendingAttachments, handleFileSelect, removePending, clearPending, attachToast, showAttachToast } =
    usePendingAttachments()

  const bottomRef = useRef(null)
  const inputRef = useRef(null)
  const fileInputRef = useRef(null)
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

  // Hold the advisory claim while THIS chat's session is live; retract it the instant the session
  // ends so another tab's `blockedBy` pre-check clears (KTD-7). The authoritative barrier is C3's
  // 409; this is only the fast cross-tab UX mirror.
  useEffect(() => {
    const chat = sessionChatRef.current
    if (!projectId || !chat) return
    if (!isActiveBuildStatus(session.status)) buildLockRef.current?.release(chat)
  }, [session.status, projectId])

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
        if (saved) {
          seqRef.current = saved.messages.length
          if (saved.context) contextRef.current = saved.context
          setMessages(saved.messages)
          return
        }
        seedFreshBuild(buildId, () => alive)
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
   * First visit to a client-minted build chat: seed the welcome bubble, or — when Sandbox/ChatPage
   * handed off a prompt — persist that turn (with any attachments) and START the build. Persist
   * BEFORE starting: BRAIN reads the project/conversation context server-side (C3 §2.1), so a build
   * started before its row exists would lose all project + attachment context.
   */
  const seedFreshBuild = (id, isAlive) => {
    if (initFiredRef.current === id) return // fire-once per chat: a remount must not start twice
    initFiredRef.current = id
    seqRef.current = 0

    if (!initialPrompt) {
      setMessages([welcomeMessage()])
      return
    }

    const blocked = buildBlockedMessage(id)
    if (blocked) {
      setMessages([welcomeMessage()])
      showAttachToast(blocked)
      return
    }

    sendingRef.current = true // the seeded prompt owns this chat's first turn
    const userSeq = seqRef.current
    seqRef.current += 1
    const pending = location.state?.pendingAttachments || []
    const provisional = { id: 'initial-user', role: 'user', parts: [{ type: 'text', text: initialPrompt }], seq: userSeq, createdAt: new Date().toISOString() }
    setMessages([provisional])

    void (async () => {
      let parts
      try {
        parts = await buildUserParts(initialPrompt, pending)
      } catch (err) {
        showAttachToast(err?.message || 'Could not attach your file — building from your description only.')
        parts = [{ type: 'text', text: initialPrompt }]
      }
      if (!isAlive() || buildIdRef.current !== id) {
        // The user switched chats mid-upload — abandon this seed, but release the (instance-wide)
        // send gate so the newly-adopted chat's composer is not permanently wedged.
        sendingRef.current = false
        return
      }
      setMessages([{ ...provisional, parts }])
      try {
        await appendBuilderMessage(
          id,
          { role: 'user', parts, seq: userSeq },
          { title: deriveTitle(initialPrompt), context: contextRef.current, projectId },
        )
      } catch (err) {
        releaseUploadedAttachments(parts)
        seqRef.current = userSeq
        sendingRef.current = false
        showAttachToast(describeSaveFailure(err, 'Could not save this build. Check your connection.'))
        if (isConversationGone(err)) navigate('/projects', { replace: true })
        return
      }
      dropTransientQuery(id)
      refreshBuilds()
      try {
        await beginOrRefineBuild(id, initialPrompt)
      } finally {
        sendingRef.current = false
      }
    })()
  }

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
   * Start (or refine) the project's build session for `activeBuildId`.
   *
   * Refine = stop()+start() (no C3 refine verb — default (a); the frame reloads). On a 409 the
   * client cannot tell reattach from block from the code alone, so it `getStatus`es the existing
   * session and RE-ATTACHES only when its projectId equals this chat's project; otherwise the block
   * banner stands (cross-project). The projectId comparison is the gate, NOT the bare 409.
   */
  const beginOrRefineBuild = async (activeBuildId, prompt) => {
    if (!projectId) return
    // Classify the CURRENT session BEFORE overwriting the refs (else the same-project test would be
    // tautological). One BuilderPage instance persists across project switches, so `session` may be
    // a live build belonging to ANOTHER project.
    const sessionLive = isActiveBuildStatus(session.status) && session.sessionId != null
    const sameProject = sessionProjectRef.current === projectId
    if (sessionLive && !sameProject) {
      // A build is live for a DIFFERENT project in this same tab. Do NOT call start() — its reset()
      // would drop that build's heartbeat and orphan it (the server would 409 anyway). Block here.
      showAttachToast('You already have a build running in another project. Stop it before starting one here.')
      return
    }

    // Stamp the originating chat/project NOW (refs — no re-render). The render gate ALSO requires
    // `session.sessionId != null`, so a cross-project block (start fails → no sessionId) stays
    // hidden even with these set; but a same-project reattach's async state updates land AFTER
    // these, so the gate is already satisfied when the framed preview appears (avoids a lost frame).
    sessionChatRef.current = activeBuildId
    sessionProjectRef.current = projectId
    // Advisory claim so other tabs see this project as building (the real lock is server-side).
    buildLockRef.current?.acquire(projectId, activeBuildId)

    if (sessionLive && sameProject) {
      // A refine turn (no C3 refine verb): end THIS project's live session before starting a fresh
      // one, so the second start never 409s the user's own live session.
      await session.stop()
    }

    const outcome = await session.start(projectId, prompt)
    if (outcome.kind === 'started') return // the advisory claim is held; the terminal effect releases it
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
        } catch {
          buildLockRef.current?.release(activeBuildId)
          showAttachToast('Could not rejoin your running build — try again.')
        }
        return
      }
      // else cross-project → `session.blocked` stays set (SessionControls renders the block banner);
      // `session.sessionId` is null (start failed) so the feed/preview stay hidden for this chat.
    }
    // No session started for this chat (cross-project block, or an error surfaced via `session.error`):
    // drop the optimistic advisory claim so it never wedges another tab.
    buildLockRef.current?.release(activeBuildId)
  }

  const handleSend = async () => {
    const text = input.trim()
    const attachments = pendingAttachments
    if (!text && attachments.length === 0) return // nothing to send
    if (sendingRef.current) {
      // A start is already in flight in THIS instance (the build-intent gate). Explain, don't drop.
      showAttachToast('Please wait for the current build to start before sending another message.')
      return
    }
    // Project-first: a build REQUIRES a project (no lazy Default — never reintroduce).
    if (!projectId) {
      showAttachToast('Open a project to start a build.')
      return
    }
    const blocked = buildBlockedMessage(buildIdRef.current)
    if (blocked) {
      showAttachToast(blocked)
      return
    }
    if (attachments.length > 0) {
      const cap = validateConversationAttachmentCap(countAttachments(messages), attachments.length)
      if (cap.error) {
        showAttachToast(cap.error)
        return
      }
    }

    sendingRef.current = true // synchronous: no second start may begin in this chat
    setInput('')
    clearPending()

    let parts
    try {
      parts = await buildUserParts(text || 'Please review the attached file(s).', attachments)
    } catch (err) {
      showAttachToast(err?.message || 'Could not upload the attachment.')
      sendingRef.current = false
      return
    }

    const activeBuildId = buildIdRef.current
    const userSeq = seqRef.current
    seqRef.current += 1
    const userMsg = { id: `local_${Date.now()}`, role: 'user', parts, seq: userSeq, createdAt: new Date().toISOString() }
    const updated = [...messages, userMsg]
    setMessages(updated)

    // Persist the user turn BEFORE starting — the attachment parts are how BRAIN reads the
    // image/PDF/office/deck context server-side (C3 §2.1). Title + context only on the first turn.
    try {
      await appendBuilderMessage(
        activeBuildId,
        { role: 'user', parts, seq: userSeq },
        userSeq === 0
          ? { title: deriveTitle(partsToText(parts)), context: contextRef.current, projectId }
          : { projectId },
      )
    } catch (err) {
      releaseUploadedAttachments(parts)
      showAttachToast(describeSaveFailure(err))
      // Roll back the optimistic message + seq ONLY if we are still on the chat this turn belongs
      // to — a mid-await chat switch means the snapshot/seq are the OTHER chat's, and writing them
      // here would clobber the now-current chat's transcript.
      if (buildIdRef.current === activeBuildId) {
        setMessages(messages)
        seqRef.current = userSeq
      }
      sendingRef.current = false
      if (isConversationGone(err)) navigate('/projects', { replace: true })
      return
    }
    dropTransientQuery(activeBuildId)
    refreshBuilds()

    try {
      await beginOrRefineBuild(activeBuildId, text || 'Please review the attached file(s).')
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
  const showSession = session.sessionId != null && sessionProjectRef.current === projectId
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
            {messages.map((msg) => (
              <div key={msg.id}>
                <div className={`flex gap-2 ${msg.role === 'user' ? 'flex-row-reverse' : ''}`}>
                  <div className={`w-6 h-6 rounded-full flex items-center justify-center flex-shrink-0 mt-0.5 ${msg.role === 'assistant' ? 'bg-primary/10' : 'bg-secondary/10'}`}>
                    {msg.role === 'assistant'
                      ? <Sparkles size={10} className="text-primary" />
                      : <User size={10} className="text-secondary" />
                    }
                  </div>
                  <div className={`max-w-[85%] rounded-2xl px-3 py-2.5 text-xs leading-relaxed ${
                    msg.role === 'user'
                      ? 'bg-tertiary text-white rounded-tr-sm'
                      : 'bg-bial-bg text-tertiary rounded-tl-sm'
                  }`}>
                    <MessageContent parts={msg.parts} />
                    <p className="text-[10px] mt-1 opacity-40">
                      {msg.createdAt ? new Date(msg.createdAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}
                    </p>
                  </div>
                </div>
              </div>
            ))}

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
            {session.error && (
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
                placeholder="Type instructions to build or refine your app..."
                className="flex-1 resize-none text-xs text-tertiary bg-bial-bg border border-bial-border rounded-xl px-3 py-2 focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20 transition placeholder:text-gray-300"
              />
              <button
                onClick={handleSend}
                disabled={!input.trim() && pendingAttachments.length === 0}
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
              blocked={session.blocked}
              reclaimed={session.reclaimed}
              feedDisconnected={showSession && session.feedDisconnected}
              quota={session.quota}
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
