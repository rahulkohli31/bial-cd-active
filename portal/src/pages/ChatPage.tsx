import { useState, useRef, useEffect, useCallback } from 'react'
import type { MouseEvent as ReactMouseEvent } from 'react'
import { useNavigate, useParams, useLocation } from 'react-router-dom'
import { Sparkles, User, Send, Plus, MessageSquare, Trash2, Hammer, Paperclip, X, FileText, FileSpreadsheet, Presentation } from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import MessageContent from '../components/chat/MessageContent'
import AttachmentLightbox from '../components/AttachmentLightbox'
import ProjectBreadcrumb from '../components/projects/ProjectBreadcrumb'
import { listProjectConversations, uuidv7 } from '../utils/conversationApi'
import type { ConversationHeader } from '../utils/conversationApi'
import { useClaudeAPI, getContextLimits, estimateConversationTokens } from '../hooks/useClaudeAPI'
import { usePendingAttachments } from '../hooks/usePendingAttachments'
import {
  loadHistory,
  newConversation,
  createConversation,
  getConversation,
  deleteConversation,
  relativeTime,
  deriveTitle,
} from '../utils/chatHistory'
import { wireMessageFromParts, buildUserParts, partsToText, countAttachments, releaseUploadedAttachments } from '../utils/attachmentStore'
import { ACCEPT_ATTR, validateConversationAttachmentCap, TEXT_MEDIA_TYPES, OFFICE_MEDIA_TYPES, DECK_MEDIA_TYPES, officeFormat } from '../utils/attachmentInput'
import type { PendingAttachment } from '../utils/attachmentInput'
import { openPdf } from '../utils/attachmentViewer'
import { describeSaveFailure, isConversationGone } from '../utils/chatErrors'
import { useDropTransientQuery } from '../hooks/useDropTransientQuery'
import type { ChatMessage } from '../utils/messageTypes'

// U7: the planning + summarize system prompts moved SERVER-SIDE (backend
// `claude/prompts.py` — selected by the conversation's kind / the ephemeral flag), so the
// browser no longer sends a `system` field at all. The context guardrail estimates against
// a flat allowance for the server-owned prompt (~1k tokens, well inside the soft margin).
const SERVER_PROMPT_ALLOWANCE = 'x'.repeat(4_000)

/**
 * The planning chat, rendered by ChatRoute at the flat `/chat/:chatId`.
 *
 * `chatId` / `projectId` / `projectName` arrive as props from ChatRoute, which has
 * already resolved the conversation's kind and its project. The `useParams` fallback
 * keeps the page renderable on its own (and keeps its tests honest about the route).
 *
 */
interface ChatPageProps {
  chatId?: string
  projectId?: string | null
  projectName?: string | null
}

interface LastTurn {
  wireMessage: ReturnType<typeof wireMessageFromParts>
  baseSeq: number
  currentChatId: string
  replaceId: string
}

/** ChatMessage plus `interrupted`, a client-only UI marker for a stalled/truncated
 * assistant turn kept on screen — never persisted or returned by the backend
 * (conversationApi.ts's projection mapping never sets it). Local to this page rather
 * than added to the shared ChatMessage in messageTypes.ts. */
type ChatUIMessage = ChatMessage & { interrupted?: boolean }

export default function ChatPage({ chatId: chatIdProp, projectId = null, projectName = null }: ChatPageProps = {}) {
  const navigate = useNavigate()
  const params = useParams()
  const chatId = chatIdProp ?? params.chatId
  const location = useLocation()

  const [history, setHistory] = useState<ConversationHeader[]>([])
  const [activeChatId, setActiveChatId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatUIMessage[]>([])
  const [input, setInput] = useState('')
  const [generating, setGenerating] = useState(false)
  // The id of the chat whose turn is in flight — tracked separately from `generating`, which is
  // a page-global flag, so the delete gate follows the actual STREAM, not the current view. This
  // keeps the streaming chat's delete disabled (and every OTHER chat's enabled) even after a
  // mid-stream navigate to a sibling chat. Cleared on both send-exit paths.
  const [streamingChatId, setStreamingChatId] = useState<string | null>(null)
  const [hydrating, setHydrating] = useState(false) // loading a saved transcript over the network
  const [showBuildModal, setShowBuildModal] = useState(false)
  const [showPromptModal, setShowPromptModal] = useState(false)
  const [builderPrompt, setBuilderPrompt] = useState('')
  const [summarizing, setSummarizing] = useState(false)
  const [viewer, setViewer] = useState<{ name: string; src: string } | null>(null) // for the pending-attachment lightbox
  const buildSuggestionFiredRef = useRef(false)
  // Fire-once-per-chat guard for the handoff `initialMessage` — a ref survives one mount, but a
  // RELOAD is a fresh mount over the same history entry, so the fire path ALSO strips the handoff
  // from history (see the hydration effect). Together they stop the reload re-post/re-call (F1).
  const initFiredRef = useRef<string | null>(null)
  // The last turn's re-send context ({ apiMessages, baseSeq, currentChatId, replaceId }) so a
  // stall/error can offer a user-initiated Regenerate — the user turn already survives
  // (persist-before-stream), so only the assistant reply needs re-requesting. `replaceId` is the
  // interrupted assistant bubble's stable id: a regenerate REPLACES it (never stacks a duplicate
  // under the partial). Cleared on success; null → nothing to regenerate.
  const lastTurnRef = useRef<LastTurn | null>(null)
  // Monotonic stream generation, bumped on every chat switch (mirrors useBuildSession's
  // relaunchGenRef): a stream captures it at launch and refuses to touch state once it changes,
  // so a superseded stream (A→B, or A→B→A before it resolved) can neither leak its
  // generating/error state into the new chat nor clobber a newer stream's flags from its finally.
  const streamGenRef = useRef(0)
  // Source of truth for "which conversation is active", kept in lockstep with
  // activeChatId via setActive. The streaming send path guards every assistant
  // write against this ref so a turn never lands on the wrong (or a deleted)
  // conversation after a mid-stream navigate/delete.
  const activeChatIdRef = useRef<string | null>(null)

  const dropTransientQuery = useDropTransientQuery()

  const { sendMessage, error, clearError, abort } = useClaudeAPI()
  const { pendingAttachments, handleFileSelect, removePending, clearPending, attachToast, showAttachToast } =
    usePendingAttachments()
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const messagesRef = useRef(messages)
  messagesRef.current = messages

  // The composer/indicator gates scope to the chat that OWNS the in-flight turn (matching the
  // per-chat delete gate below): after a mid-stream navigate, a sibling chat's Send/attach must
  // not be locked — and its transcript must not show dots — for a stream that isn't its own (F7).
  const streamingHere = generating && streamingChatId === activeChatId

  // Running context-length estimate → 'ok' | 'warn' | 'full'. Drives the
  // guardrail banner + send-disable below. Recomputed each render (cheap).
  const ctxTokens = estimateConversationTokens(messages, SERVER_PROMPT_ALLOWANCE)
  const { soft: ctxSoft, hard: ctxHard } = getContextLimits()
  const ctxLevel = ctxTokens >= ctxHard ? 'full' : ctxTokens >= ctxSoft ? 'warn' : 'ok'

  // Set the active conversation id in state AND the ref together, so the
  // streaming guard can read the current id synchronously.
  const setActive = useCallback((id: string | null) => {
    activeChatIdRef.current = id
    setActiveChatId(id)
  }, [])

  // The sidebar lists this PROJECT's planning chats, not every planning chat the user
  // owns — a chat belongs to its project, and cross-project recents would re-introduce
  // the flat all-chats model this phase replaces. Falls back to the flat list only when
  // the project is not yet known (a chat rendered without ChatRoute).
  const refreshHistory = useCallback(async () => {
    try {
      const isHeader = (c: ConversationHeader | null): c is ConversationHeader => c !== null
      const list = projectId
        ? (await listProjectConversations(projectId)).filter(isHeader).filter((c) => c.kind === 'planning')
        : (await loadHistory()).filter(isHeader)
      setHistory(list.sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime()))
    } catch {
      // Keep the current sidebar on a transient error; the next refresh recovers.
    }
  }, [projectId])

  // Load sidebar history on mount and when activeChatId changes
  useEffect(() => {
    refreshHistory()
  }, [refreshHistory, activeChatId])

  // Hydrate the routed conversation. Async (server round-trip) with a stale-response
  // guard so a fast conversation switch can't let an older fetch clobber the newer view.
  //
  // A 404 is NOT an error here: the row does not exist until the first message is
  // appended, so a brand-new chat legitimately misses. ChatRoute has already vouched
  // for this id (it only renders us with a ?projectId= in that case), so we adopt it
  // and start empty rather than bouncing the user.
  useEffect(() => {
    if (!chatId) return undefined
    // Already active locally (e.g. a brand-new chat mid-first-turn) — its first turns
    // are being written by the send path; don't hydrate-over an empty header.
    if (activeChatIdRef.current === chatId) return undefined

    // Adopt the routed chat SYNCHRONOUSLY, before any await. The displayed chat is
    // whatever the URL says, and every stream guard compares against this ref — so a
    // send fired while hydration is still in flight persists against the right id, and
    // a mid-stream navigation drops the old chat's write on the very render that
    // changed the route rather than one fetch later.
    setActive(chatId)
    setMessages([])
    setInput('') // the composer draft belongs to the OLD chat — never carry it into this one
    clearPending() // and neither do its staged attachments (no key={chatId} remount clears them)
    // A stalled turn's error banner + its Regenerate context belong to the OLD chat. ChatPage
    // stays mounted across chat navigations (no key={chatId}), so without this the banner's "Try
    // again" would linger and re-fire the previous chat's turn into this one — a phantom bubble in
    // the wrong chat and a discarded, billed model turn.
    clearError()
    lastTurnRef.current = null
    // A mid-stream chat switch must not leak the OLD chat's stream into this one (F7): supersede
    // the stream generation, ABORT the in-flight request (a genuine abort returns its partial
    // without an error — that path stays non-error by design), and reset the page-global streaming
    // flags, which as of this render describe no chat. The superseded stream's own (gen-guarded)
    // finally will not touch them again.
    streamGenRef.current += 1
    abort()
    setGenerating(false)
    setStreamingChatId(null)

    let alive = true
    setHydrating(true)
    getConversation(chatId)
      .then((conv) => {
        if (!alive || activeChatIdRef.current !== chatId) return
        const serverMessages = conv ? conv.messages : []
        setMessages(serverMessages)
        buildSuggestionFiredRef.current = false
        // Fire the handoff prompt ONLY when the SERVER transcript is empty (a genuinely new chat),
        // once per chat, and strip it from history FIRST so a reload can't re-fire it. Deciding this
        // off the transient in-memory `messages.length === 0` (set to [] synchronously on mount) is
        // what re-posted the first turn AND re-called the model on every reload — a duplicate
        // seq0/seq2. This is the builder's `fireHandoffPrompt` pattern (initFiredRef + replaceState).
        const initialMessage = location.state?.initialMessage
        if (initialMessage && serverMessages.length === 0 && initFiredRef.current !== chatId) {
          initFiredRef.current = chatId
          window.history.replaceState({}, '', window.location.pathname + window.location.search)
          fireMessage(initialMessage, [], chatId)
        }
      })
      .catch(() => {
        // A real load failure (401 is handled by the auth gate + refresh, 403-suspended
        // by the interceptor) — back to the projects index rather than crash the shell.
        if (alive) navigate('/projects', { replace: true })
      })
      .finally(() => {
        if (alive) setHydrating(false)
      })
    return () => {
      alive = false
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatId])

  // The handoff `initialMessage` is now fired from the hydration effect above (gated on the SERVER
  // transcript being empty + fire-once + history strip), NOT from a separate effect keyed on the
  // transient in-memory message count — that re-fired on every reload (F1).

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, generating])

  // Stream ONE assistant reply for a user turn the SERVER persists (U7). Shared by the send
  // path and by Regenerate (which re-requests the same reply with `regenerate: true` — the
  // user turn is already durable server-side, so no second copy is recorded). The `finally`
  // reset is defensive belt-and-suspenders: the load-bearing anti-hang fix is the
  // useClaudeAPI stall watchdog, which makes `sendMessage` actually resolve on a dead socket
  // so the spinner never sticks — but a throw anywhere in here must still clear `generating`.
  const streamAssistant = useCallback(async (
    wireMessage: ReturnType<typeof wireMessageFromParts>,
    baseSeq: number,
    currentChatId: string,
    { replaceId = null, regenerate = false }: { replaceId?: string | null; regenerate?: boolean } = {},
  ) => {
    const gen = streamGenRef.current
    const assistantId = `local_${Date.now()}_a`
    lastTurnRef.current = { wireMessage, baseSeq, currentChatId, replaceId: assistantId }
    let assistantText = ''
    setGenerating(true)
    setStreamingChatId(currentChatId)
    setMessages((prev) => {
      // Regenerate REPLACES the interrupted bubble by its stable id (never an array index), so a
      // retry can't stack a duplicate under the partial it is replacing.
      const base = replaceId ? prev.filter((m) => m.id !== replaceId) : prev
      const assistantMsg: ChatUIMessage = {
        id: assistantId,
        role: 'assistant',
        parts: [{ type: 'text', text: '' }],
        seq: baseSeq + 1,
        createdAt: new Date().toISOString(),
      }
      return [...base, assistantMsg]
    })

    try {
      const result = await sendMessage(
        wireMessage,
        (delta) => {
          // Ignore deltas once superseded: a different conversation in view, or the same chat
          // re-hydrated after an away-and-back (A→B→A) — its transcript was rebuilt without
          // this stream's bubble, so a late delta has nowhere honest to land.
          if (streamGenRef.current !== gen || activeChatIdRef.current !== currentChatId) return
          assistantText += delta
          setMessages((prev) =>
            prev.map((m) => m.id === assistantId ? { ...m, parts: [{ type: 'text', text: assistantText }] } : m)
          )
        },
        currentChatId, // the server owns history + system prompt; this names the thread
        regenerate ? { regenerate: true } : {},
      )

      // Superseded by a chat switch while awaiting: the hydration effect already aborted the
      // request, reset the flags, and rebuilt the transcript — write nothing here (F7).
      if (streamGenRef.current !== gen) return

      // A falsy result = the send failed (a stall, a network drop, a 429), was aborted, OR streamed
      // zero text. Keep a NON-EMPTY partial and mark it interrupted (plan U7) — Regenerate replaces
      // it by id; only an empty bubble is dropped. The reason surfaces via the `error` banner.
      if (!result) {
        if (assistantText && activeChatIdRef.current === currentChatId) {
          setMessages((prev) => prev.map((m) => (m.id === assistantId ? { ...m, interrupted: true } : m)))
        } else {
          setMessages((prev) => prev.filter((m) => m.id !== assistantId))
        }
        return
      }
      lastTurnRef.current = null // succeeded — nothing to regenerate

      // U7: the SERVER persisted both sides of the turn before [DONE] — no client append.
      if (activeChatIdRef.current === currentChatId) refreshHistory()

      // Check if we should suggest moving to builder
      const allMessages = [...messagesRef.current]
      const shouldSuggest =
        !buildSuggestionFiredRef.current &&
        allMessages.filter((m) => m.role === 'user').length >= 3 &&
        (
          allMessages.length >= 6 ||
          /ready to build|shall we proceed|want me to create|build this for you|sounds like a plan/i.test(assistantText)
        )

      if (shouldSuggest) {
        buildSuggestionFiredRef.current = true
        setTimeout(() => setShowBuildModal(true), 600)
      }
    } finally {
      // Gen-guarded: after a chat switch these flags were already reset by the hydration effect
      // and may since describe a NEWER stream — a stale finally must not clobber it (F7).
      if (streamGenRef.current === gen) {
        setGenerating(false)
        setStreamingChatId(null)
      }
    }
  }, [sendMessage, refreshHistory])

  const fireMessage = useCallback(async (rawText: string, attachments: PendingAttachment[] = [], explicitChatId?: string) => {
    if (generating) return
    const text = rawText.trim() || (attachments.length ? 'Please review the attached file(s).' : '')
    if (!text && attachments.length === 0) return
    // A brand-new chat passes its id explicitly: setActiveChatId hasn't committed
    // yet when handleSend schedules this, so the activeChatId closure is stale.
    const currentChatId = explicitChatId ?? activeChatId
    if (!currentChatId) return

    // The transcript BEFORE this turn, captured before any await/render so the
    // API assembly and seq are stable regardless of intervening re-renders.
    const priorMessages = messagesRef.current
    const baseSeq = priorMessages.length
    const isFirstTurn = baseSeq === 0

    // Build the user turn's parts: uploads each image/PDF (returning a server file
    // ref) and inlines each csv/txt as a text part. An upload failure — including
    // the per-user storage cap — aborts the send before anything is shown.
    let parts
    try {
      parts = await buildUserParts(text, attachments)
    } catch (err) {
      showAttachToast(err instanceof Error ? err.message : 'Could not upload the attachment.')
      return
    }

    const userMsg: ChatUIMessage = { id: `local_${Date.now()}`, role: 'user', parts, seq: baseSeq, createdAt: new Date().toISOString() }
    setMessages((prev) => [...prev, userMsg])
    setGenerating(true)
    setStreamingChatId(currentChatId) // gate THIS chat's delete for the turn's lifetime

    // U7: the conversation row must EXIST before the first turn — the stateless relay 404s
    // an unknown id (the legacy appears-on-first-append upsert is gone; the server persists
    // turns itself). Idempotent, so a retry after a failed first stream re-confirms cheaply.
    if (isFirstTurn) {
      try {
        await createConversation(currentChatId, {
          // fireMessage only runs from a route ChatRoute already resolved a project for
          // (see the module doc comment); projectId is non-null by the time a turn fires.
          projectId: projectId as string,
          title: deriveTitle(partsToText(parts)),
        })
      } catch (err) {
        // The uploads succeeded but the turn never landed — release them so the stored
        // bytes don't orphan (best-effort, non-masking).
        releaseUploadedAttachments(parts)
        setMessages((prev) => prev.filter((m) => m.id !== userMsg.id))
        setGenerating(false)
        setStreamingChatId(null)
        showAttachToast(describeSaveFailure(err))
        if (isConversationGone(err)) navigate('/projects', { replace: true })
        return
      }
    }
    dropTransientQuery(currentChatId)
    refreshHistory()

    // The stateless wire message: typed prose + fenced attachment text + owned binary refs.
    // No transcript, no bytes — the server loads history and rehydrates references (R9).
    await streamAssistant(wireMessageFromParts(parts), baseSeq, currentChatId)
  }, [activeChatId, generating, streamAssistant, refreshHistory, showAttachToast, projectId, navigate, dropTransientQuery])

  // Re-request the last turn's reply after a stall/error. User-initiated ONLY (never auto-fired):
  // the first turn bills server-side regardless of the client outcome, so a regenerate is a SECOND
  // full bill — framed to the user as "try again", not a free retry. Replaces the interrupted turn
  // (the dropped assistant bubble) rather than appending a duplicate. U7: `regenerate: true` tells
  // the server to replay history without recording a second copy of the user turn.
  const handleRegenerate = useCallback(() => {
    const turn = lastTurnRef.current
    // Only regenerate the turn for the chat currently in view. The banner is cleared on navigation,
    // but this guards the window where a stale turn could still be pointed at another conversation.
    if (generating || !turn || turn.currentChatId !== activeChatIdRef.current) return
    // `replaceId` swaps the interrupted bubble for the retry's fresh one — replace, not append.
    void streamAssistant(turn.wireMessage, turn.baseSeq, turn.currentChatId, {
      replaceId: turn.replaceId,
      regenerate: true,
    })
  }, [generating, streamAssistant])

  const handleSend = () => {
    const text = input.trim()
    const attachments = pendingAttachments
    if (!text && attachments.length === 0) return

    // Guardrails run BEFORE clearing the composer so an aborted send keeps the
    // user's draft + pending files. Context full → hard stop (send is also
    // dimmed in the UI). Per-conversation attachment cap → distinct toast.
    if (ctxLevel === 'full') return
    // A reply in flight is ENFORCED here, not by the button. The textarea has never been disabled
    // on this page, so Enter always called straight through — the dimmed Send was affordance
    // standing in for a guard that did not exist. It does now (and it must, because U4's send
    // button is `aria-disabled`, which is clickable by design).
    if (streamingHere) {
      showAttachToast('Send unlocks when the current reply finishes. Keep typing meanwhile.')
      return
    }
    if (attachments.length > 0) {
      const cap = validateConversationAttachmentCap(countAttachments(messages), attachments.length)
      if ('error' in cap) {
        showAttachToast(cap.error)
        return
      }
    }

    setInput('')
    clearPending()

    // Pass the route's chat id explicitly. Under flat routing it is known from the
    // first render, while `activeChatId` only commits once hydration resolves — a send
    // fired in that window would otherwise hit fireMessage's `if (!currentChatId) return`
    // and vanish without a trace.
    fireMessage(text, attachments, chatId)
  }

  const handleSelectChat = (id: string) => {
    setViewer(null)
    navigate(`/chat/${id}`)
    buildSuggestionFiredRef.current = false
  }

  // A new chat is always filed under THIS chat's project — there is no Default project
  // and no project-less chat. The id is minted client-side; the row appears on the
  // first append, so the project rides a transient query until then.
  const handleNewChat = () => {
    setViewer(null)
    if (!projectId) {
      navigate('/projects')
      return
    }
    // `freshlyMinted` — see handleLaunchBuilder below for why the marker lives in router state.
    navigate(`/chat/${newConversation()}?projectId=${encodeURIComponent(projectId)}&kind=planning`, {
      state: { freshlyMinted: true },
    })
  }

  const handleDeleteChat = async (e: ReactMouseEvent, id: string) => {
    e.stopPropagation()
    // If the active conversation is being deleted, clear the active id FIRST so any
    // in-flight stream's assistant write no-ops (the guard sees the id change) — an
    // in-flight reply can't resurrect the just-deleted conversation.
    if (activeChatIdRef.current === id) {
      setMessages([])
      setActive(null)
      navigate(projectId ? `/projects/${projectId}` : '/projects', { replace: true })
    }
    setHistory((prev) => prev.filter((c) => c.id !== id)) // optimistic removal
    try {
      await deleteConversation(id)
    } catch {
      refreshHistory() // reconcile — the row reappears if the delete didn't land
      return
    }
    refreshHistory()
  }

  const handleBuildApp = useCallback(async () => {
    setShowBuildModal(false)
    setShowPromptModal(true)
    setSummarizing(true)
    setBuilderPrompt('')

    let accumulated = ''
    // U7: an EPHEMERAL turn — the server loads this chat's history (it IS the planning
    // transcript, no client compilation needed), applies the summarize prompt server-side,
    // and persists nothing. The response is only a draft for the builder handoff.
    await sendMessage(
      { text: 'Extract the app requirements from this planning conversation and write the builder prompt.' },
      (delta) => {
        accumulated += delta
        setBuilderPrompt(accumulated)
      },
      // Only reachable via "Build This App", rendered solely once messages.length > 0 —
      // i.e. after hydration has set an active chat.
      activeChatId as string,
      { ephemeral: 'summarize_brief' },
    )

    setSummarizing(false)
  }, [sendMessage, activeChatId])

  /**
   * Hand the summarized brief to the project's CANONICAL build thread (003-U1).
   *
   * This used to mint a NEW builder conversation. Under newest-wins canonicalization that would
   * quietly hijack the project's thread: the fresh empty chat would become "the" thread, and the
   * transcript the user had already built up — questions, briefs, build outcomes — would be
   * orphaned in a row nothing routes to any more. Resolve the existing thread instead and stage
   * the brief as a draft; the user still confirms it there.
   *
   * (ProjectBuilder builds the same handoff payload independently — keep the two in step.)
   */
  const handleLaunchBuilder = useCallback(() => {
    if (!projectId) {
      navigate('/projects')
      return
    }
    // U13: every launch MINTS A NEW chat (the canonical builder thread is retired). The
    // refined brief rides as the first message of a Plan-mode conversation — the unified
    // chat plans it there and Build it starts the build.
    setShowPromptModal(false)
    // Mint through the SHARED `uuidv7`, never an inline `crypto.randomUUID()`. Two sites once
    // duplicated that one-liner, so when the store's mint moved to v7 (ADR-0006) they silently
    // kept minting v4 primary keys — duplicated minting logic is exactly how that drifted.
    //
    // `freshlyMinted` tells ChatRoute this id's row does not exist yet, so it can skip a GET that
    // can only 404. It rides in router STATE on purpose: state dies on reload and never travels
    // in a shared link, so a bookmarked `/chat/{id}?kind=…` still asks the server for the truth.
    navigate(
      `/chat/${uuidv7()}?projectId=${encodeURIComponent(projectId)}&kind=builder`,
      { state: { prompt: builderPrompt, mode: 'plan', theme: 'bial', uploadedFiles: [], freshlyMinted: true } },
    )
  }, [builderPrompt, navigate, projectId])

  return (
    <div className="h-screen overflow-hidden bg-bial-bg font-manrope flex flex-col">
      <Navbar />

      <div className="flex flex-1 overflow-hidden" style={{ height: 'calc(100vh - 56px)' }}>
        {/* Sidebar */}
        <aside className="w-64 flex-shrink-0 bg-white border-r border-bial-border flex flex-col overflow-hidden">
          <div className="p-3 border-b border-bial-border">
            <button
              onClick={handleNewChat}
              className="w-full flex items-center justify-center gap-2 bg-primary hover:bg-primary-dark text-white text-sm font-bold rounded-xl px-4 py-2.5 transition"
            >
              <Plus size={15} />
              New Chat
            </button>
          </div>

          <div className="flex-1 overflow-y-auto scrollbar-thin py-2">
            {history.length === 0 ? (
              <div className="px-4 py-8 text-center">
                <MessageSquare size={28} className="text-bial-border mx-auto mb-2" />
                <p className="text-xs text-neutral">No conversations yet</p>
              </div>
            ) : (
              history.map((conv) => (
                <div
                  key={conv.id}
                  onClick={() => handleSelectChat(conv.id)}
                  className={`group relative mx-2 my-0.5 rounded-xl px-3 py-2.5 cursor-pointer transition flex flex-col gap-0.5 ${
                    conv.id === activeChatId
                      ? 'bg-bial-bg border-l-2 border-primary'
                      : 'hover:bg-surface-muted border-l-2 border-transparent'
                  }`}
                >
                  <p className={`text-xs font-semibold truncate pr-6 ${conv.id === activeChatId ? 'text-primary' : 'text-tertiary'}`}>
                    {conv.title}
                  </p>
                  <p className="text-[10px] text-neutral">{relativeTime(conv.updatedAt)}</p>
                  <button
                    onClick={(e) => handleDeleteChat(e, conv.id)}
                    disabled={conv.id === streamingChatId}
                    aria-label={`Delete ${conv.title || 'chat'}`}
                    title={
                      conv.id === streamingChatId
                        ? 'Finishing a reply — you can delete this chat once it completes'
                        : 'Delete chat'
                    }
                    className={`absolute right-2 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 transition p-1 ${
                      conv.id === streamingChatId
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
        </aside>

        {/* Chat area */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Chat toolbar: back-navigation + project name only (F10 — the AI branding block
              and its avatar are gone here too, matching the trimmed builder header). */}
          <div className="bg-white border-b border-bial-border px-5 py-3 flex items-center justify-between flex-shrink-0">
            <div className="min-w-0">
              <ProjectBreadcrumb projectId={projectId} projectName={projectName} />
            </div>
            {messages.length > 0 && (
              <button
                onClick={() => setShowBuildModal(true)}
                className="flex items-center gap-2 bg-secondary hover:bg-secondary-600 text-white text-xs font-bold px-4 py-2 rounded-xl transition shadow-sm shadow-secondary/30"
              >
                <Hammer size={12} />
                Build This App
              </button>
            )}
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto px-6 py-4 space-y-3 scrollbar-thin">
            {hydrating ? (
              <div className="h-full flex items-center justify-center">
                <div className="flex gap-1.5">
                  {[0, 1, 2].map((i) => (
                    <div key={i} className="w-2 h-2 bg-primary/60 rounded-full animate-bounce" style={{ animationDelay: `${i * 0.15}s` }} />
                  ))}
                </div>
              </div>
            ) : (
              messages.length === 0 && !streamingHere && (
                <div className="h-full flex flex-col items-center justify-center text-center pb-8">
                  <div className="w-16 h-16 rounded-2xl bg-primary/10 flex items-center justify-center mb-4">
                    <Sparkles size={28} className="text-primary" />
                  </div>
                  <h2 className="text-lg font-bold text-tertiary mb-2">Plan your next app</h2>
                  <p className="text-sm text-neutral max-w-sm leading-relaxed">
                    Describe what you need in plain English. I'll help you think it through before you build.
                  </p>
                </div>
              )
            )}

            {!hydrating && messages.map((msg) => (
              <div key={msg.id} className={`flex gap-2.5 ${msg.role === 'user' ? 'flex-row-reverse' : ''}`}>
                <div className={`w-6 h-6 rounded-full flex items-center justify-center flex-shrink-0 mt-0.5 ${
                  msg.role === 'assistant' ? 'bg-primary/10' : 'bg-secondary/10'
                }`}>
                  {msg.role === 'assistant'
                    ? <Sparkles size={10} className="text-primary" />
                    : <User size={10} className="text-secondary" />
                  }
                </div>
                <div className={`max-w-[75%] rounded-2xl px-4 py-3 text-sm leading-relaxed ${
                  msg.role === 'user'
                    ? 'bg-tertiary text-white rounded-tr-sm'
                    : 'bg-white border border-bial-border text-tertiary rounded-tl-sm'
                }`}>
                  <MessageContent parts={msg.parts} isUser={msg.role === 'user'} />
                  {msg.interrupted && (
                    // A stalled turn's partial reply, kept on screen (plan U7) — the marker copy
                    // mirrors StreamIncompleteError's; Regenerate replaces this bubble by id.
                    <p className="text-[10px] mt-1.5 font-semibold text-danger/80">
                      This reply was cut off before it finished.
                    </p>
                  )}
                  <p className="text-[10px] mt-1.5 opacity-40">
                    {msg.createdAt ? new Date(msg.createdAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}
                  </p>
                </div>
              </div>
            ))}

            {streamingHere && (
              <div className="flex gap-2.5 items-center">
                <div className="w-6 h-6 rounded-full bg-primary/10 flex items-center justify-center">
                  <Sparkles size={10} className="text-primary" />
                </div>
                <div className="bg-white border border-bial-border rounded-2xl px-4 py-3 flex gap-1">
                  {[0, 1, 2].map((i) => (
                    <div
                      key={i}
                      className="w-1.5 h-1.5 bg-primary rounded-full animate-bounce"
                      style={{ animationDelay: `${i * 0.15}s` }}
                    />
                  ))}
                </div>
              </div>
            )}

            <div ref={bottomRef} />
          </div>

          {/* Input bar */}
          <div className="bg-white border-t border-bial-border p-4 flex-shrink-0">
            <div className="max-w-3xl mx-auto">
              {error && (
                <div
                  role="alert"
                  aria-live="assertive"
                  className="mb-2 flex items-center justify-between gap-3 text-xs text-danger bg-danger/5 border border-danger/20 rounded-lg px-3 py-2"
                >
                  <span>{error}</span>
                  {/* User-initiated only — a regenerate is a second full bill (the first turn bills
                      server-side regardless), so it is framed as "try again", never an auto-retry. */}
                  {!generating && (
                    <button
                      onClick={handleRegenerate}
                      className="font-bold underline whitespace-nowrap"
                    >
                      Try again
                    </button>
                  )}
                </div>
              )}
              {/* Context-length guardrail: warn as it grows, hard-stop at the window */}
              {ctxLevel === 'full' ? (
                <div className="mb-2 flex items-center justify-between gap-3 text-xs text-danger bg-danger/5 border border-danger/20 rounded-lg px-3 py-2">
                  <span>This conversation has reached its maximum length. Start a new chat to keep going.</span>
                  <button onClick={handleNewChat} className="font-bold underline whitespace-nowrap">
                    Start new chat
                  </button>
                </div>
              ) : ctxLevel === 'warn' ? (
                <div className="mb-2 flex items-center justify-between gap-3 text-xs text-tertiary bg-warning/10 border border-warning/30 rounded-lg px-3 py-2">
                  <span>This conversation is getting long. For the best results, start a new chat.</span>
                  <button onClick={handleNewChat} className="font-bold text-primary underline whitespace-nowrap">
                    New chat
                  </button>
                </div>
              ) : null}
              {/* Pending attachment preview row */}
              {pendingAttachments.length > 0 && (
                <div className="flex flex-wrap gap-2 mb-2">
                  {pendingAttachments.map((a) => (
                    <div
                      key={a.id}
                      className="group relative flex items-center gap-1.5 bg-bial-bg border border-bial-border rounded-lg px-2 py-1.5 text-xs text-tertiary"
                    >
                      {TEXT_MEDIA_TYPES.has(a.mediaType) ? (
                        <span className="flex-shrink-0 text-primary" title={a.name}>
                          {a.mediaType === 'text/csv' ? <FileSpreadsheet size={13} /> : <FileText size={13} />}
                        </span>
                      ) : OFFICE_MEDIA_TYPES.has(a.mediaType) ? (
                        <span className="flex-shrink-0 text-primary" title={a.name}>
                          {officeFormat(a.mediaType) === 'excel' ? <FileSpreadsheet size={13} /> : <FileText size={13} />}
                        </span>
                      ) : DECK_MEDIA_TYPES.has(a.mediaType) ? (
                        <span className="flex-shrink-0 text-primary" title={a.name}>
                          <Presentation size={13} />
                        </span>
                      ) : a.mediaType === 'application/pdf' ? (
                        <button
                          type="button"
                          onClick={() => openPdf(a.base64, a.name)}
                          title={`Open ${a.name}`}
                          className="flex-shrink-0 text-primary hover:opacity-80 transition"
                        >
                          <FileText size={13} />
                        </button>
                      ) : (
                        <img
                          src={`data:${a.mediaType};base64,${a.base64}`}
                          alt={a.name}
                          title={`View ${a.name}`}
                          onClick={() => setViewer({ name: a.name, src: `data:${a.mediaType};base64,${a.base64}` })}
                          className="h-8 w-8 object-cover rounded cursor-zoom-in hover:opacity-90 transition"
                        />
                      )}
                      <span className="truncate max-w-[10rem]">{a.name}</span>
                      <button
                        onClick={() => removePending(a.id)}
                        className="text-neutral hover:text-danger transition"
                        title="Remove"
                      >
                        <X size={12} />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              <div className="flex gap-3 items-end">
                <input
                  ref={fileInputRef}
                  type="file"
                  accept={ACCEPT_ATTR}
                  multiple
                  onChange={handleFileSelect}
                  className="hidden"
                />
                {/* Attach stays live while a reply streams (U4): staging a file is composing, not
                    sending, and the attachment rides the next send — which is the very message the
                    user is composing. Aligned with BuilderPage so the same URL cannot offer two
                    different composer contracts. */}
                <button
                  onClick={() => fileInputRef.current?.click()}
                  title="Attach images, PDFs, Word, Excel, or text files (CSV, TXT)"
                  className="flex-shrink-0 w-11 h-11 bg-bial-bg hover:bg-surface-muted text-neutral hover:text-primary border border-bial-border rounded-xl flex items-center justify-center transition"
                >
                  <Paperclip size={15} />
                </button>
                <textarea
                  ref={inputRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      handleSend()
                    }
                  }}
                  rows={2}
                  placeholder="Describe what you're thinking… (Shift+Enter for new line)"
                  className="flex-1 resize-none text-sm text-tertiary bg-bial-bg border border-bial-border rounded-xl px-4 py-3 focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20 transition placeholder:text-gray-300"
                />
                {/* `aria-disabled`, never `disabled` — the same reason as BuilderPage (KTD-2): a
                    keyboard user standing on Send when a reply starts would otherwise be blurred to
                    `document.body`. `handleSend` above is the enforcement. */}
                <button
                  onClick={handleSend}
                  aria-disabled={(!input.trim() && pendingAttachments.length === 0) || streamingHere || ctxLevel === 'full'}
                  className={`flex-shrink-0 w-11 h-11 bg-secondary text-white rounded-xl flex items-center justify-center transition shadow-sm ${
                    (!input.trim() && pendingAttachments.length === 0) || streamingHere || ctxLevel === 'full'
                      ? 'opacity-40 cursor-default'
                      : 'hover:bg-secondary-600'
                  }`}
                >
                  <Send size={15} />
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Pending-attachment image lightbox */}
      {viewer && (
        <AttachmentLightbox name={viewer.name} src={viewer.src} onClose={() => setViewer(null)} />
      )}

      {/* Attachment validation / cap toast */}
      {attachToast && (
        <div className="fixed bottom-6 right-6 z-50 bg-white border border-bial-border rounded-xl shadow-xl px-4 py-3 text-sm text-tertiary font-medium max-w-xs">
          {attachToast}
        </div>
      )}

      {/* Build suggestion modal */}
      {showBuildModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-8 text-center animate-in">
            <div className="w-14 h-14 rounded-2xl bg-secondary/10 flex items-center justify-center mx-auto mb-5">
              <Hammer size={26} className="text-secondary" />
            </div>
            <h2 className="text-xl font-extrabold text-tertiary mb-2">Ready to build this app?</h2>
            <p className="text-sm text-neutral leading-relaxed mb-8">
              You've mapped out a solid plan. The AI will summarise your requirements into a builder prompt you can review before generating the app.
            </p>
            <div className="flex gap-3">
              <button
                onClick={() => setShowBuildModal(false)}
                className="flex-1 px-5 py-3 border border-bial-border text-sm font-bold text-neutral rounded-xl hover:bg-surface-muted transition"
              >
                Continue Planning
              </button>
              <button
                onClick={handleBuildApp}
                className="flex-1 px-5 py-3 bg-secondary hover:bg-secondary-600 text-white text-sm font-bold rounded-xl transition shadow-sm shadow-secondary/30 flex items-center justify-center gap-2"
              >
                Build This App <Sparkles size={13} />
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Builder prompt preview modal */}
      {showPromptModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl p-8 flex flex-col gap-5 animate-in">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl bg-secondary/10 flex items-center justify-center flex-shrink-0">
                <Sparkles size={20} className="text-secondary" />
              </div>
              <div>
                <h2 className="text-lg font-extrabold text-tertiary">Builder Prompt</h2>
                <p className="text-xs text-neutral">Review and edit before launching the builder</p>
              </div>
            </div>

            {summarizing ? (
              <div className="flex flex-col items-center justify-center py-10 gap-3">
                <div className="flex gap-1">
                  {[0, 1, 2].map((i) => (
                    <div
                      key={i}
                      className="w-2 h-2 bg-primary rounded-full animate-bounce"
                      style={{ animationDelay: `${i * 0.15}s` }}
                    />
                  ))}
                </div>
                <p className="text-sm text-neutral">Summarising your requirements…</p>
                {builderPrompt && (
                  <p className="text-xs text-neutral/60 max-w-md text-center leading-relaxed mt-1">{builderPrompt.slice(0, 120)}…</p>
                )}
              </div>
            ) : (
              <textarea
                value={builderPrompt}
                onChange={(e) => setBuilderPrompt(e.target.value)}
                rows={10}
                className="w-full resize-none text-sm text-tertiary bg-bial-bg border border-bial-border rounded-xl px-4 py-3 focus:outline-none focus:border-primary focus:ring-1 focus:ring-primary/20 transition"
              />
            )}

            <div className="flex gap-3">
              <button
                onClick={() => setShowPromptModal(false)}
                className="flex-1 px-5 py-3 border border-bial-border text-sm font-bold text-neutral rounded-xl hover:bg-surface-muted transition"
              >
                Back to Chat
              </button>
              <button
                onClick={() => void handleLaunchBuilder()}
                disabled={summarizing || !builderPrompt.trim()}
                className="flex-1 px-5 py-3 bg-secondary hover:bg-secondary-600 disabled:opacity-40 text-white text-sm font-bold rounded-xl transition shadow-sm shadow-secondary/30 flex items-center justify-center gap-2"
              >
                Launch Builder <Hammer size={13} />
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
