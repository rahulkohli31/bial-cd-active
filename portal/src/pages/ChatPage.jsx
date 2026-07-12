import { useState, useRef, useEffect, useCallback } from 'react'
import { useNavigate, useParams, useLocation } from 'react-router-dom'
import { Sparkles, User, Send, Plus, MessageSquare, Trash2, Hammer, Paperclip, X, FileText, FileSpreadsheet, Presentation } from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import MessageContent from '../components/chat/MessageContent'
import AttachmentLightbox from '../components/AttachmentLightbox'
import ProjectBreadcrumb from '../components/projects/ProjectBreadcrumb'
import { listProjectConversations } from '../utils/conversationApi'
import { useClaudeAPI, getContextLimits, estimateConversationTokens } from '../hooks/useClaudeAPI'
import { usePendingAttachments } from '../hooks/usePendingAttachments'
import {
  loadHistory,
  newConversation,
  appendMessage,
  getConversation,
  deleteConversation,
  relativeTime,
  deriveTitle,
} from '../utils/chatHistory'
import { assembleApiMessages, buildUserParts, partsToText, countAttachments, releaseUploadedAttachments } from '../utils/attachmentStore'
import { ACCEPT_ATTR, validateConversationAttachmentCap, TEXT_MEDIA_TYPES, OFFICE_MEDIA_TYPES, DECK_MEDIA_TYPES, officeFormat } from '../utils/attachmentInput'
import { openPdf } from '../utils/attachmentViewer'
import { describeSaveFailure, isConversationGone } from '../utils/chatErrors'
import { useDropTransientQuery } from '../hooks/useDropTransientQuery'

const PLANNING_SYSTEM_PROMPT = `You are Citizen Developer AI, a planning assistant for the Bengaluru International Airport (BIAL) Citizen Developer Portal, powered by Anthropic Claude.

Your PRIMARY role is to help airport staff plan and define their app requirements through conversation — NOT to generate code yet.

Guidelines:
- Ask clarifying questions to understand the user's operational need
- Help them articulate what their app should do, who will use it, and what data it needs
- Suggest features based on airport operations context (flight tracking, staff rostering, baggage, gate management, etc.)
- Keep responses concise and practical — staff are busy
- If the user attaches images (screenshots, mockups, photos), PDFs (specs, sample data), or Word/Excel documents (requirements, sample datasets — provided to you as extracted text and tables), examine them and use what they actually show to inform the plan — you can see attachments, so refer to their real content
- When you feel the requirements are well-defined, summarise the plan and suggest moving to the builder
- For general questions unrelated to app planning, answer them helpfully and concisely, then gently guide the conversation back to planning if appropriate

Do not output code or JSX during the planning phase.`

const SUMMARIZE_SYSTEM_PROMPT = `You are a requirements extraction specialist. Given a planning conversation between a user and an AI assistant, extract ONLY the application requirements discussed and output a clean, structured builder prompt. Discard any off-topic discussion, general knowledge questions, or chitchat unrelated to the application being planned. Output a direct, actionable prompt starting with "Build an application for Bengaluru International Airport (BIAL) that..." — include the app's purpose, key features, target users, data needs, and any UI or workflow preferences mentioned. Be specific and concise.`

/**
 * The planning chat, rendered by ChatRoute at the flat `/chat/:chatId`.
 *
 * `chatId` / `projectId` / `projectName` arrive as props from ChatRoute, which has
 * already resolved the conversation's kind and its project. The `useParams` fallback
 * keeps the page renderable on its own (and keeps its tests honest about the route).
 *
 * @param {{chatId?: string, projectId?: string | null, projectName?: string | null}} [props]
 */
export default function ChatPage({ chatId: chatIdProp, projectId = null, projectName = null } = {}) {
  const navigate = useNavigate()
  const params = useParams()
  const chatId = chatIdProp ?? params.chatId
  const location = useLocation()

  const [history, setHistory] = useState([])
  const [activeChatId, setActiveChatId] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [generating, setGenerating] = useState(false)
  // The id of the chat whose turn is in flight — tracked separately from `generating`, which is
  // a page-global flag, so the delete gate follows the actual STREAM, not the current view. This
  // keeps the streaming chat's delete disabled (and every OTHER chat's enabled) even after a
  // mid-stream navigate to a sibling chat. Cleared on both send-exit paths.
  const [streamingChatId, setStreamingChatId] = useState(null)
  const [hydrating, setHydrating] = useState(false) // loading a saved transcript over the network
  const [showBuildModal, setShowBuildModal] = useState(false)
  const [showPromptModal, setShowPromptModal] = useState(false)
  const [builderPrompt, setBuilderPrompt] = useState('')
  const [summarizing, setSummarizing] = useState(false)
  const [viewer, setViewer] = useState(null) // { name, src } for the pending-attachment lightbox
  const buildSuggestionFiredRef = useRef(false)
  // Source of truth for "which conversation is active", kept in lockstep with
  // activeChatId via setActive. The streaming send path guards every assistant
  // write against this ref so a turn never lands on the wrong (or a deleted)
  // conversation after a mid-stream navigate/delete.
  const activeChatIdRef = useRef(null)

  const dropTransientQuery = useDropTransientQuery()

  const { sendMessage, error } = useClaudeAPI()
  const { pendingAttachments, handleFileSelect, removePending, clearPending, attachToast, showAttachToast } =
    usePendingAttachments()
  const bottomRef = useRef(null)
  const inputRef = useRef(null)
  const fileInputRef = useRef(null)
  const messagesRef = useRef(messages)
  messagesRef.current = messages

  // Running context-length estimate → 'ok' | 'warn' | 'full'. Drives the
  // guardrail banner + send-disable below. Recomputed each render (cheap).
  const ctxTokens = estimateConversationTokens(messages, PLANNING_SYSTEM_PROMPT)
  const { soft: ctxSoft, hard: ctxHard } = getContextLimits()
  const ctxLevel = ctxTokens >= ctxHard ? 'full' : ctxTokens >= ctxSoft ? 'warn' : 'ok'

  // Set the active conversation id in state AND the ref together, so the
  // streaming guard can read the current id synchronously.
  const setActive = useCallback((id) => {
    activeChatIdRef.current = id
    setActiveChatId(id)
  }, [])

  // The sidebar lists this PROJECT's planning chats, not every planning chat the user
  // owns — a chat belongs to its project, and cross-project recents would re-introduce
  // the flat all-chats model this phase replaces. Falls back to the flat list only when
  // the project is not yet known (a chat rendered without ChatRoute).
  const refreshHistory = useCallback(async () => {
    try {
      const list = projectId
        ? (await listProjectConversations(projectId)).filter((c) => c.kind === 'planning')
        : await loadHistory()
      setHistory(list.sort((a, b) => new Date(b.updatedAt) - new Date(a.updatedAt)))
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

    let alive = true
    setHydrating(true)
    getConversation(chatId)
      .then((conv) => {
        if (!alive || activeChatIdRef.current !== chatId) return
        setMessages(conv ? conv.messages : [])
        buildSuggestionFiredRef.current = false
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

  // Fire initial message once activeChatId is set from 'new' flow
  useEffect(() => {
    if (!activeChatId) return
    const initialMessage = location.state?.initialMessage
    if (initialMessage && messages.length === 0) {
      fireMessage(initialMessage, [], activeChatId)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeChatId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, generating])

  const fireMessage = useCallback(async (rawText, attachments = [], explicitChatId) => {
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
      showAttachToast(err?.message || 'Could not upload the attachment.')
      return
    }

    const userMsg = { id: `local_${Date.now()}`, role: 'user', parts, seq: baseSeq, createdAt: new Date().toISOString() }
    setMessages((prev) => [...prev, userMsg])
    setGenerating(true)
    setStreamingChatId(currentChatId) // gate THIS chat's delete for the turn's lifetime

    // Persist the user turn BEFORE streaming. The single route call upserts the header
    // AND inserts the message, so the conversation exists when `POST /v1/claude` looks it
    // up to fold in the project's description — which is why the FIRST turn of a new chat
    // gets project context at all. (The backend's own comment assumes persist-after; do
    // not "correct" this to match it.) `projectId` is required on the create branch and
    // ignored on the upsert branch, so pass it every turn. On failure, abort the send and
    // roll back the optimistic bubble — no orphan assistant turn.
    try {
      await appendMessage(
        currentChatId,
        { role: 'user', parts, seq: baseSeq },
        isFirstTurn ? { title: deriveTitle(partsToText(parts)), projectId } : { projectId },
      )
    } catch (err) {
      // The uploads succeeded but the turn never landed — release them so the
      // deck's Files-API PDF + stored bytes don't orphan (best-effort, non-masking).
      releaseUploadedAttachments(parts)
      setMessages((prev) => prev.filter((m) => m.id !== userMsg.id))
      setGenerating(false)
      setStreamingChatId(null)
      showAttachToast(describeSaveFailure(err))
      if (isConversationGone(err)) navigate('/projects', { replace: true })
      return
    }
    dropTransientQuery(currentChatId)
    refreshHistory()

    // Assemble the API messages from in-memory bytes: only the newest turn's
    // image/PDF bytes are inflated (from the composer), historical binaries dropped.
    const byteMap = new Map(attachments.map((a) => [a.id, a.base64]))
    const apiMessages = assembleApiMessages([...priorMessages, userMsg], (id) => byteMap.get(id))

    const assistantId = `local_${Date.now()}_a`
    let assistantText = ''

    setMessages((prev) => [...prev, {
      id: assistantId,
      role: 'assistant',
      parts: [{ type: 'text', text: '' }],
      seq: baseSeq + 1,
      createdAt: new Date().toISOString(),
    }])

    const result = await sendMessage(
      apiMessages,
      (delta) => {
        // Ignore deltas if the user navigated to a different conversation mid-stream.
        if (activeChatIdRef.current !== currentChatId) return
        assistantText += delta
        setMessages((prev) =>
          prev.map((m) => m.id === assistantId ? { ...m, parts: [{ type: 'text', text: assistantText }] } : m)
        )
      },
      { systemPrompt: PLANNING_SYSTEM_PROMPT },
      currentChatId, // the server folds in this project's description
    )

    setGenerating(false)
    setStreamingChatId(null)

    // A falsy result means the send failed (429/network), was aborted, OR
    // streamed zero text. Drop the optimistic empty assistant bubble so nothing
    // blank is shown or persisted. Any error message surfaces via the `error` banner.
    if (!result) {
      setMessages((prev) => prev.filter((m) => m.id !== assistantId))
      return
    }

    // Persist the assistant turn — but NO-OP if the user navigated away or deleted
    // the conversation mid-stream (guard on the active id), so an in-flight stream
    // can never resurrect a deleted conversation or write onto the wrong one.
    if (activeChatIdRef.current === currentChatId) {
      try {
        await appendMessage(currentChatId, { role: 'assistant', parts: [{ type: 'text', text: assistantText }], seq: baseSeq + 1 }, {})
        refreshHistory()
      } catch {
        showAttachToast('Your reply could not be saved.')
      }
    }

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
  }, [activeChatId, generating, sendMessage, refreshHistory, showAttachToast, projectId, navigate, dropTransientQuery])

  const handleSend = () => {
    const text = input.trim()
    const attachments = pendingAttachments
    if (!text && attachments.length === 0) return

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

    // Pass the route's chat id explicitly. Under flat routing it is known from the
    // first render, while `activeChatId` only commits once hydration resolves — a send
    // fired in that window would otherwise hit fireMessage's `if (!currentChatId) return`
    // and vanish without a trace.
    fireMessage(text, attachments, chatId)
  }

  const handleSelectChat = (id) => {
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
    navigate(`/chat/${newConversation()}?projectId=${encodeURIComponent(projectId)}&kind=planning`)
  }

  const handleDeleteChat = async (e, id) => {
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

    const transcript = messages
      .map((m) => `${m.role === 'user' ? 'User' : 'Assistant'}: ${partsToText(m.parts)}`)
      .join('\n\n')

    let accumulated = ''
    // A one-off summarization, not a persisted turn — but `conversationId` is still
    // required by the endpoint. Naming this chat is also the right answer: the summary
    // is drawn from its transcript, and the project's description grounds it.
    await sendMessage(
      [{ role: 'user', content: `Here is a planning conversation. Extract the app requirements and write a builder prompt:\n\n${transcript}` }],
      (delta) => {
        accumulated += delta
        setBuilderPrompt(accumulated)
      },
      { systemPrompt: SUMMARIZE_SYSTEM_PROMPT },
      activeChatId,
    )

    setSummarizing(false)
  }, [messages, sendMessage, activeChatId])

  // The planning chat already knows its project, so no project gate here: the build
  // chat is filed alongside this one. (ProjectBuilder builds the same handoff payload
  // independently — keep the two in step.)
  const handleLaunchBuilder = useCallback(() => {
    setShowPromptModal(false)
    if (!projectId) {
      navigate('/projects')
      return
    }
    navigate(`/chat/${newConversation()}?projectId=${encodeURIComponent(projectId)}&kind=builder`, {
      state: { prompt: builderPrompt, theme: 'bial', uploadedFiles: [] },
    })
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
          {/* Chat toolbar */}
          <div className="bg-white border-b border-bial-border px-5 py-3 flex items-center justify-between flex-shrink-0">
            <div className="flex items-center gap-3">
              <div className="relative">
                <div className="w-8 h-8 rounded-full bg-primary flex items-center justify-center">
                  <Sparkles size={14} className="text-white" />
                </div>
                <span className="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 bg-green-400 rounded-full border-2 border-white" />
              </div>
              <div className="min-w-0">
                <ProjectBreadcrumb projectId={projectId} projectName={projectName} />
                <p className="text-sm font-bold text-tertiary">Citizen Developer AI</p>
                <p className="text-[10px] text-neutral">Planning mode · powered by Anthropic</p>
              </div>
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
              messages.length === 0 && !generating && (
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
                  <p className="text-[10px] mt-1.5 opacity-40">
                    {msg.createdAt ? new Date(msg.createdAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}
                  </p>
                </div>
              </div>
            ))}

            {generating && (
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
                <div className="mb-2 text-xs text-danger bg-danger/5 border border-danger/20 rounded-lg px-3 py-2">
                  {error}
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
                <button
                  onClick={() => fileInputRef.current?.click()}
                  disabled={generating}
                  title="Attach images, PDFs, Word, Excel, or text files (CSV, TXT)"
                  className="flex-shrink-0 w-11 h-11 bg-bial-bg hover:bg-surface-muted disabled:opacity-40 text-neutral hover:text-primary border border-bial-border rounded-xl flex items-center justify-center transition"
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
                <button
                  onClick={handleSend}
                  disabled={(!input.trim() && pendingAttachments.length === 0) || generating || ctxLevel === 'full'}
                  className="flex-shrink-0 w-11 h-11 bg-secondary hover:bg-secondary-600 disabled:opacity-40 text-white rounded-xl flex items-center justify-center transition shadow-sm"
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
                onClick={handleLaunchBuilder}
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
