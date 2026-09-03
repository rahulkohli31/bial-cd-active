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
 *     can fix. `hasUpcomingMessage` restates the library's own predicate here, where the ordering
 *     is reasoned about, and its test pins it — the send path SATISFIES the rule by construction
 *     rather than by consulting it, so nothing outside that test calls it.
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
 * `text`      → a text part. Prose is prose.
 * `step`      → a `tool-call` part carrying LABEL AND STATE ONLY (R36; see the redaction note).
 * `reasoning` → a reasoning part carrying the platform's own status sentence and nothing else.
 *               The library's own renderer for that kind is reached only when a message actually
 *               carries one, which is why a boolean on the turn cannot drive the working status
 *               on its own. The status-only guarantee is structural rather than a promise: OUR
 *               part has nowhere for reasoning text to sit — see `REASONING_STATUS_TEXT`.
 * others      → nothing. `build`, `build_in_progress` and `plan_options` are not transcript prose:
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
import { attachmentsFromParts } from '../../../utils/attachmentStore'
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

/**
 * What a reasoning part carries into the library, and it is the PLATFORM's sentence.
 *
 * THE LIBRARY REFUSES AN EMPTY ONE. `fromThreadMessageLike` drops a reasoning part whose `text`
 * and `unstable_summary` are both blank, so a genuinely content-free part never reaches the
 * renderer and the status would simply never appear. This is the smallest thing that satisfies
 * that requirement without weakening the guarantee: our own `ReasoningPart` still has NO field
 * for reasoning text — the wire never carries any, and there is nowhere to put any — and what
 * crosses here is a constant.
 *
 * IT IS NEVER RENDERED. `ReasoningGroup` draws its own line and ignores its children, and the
 * `reasoning` part itself falls to the thread's `default: return null`. The words are the same
 * words the status line shows anyway, so the one case where that stopped being true would put
 * the correct sentence on screen rather than a placeholder — fail-safe rather than a marker
 * nobody would recognise.
 */
const REASONING_STATUS_TEXT = 'Working on your app'

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
  // AN ATTACHMENT-BEARING TEXT PART IS NOT PROSE. `buildUserParts` puts the whole decoded file in
  // `text` for a csv/txt and hangs the descriptor off `attachment`; the descriptor draws a chip and
  // the body goes to the model, but it is never something the citizen typed. `partsToText` has
  // always filtered it with the same `!p.attachment` test — without the filter here a staged
  // spreadsheet renders into the bubble row by row.
  if (part.type === 'text') return part.attachment ? null : { type: 'text', text: part.text }

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

  // A CONTENT-FREE REASONING PART, and the emptiness of OUR shape is the guarantee.
  // `part.type === 'reasoning'` carries no text — `ReasoningPart` has no field for it — so this
  // cannot leak reasoning content even if a later change starts putting it on the wire. What it
  // buys is the library's grouping: a message containing one is filed under the chain-of-thought
  // key, which is what reaches the status renderer. See `REASONING_STATUS_TEXT` for why the
  // library will not take a literally empty one.
  if (part.type === 'reasoning') return { type: 'reasoning', text: REASONING_STATUS_TEXT }

  // `file` parts (image/PDF) have no library part either — like the inline-text attachments above
  // they are carried as descriptors on the message and drawn as chips, not as content.
  //
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
  const seen = new Set<string>()
  const content = message.parts
    .map(convertPart)
    .filter((p): p is NonNullable<typeof p> => p !== null)
    .map((part, index) => {
      // UNIQUE TOOL-CALL IDS WITHIN A MESSAGE, enforced here rather than trusted.
      //
      // `toolCallId` is `step-{seq}`, and a collision is reachable two ways: a stored row whose
      // `seq` is missing (both become `step-undefined`), and a merged run of stored rows that
      // spans two turns whose seq spaces restart. The library groups and keys parts by this id, so
      // a collision does not render twice — it renders ONCE and silently loses the other step,
      // which is the same class of quiet loss `assertUniqueIds` refuses at the message level.
      //
      // Suffixed with the INDEX WITHIN THIS MESSAGE, which is stable for a given message object:
      // the converter is memoised on message identity, so the same message always yields the same
      // ids, and only a message that genuinely changed gets new ones.
      if (part.type !== 'tool-call') return part
      // `toolCallId` is optional on the library's type. `convertPart` always sets it, and an
      // absent one is the same collision hazard as a repeated one — every part missing it would
      // share the key `undefined` — so the two cases are handled together rather than separately.
      const id = part.toolCallId ?? 'step'
      if (part.toolCallId !== undefined && !seen.has(id)) {
        seen.add(id)
        return part
      }
      return { ...part, toolCallId: `${id}-${index}` }
    })

  // THE ATTACHMENTS RIDE BESIDE THE CONTENT, NOT IN IT. Both shapes that carry one convert to no
  // library part — the inline-text kind because its `text` is the file itself, the `file` kind
  // because it has no textual form at all — so without this the transcript simply forgets that a
  // citizen attached anything. `metadata.custom` is the library's own escape hatch for a host's
  // data, which keeps our attachment pipeline ours (no `AttachmentAdapter`) and the thread a
  // renderer.
  const attachments = attachmentsFromParts(message.parts)

  return {
    id: message.id,
    role: message.role,
    content,
    // Omitted entirely when there is nothing to carry, so an ordinary message converts to exactly
    // what it did before.
    ...(attachments.length > 0 ? { metadata: { custom: { attachments } } } : {}),
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
