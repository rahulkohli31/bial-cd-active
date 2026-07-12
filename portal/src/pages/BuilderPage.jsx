import { useState, useEffect, useRef, useCallback } from 'react'
import { useNavigate, useLocation, useParams } from 'react-router-dom'
import {
  Send, Sparkles, User, Brain, LayoutTemplate, Code2, Monitor, CheckCircle, X, Paperclip, FileText,
  FileSpreadsheet, Presentation, History, Trash2,
} from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import LivePreview from '../components/LivePreview'
import DeployBar from '../components/DeployBar'
import AttachmentChips from '../components/AttachmentChips'
import AttachmentLightbox from '../components/AttachmentLightbox'
import ProjectBreadcrumb from '../components/projects/ProjectBreadcrumb'
import { listProjectConversations } from '../utils/conversationApi'
import { describeAppFailure, describeSaveFailure, isConversationGone } from '../utils/chatErrors'
import { createBuildLock, openBuildLockChannel } from '../utils/buildLock'
import { useDropTransientQuery } from '../hooks/useDropTransientQuery'
import { useClaudeAPI, buildSystemPrompt, getContextLimits, estimateConversationTokens } from '../hooks/useClaudeAPI'
import { getAccessToken, getStoredUser } from '../utils/auth'
import { provisionApp, submitApp, getAppStatus, getAppSource } from '../utils/appRegistryApi'
import { DEPLOY_ENABLED } from '../config/features'
import { usePendingAttachments } from '../hooks/usePendingAttachments'
import { assembleApiMessages, buildUserParts, partsToText, attachmentsFromParts, countAttachments, releaseUploadedAttachments } from '../utils/attachmentStore'
import { ACCEPT_ATTR, validateConversationAttachmentCap, TEXT_MEDIA_TYPES, OFFICE_MEDIA_TYPES, DECK_MEDIA_TYPES, officeFormat } from '../utils/attachmentInput'
import { openPdf } from '../utils/attachmentViewer'
import { loadBuilds, newBuild, appendBuilderMessage, getBuild, deleteBuild, patchBuildCode, deriveTitle } from '../utils/builderHistory'
import { relativeTime } from '../utils/chatHistory'

// The from-scratch greeting (ephemeral — never persisted).
const WELCOME_TEXT = "Hello! I'm Citizen Developer AI. Tell me what you'd like to build for BIAL operations."
const welcomeMessage = () => ({ id: 'welcome', role: 'assistant', parts: [{ type: 'text', text: WELCOME_TEXT }], createdAt: new Date().toISOString() })

const STAGE_MESSAGES = [
  'Got it! Analyzing your requirements and mapping out the app structure...',
  'Requirements mapped. Creating the wireframe layout for your app...',
  'Wireframe ready. Generating the application code now — this takes a moment...',
  'Code generation complete. Rendering your live preview...',
  "Your app is ready! I've loaded the preview on the right. You can interact with it, or tell me what you'd like to change.",
]

const TOAST_STAGES = [
  { text: 'Analyzing requirements...', Icon: Brain },
  { text: 'Building wireframe...', Icon: LayoutTemplate },
  { text: 'Generating code...', Icon: Code2 },
  { text: 'Rendering preview...', Icon: Monitor },
]

const REFINEMENT_CHIPS = [
  'Change the theme to dark mode',
  'Add a real-time data table',
  'Switch to mobile layout',
]

export function extractPreviewCode(text) {
  if (!text) return null
  const match = text.match(/```jsx:preview\s*([\s\S]*?)```/)
  return match ? match[1].trim() : null
}

/**
 * Lightweight single-collection pin (Decision 11): the first collection name the
 * generated code reads/writes. Pinned across regenerations so the model can't
 * rename it and orphan previously-saved records. Returns `{ collection }` or null.
 */
export function extractDataSchema(code) {
  if (typeof code !== 'string') return null
  const m = code.match(/BIALData\.\w+\(\s*['"]([A-Za-z0-9_-]{1,64})['"]/)
  return m ? { collection: m[1] } : null
}

// Expects a STRING. Callers derive it with partsToText first so a parts[] message
// never reaches `.replace` (which throws on a non-string). Strips the jsx:preview
// fence (rendered in the live preview, not the chat). Exported for unit coverage.
export function filterCodeFromContent(content) {
  if (!content) return ''
  return content
    .replace(/```jsx:preview[\s\S]*?```/g, '')
    .replace(/```[\s\S]*?```/g, '')
    .trim()
}

function Toast({ stage, done, visible, onDismiss }) {
  if (!visible) return null

  const current = stage >= 1 && stage <= 4 ? TOAST_STAGES[stage - 1] : null

  return (
    <div className="fixed bottom-6 right-6 z-50 flex items-start gap-3 bg-white border border-bial-border rounded-xl shadow-xl p-4 max-w-xs animate-in slide-in-from-bottom-2">
      {done ? (
        <>
          <CheckCircle size={18} className="text-green-500 flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <p className="text-sm font-bold text-tertiary">App Generated Successfully</p>
            <p className="text-xs text-neutral mt-0.5">Your preview is ready. Interact with it or refine via chat.</p>
          </div>
        </>
      ) : current ? (
        <>
          <div className="w-5 h-5 bg-primary/10 rounded flex items-center justify-center flex-shrink-0 mt-0.5">
            <current.Icon size={11} className="text-primary" />
          </div>
          <div className="flex-1">
            <p className="text-xs font-semibold text-tertiary">{current.text}</p>
          </div>
        </>
      ) : null}
      <button onClick={onDismiss} className="text-neutral hover:text-tertiary transition flex-shrink-0 mt-0.5">
        <X size={13} />
      </button>
    </div>
  )
}

function MessageContent({ parts }) {
  // Derive prose from the parts model, then strip the jsx:preview code fence (it
  // renders in the live preview, not the chat bubble). Attachments render as chips.
  const filtered = filterCodeFromContent(partsToText(parts))
  const attachments = attachmentsFromParts(parts)
  if (!filtered && attachments.length === 0) return null
  const segments = filtered.split(/(\*\*[^*]+\*\*|\n)/g)
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
 * THREE DISTINCT IDENTITIES, which this page used to conflate into one:
 *
 *   conversationId — the chat        (`/chat/{id}`, PATCH /conversations/{id})
 *   projectId      — the container   (breadcrumb; header.projectId on create)
 *   appId          — the app         (provision, status, submit, DeployBar, LivePreview)
 *
 * `appId` is a FRESH uuid the server mints on provision. It is discovered by READING the
 * project (`project.appId`), never by firing a mutating provision call just to find out
 * whether an app exists.
 *
 * @param {{chatId?: string, projectId?: string | null, projectName?: string | null, projectAppId?: string | null}} [props]
 */
export default function BuilderPage({ chatId: chatIdProp, projectId = null, projectName = null, projectAppId = null } = {}) {
  const navigate = useNavigate()
  const location = useLocation()
  const params = useParams()
  // The conversation id. Named `buildId` throughout this file for continuity, but it is
  // ONLY a conversation id — never an app id. See the identity split in U8.
  const buildId = chatIdProp ?? params.chatId
  const initialPrompt = location.state?.prompt || ''
  const contextRef = useRef({
    theme: location.state?.theme || 'bial',
    uploadedFiles: location.state?.uploadedFiles || [],
  })
  const dropTransientQuery = useDropTransientQuery()
  const { sendMessage, error } = useClaudeAPI()

  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [generating, setGenerating] = useState(false)
  const [previewCode, setPreviewCode] = useState(null)
  // The CONVERSATION whose hydration has settled (its own code snapshot applied, or none). The
  // app-source fallback below waits on this so it only fills a genuinely-empty canvas — never
  // races the conversation's own-code load.
  const [hydratedChatId, setHydratedChatId] = useState(null)
  const [generationStage, setGenerationStage] = useState(0)
  const [toast, setToast] = useState({ stage: 0, done: false, visible: false })
  const [builds, setBuilds] = useState([])
  const [showBuilds, setShowBuilds] = useState(false)
  const [viewer, setViewer] = useState(null) // { name, src } for the pending-attachment lightbox
  // Deploy state for the PROJECT'S ONE APP, tagged with the chat it was read for:
  // { chatId, status, appId, appKey?, loginRequired, rejectionNote? } | null.
  //
  // The `chatId` tag is a safety interlock, not bookkeeping. React Router reuses this
  // component across /chat/{A} → /chat/{B} — there is no remount — and `config` below
  // derives the sandboxed iframe's appId/appKey from this. Untagged, the render between
  // the route change and the resolved status fetch would hand project B's code an iframe
  // still holding project A's appKey, and the preview issues data-service calls on mount.
  // A record written in that window lands in the wrong app's store, which
  // `.claude/rules/security.md` forbids outright.
  const [deploy, setDeploy] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  const { pendingAttachments, handleFileSelect, removePending, clearPending, attachToast, showAttachToast } =
    usePendingAttachments()

  const bottomRef = useRef(null)
  const inputRef = useRef(null)
  const fileInputRef = useRef(null)
  const timerRefs = useRef([])
  const toastTimer = useRef(null)
  const buildIdRef = useRef(null) // the active CONVERSATION being persisted — never an app id
  // The CONVERSATION whose load actually RESOLVED and applied. Distinct from buildIdRef
  // (set when the fetch STARTS): under StrictMode's mount→cleanup→remount, the first mount's
  // fetch is discarded after cleanup, so gating the load effect's early-return on this ref —
  // set only once a fetch survives its staleness guard — lets the remount re-fetch and apply.
  const loadedBuildRef = useRef(null)
  // Mirrors `previewCode` for the async app-source fallback: it decides whether to seed the
  // preview by reading the LATEST value, not the one captured when its fetch was kicked off
  // (a live stream may have produced code in the meantime).
  const previewCodeRef = useRef(null)
  previewCodeRef.current = previewCode
  // The project's one app, stamped with the project it belongs to: `{projectId, appId} | null`.
  // An app id is a property of a PROJECT, not of a chat, and a provision that resolves after the
  // user has navigated to another project must never be adopted here — it would hand the next
  // chat a foreign app to submit and to key its data-service calls on.
  const appIdRef = useRef(null)
  const projectIdRef = useRef(projectId)
  projectIdRef.current = projectId
  const deployRef = useRef(null) // mirrors `deploy` for async callbacks
  const initFiredRef = useRef(null) // the chat id already seeded — fire-once per chat, not per mount

  // One build at a time, per project. A project's current_code is last-write-wins, so two
  // builder chats streaming into it destroy each other's work. This closes the same-browser
  // case (two tabs); the cross-device window stays open until the backend grows a lock.
  const buildLockRef = useRef(null)
  if (buildLockRef.current === null) buildLockRef.current = createBuildLock({ channel: openBuildLockChannel() })

  // A build turn OUTLIVES this component. Unmounting aborts the SSE read, but
  // `fetchClaudeStream` catches the AbortError and RETURNS the text it accumulated, so
  // `runGeneration` sails on to append the turn, provision the app, and PATCH the code.
  // Disposing the lock on unmount would therefore retract the claim while that write is
  // still in flight — reopening the very concurrent-write race the lock exists to close.
  //
  // So: hand the lock off to the turn. The heartbeat keeps the claim alive for other tabs
  // until `generate`'s `finally` releases it, and only then is the channel closed.
  const turnInFlightRef = useRef(false)
  const disposeAfterTurnRef = useRef(false)
  // `generating` is STATE, and it is not set until several awaits into a send (attachment
  // upload, then the user-turn persist). A second Enter — or the seeded prompt racing a manual
  // send — sails past a `generating` check in that window and starts a second stream in one
  // chat: two persists, a colliding `seq`, and a nondeterministic winner for current_code.
  // This ref flips synchronously, before the first await.
  const sendingRef = useRef(false)
  useEffect(() => {
    const lock = buildLockRef.current
    return () => {
      if (turnInFlightRef.current) {
        disposeAfterTurnRef.current = true
        return
      }
      lock?.dispose()
    }
  }, [])
  const seqRef = useRef(0) // next message sort key for the active build's persisted turns
  const deletedRef = useRef(new Set()) // builds deleted mid-run: their in-flight persist must no-op (no resurrection)

  // Running context-length estimate → 'ok' | 'warn' | 'full'. The builder's
  // system prompt includes any uploaded reference files, so size it from the
  // real prompt. Drives the guardrail banner + send-disable below.
  const ctxTokens = estimateConversationTokens(messages, buildSystemPrompt(contextRef.current))
  const { soft: ctxSoft, hard: ctxHard } = getContextLimits()
  const ctxLevel = ctxTokens >= ctxHard ? 'full' : ctxTokens >= ctxSoft ? 'warn' : 'ok'

  // "Recent builds" lists THIS project's build chats. A project owns one app, so its
  // build chats are the ones that can possibly be relevant; a cross-project list would
  // invite the user to switch app identity from inside a chat.
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
    return () => {
      timerRefs.current.forEach(clearTimeout)
      if (toastTimer.current) clearTimeout(toastTimer.current)
    }
  }, [])

  useEffect(() => {
    refreshBuilds()
  }, [refreshBuilds])

  // Adopt the routed chat. One effect owns both arms, because under flat routing a
  // brand-new build chat and a saved one arrive at the same URL shape — only the
  // server's answer tells them apart:
  //
  //   saved   → hydrate the transcript, the code snapshot, and the deploy status
  //   missing → this is the first visit to a client-minted id (ChatRoute vouched for
  //             it with a ?projectId=). Seed it: welcome, or fire the handoff prompt.
  //
  // A build chat can never be "id-less" any more; the id is minted before navigation.
  useEffect(() => {
    if (!buildId) {
      navigate('/projects', { replace: true })
      return undefined
    }
    if (loadedBuildRef.current === buildId) return undefined

    let alive = true
    clearTimers()
    buildIdRef.current = buildId

    // Drop every scrap of the PREVIOUS chat before a single byte of this one arrives.
    // React Router reuses this component across /chat/{A} → /chat/{B} — there is no
    // remount — so anything not cleared here is B's chat wearing A's clothes. The app
    // identity above all: `config` below feeds a sandboxed iframe's data-service calls.
    setMessages([])
    setInput('') // the composer belongs to the OLD chat; a leaked draft would send into B
    clearPending() // and so do its staged attachments — drop them before B's first byte
    setPreviewCode(null)
    setGenerating(false)
    setGenerationStage(0)
    setToast({ stage: 0, done: false, visible: false })
    appIdRef.current = null
    deployRef.current = null
    setDeploy(null)

    getBuild(buildId)
      .then((saved) => {
        if (!alive || buildIdRef.current !== buildId) return
        // Mark THIS build as loaded only now that the fetch survived its staleness guard.
        // A StrictMode-discarded first mount never reaches here, so its remount re-fetches.
        loadedBuildRef.current = buildId
        // The conversation's own code (or its absence) is now decided. Release the app-source
        // fallback for THIS chat — every setPreviewCode in the branches below runs before the
        // batched re-render, so the fallback sees the settled value, not the teardown null.
        setHydratedChatId(buildId)
        if (saved) {
          seqRef.current = saved.messages.length // next persisted turn's sort key
          if (saved.context) contextRef.current = saved.context
          setMessages(saved.messages)
          // Render from the stored code snapshot (a single point read); fall back to
          // scanning the transcript only when no code.current is present (legacy build).
          const stored = saved.code?.current?.source
          if (stored) {
            setPreviewCode(stored)
          } else {
            const lastAssistant = [...saved.messages].reverse().find((m) => m.role === 'assistant')
            setPreviewCode(extractPreviewCode(partsToText(lastAssistant?.parts)))
          }
          setGenerating(false)
          setGenerationStage(0)
          setToast({ stage: 0, done: false, visible: false })
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

  // The project's app id arrives from ChatRoute once it has read the project. Adopt it and
  // read its status. This is the whole of "learn the app by READING the project": the SPA
  // never fires a mutating provision call to discover whether an app exists.
  useEffect(() => {
    if (!buildId || !projectAppId || currentAppId()) return undefined
    let alive = true
    appIdRef.current = { projectId, appId: projectAppId }
    readDeployStatus(buildId, projectAppId, () => alive)
    return () => {
      alive = false
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [buildId, projectAppId])

  // When THIS chat has no code of its own but its project has a built app, render that app.
  // Code is stored per-conversation (patchBuildCode writes the chat's OWN `code.current`), so a
  // second chat — or a plain "hi" turn — opens with an empty snapshot. Without this it would
  // show a blank canvas over a fully-built app (the exact bug: a chat that didn't build the app
  // rendered LivePreview's empty state). The project's durable source lives in
  // `app_registry.current_code`; read it by appId and seed the preview.
  //
  // Strictly a FALLBACK, gated three ways so it never fights the real thing:
  //   - `hydratedChatId === buildId`: the conversation load has settled, so `previewCodeRef`
  //     already reflects this chat's own code (or its absence) — no race with that fetch.
  //   - `previewCodeRef.current == null` (checked before the fetch AND again on apply): a chat
  //     with its own snapshot, or a live turn that has since streamed code, already owns the
  //     canvas. The re-check on apply closes the network-round-trip window.
  useEffect(() => {
    if (!buildId || !projectAppId || hydratedChatId !== buildId) return undefined
    if (previewCodeRef.current != null) return undefined // this chat already has code to show
    let alive = true
    getAppSource(projectAppId)
      .then(({ source }) => {
        if (!alive || buildIdRef.current !== buildId) return
        if (source && previewCodeRef.current == null) setPreviewCode(source)
      })
      .catch(() => {
        // Non-fatal: a source that can't load leaves LivePreview's empty state in place.
      })
    return () => {
      alive = false
    }
  }, [buildId, projectAppId, hydratedChatId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const clearTimers = () => {
    timerRefs.current.forEach(clearTimeout)
    timerRefs.current = []
  }

  /** The app id, but only when it still belongs to the project currently on screen. */
  const currentAppId = () => (appIdRef.current?.projectId === projectIdRef.current ? appIdRef.current.appId : null)

  /**
   * Reflect the PROJECT'S app status. Read-only — it never provisions, so opening a chat
   * can't create an abandoned draft. `appId` comes from the project; a project with no app
   * yet simply has none to read.
   */
  const readDeployStatus = (chatId, appId, isAlive) => {
    if (!appId) return
    getAppStatus(appId)
      .then((s) => {
        if (!isAlive() || buildIdRef.current !== chatId) return
        applyDeploy(s.status ? { ...s, appId } : null, chatId)
      })
      .catch((e) => {
        // Not-provisioned resolves to `{status:null}`, so a reject here is a real
        // 5xx/auth error; surface it instead of swallowing.
        console.error('Failed to read deploy status', e)
      })
  }

  /**
   * The project's one app, provisioned on demand and remembered.
   *
   * ORDERING IS LOAD-BEARING: this runs BEFORE the first `patchBuildCode`.
   * `PATCH /v1/conversations/{id}` mirrors a builder code snapshot into
   * `app_registry.current_code` only when the app row already exists
   * (`if app is not None`, backend conversations/router.py:288-296). PATCH-then-provision
   * therefore drops the FIRST build's code on the floor, and the project's NEXT chat seeds
   * from NULL and starts blank — R6 fails silently, one conversation later, while every
   * unit test still passes.
   *
   * Idempotent per project: a second builder chat provisions and gets the same appId back.
   */
  const ensureApp = async (conversationId) => {
    const known = currentAppId()
    if (known) return known
    const ownerProjectId = projectId
    const registration = await provisionApp({ conversationId, projectId: ownerProjectId })
    // Adopt the id only if the project on screen is still the one we provisioned for. A
    // background turn finishing after a cross-project navigation still needs the id for its own
    // `patchBuildCode` (we return it), but it must not poison the ref the NEXT chat reads.
    if (projectIdRef.current === ownerProjectId) {
      appIdRef.current = { projectId: ownerProjectId, appId: registration.appId }
      applyDeploy({ ...registration, appId: registration.appId }, conversationId)
    }
    return registration.appId
  }

  /**
   * First visit to a client-minted build chat: the row does not exist yet. Seed it with
   * the welcome bubble, or — when Sandbox/ChatPage handed off a prompt — persist that
   * turn and generate from it.
   *
   * Persist BEFORE streaming. `POST /v1/claude` resolves the conversation to fold in the
   * project's description and its current app code, so a turn streamed before its row
   * exists silently loses all project context. (The backend's own comment assumes the
   * opposite ordering — do not "correct" this to match it.)
   *
   * An append failure ABORTS the turn. It used to fall through to generate() from inside
   * the catch, streaming a turn that was never saved, against a conversation the server
   * could not find — a billed request that quietly produced a context-less answer.
   */
  const seedFreshBuild = (id, isAlive) => {
    if (initFiredRef.current === id) return // fire-once per chat: a remount must not generate twice
    initFiredRef.current = id
    seqRef.current = 0

    if (!initialPrompt) {
      setMessages([welcomeMessage()])
      return
    }

    // Don't persist a seed turn we cannot stream: another chat already holds the project.
    const blocked = buildBlockedMessage(id)
    if (blocked) {
      setMessages([welcomeMessage()])
      showAttachToast(blocked)
      return
    }

    sendingRef.current = true // the seeded prompt owns this chat's first turn
    const userSeq = seqRef.current
    seqRef.current += 1
    // Files picked at the Generate-App step arrive as pending attachments. Show the
    // prompt immediately; the attachment parts (office text / deck vision / image refs)
    // fill in once buildUserParts uploads them — same pipeline as the refine composer,
    // so the first generation turn can reason over a deck/doc.
    const pending = location.state?.pendingAttachments || []
    const provisional = { id: 'initial-user', role: 'user', parts: [{ type: 'text', text: initialPrompt }], seq: userSeq, createdAt: new Date().toISOString() }
    setMessages([provisional])

    void (async () => {
      let parts
      try {
        parts = await buildUserParts(initialPrompt, pending)
      } catch (err) {
        showAttachToast(err?.message || 'Could not attach your file — generating from your description only.')
        parts = [{ type: 'text', text: initialPrompt }]
      }
      if (!isAlive() || buildIdRef.current !== id) return
      const userMsg = { ...provisional, parts }
      setMessages([userMsg])
      try {
        await appendBuilderMessage(
          id,
          { role: 'user', parts, seq: userSeq },
          { title: deriveTitle(initialPrompt), context: contextRef.current, projectId },
        )
      } catch (err) {
        // The uploads succeeded but the turn never landed — release them so the deck's
        // Files-API PDF + stored bytes don't orphan (best-effort, non-masking).
        releaseUploadedAttachments(parts)
        seqRef.current = userSeq
        sendingRef.current = false
        showAttachToast(describeSaveFailure(err, 'Could not save this build. Check your connection.'))
        if (isConversationGone(err)) navigate('/projects', { replace: true })
        return // abort: never stream a turn the server has no row for
      }
      dropTransientQuery(id)
      refreshBuilds()
      const byteMap = new Map(pending.map((a) => [a.id, a.base64]))
      await generate(id, [userMsg], byteMap)
    })()
  }

  /**
   * The message to show when another build chat is already streaming into this project, or
   * null when the coast is clear. Checked before a turn is persisted so the user keeps their
   * draft; `generate` re-checks by actually taking the lock.
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
   * Hold the project's build lock for the whole stream, and give it back on EVERY exit —
   * success, abort, error, or a mid-run navigation. A lock that outlives its turn wedges the
   * project until the heartbeat expires it, which is a worse failure than no lock at all.
   */
  /**
   * @param activeBuildId the chat this turn belongs to, captured by the caller BEFORE it
   *   awaited the user-turn persist. Re-reading `buildIdRef.current` here would attribute the
   *   whole stream — and its `patchBuildCode` into the project's `current_code` — to whatever
   *   chat the user switched to during that await.
   */
  const generate = async (activeBuildId, currentMessages, byteMap = new Map()) => {
    // `blockedBy` was already consulted before the user turn was persisted; taking the lock
    // here is the actual barrier, and it closes the small race between that check and now.
    if (projectId) {
      const blocker = buildLockRef.current?.acquire(projectId, activeBuildId)
      if (blocker) {
        showAttachToast(buildBlockedMessage(activeBuildId) ?? 'Another build is already running in this project.')
        setGenerating(false)
        sendingRef.current = false
        return
      }
    }
    try {
      turnInFlightRef.current = true
      await runGeneration(activeBuildId, currentMessages, byteMap)
    } finally {
      turnInFlightRef.current = false
      sendingRef.current = false
      if (projectId) buildLockRef.current?.release(activeBuildId)
      // The component unmounted while this turn was still writing. It deferred teardown to
      // us so the claim would outlive it; now that the write is done, close the channel.
      if (disposeAfterTurnRef.current) buildLockRef.current?.dispose()
    }
  }

  const runGeneration = async (activeBuildId, currentMessages, byteMap) => {
    // The build this run belongs to is captured by the caller, so a mid-generation switch to
    // another chat can't misattribute this result. Reserve the assistant sort key now.
    const assistantSeq = seqRef.current
    seqRef.current += 1
    setGenerating(true)
    setGenerationStage(1)
    clearTimers()

    const addStage = (index) => {
      setMessages((prev) => [
        ...prev,
        {
          id: `stage-${index}-${Date.now()}`,
          role: 'assistant',
          parts: [{ type: 'text', text: STAGE_MESSAGES[index] }],
          createdAt: new Date().toISOString(),
          isStage: true,
          showChips: index === 4,
        },
      ])
    }

    addStage(0)
    setToast({ stage: 1, done: false, visible: true })

    timerRefs.current.push(setTimeout(() => { setGenerationStage(2); addStage(1); setToast({ stage: 2, done: false, visible: true }) }, 3000))
    timerRefs.current.push(setTimeout(() => { setGenerationStage(3); addStage(2); setToast({ stage: 3, done: false, visible: true }) }, 6000))
    timerRefs.current.push(setTimeout(() => { setGenerationStage(4); addStage(3); setToast({ stage: 4, done: false, visible: true }) }, 10000))

    // currentMessages already includes the new user turn; map the real
    // (non-stage, non-welcome) messages to the API shape. Only the newest turn's
    // image/PDF bytes are inflated (from the composer via byteMap); historical
    // binaries are dropped (the model already saw them).
    const realMessages = currentMessages.filter((m) => !m.isStage && m.id !== 'welcome')
    const apiMessages = assembleApiMessages(realMessages, (id) => byteMap.get(id))

    const result = await sendMessage(
      apiMessages,
      (_, full) => {
        if (buildIdRef.current !== activeBuildId) return // user switched builds mid-stream
        const code = extractPreviewCode(full)
        if (code) setPreviewCode(code)
      },
      contextRef.current,
      activeBuildId, // the server folds in this project's description + current code
    )

    clearTimers()

    // A null result means the send failed (429/network) or was aborted. Don't
    // persist anything and don't fake the success toast/stage; any error message
    // surfaces via the `error` banner above the input. Only reset the UI if this
    // run still owns the view (no mid-run build switch).
    if (result == null) {
      if (buildIdRef.current === activeBuildId) {
        setGenerating(false)
        setToast({ stage: 0, done: false, visible: false })
      }
      return
    }

    // Persist the REAL assistant result turn (parts) AND the extracted code
    // snapshot (code.current), attributed to the build this run started on,
    // regardless of any later switch — but NOT if that build was deleted mid-run
    // (deletedRef): the append upserts the header, which would resurrect a build
    // the user just deleted. Stage/welcome bubbles are never persisted. On resume
    // the preview renders from code.current — no transcript scan.
    const finalCode = extractPreviewCode(result)
    if (activeBuildId && !deletedRef.current.has(activeBuildId)) {
      try {
        await appendBuilderMessage(activeBuildId, { role: 'assistant', parts: [{ type: 'text', text: result }], seq: assistantSeq }, { projectId })
        if (finalCode) {
          // PROVISION FIRST, THEN PATCH. The backend mirrors this snapshot into
          // app_registry.current_code only if the app row already exists, so the reverse
          // order silently drops the first build's code and the project's NEXT chat seeds
          // from nothing. Provisioning is still lazy in the sense that matters — a project
          // whose builds never produce code never gets an app — just not lazy relative to
          // the code write.
          //
          // It is also no longer gated on `usesDataService`: current_code is the project's
          // durable source, not merely the data-app's, so a build with no BIALData wiring
          // must still reach it or the next chat starts blank.
          await ensureApp(activeBuildId)
          await patchBuildCode(activeBuildId, { source: finalCode, entry: 'PreviewApp', createdAt: new Date().toISOString() })
        }
        refreshBuilds()
      } catch (err) {
        if (buildIdRef.current === activeBuildId) showAttachToast(describeAppFailure(err))
        if (isConversationGone(err)) navigate('/projects', { replace: true })
      }
    }

    // If the user switched to a different build while this one was generating,
    // don't clobber the now-displayed build with this run's preview/stages/toast.
    if (buildIdRef.current !== activeBuildId) return

    if (finalCode) setPreviewCode(finalCode)

    // Surface the assistant's actual reply in the transcript right away — no page
    // refresh needed. MessageContent strips the jsx:preview fence, so a code reply
    // shows only its prose (the preview pane renders the app) while a
    // clarifying-questions reply (no code) shows in full. Skip a pure-code reply
    // (empty prose) so it doesn't leave a blank bubble.
    if (filterCodeFromContent(result)) {
      setMessages((prev) => [
        ...prev,
        {
          id: `assistant-${activeBuildId}-${assistantSeq}`,
          role: 'assistant',
          parts: [{ type: 'text', text: result }],
          seq: assistantSeq,
          createdAt: new Date().toISOString(),
        },
      ])
    }

    // Pin the chosen data collection across regenerations (Decision 11) and warn
    // when a regeneration renames it (previously-saved records would not appear).
    if (finalCode) {
      const schema = extractDataSchema(finalCode)
      if (schema) {
        const prev = contextRef.current.dataSchema
        if (prev?.collection && prev.collection !== schema.collection) {
          showAttachToast(
            `Heads up: the data collection changed from “${prev.collection}” to “${schema.collection}”. Previously saved records won't appear under the new name — ask an admin to clear test data if needed.`,
          )
        }
        contextRef.current.dataSchema = schema
      }
    }

    setGenerationStage(5)
    // Only celebrate a real build. When the model replied with questions / prose
    // and produced no app (no finalCode), its reply is already shown above — don't
    // add the "Your app is ready" bubble + refinement chips or pop the success
    // toast over an empty preview.
    if (finalCode) {
      addStage(4)
      setToast({ stage: 0, done: true, visible: true })
      toastTimer.current = setTimeout(() => setToast((t) => ({ ...t, visible: false })), 6000)
    } else {
      setToast({ stage: 0, done: false, visible: false })
    }
    setGenerating(false)
  }

  const handleSend = async () => {
    const text = input.trim()
    const attachments = pendingAttachments
    if (!text && attachments.length === 0) return // nothing to send — a silent no-op is fine
    if (generating || sendingRef.current) {
      // A turn is already in flight in THIS builder instance — and `sendingRef` is
      // instance-wide, so it may be a turn we started in a chat we've since navigated away
      // from. The Send button doesn't reflect `sendingRef` (a ref drives no re-render), so it
      // can look enabled while this fires. Explain the block instead of dropping the click.
      showAttachToast('Please wait for the current build to finish before sending another message.')
      return
    }

    // Guardrails run BEFORE clearing the composer so an aborted send keeps the
    // user's draft + pending files. Context full → hard stop (send is also
    // disabled in the UI). Per-conversation attachment cap → distinct toast.
    if (ctxLevel === 'full') return
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

    sendingRef.current = true // synchronous: no second stream may start in this chat
    setInput('')
    clearPending()

    // Build the user turn's parts: uploads each image/PDF (server file ref) and
    // inlines each csv/txt as a text part. An upload/cap failure aborts the send.
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

    // Persist the user turn (upserts the header) BEFORE generating — `POST /v1/claude`
    // resolves the conversation to fold in the project's description and code seed, so a
    // stream that outruns its row silently loses all project context. Title + generation
    // context only on the first turn; `projectId` always (the server ignores it on the
    // upsert branch, and it is REQUIRED on the create branch). On failure, abort + roll back.
    try {
      await appendBuilderMessage(
        activeBuildId,
        { role: 'user', parts, seq: userSeq },
        userSeq === 0
          ? { title: deriveTitle(partsToText(parts)), context: contextRef.current, projectId }
          : { projectId },
      )
    } catch (err) {
      // The uploads succeeded but the turn never landed — release them so the
      // deck's Files-API PDF + stored bytes don't orphan (best-effort, non-masking).
      releaseUploadedAttachments(parts)
      showAttachToast(describeSaveFailure(err))
      setMessages(messages)
      seqRef.current = userSeq
      sendingRef.current = false
      if (isConversationGone(err)) navigate('/projects', { replace: true })
      return
    }
    dropTransientQuery(activeBuildId)
    refreshBuilds()

    const byteMap = new Map(attachments.map((a) => [a.id, a.base64]))
    await generate(activeBuildId, updated, byteMap)
  }

  // Keep the deploy ref in sync with state so async callbacks read the latest. Every
  // deploy record is stamped with the chat it was read for, so a render can tell whether
  // it still belongs to the chat on screen.
  const applyDeploy = (d, chatId) => {
    const tagged = d ? { ...d, chatId } : null
    deployRef.current = tagged
    setDeploy(tagged)
  }

  // Submit the PROJECT'S APP for admin review — keyed on appId, never on the conversation.
  // A failure surfaces a toast — never a silent drop.
  const handleSubmit = async () => {
    const chatId = buildIdRef.current
    const appId = currentAppId()
    if (!appId || submitting) return
    setSubmitting(true)
    try {
      const res = await submitApp(appId, previewCode)
      applyDeploy({ ...(deployRef.current || {}), appId, status: res.status, rejectionNote: null }, chatId)
      showAttachToast('Submitted for deployment — pending admin review.')
    } catch (e) {
      showAttachToast(e.message)
    } finally {
      setSubmitting(false)
    }
  }

  // Re-read the live deploy status (admin may have approved/rejected/disabled).
  // Read-only — never provisions, so polling can't create abandoned drafts.
  const refreshDeployStatus = useCallback(async () => {
    const chatId = buildIdRef.current
    const appId = currentAppId()
    if (!appId) return
    try {
      const s = await getAppStatus(appId)
      if (buildIdRef.current === chatId) applyDeploy(s.status ? { ...s, appId } : null, chatId)
    } catch (e) {
      // Not-provisioned resolves to `{status:null}`, so a throw here is a real 5xx/auth
      // error — surface it (keep the current status; the next focus/refresh recovers).
      console.error('Failed to refresh deploy status', e)
    }
  }, [])

  // Refresh the deploy status when the window regains focus (cheap, read-only) so
  // an admin approval/rejection shows up without a manual reload.
  useEffect(() => {
    const onFocus = () => {
      if (deployRef.current) refreshDeployStatus()
    }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [refreshDeployStatus])

  const handleSelectBuild = (id) => {
    setShowBuilds(false)
    if (id === buildIdRef.current) return
    setViewer(null)
    navigate(`/chat/${id}`)
  }

  // A new build chat is filed under THIS chat's project — a project owns one app, so a
  // second build chat continues the same codebase rather than starting a new one. The id
  // is minted here; the row appears on its first append.
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
    // Mark the id deleted BEFORE the reset/await so an in-flight generation's
    // assistant persist short-circuits (the deletedRef check in generate) — the
    // upsert-on-append would otherwise re-create the header and resurrect the
    // build. Resetting to a fresh build also stops the UI clobber (buildIdRef change).
    deletedRef.current.add(id)
    if (id === buildIdRef.current) {
      // The chat we are looking at is gone. Go up to the project, not into a brand-new
      // build chat the user never asked for.
      setShowBuilds(false)
      setViewer(null)
      clearTimers()
      navigate(projectId ? `/projects/${projectId}` : '/projects', { replace: true })
    }
    setBuilds((prev) => prev.filter((b) => b.id !== id)) // optimistic removal
    try {
      await deleteBuild(id)
    } catch {
      deletedRef.current.delete(id) // delete didn't land — allow future writes to it again
      refreshBuilds() // reconcile — the row reappears if the delete didn't land
      return
    }
    refreshBuilds()
  }

  // Render-time isolation guard. A deploy record read for chat A is not usable while chat
  // B is on screen, and this is evaluated on the SAME render that first sees the new
  // :chatId — before any effect runs, before any fetch resolves. The alternative (clear it
  // in an effect) leaves one committed render in which the sandboxed iframe holds project
  // A's appKey; its mount-time data-service calls would write into A's record store.
  // The existing `buildIdRef.current !== saved.id` guard prevents a stale WRITE; it does
  // nothing about a stale READ.
  const activeDeploy = deploy?.chatId === buildId ? deploy : null

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
                  builds.map((b) => (
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
                        disabled={generating && b.id === buildIdRef.current}
                        aria-label={`Delete ${b.title || 'build'}`}
                        title={
                          generating && b.id === buildIdRef.current
                            ? 'Finishing a build — you can delete it once the turn completes'
                            : 'Delete build'
                        }
                        className={`absolute right-1.5 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 transition p-1 ${
                          generating && b.id === buildIdRef.current
                            ? 'text-neutral/40 cursor-not-allowed'
                            : 'text-neutral hover:text-danger'
                        }`}
                      >
                        <Trash2 size={11} />
                      </button>
                    </div>
                  ))
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

                {msg.showChips && (
                  <div className="ml-8 mt-2 flex flex-wrap gap-1.5">
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
            ))}

            {generating && (
              <div className="flex gap-2 items-center">
                <div className="w-6 h-6 rounded-full bg-primary/10 flex items-center justify-center">
                  <Sparkles size={10} className="text-primary" />
                </div>
                <div className="bg-bial-bg rounded-2xl px-3 py-2.5 flex gap-1">
                  {[0, 1, 2].map((i) => (
                    <div key={i} className="w-1.5 h-1.5 bg-primary rounded-full animate-bounce" style={{ animationDelay: `${i * 0.15}s` }} />
                  ))}
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          {/* Input */}
          <div className="p-3 border-t border-bial-border space-y-2">
            {error && (
              <div className="text-[11px] text-danger bg-danger/5 border border-danger/20 rounded-lg px-2.5 py-1.5">
                {error}
              </div>
            )}
            {/* Context-length guardrail: warn as it grows, hard-stop at the window */}
            {ctxLevel === 'full' ? (
              <div className="text-[11px] text-danger bg-danger/5 border border-danger/20 rounded-lg px-2.5 py-1.5">
                <p className="mb-1">This build conversation has reached its maximum length.</p>
                <button onClick={handleNewBuild} className="font-bold underline">
                  Start new build
                </button>
              </div>
            ) : ctxLevel === 'warn' ? (
              <div className="text-[11px] text-tertiary bg-warning/10 border border-warning/30 rounded-lg px-2.5 py-1.5">
                <p className="mb-1">This build conversation is getting long.</p>
                <button onClick={handleNewBuild} className="font-bold text-primary underline">
                  Start new build
                </button>
              </div>
            ) : null}
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
                disabled={generating}
                title="Attach images, PDFs, Word, Excel, or text files (CSV, TXT)"
                className="flex-shrink-0 w-9 h-9 bg-bial-bg hover:bg-surface-muted disabled:opacity-40 text-neutral hover:text-primary border border-bial-border rounded-xl flex items-center justify-center transition"
              >
                <Paperclip size={13} />
              </button>
              <textarea
                ref={inputRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() } }}
                rows={2}
                placeholder="Type instructions to refine your app..."
                className="flex-1 resize-none text-xs text-tertiary bg-bial-bg border border-bial-border rounded-xl px-3 py-2 focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20 transition placeholder:text-gray-300"
              />
              <button
                onClick={handleSend}
                disabled={(!input.trim() && pendingAttachments.length === 0) || generating || ctxLevel === 'full'}
                className="flex-shrink-0 w-9 h-9 bg-secondary hover:bg-secondary-600 disabled:opacity-40 text-white rounded-xl flex items-center justify-center transition"
              >
                <Send size={13} />
              </button>
            </div>
          </div>
        </div>

        {/* Preview */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {DEPLOY_ENABLED && previewCode && activeDeploy?.appId && (
            <DeployBar
              status={activeDeploy.status}
              appId={activeDeploy.appId}
              rejectionNote={activeDeploy.rejectionNote}
              busy={submitting}
              onSubmit={handleSubmit}
              onRefresh={refreshDeployStatus}
            />
          )}
          <LivePreview
            previewCode={previewCode}
            generating={generating}
            generationStage={generationStage}
            prompt={initialPrompt}
            config={
              activeDeploy?.appKey
                ? { appId: activeDeploy.appId, appKey: activeDeploy.appKey, baseUrl: '/api', loginRequired: Boolean(activeDeploy.loginRequired) }
                : undefined
            }
            accessToken={activeDeploy?.loginRequired ? getAccessToken() : undefined}
            user={activeDeploy?.loginRequired ? getStoredUser() : undefined}
          />
        </div>
      </div>

      <Toast
        stage={toast.stage}
        done={toast.done}
        visible={toast.visible}
        onDismiss={() => setToast((t) => ({ ...t, visible: false }))}
      />

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
