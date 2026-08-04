/**
 * Parts-model transform helpers (the byte store is gone — bytes live server-side
 * via attachmentApi.js). This module knows how the neutral `parts[]` content
 * model maps onto the Anthropic request shape and onto display.
 *
 * A message's `parts[]` is one of:
 *   - { type:'text', text }                              — user/assistant prose
 *   - { type:'text', text, attachment:{attachmentId,name,mediaType,size} }
 *                                                        — an inline csv/txt
 *                                                          attachment (content in
 *                                                          `text`, shown as a chip,
 *                                                          re-inlined every turn)
 *   - { type:'file', attachmentId, key, kind:'image'|'document', name, mediaType, size }
 *                                                        — image/PDF bytes in the
 *                                                          object store
 *   - { type:'file', kind:'office', format:'word'|'excel', attachmentId, key,
 *       name, mediaType, size, text, truncated }
 *                                                        — a HYBRID: the original
 *                                                          .docx/.xlsx bytes live
 *                                                          in the object store (chip
 *                                                          re-downloads them) but are
 *                                                          NEVER sent to the model;
 *                                                          the server-extracted
 *                                                          Markdown (`text`) is sent
 *                                                          as a sticky text block.
 *   - { type:'file', kind:'deck', attachmentId, key, name, mediaType, size,
 *       pdfFileId, pageCount }
 *                                                        — a .pptx: the original
 *                                                          bytes live in the object
 *                                                          store (chip re-downloads
 *                                                          them); the model sees a
 *                                                          sticky vision `document`
 *                                                          block referencing the
 *                                                          INTERNAL converted PDF by
 *                                                          `pdfFileId` (never the
 *                                                          .pptx, never base64). The
 *                                                          PDF is invisible to the
 *                                                          user — only the .pptx is
 *                                                          ever surfaced.
 *
 * The send path is byte-free entirely (U7): the browser sends only the new message —
 * typed prose, fenced attachment text, and OWNED refs for stored binaries
 * (`wireMessageFromParts`); the server rehydrates bytes and replays history from its
 * own store.
 */
import { TEXT_MEDIA_TYPES, OFFICE_MEDIA_TYPES, DECK_MEDIA_TYPES } from './attachmentInput'
import type { PendingAttachment } from './attachmentInput'
import { uploadAttachment as defaultUpload, deleteAttachment as defaultDelete } from './attachmentApi'
import type { MessagePart, FilePartOffice, TextPart } from './messageTypes'

/** The chip descriptor `attachmentsFromParts` builds — traced from its one real
 * consumer, `AttachmentChips.jsx`'s own doc comment: `{ attachmentId, kind,
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

/** The U7 stateless wire message `wireMessageFromParts` resolves to. */
export interface WireMessage {
  text: string
  attachmentTexts?: string[]
  attachmentIds?: string[]
}

/** Strip characters from a filename that could break out of the `name="..."`
 * attribute (quotes, angle brackets, newlines). Mirrors server `sanitizeFenceName`. */
function sanitizeFenceName(name: string): string {
  return String(name || '').replace(/[\r\n"<>]/g, ' ').slice(0, 200)
}

/** Neutralise any literal `</attachment>` inside fenced DATA so attacker-controlled
 * content (filename or file body) can't close the fence early and have the rest
 * read as instructions. Mirrors server `neutralizeFence`. */
function neutralizeFence(text: string): string {
  return String(text || '').replace(/<\/(attachment)/gi, '<\\/$1')
}

/** The model-facing fence for an office part's extracted text (Decision 3) —
 * MUST match the server's `officeFence` so client/server assembly agree. */
function officeFence(part: FilePartOffice): string {
  return `<attachment name="${sanitizeFenceName(part.name)}" type="${part.format}">\n${neutralizeFence(part.text)}\n</attachment>`
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
 * Map ONE user turn's `parts[]` to the U7 stateless wire message:
 * `{ text, attachmentTexts, attachmentIds }`.
 *
 * The full-transcript Anthropic assembly (`assembleApiMessages`) died with R9 — the server
 * loads history from its own store, so the browser sends only the NEW message:
 *  - typed prose → `text`
 *  - inline text attachments + office extractions → complete `<attachment>` fences in
 *    `attachmentTexts` (the same sanitize/neutralize guards as before — the server treats
 *    them as opaque data blocks)
 *  - image/PDF file parts → `attachmentIds` (owned refs; the SERVER rehydrates the stored
 *    bytes at send — no base64 rides the chat body any more)
 *  - deck parts → dropped (deck attachments are disabled server-side; the retired
 *    Files-API `file_id` path has no stateless equivalent)
 */
export function wireMessageFromParts(parts: MessagePart[]): WireMessage {
  const attachmentTexts: string[] = []
  const attachmentIds: string[] = []
  const prose: string[] = []
  if (Array.isArray(parts)) {
    for (const p of parts) {
      if (p?.type === 'text') {
        if (p.attachment) {
          attachmentTexts.push(
            `<attachment name="${sanitizeFenceName(p.attachment.name)}" type="text">\n${neutralizeFence(p.text)}\n</attachment>`,
          )
        } else if (typeof p.text === 'string') {
          prose.push(p.text)
        }
      } else if (p?.type === 'file') {
        if (p.kind === 'office') {
          attachmentTexts.push(officeFence(p))
        } else if (p.kind !== 'deck' && typeof p.attachmentId === 'string') {
          attachmentIds.push(p.attachmentId)
        }
      }
    }
  }
  const message: WireMessage = { text: prose.join('\n') }
  if (attachmentTexts.length > 0) message.attachmentTexts = attachmentTexts
  if (attachmentIds.length > 0) message.attachmentIds = attachmentIds
  return message
}

/**
 * Build a user turn's `parts[]` from the composer: uploads each image/PDF (via
 * `upload`, returning a file ref) and inlines each csv/txt as a text-attachment
 * part; the typed prose becomes the final text part. Attachment parts come first
 * (display chips above text, and Anthropic file-before-text ordering at assembly).
 * An upload failure propagates so the caller can abort the send.
 *
 * @param {string} text                       the typed message
 * @param {Array}  pendingAttachments         [{id,name,mediaType,size,base64}]
 * @param {Function} [upload]                  uploadAttachment (injectable for tests)
 */
export async function buildUserParts(
  text: string,
  pendingAttachments: PendingAttachment[] = [],
  upload: typeof defaultUpload = defaultUpload,
): Promise<MessagePart[]> {
  const parts: MessagePart[] = []
  for (const a of pendingAttachments) {
    if (TEXT_MEDIA_TYPES.has(a.mediaType)) {
      parts.push({
        type: 'text',
        text: decodeBase64Text(a.base64),
        attachment: { attachmentId: a.id, name: a.name, mediaType: a.mediaType, size: a.size },
      })
    } else if (OFFICE_MEDIA_TYPES.has(a.mediaType)) {
      // The server stores the original AND returns the extracted Markdown (`text`);
      // the office part carries that text (→ model, sticky) plus the stored ref
      // (→ chip / re-download). The original bytes are never sent to the model.
      const ref = await upload({ attachmentId: a.id, name: a.name, mediaType: a.mediaType, size: a.size, base64: a.base64 })
      // UNCHECKED (matches pre-migration behavior): AttachmentRef's format/text/
      // truncated are typed loosely (shared across image/office/deck refs) since
      // that's what the server ACTUALLY guarantees for an office upload — trusted,
      // not re-validated here.
      parts.push({
        type: 'file',
        kind: 'office',
        format: ref.format as 'word' | 'excel',
        attachmentId: ref.attachmentId,
        key: ref.key,
        name: ref.name,
        mediaType: ref.mediaType,
        size: ref.size,
        text: ref.text as string,
        truncated: ref.truncated as boolean,
        truncationNote: ref.truncationNote,
      })
    } else if (DECK_MEDIA_TYPES.has(a.mediaType)) {
      // The server converts the deck to a PDF, uploads it to the Files API, and
      // returns the original-bytes ref PLUS the internal pdfFileId/pageCount. The
      // deck part carries pdfFileId (→ model, sticky vision block) and the .pptx
      // ref (→ chip / re-download). No base64 ever reaches the deck send path.
      const ref = await upload({ attachmentId: a.id, name: a.name, mediaType: a.mediaType, size: a.size, base64: a.base64 })
      // UNCHECKED (matches pre-migration behavior) — see the office branch above.
      parts.push({
        type: 'file',
        kind: 'deck',
        attachmentId: ref.attachmentId,
        key: ref.key,
        name: ref.name,
        mediaType: ref.mediaType,
        size: ref.size,
        pdfFileId: ref.pdfFileId as string,
        pageCount: ref.pageCount as number,
      })
    } else {
      const ref = await upload({ attachmentId: a.id, name: a.name, mediaType: a.mediaType, size: a.size, base64: a.base64 })
      // UNCHECKED (matches pre-migration behavior) — see the office branch above.
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
