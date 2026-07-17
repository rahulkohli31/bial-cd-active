import { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import { useNavigate, useParams, useLocation } from 'react-router-dom'
import { AssistantRuntimeProvider, useLocalRuntime, useThread, useComposerRuntime } from '@assistant-ui/react'
import { Sparkles, Plus, MessageSquare, Trash2, Hammer } from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import { Thread, AssistantActionBarNoRegenerate } from '../components/thread'
import ProjectBreadcrumb from '../components/projects/ProjectBreadcrumb'
import { listProjectConversations } from '../utils/conversationApi'
import { useClaudeAPI, getContextLimits, estimateConversationTokens } from '../hooks/useClaudeAPI'
import {
  loadHistory,
  newConversation,
  appendMessage,
  getConversation,
  deleteConversation,
  relativeTime,
  deriveTitle,
} from '../utils/chatHistory'
import { partsToText } from '../utils/attachmentStore'
import { useDropTransientQuery } from '../hooks/useDropTransientQuery'
import { createClaudeChatModelAdapter } from '../utils/assistantUiAdapter'
import { clearSession, SIGNOUT_REASONS } from '../utils/auth'

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

// Bridges the this app's own `{role, parts}` persistence shape (chatHistory.js,
// appendMessage/getConversation) to assistant-ui's ThreadMessageLike, used only
// to SEED useLocalRuntime's initialMessages — read once at construction.
function legacyMessagesToThreadMessageLike(msgs) {
  return msgs.map((m) => ({
    id: m.id,
    role: m.role,
    content: partsToText(m.parts),
    createdAt: m.createdAt ? new Date(m.createdAt) : undefined,
  }))
}

// The reverse direction: assistant-ui's live ThreadMessage[] back to this app's
// `{role, parts}` shape, so the existing ctxLevel guardrail / build-suggestion
// transcript / "has any messages" checks can keep operating on familiar data
// without caring that Thread now owns the actual message list.
function threadMessagesToLegacy(msgs) {
  return msgs.map((m) => ({
    role: m.role,
    parts: (m.content || [])
      .filter((p) => p.type === 'text')
      .map((p) => ({ type: 'text', text: p.text })),
  }))
}

// Renders nothing — just relays assistant-ui's reactive thread state back up
// to ChatPage's plain React state via a subscription hook, since ChatPage's
// sidebar/toolbar/modals live outside <Thread> and can't call useThread directly.
function ThreadStateBridge({ onMessagesChange }) {
  const messages = useThread((s) => s.messages)
  useEffect(() => {
    onMessagesChange(messages)
  }, [messages, onMessagesChange])
  return null
}

// Replicates the old "fire the message that arrived via location.state on a
// brand-new chat" behavior, now via the composer runtime instead of a direct
// fireMessage() call. Only ever acts once per mount (i.e. once per chat).
function InitialMessageSender({ initialMessage }) {
  const composerRuntime = useComposerRuntime()
  const firedRef = useRef(false)
  useEffect(() => {
    if (firedRef.current) return
    firedRef.current = true
    composerRuntime.setText(initialMessage)
    composerRuntime.send()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return null
}

// Phase 1: the adapter can't support editing a past user turn or regenerating
// an assistant reply (see assistantUiAdapter.js's persistedUserIds comment) —
// both re-invoke run() with a truncated messages array, and the adapter
// recomputes `seq` from that array's length, colliding with the turn's
// already-persisted seq instead of superseding it. Hidden via Thread's own
// components override rather than left clickable-but-broken.
const NoUserActionBar = () => null

// Mounted only once hydration has resolved (see ChatPage), so useLocalRuntime's
// initialMessages is always seeded with the real transcript on its one-and-only
// construction for this chat — never with a still-loading empty array.
function ChatRuntimeArea({ initialMessages, adapterOptions, onLiveMessagesChange, initialMessageToFire, sendBlocked }) {
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const adapter = useMemo(() => createClaudeChatModelAdapter(adapterOptions), [])
  const runtime = useLocalRuntime(adapter, { initialMessages })
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadStateBridge onMessagesChange={onLiveMessagesChange} />
      {initialMessageToFire && <InitialMessageSender initialMessage={initialMessageToFire} />}
      <Thread
        composerDisabled={sendBlocked}
        components={{ AssistantActionBar: AssistantActionBarNoRegenerate, UserActionBar: NoUserActionBar }}
      />
    </AssistantRuntimeProvider>
  )
}

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
  // The hydrated transcript, in this app's legacy {role, parts} shape. Frozen per
  // chat — only written by the hydration effect — and used purely to SEED
  // ChatRuntimeArea's initialMessages once hydration resolves.
  const [hydratedMessages, setHydratedMessages] = useState([])
  // The live transcript, kept in sync with the actual Thread runtime via
  // ThreadStateBridge. Drives the ctxLevel guardrail, the "has any messages"
  // check, and the build-suggestion transcript — everything that needs to see
  // messages as they're actually sent/streamed, not just the hydrated seed.
  const [liveMessages, setLiveMessages] = useState([])
  const [chatError, setChatError] = useState(null)
  // The id of the chat whose turn is in flight — tracked separately from React
  // render state via adapter onRunStart/onRunEnd callbacks, so the delete gate
  // follows the actual network request, not just Thread's broader isRunning
  // window. Cleared on every exit path (success, error, or cancel).
  const [streamingChatId, setStreamingChatId] = useState(null)
  // The chatId whose hydration has FULLY resolved — not a lagging boolean, because a
  // boolean initialized to false would let ChatRuntimeArea (and its InitialMessageSender)
  // mount for one render before the hydration effect below ever runs, firing an initial
  // message send prematurely; when the effect then legitimately flips loading on, that
  // subtree unmounts mid-flight, and remounts (and re-fires the send) once hydration
  // resolves — a real double-send, not a hypothetical. Gating on "is this render's chatId
  // the one we've fully hydrated" is correct on every render, including the very first.
  const [readyForChatId, setReadyForChatId] = useState(null)
  const hydrating = readyForChatId !== chatId
  const [showBuildModal, setShowBuildModal] = useState(false)
  const [showPromptModal, setShowPromptModal] = useState(false)
  const [builderPrompt, setBuilderPrompt] = useState('')
  const [summarizing, setSummarizing] = useState(false)
  const buildSuggestionFiredRef = useRef(false)
  // Source of truth for "which conversation is active", kept in lockstep with
  // activeChatId via setActive. The adapter's getChatId/isConversationStillActive
  // read this ref directly so a turn never lands on the wrong (or a deleted)
  // conversation after a mid-stream navigate/delete.
  const activeChatIdRef = useRef(null)
  const projectIdRef = useRef(projectId)
  projectIdRef.current = projectId
  const liveMessagesRef = useRef(liveMessages)
  liveMessagesRef.current = liveMessages

  const dropTransientQuery = useDropTransientQuery()

  const { sendMessage, error } = useClaudeAPI()

  // Running context-length estimate → 'ok' | 'warn' | 'full'. Drives the
  // guardrail banner below. Recomputed each render (cheap) from the LIVE transcript.
  const ctxTokens = estimateConversationTokens(liveMessages, PLANNING_SYSTEM_PROMPT)
  const { soft: ctxSoft, hard: ctxHard } = getContextLimits()
  const ctxLevel = ctxTokens >= ctxHard ? 'full' : ctxTokens >= ctxSoft ? 'warn' : 'ok'

  // Set the active conversation id in state AND the ref together, so the
  // adapter's callbacks can read the current id synchronously.
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
    setHydratedMessages([])
    setLiveMessages([])
    setChatError(null)

    // Guard every continuation on the REF, not a per-invocation `alive` closure flag:
    // under StrictMode's dev-only mount→cleanup→mount simulation, the ref (set
    // synchronously above, and shared across both invocations of this effect) still
    // correctly reflects "is this chat still the active one" — an `alive` flag captured
    // per-closure would be flipped false by the simulated cleanup before this fetch
    // ever resolves, permanently starving the real second invocation (which itself
    // no-ops via the guard at the top of this effect, since the ref is already set).
    getConversation(chatId)
      .then((conv) => {
        if (activeChatIdRef.current !== chatId) return
        const msgs = conv ? conv.messages : []
        setHydratedMessages(msgs)
        setLiveMessages(msgs)
        buildSuggestionFiredRef.current = false
      })
      .catch(() => {
        // A real load failure (401 is handled by the auth gate + refresh, 403-suspended
        // by the interceptor) — back to the projects index rather than crash the shell.
        if (activeChatIdRef.current === chatId) navigate('/projects', { replace: true })
      })
      .finally(() => {
        if (activeChatIdRef.current === chatId) setReadyForChatId(chatId)
      })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatId])

  const ctxLevelFull = useCallback(() => {
    const tokens = estimateConversationTokens(liveMessagesRef.current, PLANNING_SYSTEM_PROMPT)
    return tokens >= getContextLimits().hard
  }, [])

  const onRunStart = useCallback((id) => {
    setChatError(null)
    setStreamingChatId(id)
  }, [])

  const onRunEnd = useCallback((id) => {
    setStreamingChatId((prev) => (prev === id ? null : prev))
  }, [])

  const onAssistantTurnComplete = useCallback((_finalText, shouldSuggestBuild) => {
    if (shouldSuggestBuild && !buildSuggestionFiredRef.current) {
      buildSuggestionFiredRef.current = true
      setTimeout(() => setShowBuildModal(true), 600)
    }
  }, [])

  const onAuthFailed = useCallback(() => {
    clearSession(SIGNOUT_REASONS.EXPIRED)
    navigate('/login')
  }, [navigate])

  const onConversationGone = useCallback(() => {
    navigate('/projects', { replace: true })
  }, [navigate])

  const isConversationStillActive = useCallback((id) => activeChatIdRef.current === id, [])

  // The next unused persisted seq, derived from the HYDRATED transcript's real
  // max (not the live, possibly phantom-inflated assistant-ui array) — see
  // assistantUiAdapter.js's nextSeq counter (PR #35 comment 1).
  const initialNextSeq = useMemo(
    () => hydratedMessages.reduce((max, m) => Math.max(max, m.seq), -1) + 1,
    [hydratedMessages],
  )

  const adapterOptions = useMemo(
    () => ({
      systemPrompt: PLANNING_SYSTEM_PROMPT,
      getChatId: () => activeChatIdRef.current,
      getProjectId: () => projectIdRef.current,
      appendMessage,
      deriveTitle,
      onAuthFailed,
      isConversationStillActive,
      onError: (message) => setChatError(message),
      onConversationGone,
      ctxLevelFull,
      dropTransientQuery,
      refreshHistory,
      onAssistantTurnComplete,
      onRunStart,
      onRunEnd,
      initialNextSeq,
    }),
    [onAuthFailed, isConversationStillActive, onConversationGone, ctxLevelFull, dropTransientQuery, refreshHistory, onAssistantTurnComplete, onRunStart, onRunEnd, initialNextSeq],
  )

  const initialMessages = useMemo(
    () => legacyMessagesToThreadMessageLike(hydratedMessages),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [chatId, hydratedMessages],
  )

  const handleLiveMessagesChange = useCallback((msgs) => {
    setLiveMessages(threadMessagesToLegacy(msgs))
  }, [])

  const initialMessageToFire =
    hydratedMessages.length === 0 ? location.state?.initialMessage : undefined

  const handleSelectChat = (id) => {
    navigate(`/chat/${id}`)
    buildSuggestionFiredRef.current = false
  }

  // A new chat is always filed under THIS chat's project — there is no Default project
  // and no project-less chat. The id is minted client-side; the row appears on the
  // first append, so the project rides a transient query until then.
  const handleNewChat = () => {
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

    const transcript = liveMessages
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
  }, [liveMessages, sendMessage, activeChatId])

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

  const hasMessages = liveMessages.length > 0

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
            {hasMessages && (
              <button
                onClick={() => setShowBuildModal(true)}
                className="flex items-center gap-2 bg-secondary hover:bg-secondary-600 text-white text-xs font-bold px-4 py-2 rounded-xl transition shadow-sm shadow-secondary/30"
              >
                <Hammer size={12} />
                Build This App
              </button>
            )}
          </div>

          {/* Guardrail / error banners — kept as siblings of Thread, above the composer */}
          <div className="px-6 pt-3 flex-shrink-0">
            {(chatError || error) && (
              <div className="mb-2 text-xs text-danger bg-danger/5 border border-danger/20 rounded-lg px-3 py-2">
                {chatError || error}
              </div>
            )}
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
          </div>

          {/* Thread (messages + composer) */}
          <div className="flex-1 overflow-hidden">
            {hydrating ? (
              <div className="h-full flex items-center justify-center">
                <div className="flex gap-1.5">
                  {[0, 1, 2].map((i) => (
                    <div key={i} className="w-2 h-2 bg-primary/60 rounded-full animate-bounce" style={{ animationDelay: `${i * 0.15}s` }} />
                  ))}
                </div>
              </div>
            ) : (
              <ChatRuntimeArea
                key={chatId}
                initialMessages={initialMessages}
                adapterOptions={adapterOptions}
                onLiveMessagesChange={handleLiveMessagesChange}
                initialMessageToFire={initialMessageToFire}
                sendBlocked={ctxLevel === 'full'}
              />
            )}
          </div>
        </div>
      </div>

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
