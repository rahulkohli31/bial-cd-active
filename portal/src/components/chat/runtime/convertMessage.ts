/**
 * THE SEAM. Above it everything is the library's; below it everything is the server's. One
 * function maps our `ChatMessage` onto assistant-ui's `ThreadMessageLike`; the live stream and the
 * reload projection produce the SAME shape, so nothing branches on chat kind and a live message
 * renders identically to a reloaded one.
 *
 * WHAT A PART BECOMES: `text` → text part; `step` → tool-call, LABEL AND STATE ONLY; `reasoning` →
 * the platform's status sentence only (`ReasoningPart` has no field for real reasoning text);
 * everything else (`build`, `build_in_progress`, `plan_options`) drops — a message whose parts all
 * drop still exists, empty, unrendered. THE REDACTION WALL IS HERE, NOT AT THE DRAW SITE: a step
 * becomes `toolName` + `state`, NOTHING ELSE — no `args`/`result`/`detail` — so an expander has
 * nothing to leak even if it later renders every field a part holds.
 *
 * WHY THIS EXISTS — five identity traps, verified in the installed 0.15.17 source: (1) omitting
 * `id` falls back to array index, so always pass the server's composite key
 * (`srv_{seq}_{kind}_{index}`, since `seq` alone collides); (2) duplicate ids silently DROP a
 * message, so `assertUniqueIds` throws instead; (3) the library mints an id itself when `isRunning
 * && last.role !== "assistant"`, so identity stays server-owned by construction on the send path
 * (`hasUpcomingMessage` restates that predicate for its test); (4) the converter caches on OBJECT
 * IDENTITY (a WeakMap), so mutating in place is invisible — every streamed update needs a NEW
 * object for the changed message and unchanged identity for the rest; (5) `setMessages` is never
 * provided, since supplying it switches on `switchToBranch`/`delete`.
 */
import type { ChatMessage, MessagePart } from '../../../utils/messageTypes'
import { attachmentsFromParts } from '../../../utils/attachmentStore'
import type { ThreadMessageLike } from '@assistant-ui/react'

/** What a step's state becomes on the tool-call part the group renders. */
export type ActivityState = 'running' | 'ok' | 'failed'

/**
 * The tool-call args we allow onto a part. Deliberately a closed shape rather than the step's
 * own fields: this object IS what an expander can render, so it holds only what a row may read
 * — the server's friendly label and the state.
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
 * The platform's own status sentence, carried into the library as the reasoning part's text.
 * A blank `text`+`unstable_summary` is dropped by `fromThreadMessageLike`, so this constant is
 * the smallest fix that satisfies that without inventing a real field — `ReasoningPart` has
 * nowhere else to put reasoning text. Never actually rendered (`ReasoningGroup` draws its own
 * line, ignoring children), but it is the same words the status line already shows, so if that
 * stopped being true this would fail safe onto the correct sentence, not an unrecognised one.
 */
const REASONING_STATUS_TEXT = 'Working on your app'

/** The one place a step's wire state becomes a rendered state. */
function activityState(state: 'ok' | 'failed' | 'pending'): ActivityState {
  if (state === 'ok') return 'ok'
  if (state === 'failed') return 'failed'
  // `pending` is a step that started and has not resolved. The group reports itself running when
  // any contained part is running while its message is still being written, and that is what
  // drives the live count and the label.
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
      // NOTHING ELSE — no `result`, no `artifact`, no `detail`. That omission is the redaction wall.
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
 * The library's own rule for minting an assistant message of its own, restated so the send
 * path can be tested against the same predicate the runtime uses: `isRunning && last.role
 * !== "assistant"`. True means an optimistic assistant message appends with an id WE DO NOT
 * CONTROL, breaking server-owned identity — so the send path's job is making sure a
 * server-owned assistant message is already last the instant `isRunning` flips true.
 */
export function hasUpcomingMessage(isRunning: boolean, messages: readonly ChatMessage[]): boolean {
  const last = messages[messages.length - 1]
  return isRunning && last?.role !== 'assistant'
}
