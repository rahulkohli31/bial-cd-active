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
import { SendRefusal } from '../components/chat/sendRefusal'
import TurnBanner from '../components/chat/TurnBanner'
import { DURATION, LAYOUT_EASE } from '../lib/motion'
import { getStoredUser } from '../utils/auth'
import { buildUserParts, releaseUploadedAttachments } from '../utils/attachmentStore'
import {
  createConversation,
  getConversation,
  uuidv7,
  type ActiveTurn,
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
const TURN_LOST_TEXT = 'The connection dropped. Reload to catch up.'
const SENDING_BLOCKED_WHILE_LOADING = 'Loading this conversation…'
const SENDING_BLOCKED_AFTER_FAILURE = 'Load this conversation before sending.'
/** The `gone` state has nothing to load, so it cannot be told to load it. */
const SENDING_BLOCKED_WHEN_GONE = 'Start a new chat to send a message.'

const ANNOUNCE = {
  started: 'Reply started.',
  finished: 'Reply finished.',
  turnFailed: 'The reply failed. A retry is available.',
  loadFailed: 'This conversation could not be loaded. A retry is available.',
} as const

/**
 * An assistant message built live from the stream — one per reply, matching the reload shape.
 *
 * THE STATUS IS A PART AND NOT A FLAG, because the thread draws one only for a message that
 * carries it, and it rides at the TAIL: the model is thinking after what it has written so far,
 * and a row pinned above the prose pushes read paragraphs down the screen on every burst. It
 * carries no text — the shape has no field for any — so the reasoning itself cannot reach here.
 */
function replyMessage(id: string, seq: number, text: string, working: boolean): ChatMessage {
  const parts: MessagePart[] = [{ type: 'text', text }]
  if (working) parts.push({ type: 'reasoning' })
  return { id, role: 'assistant', parts, seq }
}

/** The prose of a message, which is the whole of what a reply on this surface accumulates. */
function textOf(message: ChatMessage): string {
  return message.parts.map((part) => (part.type === 'text' ? part.text : '')).join('')
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
  // WHEN THE RUNNING TURN OPENED, so the working line counts the turn rather than itself. The
  // status row is torn down and rebuilt on every burst, so a row timing its own mount reports the
  // burst and the count a citizen is watching falls back to zero.
  const [turnStartedAt, setTurnStartedAt] = useState<number | null>(null)
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
  // Does the model have the floor? A ref because the frame handler is built once per turn and
  // cannot see state changing under it, and every delta has to be rebuilt carrying the answer.
  const workingRef = useRef(false)

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

  /**
   * Raise or lower the model's status on the live reply.
   *
   * A turn that has said nothing yet gets an empty message to carry it: the whole point of the
   * status is telling a thinking agent from a hung one, and that question is asked hardest before
   * the first word arrives.
   */
  const showWorking = useCallback((replyId: string, working: boolean) => {
    workingRef.current = working
    setMessages((held) => {
      const last = held[held.length - 1]
      if (!last || last.id !== replyId) {
        return working ? [...held, replyMessage(replyId, seqRef.current++, '', true)] : held
      }
      return [...held.slice(0, -1), replyMessage(replyId, last.seq ?? 0, textOf(last), working)]
    })
  }, [])

  const pushFrame = useCallback(
    (frame: TurnFrame, replyId: string) => {
      // A FRAME THIS PARSER DOES NOT KNOW IS NOT AN ERROR — the stream has to stay
      // forward-extensible — but neither is it something to read fields off.
      if (!isKnownFrame(frame)) return
      if (frame.type === 'text_delta') {
        setMessages((held) => {
          const last = held[held.length - 1]
          if (last && last.id === replyId) {
            const text = textOf(last) + frame.text
            return [
              ...held.slice(0, -1),
              replyMessage(replyId, last.seq ?? 0, text, workingRef.current),
            ]
          }
          return [
            ...held,
            replyMessage(replyId, seqRef.current++, frame.text, workingRef.current),
          ]
        })
        return
      }
      if (frame.type === 'working') {
        showWorking(replyId, frame.working)
        return
      }
      if (frame.type === 'turn_ended') {
        // THE TERMINAL IS WHERE THE STATUS COMES DOWN. The flag is edge-triggered, so a turn
        // whose last act was thinking sends no falling edge of its own and the row would spin
        // over a finished reply. Nothing else on this frame has a reader here: what a turn ended
        // as is already told by the reply itself, by the banner an `error` frame wrote, or by the
        // stream simply ending.
        showWorking(replyId, false)
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
        // so far, and the deltas that follow continue from it — the status it was taken in
        // included, or a tab that rejoined mid-thought sits on a still screen until the next
        // frame happens to change something.
        workingRef.current = frame.working
        const text = frame.parts.map((part) => (part.type === 'text' ? part.text : '')).join('')
        if (text) {
          setMessages((held) => [
            ...held,
            replyMessage(replyId, seqRef.current++, text, frame.working),
          ])
        } else {
          showWorking(replyId, frame.working)
        }
        if (frame.errorMessage) setBanner(frame.errorMessage)
        return
      }
      // EVERY REMAINING FRAME NARRATES WORK ON AN APP, and this surface builds none: `step`,
      // `plan_options`, `workspace`, `preview`, `diagnostic` and `compile` describe a sandbox and
      // a build a generic chat never has. They are dropped deliberately rather than by falling
      // off the end of the list.
    },
    [showWorking],
  )

  /**
   * RE-ATTACH to a turn that is still running server-side.
   *
   * A reload mid-reply lands on a transcript whose newest row is the citizen's own message: the
   * reply is being produced, but this tab holds no socket — so the screen would sit frozen with
   * no way to stop it, and the next send would be refused as busy. Subscribed with NO cursor even
   * though the server reports one, because the catch-up snapshot carries the turn so far and a
   * cursor would tail past everything already written.
   */
  const reattachToTurn = useCallback(
    async (id: string, activeTurn: ActiveTurn) => {
      const controller = new AbortController()
      abortRef.current = controller
      workingRef.current = false
      setTurnId(activeTurn.turnId)
      setIsRunning(true)
      setBanner(null)
      // Nothing this tab holds records when the turn opened, so the count starts at the rejoin
      // rather than at an earlier instant nobody measured.
      setTurnStartedAt(Date.now())
      setAnnouncement(ANNOUNCE.started)
      const replyId = `live-${id}-${activeTurn.turnId}`
      try {
        await readTurnStream({
          conversationId: id,
          turnId: activeTurn.turnId,
          signal: controller.signal,
          onFrame: (frame) => pushFrame(frame, replyId),
        })
        if (!aliveRef.current) return
        setAnnouncement(ANNOUNCE.finished)
      } catch {
        if (!aliveRef.current) return
        setBanner(TURN_LOST_TEXT)
        setAnnouncement(ANNOUNCE.turnFailed)
      } finally {
        if (aliveRef.current) {
          showWorking(replyId, false)
          setIsRunning(false)
          setTurnId(null)
        }
        abortRef.current = null
      }
    },
    [pushFrame, showWorking],
  )

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
      // A REPLY STILL BEING WRITTEN IS REJOINED RATHER THAN WAITED OUT. The server reports the
      // running turn on every kind of conversation, and a transcript that ignored it would freeze
      // mid-reply, offer no Stop, and have its next send refused as busy.
      if (loaded.activeTurn) void reattachToTurn(loaded.id, loaded.activeTurn)
    },
    [navigate, reattachToTurn],
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

  const runTurn = useCallback(
    async (
      id: string,
      text: string,
      attachmentIds: string[],
      /** What to undo if the server refuses the turn at the door — see the catch below. */
      onRefused?: () => void,
    ) => {
      const controller = new AbortController()
      abortRef.current = controller
      workingRef.current = false
      setIsRunning(true)
      setBanner(null)
      setTurnStartedAt(Date.now())
      setAnnouncement(ANNOUNCE.started)
      const replyId = `live-${id}-${Date.now()}`
      // DID THE SERVER ACCEPT THE TURN? A 202 means the citizen's message is persisted and the
      // reply runs detached whatever this tab does next, so everything past it is subscription
      // plumbing — and a subscription that breaks must not take back a message the database holds.
      let posted = false
      try {
        const started = await startTurn(id, {
          text,
          attachmentTexts: [],
          attachmentIds,
        })
        posted = true
        if (!aliveRef.current) return
        setTurnId(started.turnId)
        setContextTokens(started.contextTokens)
        // THE RETRY CONTROL ONLY EVER RE-RUNS A TURN THE SERVER TOOK. A refused turn leaves the
        // citizen's words in the composer, so a second way to send them would send them twice.
        setLastSend({ text, attachmentIds })
        await readTurnStream({
          conversationId: id,
          turnId: started.turnId,
          signal: controller.signal,
          onFrame: (frame) => pushFrame(frame, replyId),
        })
        if (!aliveRef.current) return
        setAnnouncement(ANNOUNCE.finished)
      } catch (err) {
        // BEFORE THE LIVENESS GUARD, because taking back what a refusal orphaned is not a paint
        // decision: the uploads are on the server whether or not this screen is still here.
        if (!posted) onRefused?.()
        if (!aliveRef.current) return
        // THE PARTIAL REPLY STAYS IN THE TRANSCRIPT, and the banner carries the fact. Removing
        // what the model had already written would make a failure look like a turn that never
        // happened, and the citizen would lose words they had already read.
        setBanner(err instanceof TurnStartError ? err.message : LOAD_FAILED_TEXT)
        setAnnouncement(ANNOUNCE.turnFailed)
      } finally {
        if (aliveRef.current) {
          showWorking(replyId, false)
          setIsRunning(false)
          setTurnId(null)
        }
        abortRef.current = null
      }
    },
    [pushFrame, showWorking],
  )

  const handleSubmit = useCallback(
    async ({ text: rawText, attachments }: ComposerSubmission) => {
      const text =
        rawText.trim() || (attachments.length ? 'Please review the attached file(s).' : '')
      if (!text) return
      // THE PREVIOUS COMPLAINT IS RETIRED BY THE ATTEMPT THAT ANSWERS IT. Nothing else clears this
      // region, so a refused seventh file left "You can attach at most 5 files per message." sitting
      // in red above a composer that had since sent those five and been answered.
      setUrgent(null)
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
        //
        // AS A REFUSAL, AND A SILENT ONE. The composer keeps `err.message` only for a refusal and
        // paints its own generic sentence over anything else — which is the server's real reason
        // lost, on the one failure where "try again" can be advice that cannot work. Silent
        // because the sentence is already in the urgent region a line above.
        const message =
          err instanceof Error
            ? err.message
            : isFirstMessage
              ? 'The chat could not be started. Try again.'
              : 'Could not upload the attachment. Try again.'
        setUrgent(message)
        throw new SendRefusal(message, { silent: true })
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
      const userSeq = seqRef.current
      const userId = `user-${id}-${userSeq}`
      seqRef.current += 1
      setMessages((held) => [...held, { id: userId, role: 'user', parts, seq: userSeq }])
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

      let refused = false
      await runTurn(id, text, attachmentIds, () => {
        // THE SERVER PERSISTED NOTHING. A turn refused at the door — the daily cap, a
        // conversation already busy, a model that is not configured — leaves a bubble on screen
        // that the database disagrees with: it looks sent, and it disappears at the next reload.
        // So the optimistic message goes back and the seq with it, and the files it named are
        // released, because no message will ever reference them and they still count against
        // this conversation's attachment allowance.
        refused = true
        setMessages((held) => held.filter((m) => m.id !== userId))
        seqRef.current = userSeq
        releaseUploadedAttachments(parts)
      })
      // AND THE COMPOSER KEEPS THE WORDS, because the bubble that was holding them is gone.
      // Silent: the banner runTurn wrote is the account of this, and a second sentence about
      // one refusal is a contradiction rather than more information.
      if (refused) throw new SendRefusal('', { silent: true })
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

  // WHAT A NEW CONVERSATION STARTS FROM: a fresh id, an empty transcript, nothing carried over.
  // Two ways in reach it — the control in the gone state, and the address itself dropping back to
  // `/assistant` — so the reset is its own function and only one of them navigates.
  const wipeTheSlate = useCallback(() => {
    // THIS TAB STOPS READING whatever reply it was watching. The turn keeps running server-side;
    // what must not happen is its deltas landing in the transcript that replaced it.
    abortRef.current?.abort()
    resolvedIdRef.current = null
    workingRef.current = false
    setMintedId(uuidv7())
    setMessages([])
    setBanner(null)
    setUrgent(null)
    setLastSend(null)
    seqRef.current = 0
    setContextTokens(null)
    setPhase('greeting')
  }, [])

  const toGreeting = useCallback(() => {
    wipeTheSlate()
    navigate('/assistant')
  }, [navigate, wipeTheSlate])

  // THE NAVIGATION ENTRY LEAVES BY THE ADDRESS ALONE, and both addresses render this same
  // element — so a link back to `/assistant` unmounts nothing and resets nothing by itself. The
  // conversation just left would stay on screen, and the next send would append to it, because
  // `chatId` falls back to the id it was first sent under. The address going FROM a conversation
  // TO none is the reset.
  //
  // THE TRANSITION, NOT THE STATE: the first send claims its address while this surface is
  // already holding the conversation, so a rule reading "no address and something on screen"
  // could wipe the send that is taking the address.
  const lastAddressRef = useRef(routedId)
  useEffect(() => {
    const left = lastAddressRef.current !== undefined && routedId === undefined
    lastAddressRef.current = routedId
    if (!left || phase === 'greeting') return
    wipeTheSlate()
  }, [routedId, phase, wipeTheSlate])

  const contextWarning = useMemo(() => contextState(contextTokens).message, [contextTokens])
  const showGreeting = phase === 'greeting'
  const gate = showGreeting
    ? undefined
    : phase === 'loading'
      ? { blocked: true, reason: SENDING_BLOCKED_WHILE_LOADING }
      : phase === 'failed'
        ? { blocked: true, reason: SENDING_BLOCKED_AFTER_FAILURE }
        : phase === 'gone'
          ? { blocked: true, reason: SENDING_BLOCKED_WHEN_GONE }
          : undefined

  const parts = headlineParts(greeting.headline, greeting.name)

  return (
    // THE TWO STATES WANT OPPOSITE HEIGHTS, and one rule cannot serve both.
    //
    // GREETING — `min-h-full`, never `h-full`. This box must be free to outgrow the pane, because
    // `justify-center` centres within whatever height it ends up with: `min-h-full` gives it the
    // pane's height to centre in, while a child asking for `min-h-full` gets nothing (a percentage
    // minimum resolves against a parent whose height is `auto`) and collapses onto its own content.
    // That was the shipped bug — the greeting sitting at the top of a full-height page.
    //
    // TRANSCRIPT — `h-full`, and it has to be DEFINITE. `flex-1` divides a known height; against a
    // growable one it divides nothing, so the thread rendered at its full content height and pushed
    // the composer past the fold, leaving the citizen to scroll the page to reach the box they were
    // typing in. Definite here plus `min-h-0` below is what pins the composer and puts the
    // scrolling inside the transcript, where the build chat already keeps it.
    <div
      className={`relative flex flex-col bg-bial-bg font-manrope ${
        showGreeting ? 'min-h-full' : 'h-full'
      }`}
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
        {/* `flex-1` — NOT `min-h-full` — is what holds this box open to the full pane, and the
            centring below is worthless without it. See the parent's note. */}
        <div
          data-testid="assistant-column"
          className={`relative flex min-h-0 flex-1 flex-col items-center px-6 ${
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
            // THE TRANSCRIPT SITS DIRECTLY ON THE PAGE GROUND, with no card, border or fill of its
            // own — the same as the build chat's thread, and the same as every assistant people
            // already use. Contrast is a question about the TEXT: `text-neutral` is 4.37:1 on this
            // ground and fails, `text-status-grey-fg` is 6.84:1 and passes, so the muted lines below
            // carry that token. Drawing a white panel to rescue a grey would buy one AA pass and a
            // container on every screen.
            <div
              data-testid="assistant-transcript"
              ref={transcriptRef}
              tabIndex={-1}
              aria-label="Conversation"
              // `focus:outline-none` because the focus here is PROGRAMMATIC and the target is not a
              // control. Sending with Enter puts the browser in keyboard modality, so `:focus-visible`
              // matches on the `tabIndex={-1}` container and Chromium paints its own 1px ring right
              // round the transcript — in its blue, not the portal's. The move exists to carry a
              // screen reader to the reply, and that is unaffected by dropping the ring.
              className="mb-3 w-full max-w-thread min-h-0 flex-1 overflow-hidden focus:outline-none"
            >
              {phase === 'loading' ? (
                <div
                  data-testid="assistant-loading"
                  role="status"
                  aria-live="polite"
                  aria-busy="true"
                  className="flex h-full items-center justify-center p-8"
                >
                  <p className="text-sm font-medium text-status-grey-fg">Loading this conversation…</p>
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
                  {/* The turn's own start, so the working line's count measures the reply rather
                      than the row drawing it — the row is rebuilt on every burst. */}
                  <ChatThread turnStartedAt={turnStartedAt} />
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
