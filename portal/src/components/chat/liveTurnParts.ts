/**
 * A live turn's content, accumulated from its frames and drawn as the streaming assistant message.
 * Both chat pages build their reply here, so a reply reads the same live on either page and the
 * same again after a reload.
 */
import type { MessagePart } from '../../utils/messageTypes'
import type { StepItem } from '../../utils/turnStreamApi'

export type LivePart = { kind: 'text'; text: string } | { kind: 'step'; toolCallId: string; step: StepItem }

export interface LiveTurn {
  /** Prose and steps, in the order the turn produced them. */
  parts: LivePart[]
  /** The server's `working` flag: the model has the floor and nothing readable has arrived yet. */
  working: boolean
}

/**
 * The parts of the STREAMING assistant message, in the order the turn produced them. ORDER IS THE
 * RENDER: `groupPartByType` coalesces ADJACENT steps into one group, so prose between two steps
 * seals the first and opens a second. Hidden steps are DROPPED, not positioned — a gap would break
 * that adjacency; the flag never covers reads or a failed step, both the server's call. A NEW ARRAY
 * EVERY TIME: the runtime caches on OBJECT IDENTITY (`convertMessage` trap 4), so a
 * mutated-in-place list would silently never re-render.
 */
export function streamingParts(turn: LiveTurn): MessagePart[] {
  const parts: MessagePart[] = []
  for (const part of turn.parts) {
    if (part.kind === 'text') {
      // An empty text part renders no element, so an in-flight turn with steps and no prose
      // yet is just its activity — which is exactly what should be on screen at that moment.
      parts.push({ type: 'text', text: part.text })
    } else if (!part.step.hidden) {
      parts.push({ type: 'step', step: part.step })
    }
  }
  // THE STATUS RIDES AT THE TAIL, and only while the model has the floor. It carries no text — the
  // shape has no field for any — so "status only, never the reasoning" is structural. At the tail
  // because the model thinks again between tool calls, and a row pinned above the prose would push
  // paragraphs the citizen had already read down the screen on every burst.
  if (turn.working) parts.push({ type: 'reasoning' })
  // THE STREAMING MESSAGE ALWAYS ENDS ON A TEXT PART, and the empty one is load-bearing twice:
  //
  //  1. `hasUpcomingMessage` — the library appends an optimistic assistant message with an id we
  //     do not control the moment `isRunning` is true and the last message is not an assistant's
  //     (convertMessage trap 3), and a message whose parts all convert to nothing makes that
  //     reachable.
  //  2. The transcript's step-only rule — a message made ONLY of steps is a STORED row that the
  //     live message is re-telling, and it is dropped for the turn in flight. Without this the
  //     live message matched that rule against itself and vanished mid-build.
  if (parts[parts.length - 1]?.type !== 'text') parts.push({ type: 'text', text: '' })
  return parts
}

/** Append `text` to the block already open, or open a new one.
 *
 * A delta that arrives when the newest part is a STEP opens a block whatever the frame says:
 * appending to a sealed block would move that prose back above the step it was written after,
 * silently reordering the turn. */
export function appendText(turn: LiveTurn, text: string, newBlock: boolean): void {
  const newest = turn.parts[turn.parts.length - 1]
  if (!newBlock && newest?.kind === 'text') {
    newest.text += text
    return
  }
  turn.parts.push({ kind: 'text', text })
}

/** Record a step at its position, or replace the one already there; `true` when it is new.
 *
 * The `finished` frame carries the same tool-call id as its `started` one and REPLACES it in
 * place: appending would stack a spinner beside its own result, and the activity group's live
 * count would climb while the same step re-rendered. */
export function putStep(turn: LiveTurn, toolCallId: string, step: StepItem): boolean {
  const at = turn.parts.findIndex((part) => part.kind === 'step' && part.toolCallId === toolCallId)
  if (at === -1) turn.parts.push({ kind: 'step', toolCallId, step })
  else turn.parts[at] = { kind: 'step', toolCallId, step }
  return at === -1
}
