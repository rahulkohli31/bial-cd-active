/**
 * Parts-model transform helpers (bytes live server-side via attachmentApi.ts). Maps `parts[]` onto
 * the Anthropic request shape and onto display. A part this composer MINTS is prose, or a `file`
 * whose bytes sit in the object store — there is one producer now, and it is the uploaded one.
 *
 * OLDER PART SHAPES STILL ARRIVE FROM HISTORY and are still rendered: an inline text attachment,
 * an `office` part carrying server-extracted Markdown, a `deck` part naming a converted PDF by
 * `pdfFileId`. Nothing produces them any more, and the readers stay because a transcript written
 * before this change is still a transcript someone opens.
 *
 * The send path is byte-free: the browser sends only the new message — prose and OWNED refs for
 * stored binaries (`wireMessageFromParts`); the server rehydrates bytes and replays history from
 * its own store.
 */
import type { PendingAttachment } from './attachmentInput'
import { uploadAttachment as defaultUpload, deleteAttachment as defaultDelete } from './attachmentApi'
import type { MessagePart, TextPart } from './messageTypes'

/** The chip descriptor `attachmentsFromParts` builds — traced from its one real
 * consumer, `AttachmentChips.tsx`'s own doc comment: `{ attachmentId, kind,
 * name, mediaType, format?, truncated? }`, plus `truncationNote` (read here,
 * used for the chip's tooltip). */
export interface AttachmentDescriptor {
  attachmentId: string
  kind: string
  name: string
  mediaType: string
  format?: string
  truncated?: boolean
  truncationNote?: string
}

/** The stateless wire message `wireMessageFromParts` resolves to. */
export interface WireMessage {
  text: string
  attachmentTexts?: string[]
  attachmentIds?: string[]
}

/**
 * Decode stored base64 bytes back to text via Uint8Array → TextDecoder (UTF-8)
 * so multibyte content (accents, €, CJK) round-trips; bare `atob` yields latin1.
 * A leading U+FEFF BOM is stripped.
 */
export function decodeBase64Text(b64: string): string {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
  const text = new TextDecoder().decode(bytes)
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text
}

/** Plain prose text from a `parts[]` (display bubble + transcript). Excludes
 * inline-attachment parts (those render as chips) and file parts. Accepts a raw
 * string too (defensive, for any legacy/assistant content). */
export function partsToText(parts: MessagePart[] | string): string {
  if (typeof parts === 'string') return parts
  if (!Array.isArray(parts)) return ''
  return parts
    .filter((p): p is TextPart => p?.type === 'text' && !p.attachment && typeof p.text === 'string')
    .map((p) => p.text)
    .join('\n')
}

/** The attachment descriptors in a message's parts (file parts + inline-text
 * attachments), for AttachmentChips. */
export function attachmentsFromParts(parts: MessagePart[]): AttachmentDescriptor[] {
  if (!Array.isArray(parts)) return []
  const out: AttachmentDescriptor[] = []
  for (const p of parts) {
    if (p?.type === 'file') {
      const d: AttachmentDescriptor = { attachmentId: p.attachmentId, kind: p.kind, name: p.name, mediaType: p.mediaType }
      if (p.kind === 'office') {
        d.format = p.format // drives the Word/Excel chip icon
        d.truncated = p.truncated // chip shows a "truncated" note when set
        if (p.truncationNote) d.truncationNote = p.truncationNote // human-readable detail for the tooltip
      }
      // Deck: surface ONLY name/kind/mediaType (the .pptx). pdfFileId/pageCount are
      // internal plumbing and must never reach a user-visible field (invisible
      // conversion), so they're deliberately omitted from the chip descriptor.
      out.push(d)
    } else if (p?.type === 'text' && p.attachment) {
      out.push({ attachmentId: p.attachment.attachmentId, kind: 'text', name: p.attachment.name, mediaType: p.attachment.mediaType })
    }
  }
  return out
}

/** Total attachment count across a conversation's messages (for the per-
 * conversation cap). Counts file parts + inline-text attachment parts. */
export function countAttachments(messages: unknown): number {
  if (!Array.isArray(messages)) return 0
  return (messages as Array<{ parts?: MessagePart[] }>).reduce(
    (n, m) => n + (m?.parts || []).filter((p) => p?.type === 'file' || (p?.type === 'text' && p?.attachment)).length,
    0,
  )
}

/**
 * Map ONE user turn's `parts[]` to the stateless wire message `{ text, attachmentTexts,
 * attachmentIds }`. The full-transcript Anthropic assembly is gone — the server loads
 * history itself, so the browser sends only the NEW message:
 *  - typed prose → `text`.
 *  - file parts → `attachmentIds` (owned refs; the SERVER rehydrates bytes for the model lane
 *    and writes code-lane files into the workspace).
 *
 * Nothing is written into `attachmentTexts`. The field stays on the wire type because readers
 * still accept it, but every attachment is an uploaded file now — see the two branches below
 * for why their filters outlive the producers that fed them.
 */
export function wireMessageFromParts(parts: MessagePart[]): WireMessage {
  const attachmentIds: string[] = []
  const prose: string[] = []
  if (Array.isArray(parts)) {
    for (const p of parts) {
      if (p?.type === 'text') {
        // NO FENCE BRANCH ANY MORE. A text attachment used to be read in the browser and
        // pushed into the prompt as an `<attachment ...>` block; every attachment is an uploaded
        // file now, so nothing mints a text part carrying `.attachment`.
        //
        // THE FILTER STAYS, and it is not the same thing as the producer. Conversations already
        // on disk carry these parts, and `convertMessage`'s twin is the only thing keeping a
        // stored CSV body out of the citizen's own message bubble - the server-side fence check
        // has the same job and the same reason for outliving its producer.
        if (!p.attachment && typeof p.text === 'string') {
          prose.push(p.text)
        }
      } else if (p?.type === 'file') {
        // NO `kind` branch any more. The office fence and the `kind !== 'deck'` filter both went
        // with the producers below: a filter that excludes a part nothing can mint is a guard
        // against a state that cannot occur, and it reads as though the state still can.
        if (typeof p.attachmentId === 'string') attachmentIds.push(p.attachmentId)
      }
    }
  }
  const message: WireMessage = { text: prose.join('\n') }
  if (attachmentIds.length > 0) message.attachmentIds = attachmentIds
  return message
}

/**
 * Build a user turn's `parts[]` from the composer: uploads every attachment via `upload`,
 * returning a file ref for each; typed prose becomes the final text part. Attachment parts
 * come first (chips above text, and Anthropic file-before-text ordering). An upload failure
 * propagates so the caller can abort the send. `conversationId` stamps each upload with the
 * thread it belongs to, which is what scopes the per-conversation count and what the reader
 * is later pointed at.
 */
export async function buildUserParts(
  text: string,
  pendingAttachments: PendingAttachment[] = [],
  upload: typeof defaultUpload = defaultUpload,
  conversationId?: string,
): Promise<MessagePart[]> {
  const parts: MessagePart[] = []
  // ONE PATH, FOR EVERY FORMAT. Three producers used to branch here — an inline text part, an
  // office part and a deck part — and all three are gone with the media-type sets that fed them.
  // The last to go was the inline arm, whose set had been emptied so its readers could migrate
  // one at a time; a branch whose condition is permanently false is a claim that some file still
  // travels inside the prompt, and none does.
  //
  // TWO THINGS FALL OUT, and both are gains rather than side effects: the office branch used to
  // run BEFORE the magic-byte check, so that check is now the sole gate for every uploaded file;
  // and extraction ran before storage, so a rejected file never orphaned a blob — an ordering
  // that survives because there is no extraction left to order.
  for (const a of pendingAttachments) {
    const ref = await upload({ attachmentId: a.id, name: a.name, mediaType: a.mediaType, size: a.size, base64: a.base64, conversationId })
    // UNCHECKED, matching pre-migration behaviour: `AttachmentRef`'s fields are what the server
    // actually guarantees for an upload — trusted here, not re-validated.
    parts.push({
      type: 'file',
      attachmentId: ref.attachmentId,
      key: ref.key,
      kind: ref.kind as 'image' | 'document',
      name: ref.name,
      mediaType: ref.mediaType,
      size: ref.size,
    })
  }
  parts.push({ type: 'text', text })
  return parts
}

/**
 * Best-effort release of the attachments `buildUserParts` already uploaded for a
 * turn that then FAILED to persist/send. Without this, a deck's Files-API PDF +
 * stored `.pptx` (and any image/PDF object) would orphan server-side. Each delete
 * is fire-and-forget and swallows its own error, so cleanup can never throw into —
 * or mask — the original send failure. Decks forward `pdfFileId` so the route also
 * releases the internal converted PDF.
 */
export function releaseUploadedAttachments(parts: unknown, del: typeof defaultDelete = defaultDelete): void {
  if (!Array.isArray(parts)) return
  for (const p of parts as MessagePart[]) {
    if (p?.type !== 'file' || typeof p.attachmentId !== 'string') continue
    Promise.resolve(del(p.attachmentId, { pdfFileId: p.kind === 'deck' ? p.pdfFileId : undefined })).catch(() => {})
  }
}
