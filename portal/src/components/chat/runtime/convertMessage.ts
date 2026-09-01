/**
 * THE SEAM. Above it everything is the library's; below it everything is the server's.
 *
 * One function maps our `ChatMessage` — server id, seq, role, parts[] — onto assistant-ui's
 * `ThreadMessageLike`. Both the live stream and the reload projection produce the SAME
 * `ChatMessage`, so both arrive here and both render identically. That is R72's surface half and
 * AE43, and it is a property of there being one converter rather than a rule anyone enforces.
 *
 * ══ FIVE IDENTITY TRAPS, ALL VERIFIED IN THE INSTALLED 0.15.17 SOURCE ══
 *
 * Carried here as code, not as a risks table:
 *
 *  1. OMIT `id` AND THE FALLBACK IS THE ARRAY INDEX. Every message we hand over carries the
 *     server's id. `messagesFromProjection` already mints stable composite keys — `srv_{seq}_{kind}_{index}`
 *     — and the comment above them explains why seq alone collides (one row projects several
 *     items). We reuse those; we do not invent a second identity scheme.
 *  2. DUPLICATE IDS DROP MESSAGES. The runtime keeps the LAST occurrence and only `console.warn`s,
 *     so a collision costs a turn and says so nowhere anyone looks. `assertUniqueIds` throws with
 *     the offending id named instead — loud, at the seam, before the runtime can swallow it.
 *  3. THE LIBRARY MINTS EXACTLY ONE ID. `hasUpcomingMessage` is `isRunning && last.role !== "assistant"`;
 *     when true it appends an optimistic assistant message with an id we do not control. Keeping
 *     identity server-owned is therefore a SEQUENCING requirement on the send path — a server-owned
 *     assistant message must be last the instant `isRunning` flips true — not something this file
 *     can fix. `hasUpcomingMessage` exports the predicate so the send path and its test can both
 *     read the same rule.
 *  4. THE CONVERTER CACHES ON OBJECT IDENTITY. The runtime holds a WeakMap keyed on the message
 *     object and short-circuits on a hit, so MUTATING A MESSAGE IN PLACE IS INVISIBLE — the UI
 *     simply never re-renders. Every streamed update must produce a NEW object for the changed
 *     message and preserve identity for every unchanged one. This is also the fix for the
 *     "activity groups flicker and disappear" failure: the record of tool calls lives outside the
 *     loop reading the stream, so a chunk carrying only text cannot erase earlier calls.
 *  5. `setMessages` IS NOT FREE. Providing it switches on `switchToBranch` AND `delete`. It is
 *     never provided, and U4's exact-equality capability test is what keeps it that way.
 *
 * ══ WHAT A PART BECOMES ══
 *
 * `text`   → a text part. Prose is prose.
 * `step`   → a `tool-call` part carrying LABEL AND STATE ONLY (R36; see the redaction note).
 * others   → nothing. `build`, `build_in_progress` and `plan_options` are not transcript prose:
 *            the first two are replaced by the activity group's own terminal handling, and
 *            `plan_options` is the offer, which U16 renders on the composer rather than inline.
 *            Dropping a part is not the same as dropping a message — a message whose parts all
 *            drop still exists, with empty content, and the thread renders no element for it.
 *
 * ══ R36's WALL IS HERE, NOT AT THE DRAW SITE ══
 *
 * A step becomes a tool-call part with `toolName` and a `state`, and NOTHING ELSE. No `args`, no
 * `result`, no `detail`. The expander (R33) therefore has nothing to leak even if someone later
 * renders every field a part holds — which is the point of putting the wall at the converter
 * rather than trusting a promise at the component. `toStepItem` in `turnStreamApi.ts` already
 * narrows `detail` away on both paths, so this is the second of two walls, not the only one.
 */
import type { ChatMessage, MessagePart } from '../../../utils/messageTypes'
import type { ThreadMessageLike } from '@assistant-ui/react'

/** What a step's state becomes on the tool-call part the group renders. */
export type ActivityState = 'running' | 'ok' | 'failed'

/**
 * The tool-call args we allow onto a part. Deliberately a closed shape rather than the step's
 * own fields: this object IS what an expander can render, so it holds only what R35b says a row
 * may read — the server's friendly label and the state.
 */
export type ActivityArgs = {
  label: string
  state: ActivityState
}

/**
 * One element of a library message's content array.
 *
 * `ThreadMessageLike['content']` is `string | readonly Part[]` — indexing that union by number
 * would hand back `string | Part` and quietly let a bare string through. Excluding the string arm
 * first is what makes the return type of `convertPart` mean "a part".
 */
export type LibraryPart = Exclude<ThreadMessageLike['content'], string>[number]

const TOOL_NAME = 'activity'

/** The one place a step's wire state becomes a rendered state. */
function activityState(state: 'ok' | 'failed' | 'pending'): ActivityState {
  if (state === 'ok') return 'ok'
  if (state === 'failed') return 'failed'
  // `pending` is a step that started and has not resolved. The group reports itself running when
  // any contained part is running, and that is what drives the live count and the label.
  return 'running'
}

/**
 * Map ONE of our parts onto zero or one library parts.
 *
 * Returns `null` for a part with no rendered form. Exported so the redaction test can assert on
 * the converted object as well as on the DOM — the first is the guarantee, the second only its
 * symptom.
 */
export function convertPart(part: MessagePart): LibraryPart | null {
  if (part.type === 'text') return { type: 'text', text: part.text }

  if (part.type === 'step') {
    // `tool` and `hidden` are deliberately NOT destructured. `tool` is the raw command name and an
    // unrecognised one must never reach the screen as argv — the server's classifier failing
    // closed is the other half of that guarantee. `hidden` is filtered upstream, on both paths,
    // so a hidden step never becomes a part at all.
    const { label, state, seq } = part.step
    const args: ActivityArgs = { label, state: activityState(state) }
    return {
      type: 'tool-call',
      // Stable across every delta that touches this step. If it moved, the group would see a new
      // part each time and the live count would climb while the same step re-rendered.
      toolCallId: `step-${seq}`,
      toolName: TOOL_NAME,
      args,
      // NOTHING ELSE — no `result`, no `artifact`, no `detail`. That omission is R36's wall.
    }
  }

  // `build` / `build_in_progress` / `plan_options` — see the docblock. No element, by omission.
  return null
}

/**
 * The converter handed to `useExternalStoreRuntime`.
 *
 * Signature matches `ExternalStoreMessageConverter<ChatMessage>`: `(message, idx)`. The index is
 * DELIBERATELY IGNORED — the moment identity depends on position, inserting a message renumbers
 * every one after it and the runtime treats the whole tail as new.
 */
export function convertMessage(message: ChatMessage): ThreadMessageLike {
  const content = message.parts
    .map(convertPart)
    .filter((p): p is NonNullable<typeof p> => p !== null)

  return {
    id: message.id,
    role: message.role,
    content,
  }
}

/**
 * Fail loudly on a duplicate id, at the seam, before the runtime can swallow it.
 *
 * The runtime's own behaviour is to keep the last occurrence and `console.warn` — so a collision
 * silently costs a turn, and the only evidence is a line in a console nobody has open. A thrown
 * error naming the id is strictly better than a transcript that quietly lost a message.
 */
export function assertUniqueIds(messages: readonly ChatMessage[]): void {
  const seen = new Set<string>()
  for (const message of messages) {
    if (seen.has(message.id)) {
      throw new Error(
        `Duplicate message id "${message.id}" in the transcript. assistant-ui keeps only the last ` +
          `occurrence and warns, so this would silently drop a turn. Message ids are the server's ` +
          `and must be unique — see messagesFromProjection's composite keys.`,
      )
    }
    seen.add(message.id)
  }
}

/**
 * The library's own rule for when it mints an assistant message of its own, restated so the send
 * path can be tested against the same predicate the runtime uses.
 *
 * `hasUpcomingMessage = isRunning && last.role !== "assistant"`. When this is true the runtime
 * appends an optimistic assistant message carrying an id WE DO NOT CONTROL, which breaks
 * server-owned identity for the whole turn. The send path's job is to make sure a server-owned
 * assistant message is already last at the instant `isRunning` flips true.
 */
export function hasUpcomingMessage(isRunning: boolean, messages: readonly ChatMessage[]): boolean {
  const last = messages[messages.length - 1]
  return isRunning && last?.role !== 'assistant'
}
