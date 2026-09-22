/**
 * BIAL CHAT — a conversation that belongs to the citizen rather than to a project.
 *
 * ONE COMPONENT SERVES BOTH ADDRESSES, and that is a decision rather than a convenience.
 * `/assistant` and `/assistant/:chatId` render this same element, which resolves to one of four
 * states: no conversation yet, restoring one, showing one, or telling the citizen it is gone. A
 * sibling route component for the addressed state would unmount this one on the first send —
 * remounting `Backdrop` and restarting its animation underneath a reply that had just begun. One
 * component means the backdrop never unmounts and the greeting gives way beneath it.
 *
 * NO SECOND CHAT STACK. The runtime provider, the thread and the composer are the builder's own;
 * what this page adds is the small part a generic chat needs and a build chat does not — no app
 * pane, no workspace sentence, no plan offer, and a transcript column bounded and centred because
 * this surface declares no second pane.
 *
 * "HAS RESOLVED" IS ITS OWN BIT, AND IT IS NOT "WHICH ADDRESS IS CURRENT". A transcript load that
 * fails for any reason short of a confirmed, owner-scoped absence keeps the citizen at their
 * address with a retry. It must never fall back to the greeting, and it must never redirect to a
 * list — the builder chat's answer to a failed load is a redirect, and inheriting it here would
 * tell someone their conversation was destroyed because their connection blinked.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { motion } from 'motion/react'

import Backdrop from '../components/assistant/Backdrop'
import {
  firstName,
  headlineParts,
  lastGreeting,
  pickGreeting,
  rememberGreeting,
} from '../components/assistant/greetings'
import Announcer from '../components/chat/Announcer'
import ChatThread from '../components/chat/ChatThread'
import Composer from '../components/chat/Composer'
import ChatRuntimeProvider from '../components/chat/runtime/ChatRuntimeProvider'
import TurnBanner from '../components/chat/TurnBanner'
import { DURATION, LAYOUT_EASE } from '../lib/motion'
import { getStoredUser } from '../utils/auth'
import { buildUserParts } from '../utils/attachmentStore'
import {
  createConversation,
  getConversation,
  uuidv7,
  type ConversationWithMessages,
} from '../utils/conversationApi'
import { contextState } from '../utils/contextLimits'
import type { ComposerSubmission } from '../components/chat/Composer'
import type { ChatMessage, MessagePart } from '../utils/messageTypes'
import {
  isKnownFrame,
  readTurnStream,
  startTurn,
  stopTurn,
  TurnStartError,
  type TurnFrame,
} from '../utils/turnStreamApi'

export const GENERIC_KIND = 'generic'

/**
 * What this surface is resolved to, and the states are not interchangeable.
 *
 * `failed` and `gone` are the pair the prior kind expansion got wrong: a load that did not
 * resolve is not the same fact as a conversation that is not there. `failed` keeps the address
 * and offers a retry; `gone` says so and offers the one way forward.
 */
type Phase = 'greeting' | 'loading' | 'ready' | 'failed' | 'gone'

const GONE_TEXT = 'This conversation is no longer here.'
const LOAD_FAILED_TEXT = 'This conversation could not be loaded.'
const SENDING_BLOCKED_WHILE_LOADING = 'Loading this conversation…'
const SENDING_BLOCKED_AFTER_FAILURE = 'Load this conversation before sending.'

const ANNOUNCE = {
  started: 'Reply started.',
  finished: 'Reply finished.',
  turnFailed: 'The reply failed. A retry is available.',
  loadFailed: 'This conversation could not be loaded. A retry is available.',
} as const

/** An assistant message built live from the stream — one per reply, matching the reload shape. */
function replyMessage(id: string, seq: number, text: string): ChatMessage {
  const parts: MessagePart[] = [{ type: 'text', text }]
  return { id, role: 'assistant', parts, seq }
}

export default function AssistantPage() {
  const { chatId: routedId } = useParams()
  const navigate = useNavigate()

  // MINTED ONCE PER GREETING-SCREEN MOUNT, not once per press, and reused across retries.
  // Creation is idempotent only per client-minted id, so a double press or an Enter repeat on the
  // very first message would otherwise create two conversations — and the second is unreachable,
  // because there is no list to find it in.
  const [mintedId, setMintedId] = useState(() => uuidv7())
  const chatId = routedId ?? mintedId

  const [phase, setPhase] = useState<Phase>(routedId ? 'loading' : 'greeting')
  const [messages, setMessages] = useState<readonly ChatMessage[]>([])
  const [isRunning, setIsRunning] = useState(false)
  const [turnId, setTurnId] = useState<string | null>(null)
  const [banner, setBanner] = useState<string | null>(null)
  const [urgent, setUrgent] = useState<string | null>(null)
  const [announcement, setAnnouncement] = useState<string | null>(null)
  const [contextTokens, setContextTokens] = useState<number | null>(null)
  const [lastSend, setLastSend] = useState<{
    text: string
    attachmentIds: string[]
  } | null>(null)

  // THE CONVERSATION THIS PAGE HAS ALREADY RESOLVED, which is not the same thing as the one in
  // the address. The first send takes an address for a conversation this page is already holding
  // — and already streaming a reply into — so a load keyed on the address alone would fire the
  // instant the URL changed, overwrite the transcript with the server's empty one, and drop the
  // message in flight. The builder route answers the same hazard with a router-state marker;
  // this is that answer, held where it cannot be forged from a link.
  const resolvedIdRef = useRef<string | null>(null)
  const aliveRef = useRef(true)
  const abortRef = useRef<AbortController | null>(null)
  const transcriptRef = useRef<HTMLDivElement | null>(null)
  const retryRef = useRef<HTMLButtonElement | null>(null)
  const seqRef = useRef(0)

  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
      abortRef.current?.abort()
    }
  }, [])

  // PICKED ONCE, ON MOUNT, AND `useState` RATHER THAN `useMemo` IS THE WHOLE POINT. A greeting
  // chosen during render would change under any re-render — a keystroke in the composer is
  // enough — and a heading that rewrites itself while someone is typing under it reads as a
  // glitch. `useMemo` is a performance hint React is allowed to discard and recompute; a state
  // initialiser is the only hook that promises to run exactly once for the life of the mount.
  const [greeting] = useState(() => {
    const name = firstName(getStoredUser()?.display_name)
    return {
      name,
      ...pickGreeting({
        hour: new Date().getHours(),
        name,
        exclude: lastGreeting(),
      }),
    }
  })

  // Remembered after the pick rather than inside it, so choosing a greeting stays a pure function
  // of its arguments and the one thing here that can throw sits on its own.
  useEffect(() => rememberGreeting(greeting.headline), [greeting])

  const applyLoaded = useCallback(
    (loaded: ConversationWithMessages) => {
      // A CHAT THAT BELONGS TO A PROJECT DOES NOT BELONG HERE. This address is mounted for any
      // conversation id, so a pasted or bookmarked link can land a plan or build chat on a
      // surface with no app pane, no workspace sentence and no breadcrumb. The builder route
      // sends a generic chat here for the same reason, and both replace history so Back does not
      // bounce between the two.
      //
      // ON THE SERVER-RESOLVED KIND, never on anything in the URL: the address carries no kind,
      // and a query that did would be the caller's to forge.
      if (loaded.kind !== GENERIC_KIND) {
        navigate(`/chat/${loaded.id}`, { replace: true })
        return
      }
      resolvedIdRef.current = loaded.id
      setMessages(loaded.messages)
      setContextTokens(loaded.contextTokens)
      seqRef.current = loaded.messages.length
      setPhase('ready')
    },
    [navigate],
  )

  const load = useCallback(
    async (id: string) => {
      setPhase('loading')
      setBanner(null)
      try {
        const loaded = await getConversation(id)
        if (!aliveRef.current) return
        // `null` IS THE CONFIRMED ABSENCE and nothing else is. `getConversation` answers a
        // 404 — which is also how a cross-user id resolves — with null, and throws for every
        // other failure. Those two arms are the whole distinction this surface rests on.
        if (loaded === null) {
          setPhase('gone')
          return
        }
        applyLoaded(loaded)
      } catch {
        if (!aliveRef.current) return
        setPhase('failed')
        setAnnouncement(ANNOUNCE.loadFailed)
      }
    },
    [applyLoaded],
  )

  useEffect(() => {
    if (!routedId || resolvedIdRef.current === routedId) return
    void load(routedId)
  }, [routedId, load])

  // FOCUS LANDS ON THE TRANSCRIPT when the address changes, and on the retry control when an
  // inline failure paints — the two moments where what the citizen is looking at is replaced by
  // something they did not click.
  useEffect(() => {
    if (phase === 'ready') transcriptRef.current?.focus()
    if (phase === 'failed') retryRef.current?.focus()
  }, [phase])

  const pushFrame = useCallback((frame: TurnFrame, replyId: string) => {
    // A FRAME THIS PARSER DOES NOT KNOW IS NOT AN ERROR — the stream has to stay
    // forward-extensible — but neither is it something to read fields off.
    if (!isKnownFrame(frame)) return
    if (frame.type === 'text_delta') {
      setMessages((held) => {
        const last = held[held.length - 1]
        if (last && last.id === replyId) {
          const text =
            last.parts.map((p) => (p.type === 'text' ? p.text : '')).join('') + frame.text
          return [...held.slice(0, -1), replyMessage(replyId, last.seq ?? 0, text)]
        }
        return [...held, replyMessage(replyId, seqRef.current++, frame.text)]
      })
      return
    }
    if (frame.type === 'error') {
      setBanner(frame.message)
      return
    }
    if (frame.type === 'quota') {
      setBanner(
        `You have used your daily allowance. It resets at ${new Date(frame.resetsAt).toLocaleTimeString()}.`,
      )
      return
    }
    if (frame.type === 'snapshot') {
      // A REATTACH, not a fresh turn: the catch-up snapshot carries what the turn has produced
      // so far, and the deltas that follow continue from it.
      const text = frame.parts.map((part) => (part.type === 'text' ? part.text : '')).join('')
      if (text) setMessages((held) => [...held, replyMessage(replyId, seqRef.current++, text)])
      if (frame.errorMessage) setBanner(frame.errorMessage)
    }
  }, [])

  const runTurn = useCallback(
    async (id: string, text: string, attachmentIds: string[]) => {
      const controller = new AbortController()
      abortRef.current = controller
      setIsRunning(true)
      setBanner(null)
      setAnnouncement(ANNOUNCE.started)
      const replyId = `live-${id}-${Date.now()}`
      try {
        const started = await startTurn(id, {
          text,
          attachmentTexts: [],
          attachmentIds,
        })
        if (!aliveRef.current) return
        setTurnId(started.turnId)
        setContextTokens(started.contextTokens)
        await readTurnStream({
          conversationId: id,
          turnId: started.turnId,
          signal: controller.signal,
          onFrame: (frame) => pushFrame(frame, replyId),
        })
        if (!aliveRef.current) return
        setAnnouncement(ANNOUNCE.finished)
      } catch (err) {
        if (!aliveRef.current) return
        // THE PARTIAL REPLY STAYS IN THE TRANSCRIPT, and the banner carries the fact. Removing
        // what the model had already written would make a failure look like a turn that never
        // happened, and the citizen would lose words they had already read.
        setBanner(err instanceof TurnStartError ? err.message : LOAD_FAILED_TEXT)
        setAnnouncement(ANNOUNCE.turnFailed)
      } finally {
        if (aliveRef.current) {
          setIsRunning(false)
          setTurnId(null)
        }
        abortRef.current = null
      }
    },
    [pushFrame],
  )

  const handleSubmit = useCallback(
    async ({ text: rawText, attachments }: ComposerSubmission) => {
      const text =
        rawText.trim() || (attachments.length ? 'Please review the attached file(s).' : '')
      if (!text) return
      const isFirstMessage = routedId === undefined
      const id = chatId

      let parts
      try {
        // ★ THE CHAT EXISTS BEFORE ITS FILES DO. An upload names the conversation it belongs to
        // and that conversation has to be written already, so the create call fires BEFORE the
        // upload rather than beside it. Only on the first message: every later turn is sending
        // into a chat that plainly exists.
        if (isFirstMessage) await createConversation({ id, kind: GENERIC_KIND })
        parts = await buildUserParts(text, attachments, undefined, id)
      } catch (err) {
        // THE COMPOSER KEEPS EVERYTHING IT WAS HOLDING, because this rejects. A create that
        // fails leaves the citizen on the greeting with their typed text; an upload that fails
        // after the create leaves them at the new address with the staged file, which is why
        // the address is taken below rather than here.
        if (isFirstMessage) {
          setUrgent(
            err instanceof Error ? err.message : 'The chat could not be started. Try again.',
          )
          throw err
        }
        setUrgent(
          err instanceof Error ? err.message : 'Could not upload the attachment. Try again.',
        )
        throw err
      }

      // THE ADDRESS IS TAKEN ONCE THE ROW EXISTS, and `replace` rather than `push`: the greeting
      // is not a place to go Back to — its conversation is this one. The resolution is claimed
      // BEFORE the navigation, so the load effect that wakes on the new address finds this
      // conversation already resolved and leaves the reply in flight alone.
      if (isFirstMessage) {
        resolvedIdRef.current = id
        navigate(`/assistant/${id}`, { replace: true })
      }
      setPhase('ready')
      setMessages((held) => [
        ...held,
        {
          id: `user-${id}-${seqRef.current}`,
          role: 'user',
          parts,
          seq: seqRef.current++,
        },
      ])
      // BOTH SHAPES AN UPLOADED FILE TAKES. `buildUserParts` emits a `file` part for a binary
      // and a `text` part carrying an `attachment` descriptor for a file whose bytes travel as
      // text — reading only one of the two sends the turn without the ids of half the files
      // the citizen attached.
      const attachmentIds = parts
        .map((part) =>
          part.type === 'file'
            ? part.attachmentId
            : part.type === 'text'
              ? part.attachment?.attachmentId
              : undefined,
        )
        .filter((value): value is string => typeof value === 'string')
      setLastSend({ text, attachmentIds })
      await runTurn(id, text, attachmentIds)
    },
    [chatId, navigate, routedId, runTurn],
  )

  const retryTurn = useCallback(() => {
    if (!lastSend) return
    void runTurn(chatId, lastSend.text, lastSend.attachmentIds)
  }, [chatId, lastSend, runTurn])

  const handleCancel = useCallback(async () => {
    if (turnId) await stopTurn(chatId, turnId)
    abortRef.current?.abort()
  }, [chatId, turnId])

  // THE NAVIGATION ENTRY IS THE ONLY WAY TO START A NEW CONVERSATION, and this is what it
  // reaches: a fresh id, an empty transcript, and nothing carried over.
  const toGreeting = useCallback(() => {
    resolvedIdRef.current = null
    setMintedId(uuidv7())
    setMessages([])
    setBanner(null)
    setUrgent(null)
    setLastSend(null)
    seqRef.current = 0
    setContextTokens(null)
    setPhase('greeting')
    navigate('/assistant')
  }, [navigate])

  const contextWarning = useMemo(() => contextState(contextTokens).message, [contextTokens])
  const showGreeting = phase === 'greeting'
  const gate = showGreeting
    ? undefined
    : phase === 'loading'
      ? { blocked: true, reason: SENDING_BLOCKED_WHILE_LOADING }
      : phase === 'failed' || phase === 'gone'
        ? { blocked: true, reason: SENDING_BLOCKED_AFTER_FAILURE }
        : undefined

  const parts = headlineParts(greeting.headline, greeting.name)

  return (
    // `min-h-full`, NEVER `h-full`, AND NOTHING CLIPS HERE. A fixed height plus `overflow-hidden`
    // centred the stack in a box it could outgrow. The backdrop clips itself.
    <div
      className="relative flex min-h-full flex-col bg-bial-bg font-manrope"
      data-testid="assistant-page"
    >
      {/* THE BACKDROP KEEPS RUNNING once a conversation starts — the motes and the aircraft
          continue behind the transcript rather than stopping at the first message, so the surface
          stays itself instead of becoming a plain page the moment it is used. It honours the
          reduced-motion setting, and that entry survives this change because the component is
          untouched and never remounts. */}
      <Backdrop />

      {/* ONE RUNTIME, AROUND EVERYTHING THAT READS IT — the transcript and the composer both
          resolve against `useAui()`, so a provider wrapped around only one of them gives this
          screen two composer states and the one the citizen typed into is not the one the
          transcript belongs to. */}
      <ChatRuntimeProvider
        messages={messages}
        isRunning={isRunning}
        onNew={async () => undefined}
        onCancel={handleCancel}
      >
        <div
          className={`relative flex min-h-full flex-1 flex-col items-center px-6 ${
            showGreeting ? 'justify-center py-12' : 'justify-end pb-6 pt-4'
          }`}
        >
          {showGreeting ? (
            // ONE MOTION ON THE WHOLE STACK, not a stagger across its three parts.
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: DURATION.layout, ease: LAYOUT_EASE }}
              className="w-full max-w-thread"
            >
              <h1
                data-testid="assistant-greeting"
                className="text-center text-[clamp(30px,4.2vw,44px)] font-extrabold leading-[1.14] tracking-[-1px] text-tertiary"
              >
                {parts.map((segment, index) =>
                  segment.accent ? (
                    // The one word in colour on the whole screen.
                    <em key={`${index}-${segment.text}`} className="mr-[0.04em] text-primary">
                      {segment.text}
                    </em>
                  ) : (
                    <span key={`${index}-${segment.text}`}>{segment.text}</span>
                  ),
                )}
              </h1>
              <p className="mt-2.5 text-center text-[clamp(13px,1.1vw,15px)] leading-relaxed text-status-grey-fg wide:mt-3">
                {greeting.sub}
              </p>
            </motion.div>
          ) : (
            // THE TRANSCRIPT COLUMN SITS ON WHITE, not on the page's shaded ground. The portal's
            // muted grey measures 4.44:1 against that ground — under the contrast floor — and this
            // is the page the prior design's finding was written for.
            <div
              data-testid="assistant-transcript"
              ref={transcriptRef}
              tabIndex={-1}
              aria-label="Conversation"
              className="mb-3 w-full max-w-thread flex-1 overflow-hidden rounded-2xl border border-bial-border bg-white"
            >
              {phase === 'loading' ? (
                <div
                  data-testid="assistant-loading"
                  role="status"
                  aria-live="polite"
                  aria-busy="true"
                  className="flex h-full items-center justify-center p-8"
                >
                  <p className="text-sm font-medium text-neutral">Loading this conversation…</p>
                </div>
              ) : phase === 'gone' ? (
                <div
                  data-testid="assistant-gone"
                  className="flex h-full flex-col items-center justify-center gap-3 p-8"
                >
                  <p className="text-sm text-tertiary">{GONE_TEXT}</p>
                  {/* THE ONE WAY FORWARD. At phone width the navigation entry is inside a drawer,
                    so without this control the state is a dead end. It reaches the same place
                    that entry does, so nothing new is invented to begin with. */}
                  <button
                    type="button"
                    data-testid="assistant-start-new"
                    onClick={toGreeting}
                    className="text-xs text-primary underline underline-offset-2 hover:text-primary-600"
                  >
                    Start a new chat
                  </button>
                </div>
              ) : phase === 'failed' ? (
                <div
                  data-testid="assistant-load-failed"
                  className="flex h-full flex-col items-center justify-center gap-3 p-8"
                >
                  <p className="text-sm text-tertiary">{LOAD_FAILED_TEXT}</p>
                  <button
                    type="button"
                    ref={retryRef}
                    data-testid="assistant-load-retry"
                    onClick={() => void load(chatId)}
                    className="text-xs text-primary underline underline-offset-2 hover:text-primary-600"
                  >
                    Try again
                  </button>
                </div>
              ) : (
                <div className="h-full">
                  <ChatThread />
                </div>
              )}
            </div>
          )}

          <div
            className={
              showGreeting ? 'mt-6 w-full max-w-thread wide:mt-[30px]' : 'w-full max-w-thread'
            }
          >
            {/* The polite region for turn activity, permanently mounted so its text is announced
                when it arrives rather than being injected together with its region. */}
            <Announcer message={announcement} />

            {/* THE ONE BANNER SLOT: a turn error, or the daily-cap sentence. Newest wins. */}
            <TurnBanner text={banner} />
            {banner && lastSend && !isRunning ? (
              <div className="px-3 pb-1">
                <button
                  type="button"
                  data-testid="assistant-turn-retry"
                  onClick={retryTurn}
                  className="text-[11px] text-primary underline underline-offset-2 hover:text-primary-600"
                >
                  Retry
                </button>
              </div>
            ) : null}

            {/* ASSERTIVE, and reserved for what genuinely interrupts: a refused file, a refused
                send. Permanently mounted for the same reason the polite regions are. */}
            <p
              role="alert"
              data-testid="assistant-urgent"
              className={`text-xs text-status-red-fg ${urgent ? 'mt-1.5' : ''}`}
            >
              {urgent ?? ''}
            </p>

            <Composer
              // THE MINTED ID, EVEN ON THE GREETING, because `null` here disables sending
              // outright — and the greeting screen is exactly where the first send happens. The
              // id exists a round trip before its row does: that ordering is what lets an upload
              // name the conversation it belongs to, and it is what the draft is keyed on.
              conversationId={chatId}
              placeholder="Message BIAL Chat"
              onSubmit={handleSubmit}
              isRunning={isRunning}
              gate={gate}
              contextWarning={contextWarning}
              stop={
                isRunning && turnId
                  ? {
                      running: true,
                      resolveTarget: () => ({ conversationId: chatId, turnId }),
                      onStopTurn: stopTurn,
                    }
                  : undefined
              }
              onUrgent={setUrgent}
              // No ground and no gutter of its own: the box sits directly on the platform's,
              // which is what the board draws.
              frameClassName="flex w-full flex-col gap-1.5"
              // `text-neutral` is 4.37:1 on this page's ground and fails AA. See the prop.
              noteClassName="text-status-grey-fg"
            />
          </div>
        </div>
      </ChatRuntimeProvider>
    </div>
  )
}
