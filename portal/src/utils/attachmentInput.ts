/**
 * Pure helpers for the chat attachment composer (shared by ChatPage and
 * BuilderPage). Validation + base64 reading + ref-building live here so the
 * composer logic is testable without a DOM render. The real trust boundary is
 * the server (media-type allowlist + magic-byte check); these checks are UX.
 */
import { DECK_ATTACHMENTS_ENABLED } from '../config/features.js'

// The two OOXML (Office) media types. Word/Excel are uploaded like image/PDF
// binaries, but the SERVER extracts them to Markdown and the model only ever sees
// that text (sticky) — the original bytes are stored for re-download, never sent
// to Claude. See attachmentStore (office part) and server/office-extract.js.
export const WORD_MEDIA_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
export const EXCEL_MEDIA_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
export const OFFICE_MEDIA_TYPES = new Set([WORD_MEDIA_TYPE, EXCEL_MEDIA_TYPE])

// PowerPoint (.pptx). A deck is a VISUAL medium, so unlike Word/Excel it is NOT
// text-extracted: the server renders it to a PDF and the model reads that with
// vision. The original .pptx is the ONLY user-facing artifact (stored for
// re-download); the conversion is invisible to the user. See attachmentStore
// (deck part), server/deck-convert.js, and the plan's "invisible" user story.
export const PPTX_MEDIA_TYPE = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
export const DECK_MEDIA_TYPES = new Set([PPTX_MEDIA_TYPE])

// Native Anthropic image/document types, inline text files (CSV/plain-text), and
// Office docs (docx/xlsx — server-extracted to text). Text files aren't native
// documents — they travel as fenced inline text parts (see
// attachmentStore.buildUserParts), but they share this allowlist so the validator
// and OS file picker accept them.
// PowerPoint (.pptx) is OFFERED only when the deck feature is on.
// resolveMediaType + the legacy-.ppt reject below are flag-independent; the
// SERVER is the authoritative gate (a clean 501 when off).
export const ALLOWED_MEDIA_TYPES = [
  'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'application/pdf',
  'text/csv', 'text/plain', WORD_MEDIA_TYPE, EXCEL_MEDIA_TYPE,
  ...(DECK_ATTACHMENTS_ENABLED ? [PPTX_MEDIA_TYPE] : []),
]
// Text media types are special-cased everywhere binary attachments are: inlined
// as text blocks (sticky across turns), sized by bytes in the context estimate,
// and previewed as a labelled icon (no thumbnail).
export const TEXT_MEDIA_TYPES = new Set(['text/csv', 'text/plain'])
// Extension tokens let the OS picker show .csv/.txt/.docx/.xlsx (+ .pptx when the
// deck feature is on) even when the OS reports an inconsistent/empty MIME (see
// resolveMediaType).
export const ACCEPT_ATTR = [
  ...ALLOWED_MEDIA_TYPES, '.csv', '.txt', '.docx', '.xlsx',
  ...(DECK_ATTACHMENTS_ENABLED ? ['.pptx'] : []),
].join(',')

/** `'word' | 'excel' | null` for a media type — drives the Office chip icon. */
export function officeFormat(mediaType: string): 'word' | 'excel' | null {
  if (mediaType === WORD_MEDIA_TYPE) return 'word'
  if (mediaType === EXCEL_MEDIA_TYPE) return 'excel'
  return null
}

export const MAX_FILE_SIZE = 4 * 1024 * 1024 // 4 MB on the original File.size (image/PDF)
// Text files are inlined verbatim into the prompt, so they're capped far lower
// than binary attachments: 256 KB per file and 512 KB total across one selection
// keep accumulated inline text under the context warn/truncation budgets.
export const MAX_TEXT_FILE_SIZE = 256 * 1024
export const MAX_TEXT_BYTES_PER_CONVERSATION = 512 * 1024
export const MAX_FILES_PER_MESSAGE = 5
// Cumulative cap across a whole conversation (all turns). Distinct from the
// per-message cap above and the per-user 50 MB object-store cap (enforced
// server-side); checked at send time where the full conversation is visible.
export const MAX_ATTACHMENTS_PER_CONVERSATION = 20

// Legacy `.doc` (binary Word 97-2003) is NOT supported — mammoth only reads the
// OOXML `.docx`. Surface a clear, honest message rather than a confusing parse
// failure server-side.
export const LEGACY_DOC_REJECT_MSG = 'Legacy .doc files aren\'t supported — please save as .docx (or PDF) and re-upload.'

// Legacy `.ppt` (binary PowerPoint 97-2003) is NOT supported — only the OOXML
// `.pptx`. Clear message rather than a confusing server-side failure. (No "PDF"
// hint here: the user must never learn we convert decks to PDF internally.)
export const LEGACY_PPT_REJECT_MSG = 'Legacy .ppt files aren\'t supported — please save as .pptx and re-upload.'

/**
 * Canonicalize a file's media type by extension first. Browsers/OSes report
 * Office and text types inconsistently (`.csv` as `text/csv`,
 * `application/vnd.ms-excel`, or empty; `.docx`/`.xlsx` often with an empty or
 * generic MIME), so resolving by extension is the reliable signal. All allowlist
 * + size-cap + stored-ref decisions run against this resolved type, never raw
 * `file.type`.
 */
export function resolveMediaType(file: File): string {
  const name = file.name || ''
  if (/\.csv$/i.test(name)) return 'text/csv'
  if (/\.txt$/i.test(name)) return 'text/plain'
  if (/\.docx$/i.test(name)) return WORD_MEDIA_TYPE
  if (/\.xlsx$/i.test(name)) return EXCEL_MEDIA_TYPE
  if (/\.pptx$/i.test(name)) return PPTX_MEDIA_TYPE
  return file.type
}

/**
 * Validate a batch of newly selected files against the per-message rules.
 * Returns `{ error }` with a user-facing message on the first violation, or
 * `{ ok: true }` when all pass. The media type is RESOLVED first (so an
 * OS-mislabeled CSV isn't rejected before canonicalization), and both the
 * allowlist check and the size cap run against that resolved type. Caps are
 * measured on the original File.size.
 *
 * `existingTextBytes` is the byte total of text attachments ALREADY pending in
 * the composer, so the text budget is enforced across multiple picks in one
 * message — not just within a single selection (otherwise stacking picks would
 * bypass it).
 */
export type AttachmentValidationResult = { error: string } | { ok: true }

export function validateAttachmentFiles(
  incoming: File[],
  currentCount = 0,
  existingTextBytes = 0,
): AttachmentValidationResult {
  if (currentCount + incoming.length > MAX_FILES_PER_MESSAGE) {
    return { error: `You can attach at most ${MAX_FILES_PER_MESSAGE} files per message.` }
  }
  let textBytes = existingTextBytes
  for (const file of incoming) {
    // Legacy binary Word (.doc) — not the OOXML .docx mammoth reads. Reject clearly.
    // The extension is authoritative: a real .docx/.xlsx that the OS mislabels with
    // the legacy `application/msword` MIME must NOT be rejected (extension wins).
    const name = file.name || ''
    if (/\.doc$/i.test(name) || (file.type === 'application/msword' && !/\.(docx|xlsx)$/i.test(name))) {
      return { error: LEGACY_DOC_REJECT_MSG }
    }
    // Legacy binary PowerPoint (.ppt) — not the OOXML .pptx. The extension is
    // authoritative: a real .pptx the OS mislabels as `application/vnd.ms-powerpoint`
    // must NOT be rejected (extension wins). `\.ppt$` never matches `.pptx`.
    if (/\.ppt$/i.test(name) || (file.type === 'application/vnd.ms-powerpoint' && !/\.pptx$/i.test(name))) {
      return { error: LEGACY_PPT_REJECT_MSG }
    }
    const mediaType = resolveMediaType(file)
    if (!ALLOWED_MEDIA_TYPES.includes(mediaType)) {
      const ppt = DECK_ATTACHMENTS_ENABLED ? ', a PowerPoint (.pptx)' : ''
      return { error: `"${file.name}" isn't supported. Attach an image (PNG, JPEG, GIF, WebP), a PDF, a Word (.docx) or Excel (.xlsx)${ppt} file, or a text file (CSV, TXT).` }
    }
    const isTextFile = TEXT_MEDIA_TYPES.has(mediaType)
    if (isTextFile) {
      if (file.size > MAX_TEXT_FILE_SIZE) {
        return { error: `"${file.name}" exceeds the ${MAX_TEXT_FILE_SIZE / 1024} KB limit for text files.` }
      }
      textBytes += file.size
    } else if (file.size > MAX_FILE_SIZE) {
      return { error: `"${file.name}" exceeds the 4 MB limit.` }
    }
  }
  // Inline text is sent on every turn (sticky), so bound the running total of
  // pending text bytes — not just per file — to keep the prompt in budget.
  if (textBytes > MAX_TEXT_BYTES_PER_CONVERSATION) {
    return { error: `Attached text files exceed the ${MAX_TEXT_BYTES_PER_CONVERSATION / 1024} KB total limit. Remove some and try again.` }
  }
  return { ok: true }
}

/** The pending-composer shape (`usePendingAttachments.js`) — transient base64
 * held client-side until the message sends. */
export interface PendingAttachment {
  id: string
  name: string
  mediaType: string
  size: number
  base64: string
}

/** Sum the byte size of the text attachments in a pending/ref list. Accepts
 * `unknown` (not just `PendingAttachment[]`) — the doc'd contract is "a
 * pending/ref list," and the only fields ever read are `mediaType`/`size`,
 * shared by both the pre-upload pending shape and the post-upload server ref.
 * A genuine non-array is a real call shape, not just defensive code:
 * `attachmentInput.test.js` pins `textAttachmentBytes(null) === 0`. */
export function textAttachmentBytes(attachments: unknown): number {
  if (!Array.isArray(attachments)) return 0
  return (attachments as Array<{ mediaType?: string; size?: number }>).reduce(
    (n, a) => n + (TEXT_MEDIA_TYPES.has(a.mediaType ?? '') ? a.size || 0 : 0),
    0,
  )
}

/**
 * Validate that adding `incomingCount` attachments won't push the conversation
 * over the cumulative per-conversation cap. `existingCount` is the number of
 * attachment refs already persisted across the conversation's messages. Returns
 * `{ error }` (distinct wording from the storage-full message) or `{ ok: true }`.
 */
export function validateConversationAttachmentCap(existingCount = 0, incomingCount = 0): AttachmentValidationResult {
  if (existingCount + incomingCount > MAX_ATTACHMENTS_PER_CONVERSATION) {
    return {
      error: `This conversation has reached its limit of ${MAX_ATTACHMENTS_PER_CONVERSATION} attachments. Start a new chat to add more.`,
    }
  }
  return { ok: true }
}

/** Read a File as raw base64 (stripping the `data:<type>;base64,` prefix). */
export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = String(reader.result)
      const comma = result.indexOf(',')
      resolve(comma >= 0 ? result.slice(comma + 1) : result)
    }
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}

/** A unique attachment id (namespacing of bytes is by id within the store). */
export function newAttachmentId() {
  return `att_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
}

/** The lightweight pre-upload ref `toAttachmentRef` strips a `PendingAttachment`
 * down to. Distinct from `attachmentApi.ts`'s `AttachmentRef` (the POST-upload
 * server-returned ref) — same-sounding name, different shape/purpose, so this
 * one gets its own name rather than colliding. */
export type PendingAttachmentRef = Pick<PendingAttachment, 'id' | 'name' | 'mediaType' | 'size'>

/** Strip transient base64 to the lightweight ref persisted in the conversation. */
export function toAttachmentRef({ id, name, mediaType, size }: PendingAttachmentRef): PendingAttachmentRef {
  return { id, name, mediaType, size }
}
