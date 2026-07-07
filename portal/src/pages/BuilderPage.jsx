import { useState, useEffect, useRef, useCallback } from 'react'
import { useNavigate, useLocation, useParams } from 'react-router-dom'
import {
  Send, ArrowLeft, Sparkles, User, Brain, LayoutTemplate, Code2, Monitor, CheckCircle, X, Paperclip, FileText,
  FileSpreadsheet, Presentation, History, Trash2,
} from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import LivePreview from '../components/LivePreview'
import DeployBar from '../components/DeployBar'
import AttachmentChips from '../components/AttachmentChips'
import AttachmentLightbox from '../components/AttachmentLightbox'
import { useClaudeAPI, buildSystemPrompt, getContextLimits, estimateConversationTokens } from '../hooks/useClaudeAPI'
import { getAccessToken, getStoredUser } from '../utils/auth'
import { provisionApp, submitApp, getAppStatus } from '../utils/appRegistryApi'
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

/** True when the generated code wires to the shared Data Service (a persist/share app). */
export function usesDataService(code) {
  return typeof code === 'string' && /\bBIALData\b/.test(code)
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

export default function BuilderPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const { buildId } = useParams()
  const initialPrompt = location.state?.prompt || ''
  const contextRef = useRef({
    theme: location.state?.theme || 'bial',
    uploadedFiles: location.state?.uploadedFiles || [],
  })
  const { sendMessage, error } = useClaudeAPI()

  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [generating, setGenerating] = useState(false)
  const [previewCode, setPreviewCode] = useState(null)
  const [generationStage, setGenerationStage] = useState(0)
  const [toast, setToast] = useState({ stage: 0, done: false, visible: false })
  const [builds, setBuilds] = useState([])
  const [showBuilds, setShowBuilds] = useState(false)
  const [viewer, setViewer] = useState(null) // { name, src } for the pending-attachment lightbox
  // Deploy state for the active build: { status, appId, appKey?, loginRequired, rejectionNote? } | null.
  // Set when a data app is provisioned, when the build is submitted, or on resume.
  const [deploy, setDeploy] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  const { pendingAttachments, handleFileSelect, removePending, clearPending, attachToast, showAttachToast } =
    usePendingAttachments()

  const bottomRef = useRef(null)
  const inputRef = useRef(null)
  const fileInputRef = useRef(null)
  const timerRefs = useRef([])
  const toastTimer = useRef(null)
  const buildIdRef = useRef(null) // the active build being persisted
  const deployRef = useRef(null) // mirrors `deploy` for async callbacks (provision-on-data)
  const initFiredRef = useRef(false) // fire-once guard for the initial-create effect
  const seqRef = useRef(0) // next message sort key for the active build's persisted turns
  const deletedRef = useRef(new Set()) // builds deleted mid-run: their in-flight persist must no-op (no resurrection)

  // Running context-length estimate → 'ok' | 'warn' | 'full'. The builder's
  // system prompt includes any uploaded reference files, so size it from the
  // real prompt. Drives the guardrail banner + send-disable below.
  const ctxTokens = estimateConversationTokens(messages, buildSystemPrompt(contextRef.current))
  const { soft: ctxSoft, hard: ctxHard } = getContextLimits()
  const ctxLevel = ctxTokens >= ctxHard ? 'full' : ctxTokens >= ctxSoft ? 'warn' : 'ok'

  const refreshBuilds = useCallback(async () => {
    try {
      const list = await loadBuilds()
      setBuilds(list.sort((a, b) => new Date(b.updatedAt) - new Date(a.updatedAt)))
    } catch {
      // Keep the current list on a transient error; the next refresh recovers.
    }
  }, [])

  useEffect(() => {
    return () => {
      timerRefs.current.forEach(clearTimeout)
      if (toastTimer.current) clearTimeout(toastTimer.current)
    }
  }, [])

  useEffect(() => {
    refreshBuilds()
  }, [refreshBuilds])

  // Resume a saved build when the :buildId route param points at one (refresh,
  // or a click in Recent builds). Async (server round-trip) with a stale-response
  // guard. Skipped when this build is already active (e.g. just created below) so
  // an in-flight generation isn't clobbered.
  useEffect(() => {
    if (!buildId || buildIdRef.current === buildId) return
    let alive = true
    getBuild(buildId)
      .then((saved) => {
        if (!alive) return
        if (!saved) {
          navigate('/workspace/builder', { replace: true })
          return
        }
        clearTimers()
        buildIdRef.current = saved.id
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
        // Reflect this build's live deploy status (read-only; never provisions).
        getAppStatus(saved.id)
          .then((s) => {
            if (!alive || buildIdRef.current !== saved.id) return
            const d = s.status ? s : null
            deployRef.current = d
            setDeploy(d)
          })
          .catch(() => {})
      })
      .catch(() => {
        if (alive) navigate('/workspace/builder', { replace: true })
      })
    return () => {
      alive = false
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [buildId])

  // First-prompt flow (from the Sandbox/ChatPage handoff): create a build, seed
  // the user turn, generate, and switch the URL to the resumable :buildId form.
  useEffect(() => {
    if (buildId) return // the resume effect owns this case
    if (initFiredRef.current) return // fire-once: StrictMode/remount must not create a 2nd build or 2nd generation
    initFiredRef.current = true
    if (initialPrompt) {
      const id = newBuild() // sync UUID; header created on the first append
      buildIdRef.current = id
      seqRef.current = 0
      const userSeq = seqRef.current
      seqRef.current += 1
      // Files picked at the Generate-App step arrive as pending attachments. Show
      // the prompt immediately; the attachment parts (office text / deck vision /
      // image refs) fill in once buildUserParts uploads them — same pipeline as the
      // refine composer, so the first generation turn can reason over a deck/doc.
      const pending = location.state?.pendingAttachments || []
      const provisional = { id: 'initial-user', role: 'user', parts: [{ type: 'text', text: initialPrompt }], seq: userSeq, createdAt: new Date().toISOString() }
      setMessages([provisional])
      navigate(`/workspace/builder/${id}`, { replace: true })
      // Build the seed user turn (uploading any attachments), persist it (creates
      // the header with title + context), then generate. Generation is the value
      // here, so a persist blip surfaces a toast but doesn't abort; an upload
      // failure falls back to the prompt text so generation still proceeds.
      ;(async () => {
        let parts
        try {
          parts = await buildUserParts(initialPrompt, pending)
        } catch (err) {
          showAttachToast(err?.message || 'Could not attach your file — generating from your description only.')
          parts = [{ type: 'text', text: initialPrompt }]
        }
        const userMsg = { ...provisional, parts }
        setMessages([userMsg])
        try {
          await appendBuilderMessage(
            id,
            { role: 'user', parts, seq: userSeq },
            { title: deriveTitle(initialPrompt), context: contextRef.current },
          )
          refreshBuilds()
        } catch {
          showAttachToast('Could not save this build. Check your connection.')
        }
        const byteMap = new Map(pending.map((a) => [a.id, a.base64]))
        generate([userMsg], byteMap)
      })()
    } else {
      setMessages([welcomeMessage()])
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const clearTimers = () => {
    timerRefs.current.forEach(clearTimeout)
    timerRefs.current = []
  }

  const generate = async (currentMessages, byteMap = new Map()) => {
    // Capture the build this run belongs to + reserve its assistant sort key, so a
    // mid-generation switch to another build can't misattribute this result.
    const activeBuildId = buildIdRef.current
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
        await appendBuilderMessage(activeBuildId, { role: 'assistant', parts: [{ type: 'text', text: result }], seq: assistantSeq }, {})
        if (finalCode) {
          await patchBuildCode(activeBuildId, { source: finalCode, entry: 'PreviewApp', createdAt: new Date().toISOString() })
        }
        refreshBuilds()
      } catch {
        showAttachToast('Your generated app could not be saved.')
      }
    }

    // Provision the shared data backend the FIRST time a build wires to BIALData
    // (Decision 7: stable appId from build time through deploy). Idempotent; the
    // data written while building persists into the deployed app unchanged. Runs
    // for the build that generated the code regardless of a later switch, but the
    // UI state is only adopted if this build is still in view.
    if (activeBuildId && usesDataService(finalCode) && deployRef.current?.appId !== activeBuildId) {
      try {
        const reg = await provisionApp(activeBuildId)
        if (buildIdRef.current === activeBuildId) applyDeploy(reg)
      } catch (e) {
        if (buildIdRef.current === activeBuildId) showAttachToast(`Could not enable data for this app: ${e.message}`)
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
    if ((!text && attachments.length === 0) || generating) return

    // Guardrails run BEFORE clearing the composer so an aborted send keeps the
    // user's draft + pending files. Context full → hard stop (send is also
    // disabled in the UI). Per-conversation attachment cap → distinct toast.
    if (ctxLevel === 'full') return
    if (attachments.length > 0) {
      const cap = validateConversationAttachmentCap(countAttachments(messages), attachments.length)
      if (cap.error) {
        showAttachToast(cap.error)
        return
      }
    }

    setInput('')
    clearPending()

    // Build the user turn's parts: uploads each image/PDF (server file ref) and
    // inlines each csv/txt as a text part. An upload/cap failure aborts the send.
    let parts
    try {
      parts = await buildUserParts(text || 'Please review the attached file(s).', attachments)
    } catch (err) {
      showAttachToast(err?.message || 'Could not upload the attachment.')
      return
    }

    // Ensure a build exists (a from-scratch session has none yet).
    if (!buildIdRef.current) {
      const id = newBuild() // sync UUID; header created on the first append
      buildIdRef.current = id
      seqRef.current = 0
      navigate(`/workspace/builder/${id}`, { replace: true })
    }
    const activeBuildId = buildIdRef.current
    const userSeq = seqRef.current
    seqRef.current += 1

    const userMsg = { id: `local_${Date.now()}`, role: 'user', parts, seq: userSeq, createdAt: new Date().toISOString() }
    const updated = [...messages, userMsg]
    setMessages(updated)

    // Persist the user turn (upserts the header) before generating. Title +
    // generation context only on the first turn. On failure, abort + roll back.
    try {
      await appendBuilderMessage(
        activeBuildId,
        { role: 'user', parts, seq: userSeq },
        userSeq === 0 ? { title: deriveTitle(partsToText(parts)), context: contextRef.current } : {},
      )
    } catch {
      // The uploads succeeded but the turn never landed — release them so the
      // deck's Files-API PDF + stored bytes don't orphan (best-effort, non-masking).
      releaseUploadedAttachments(parts)
      showAttachToast('Could not save your message. Check your connection and try again.')
      setMessages(messages)
      seqRef.current = userSeq
      return
    }
    refreshBuilds()

    const byteMap = new Map(attachments.map((a) => [a.id, a.base64]))
    await generate(updated, byteMap)
  }

  // Keep the deploy ref in sync with state so async callbacks read the latest.
  const applyDeploy = (d) => {
    deployRef.current = d
    setDeploy(d)
  }

  // Submit the current build for admin review (idempotent ensure-draft server-side).
  // A failure surfaces a toast — never a silent drop.
  const handleSubmit = async () => {
    const id = buildIdRef.current
    if (!id || submitting) return
    setSubmitting(true)
    try {
      const res = await submitApp(id, previewCode)
      applyDeploy({ ...(deployRef.current || {}), appId: id, status: res.status, rejectionNote: null })
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
    const id = buildIdRef.current
    if (!id) return
    try {
      const s = await getAppStatus(id)
      if (buildIdRef.current === id) applyDeploy(s.status ? s : null)
    } catch {
      // transient — keep the current status; the next focus/refresh recovers
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
    navigate(`/workspace/builder/${id}`)
  }

  const handleNewBuild = () => {
    setShowBuilds(false)
    setViewer(null)
    buildIdRef.current = null
    seqRef.current = 0
    navigate('/workspace/builder', { replace: true, state: {} })
    clearTimers()
    setMessages([welcomeMessage()])
    setPreviewCode(null)
    setGenerating(false)
    setGenerationStage(0)
    deployRef.current = null
    setDeploy(null)
  }

  const handleDeleteBuild = async (e, id) => {
    e.stopPropagation()
    // Mark the id deleted BEFORE the reset/await so an in-flight generation's
    // assistant persist short-circuits (the deletedRef check in generate) — the
    // upsert-on-append would otherwise re-create the header and resurrect the
    // build. Resetting to a fresh build also stops the UI clobber (buildIdRef change).
    deletedRef.current.add(id)
    if (id === buildIdRef.current) handleNewBuild()
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

  return (
    <div className="h-screen flex flex-col font-manrope bg-bial-bg overflow-hidden">
      <Navbar />

      <div className="flex flex-1 overflow-hidden">
        {/* Chat panel */}
        <div className="w-72 xl:w-80 flex flex-col bg-white border-r border-bial-border flex-shrink-0">
          {/* Agent header */}
          <div className="p-4 border-b border-bial-border relative">
            <div className="flex items-center justify-between gap-2 mb-3">
              <button
                onClick={() => navigate('/workspace/sandbox')}
                className="flex items-center gap-2 p-1 rounded-lg text-neutral hover:text-primary hover:bg-bial-bg transition"
              >
                <ArrowLeft size={15} />
                <span className="text-xs">Back to Sandbox</span>
              </button>
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
                        className="absolute right-1.5 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 text-neutral hover:text-danger transition p-1"
                        title="Delete build"
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
          {DEPLOY_ENABLED && previewCode && buildIdRef.current && (
            <DeployBar
              status={deploy?.status}
              appId={buildIdRef.current}
              rejectionNote={deploy?.rejectionNote}
              busy={submitting}
              onSubmit={handleSubmit}
              onRefresh={deploy ? refreshDeployStatus : null}
            />
          )}
          <LivePreview
            previewCode={previewCode}
            generating={generating}
            generationStage={generationStage}
            prompt={initialPrompt}
            config={
              deploy?.appKey
                ? { appId: deploy.appId, appKey: deploy.appKey, baseUrl: '/api', loginRequired: Boolean(deploy.loginRequired) }
                : undefined
            }
            accessToken={deploy?.loginRequired ? getAccessToken() : undefined}
            user={deploy?.loginRequired ? getStoredUser() : undefined}
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
