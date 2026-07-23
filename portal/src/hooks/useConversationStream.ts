/**
 * useConversationStream (U10) — the chat panel's view of a conversation's live turn.
 *
 * Subscribe once; the hook applies the server's snapshot-then-tail contract: a `snapshot`
 * frame REPLACES local turn state (it is the consolidated truth — fresh subscribe, F5,
 * or a resume that fell past the ring), and tail frames append to it. Steps key on
 * `toolCallId` so a `finished` step replaces its `started` card in place. On a truncated
 * socket the hook resubscribes ONCE with its cursor (gap-free resume — the server
 * degrades to a fresh snapshot when it cannot prove continuity); a stall surfaces as
 * `stalled` rather than silently hanging (distinct outcomes, streamed-reply learning).
 *
 * U13 wires this into the composer/BuilderPage; U15 renders the friendly steps. Until
 * then the hook is the tested seam.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import {
  isKnownFrame,
  readTurnStream,
  startTurn,
  stopTurn,
  type PlanOptionsItem,
  type ProjectionItem,
  type StartTurnMessage,
  type StepItem,
  type TurnFrame,
} from '../utils/turnStreamApi'

export type TurnStreamStatus =
  | 'idle'
  | 'connecting'
  | 'streaming'
  | 'completed'
  | 'failed'
  | 'stopped'
  | 'stalled'
  | 'error'

export interface ConversationStreamState {
  status: TurnStreamStatus
  turnId: string | null
  /** Projection items from the snapshot (persisted rows — the durable part). */
  items: ProjectionItem[]
  /** The in-flight assistant text (snapshot textSoFar + live deltas). */
  text: string
  /** Friendly steps in arrival order; finished steps replace their started card. */
  steps: StepItem[]
  /** The in-band failure notice, when the turn failed. */
  errorMessage: string | null
  /** The newest plan-options card (U11) — pending cards render the buttons. */
  planOptions: PlanOptionsItem | null
}

const IDLE_STATE: ConversationStreamState = {
  status: 'idle',
  turnId: null,
  items: [],
  text: '',
  steps: [],
  errorMessage: null,
  planOptions: null,
}

interface StepSlot {
  toolCallId: string
  item: StepItem
}

/** Narrow a projection item to the plan-options card shape (parse, don't cast). */
function toPlanOptionsItem(item: ProjectionItem): PlanOptionsItem | null {
  if (item.type !== 'plan_options') return null
  const { toolCallId, state, mode, reason } = item
  if (typeof toolCallId !== 'string' || typeof state !== 'string') return null
  if (state !== 'pending' && state !== 'refine' && state !== 'build' && state !== 'build_failed') {
    return null
  }
  return {
    type: 'plan_options',
    seq: item.seq,
    mode: typeof mode === 'string' ? mode : '',
    toolCallId,
    state,
    reason: typeof reason === 'string' ? reason : null,
  }
}

export interface UseConversationStream {
  state: ConversationStreamState
  /** POST the message, then subscribe to the turn's stream. */
  send: (message: StartTurnMessage) => Promise<void>
  /** Subscribe without sending (reload onto a running turn — `activeTurn` from the GET). */
  attach: (turnId?: string, cursor?: number) => void
  /** The explicit cancel (disconnect never cancels). */
  stop: () => Promise<void>
}

export function useConversationStream(conversationId: string | null): UseConversationStream {
  const [state, setState] = useState<ConversationStreamState>(IDLE_STATE)
  const abortRef = useRef<AbortController | null>(null)
  const cursorRef = useRef(0)
  const turnRef = useRef<string | null>(null)
  const stepsRef = useRef<StepSlot[]>([])
  const resumedOnceRef = useRef(false)

  useEffect(() => {
    // A different conversation (or unmount) tears the subscription down; the TURN keeps
    // running server-side — disconnect is never cancel.
    return () => abortRef.current?.abort()
  }, [conversationId])

  const applyFrame = useCallback((frame: TurnFrame) => {
    if (!isKnownFrame(frame)) return // forward-extensible: unknown types are non-events
    cursorRef.current = frame.seq
    if (frame.type === 'snapshot') {
      turnRef.current = frame.turnId
      stepsRef.current = frame.steps.map((item, index) => ({
        toolCallId: `snapshot-${index}`,
        item,
      }))
      const snapshotCards = frame.items
        .map(toPlanOptionsItem)
        .filter((card): card is PlanOptionsItem => card !== null)
      setState({
        status: frame.turnStatus === 'running' ? 'streaming' : frame.turnStatus,
        turnId: frame.turnId,
        items: frame.items,
        text: frame.textSoFar,
        steps: frame.steps,
        errorMessage: null,
        planOptions: snapshotCards.length > 0 ? snapshotCards[snapshotCards.length - 1] : null,
      })
    } else if (frame.type === 'text_delta') {
      setState((prev) => ({ ...prev, status: 'streaming', text: prev.text + frame.text }))
    } else if (frame.type === 'step') {
      const slots = stepsRef.current
      const existing = slots.findIndex((slot) => slot.toolCallId === frame.toolCallId)
      if (existing >= 0) slots[existing] = { toolCallId: frame.toolCallId, item: frame.item }
      else slots.push({ toolCallId: frame.toolCallId, item: frame.item })
      setState((prev) => ({ ...prev, steps: slots.map((slot) => slot.item) }))
    } else if (frame.type === 'plan_options') {
      setState((prev) => ({ ...prev, planOptions: frame.item }))
    } else if (frame.type === 'error') {
      setState((prev) => ({ ...prev, errorMessage: frame.message }))
    } else if (frame.type === 'turn_ended') {
      setState((prev) => ({ ...prev, status: frame.status, turnId: frame.turnId }))
    }
  }, [])

  const subscribe = useCallback(
    (turnId?: string, cursor?: number) => {
      if (!conversationId) return
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      resumedOnceRef.current = false
      setState((prev) => ({ ...prev, status: 'connecting' }))

      const run = async (resumeTurn?: string, resumeCursor?: number): Promise<void> => {
        const outcome = await readTurnStream({
          conversationId,
          turnId: resumeTurn,
          cursor: resumeCursor,
          signal: controller.signal,
          onFrame: applyFrame,
        })
        if (controller.signal.aborted) return
        if (outcome === 'stalled') {
          setState((prev) => ({ ...prev, status: 'stalled' }))
          return
        }
        if (outcome === 'truncated') {
          // One cursor-resume covers a dropped socket; the server re-snapshots when the
          // cursor fell out of the ring. A second truncation is a real problem.
          if (!resumedOnceRef.current) {
            resumedOnceRef.current = true
            await run(turnRef.current ?? undefined, cursorRef.current)
            return
          }
          setState((prev) => ({ ...prev, status: 'error' }))
        }
        // 'completed': the terminal frame already set the semantic status.
      }

      run(turnId, cursor).catch(() => {
        if (!controller.signal.aborted) {
          setState((prev) => ({ ...prev, status: 'error' }))
        }
      })
    },
    [conversationId, applyFrame]
  )

  const send = useCallback(
    async (message: StartTurnMessage) => {
      if (!conversationId) throw new Error('no conversation to send into')
      const { turnId } = await startTurn(conversationId, message)
      turnRef.current = turnId
      cursorRef.current = 0
      subscribe()
    },
    [conversationId, subscribe]
  )

  const stop = useCallback(async () => {
    if (!conversationId || !turnRef.current) return
    await stopTurn(conversationId, turnRef.current)
  }, [conversationId])

  return { state, send, attach: subscribe, stop }
}
