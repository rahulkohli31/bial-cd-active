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
 * The send path is download-free for binaries: only the NEWEST turn's image/PDF
 * bytes are inflated (from the in-memory composer via `getNewestBinaryBase64`);
 * historical binaries are dropped; text + office attachments are sticky text
 * (no fetch ever).
 */
import { TEXT_MEDIA_TYPES, OFFICE_MEDIA_TYPES, DECK_MEDIA_TYPES } from './attachmentInput.js'
import { uploadAttachment as defaultUpload, deleteAttachment as defaultDelete } from './attachmentApi.js'

/** Strip characters from a filename that could break out of the `name="..."`
 * attribute (quotes, angle brackets, newlines). Mirrors server `sanitizeFenceName`. */
function sanitizeFenceName(name) {
  return String(name || '').replace(/[\r\n"<>]/g, ' ').slice(0, 200)
}

/** Neutralise any literal `</attachment>` inside fenced DATA so attacker-controlled
 * content (filename or file body) can't close the fence early and have the rest
 * read as instructions. Mirrors server `neutralizeFence`. */
function neutralizeFence(text) {
  return String(text || '').replace(/<\/(attachment)/gi, '<\\/$1')
}

/** The model-facing fence for an office part's extracted text (Decision 3) —
 * MUST match the server's `officeFence` so client/server assembly agree. */
function officeFence(part) {
  return `<attachment name="${sanitizeFenceName(part.name)}" type="${part.format}">\n${neutralizeFence(part.text)}\n</attachment>`
}

/**
 * Decode stored base64 bytes back to text via Uint8Array → TextDecoder (UTF-8)
 * so multibyte content (accents, €, CJK) round-trips; bare `atob` yields latin1.
 * A leading U+FEFF BOM is stripped.
 */
export function decodeBase64Text(b64) {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
  const text = new TextDecoder().decode(bytes)
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text
}

/** Plain prose text from a `parts[]` (display bubble + transcript). Excludes
 * inline-attachment parts (those render as chips) and file parts. Accepts a raw
 * string too (defensive, for any legacy/assistant content). */
export function partsToText(parts) {
  if (typeof parts === 'string') return parts
  if (!Array.isArray(parts)) return ''
  return parts
    .filter((p) => p?.type === 'text' && !p.attachment && typeof p.text === 'string')
    .map((p) => p.text)
    .join('\n')
}

/** The attachment descriptors in a message's parts (file parts + inline-text
 * attachments), for AttachmentChips. */
export function attachmentsFromParts(parts) {
  if (!Array.isArray(parts)) return []
  const out = []
  for (const p of parts) {
    if (p?.type === 'file') {
      const d = { attachmentId: p.attachmentId, kind: p.kind, name: p.name, mediaType: p.mediaType }
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
export function countAttachments(messages) {
  if (!Array.isArray(messages)) return 0
  return messages.reduce(
    (n, m) => n + (m?.parts || []).filter((p) => p?.type === 'file' || (p?.type === 'text' && p?.attachment)).length,
    0,
  )
}

/** Build one message's Anthropic `content` from its parts (string when no
 * attachment blocks are emitted — the unchanged path). */
function buildContent(parts, isNewest, getNewestBinaryBase64) {
  if (!Array.isArray(parts)) return ''
  const blocks = []
  const prose = []
  for (const p of parts) {
    if (p?.type === 'text') {
      if (p.attachment) {
        // Inline text attachment: STICKY (re-sent every turn), fenced as DATA so
        // the model never reads it as instructions. Sanitise the name + content
        // so neither can break out of the fence (same guard as office).
        blocks.push({
          type: 'text',
          text: `<attachment name="${sanitizeFenceName(p.attachment.name)}" type="text">\n${neutralizeFence(p.text)}\n</attachment>`,
        })
      } else if (typeof p.text === 'string') {
        prose.push(p.text)
      }
    } else if (p?.type === 'file') {
      if (p.kind === 'office') {
        // STICKY extracted text, every turn; the original bytes are never inlined.
        blocks.push({ type: 'text', text: officeFence(p) })
        continue
      }
      if (p.kind === 'deck') {
        // STICKY vision document block referencing the INTERNAL Files-API PDF by
        // file_id (cheap to re-send every turn, cheap to re-read under the cache;
        // cache_control makes follow-ups ~0.1x). No base64; the user only sees .pptx.
        if (p.pdfFileId) {
          blocks.push({
            type: 'document',
            source: { type: 'file', file_id: p.pdfFileId },
            cache_control: { type: 'ephemeral' },
          })
        }
        continue
      }
      if (!isNewest) continue // historical binary dropped — the model already saw it
      const data = getNewestBinaryBase64(p.attachmentId)
      if (!data) continue // bytes unavailable → skip rather than a null-data block
      if (p.kind === 'document' || p.mediaType === 'application/pdf') {
        blocks.push({ type: 'document', source: { type: 'base64', media_type: 'application/pdf', data } })
      } else {
        blocks.push({ type: 'image', source: { type: 'base64', media_type: p.mediaType, data } })
      }
    }
  }
  const proseText = prose.join('\n')
  if (blocks.length === 0) return proseText // plain string
  blocks.push({ type: 'text', text: proseText }) // text after files (Anthropic ordering)
  return blocks
}

/**
 * Anthropic permits at most 4 `cache_control` breakpoints PER REQUEST. Each deck
 * emits a sticky `document` block that would otherwise carry its own marker, so a
 * conversation with 5+ decks would exceed the cap and every send would 400. A
 * single trailing breakpoint caches the whole prefix anyway, so keep
 * `cache_control` on ONLY the last deck block across the assembled request and
 * strip it from every earlier one. Mutates the blocks in place (they were just
 * built here, so this is safe) and returns the same array.
 */
function capDeckCacheBreakpoints(apiMessages) {
  const deckBlocks = []
  for (const m of apiMessages) {
    if (!Array.isArray(m.content)) continue
    for (const b of m.content) {
      if (b?.type === 'document' && b.source?.type === 'file' && b.cache_control) deckBlocks.push(b)
    }
  }
  for (let i = 0; i < deckBlocks.length - 1; i += 1) delete deckBlocks[i].cache_control
  return apiMessages
}

/**
 * Map a conversation's `parts[]` messages to the API `{ role, content }` shape.
 * SYNCHRONOUS and download-free: the newest turn's image/PDF bytes come from
 * `getNewestBinaryBase64(attachmentId)` (the in-memory composer), historical
 * binaries are dropped, text attachments are inline.
 */
export function assembleApiMessages(messages, getNewestBinaryBase64 = () => undefined) {
  const lastIdx = messages.length - 1
  return capDeckCacheBreakpoints(
    messages.map((m, i) => ({
      role: m.role,
      content: buildContent(m.parts, i === lastIdx, getNewestBinaryBase64),
    })),
  )
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
export async function buildUserParts(text, pendingAttachments = [], upload = defaultUpload) {
  const parts = []
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
      parts.push({
        type: 'file',
        kind: 'office',
        format: ref.format,
        attachmentId: ref.attachmentId,
        key: ref.key,
        name: ref.name,
        mediaType: ref.mediaType,
        size: ref.size,
        text: ref.text,
        truncated: ref.truncated,
        truncationNote: ref.truncationNote,
      })
    } else if (DECK_MEDIA_TYPES.has(a.mediaType)) {
      // The server converts the deck to a PDF, uploads it to the Files API, and
      // returns the original-bytes ref PLUS the internal pdfFileId/pageCount. The
      // deck part carries pdfFileId (→ model, sticky vision block) and the .pptx
      // ref (→ chip / re-download). No base64 ever reaches the deck send path.
      const ref = await upload({ attachmentId: a.id, name: a.name, mediaType: a.mediaType, size: a.size, base64: a.base64 })
      parts.push({
        type: 'file',
        kind: 'deck',
        attachmentId: ref.attachmentId,
        key: ref.key,
        name: ref.name,
        mediaType: ref.mediaType,
        size: ref.size,
        pdfFileId: ref.pdfFileId,
        pageCount: ref.pageCount,
      })
    } else {
      const ref = await upload({ attachmentId: a.id, name: a.name, mediaType: a.mediaType, size: a.size, base64: a.base64 })
      parts.push({ type: 'file', attachmentId: ref.attachmentId, key: ref.key, kind: ref.kind, name: ref.name, mediaType: ref.mediaType, size: ref.size })
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
export function releaseUploadedAttachments(parts, del = defaultDelete) {
  if (!Array.isArray(parts)) return
  for (const p of parts) {
    if (p?.type !== 'file' || typeof p.attachmentId !== 'string') continue
    Promise.resolve(del(p.attachmentId, { pdfFileId: p.kind === 'deck' ? p.pdfFileId : undefined })).catch(() => {})
  }
}
