import { imageSize } from 'image-size'
import type { ChatMessage } from './messageTypes'

/**
 * Pure helpers for the chat attachment composer. Validation + base64 reading +
 * ref-building live here so the composer logic is testable without a DOM render. The real trust boundary is
 * the server (media-type allowlist + magic-byte check); these checks are UX.
 */
/**
 * The CODE lane — a reader in the project's workspace opens these and reports what it found.
 * Mirrors `media/lanes.py`'s set, and a chip's shape is decided by this membership too.
 *
 * `text/plain` is deliberately NOT here: it works today and stops, because no client
 * requirement names it and every format costs a reader arm, refusal copy, a test and a line
 * in the help page. Scope Boundaries records it as a withdrawal rather than a format never
 * added.
 */
export const CODE_LANE_MEDIA_TYPES = [
  'text/csv', 'text/tab-separated-values',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
]
const IMAGE_MEDIA_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']
/**
 * The MODEL lane — it reads these bytes itself, with no reader and no workspace involved.
 * Named as its own export because a generic conversation narrows to exactly this lane; the
 * backend mirror is `MODEL_LANE_MEDIA` in `media/lanes.py`.
 */
export const MODEL_LANE_MEDIA_TYPES = [...IMAGE_MEDIA_TYPES, 'application/pdf']
/**
 * THE LINE IS A RULE, NOT A LIST: every attachment uploads as itself, and its media type
 * decides which lane reads it. Nothing is converted here and nothing rides inline in the
 * prompt.
 */
export const ALLOWED_MEDIA_TYPES = [
  ...MODEL_LANE_MEDIA_TYPES,
  ...CODE_LANE_MEDIA_TYPES,
]
// WHAT CAN BE SHOWN AS TEXT, which is a different question from how a file travels, and
// the reason `ALLOWED_MEDIA_TYPES` cannot simply be reused for it: `AttachmentPreview` asks whether
// pressing a chip can render the file in place. A CSV is still perfectly readable in a browser,
// and losing that preview would be a real regression smuggled in by a transport change.
//
// Office formats are absent on purpose: a spreadsheet, document or deck cannot be rendered in a
// browser without a converter this platform does not host, so their chips return the file
// instead.
export const TEXT_PREVIEW_MEDIA_TYPES = new Set(['text/csv', 'text/tab-separated-values'])

// The pre-2007 binary formats are refused by name, with the format to re-save them as, which is
// accepted. Keyed by extension: it is the one signal the OS reports reliably.
const LEGACY_OFFICE = new Map([
  ['doc', { application: 'Word', modern: '.docx' }],
  ['xls', { application: 'Excel', modern: '.xlsx' }],
  ['ppt', { application: 'PowerPoint', modern: '.pptx' }],
])

function legacyOfficeFormat(fileName: string) {
  const extension = fileName.match(/\.([^.]+)$/)?.[1].toLowerCase()
  return extension === undefined ? undefined : LEGACY_OFFICE.get(extension)
}

// Extension tokens let the OS picker show these even when it reports an inconsistent or empty
// MIME — which it does constantly for Office and delimited files (see `resolveMediaType`).
//
// `.tab` IS HERE BECAUSE THE SERVER ACCEPTS IT. `resolveMediaType` already maps a `.tab` to
// `text/tab-separated-values` and the TSV door admits that suffix by name — but the picker filters
// on this list, so a citizen browsing for `movements.tab` could not select it at all, and the file
// they were told was supported was invisible. The drag path worked; the picker path failed silently.
//
// The legacy extensions ride along so those files reach `validateAttachmentFiles`, which names them;
// the library's own refusal of a file it filters out carries no file name.
export const ACCEPT_ATTR = [
  ...ALLOWED_MEDIA_TYPES, '.csv', '.tsv', '.tab', '.xlsx', '.docx', '.pptx',
  ...[...LEGACY_OFFICE.keys()].map((ext) => `.${ext}`),
].join(',')

// SIZE LIMITS in MiB; a backend test holds each to its `media/lanes.py` twin. Measured on the
// original `File.size`, so a citizen learns a file is too large before it is read, encoded and sent.
export const IMAGE_MAX_MB = 7
// Pictures and PDFs are sent to the assistant again with every message, and it reads at most
// 32 MB at once — about 24 MB of files once encoded. So this is a chat's total for them, and
// with it the most one PDF can be.
export const CHAT_FILES_MAX_MB = 20
export const CODE_LANE_MAX_MB = 30
/** The provider refuses a picture wider or taller than this. */
const IMAGE_MAX_PIXELS = 8000

const MIB = 1024 * 1024

export const MAX_FILES_PER_MESSAGE = 5
// Cumulative cap across a whole conversation (all turns). Distinct from the
// per-message cap above; checked at send time where the full conversation is visible.
export const MAX_ATTACHMENTS_PER_CONVERSATION = 20

/** The refusal for a file that is not accepted, naming what IS. Legacy Office files are refused earlier. */
export function unsupportedFileMessage(fileName: string): string {
  return `"${fileName}" ${unsupportedFormatMessage()}`
}

/**
 * THE SAME ADVICE, WITH NO NAME TO HANG IT ON: the library's own accept-filter reason
 * doesn't carry the file, only its own sentence — "File type application/vnd… is not
 * accepted. Accepted types: image/png,…" — a raw MIME list read at someone who dragged in
 * a spreadsheet. Rather than let that path speak the library's words, it speaks this one:
 * one author for the advice, the file name the only thing that varies.
 */
export const ATTACHMENT_LANES_SENTENCE =
  "Attach a picture or a PDF and I'll look at it; attach a spreadsheet, document or slide deck " +
  "and I'll open it with code."

/**
 * ONE SENTENCE PER SURFACE, EVERYWHERE ON IT. The composer, the help page and every
 * unsupported-format refusal carry that surface's lane sentence and nothing else — three
 * sentences that drift is how the removed rule failed.
 *
 * IT DESCRIBES WHAT HAPPENS, NOT WHICH EXTENSIONS ARE ON A LIST. A list of ten formats is the
 * shape the old copy failed as: it goes stale the moment the allowlist moves, and it tells a
 * citizen nothing about why a spreadsheet behaves differently from a photograph.
 */
export function unsupportedFormatMessage(lanes: AttachmentLanes = BOTH_ATTACHMENT_LANES): string {
  return `isn't supported. ${lanes.sentence}`
}

/**
 * THE GENERIC CHAT'S OWN REFUSAL. `ATTACHMENT_LANES_SENTENCE` promises to open a spreadsheet,
 * document or deck with code, and a generic conversation has no sandbox to keep that promise —
 * so it gets a sentence that never makes it. Byte-identical to
 * `GENERIC_ATTACHMENT_LANES_SENTENCE` in `backend/src/api/v1/attachments/router.py`.
 */
export const GENERIC_ATTACHMENT_LANES_SENTENCE =
  "Attach a picture or a PDF and I'll look at it — a spreadsheet, document or slide deck " +
  "isn't accepted in this chat."

/**
 * WHAT ONE SURFACE OFFERS, as a single value. The picker's filter and the sentence a refusal
 * carries are the same decision, and passing them side by side is how they drift apart — a
 * picker that offers a spreadsheet under a refusal saying spreadsheets are not accepted.
 */
export interface AttachmentLanes {
  /** What the OS picker offers, and what the library's own filter admits before `add` runs. */
  accept: string
  /** What every unsupported-format refusal on this surface says. */
  sentence: string
}

/** Both lanes: the model reads the pictures and PDFs itself, a workspace reader opens the rest. */
export const BOTH_ATTACHMENT_LANES: AttachmentLanes = {
  accept: ACCEPT_ATTR,
  sentence: ATTACHMENT_LANES_SENTENCE,
}

/**
 * THE MODEL LANE ALONE, for a conversation with no workspace to open a spreadsheet in. No
 * extension tokens ride here, unlike `ACCEPT_ATTR`: their whole job is to get Office and
 * delimited files past an OS that mislabels them, and those are exactly what this surface refuses.
 */
export const MODEL_LANE_ONLY: AttachmentLanes = {
  accept: MODEL_LANE_MEDIA_TYPES.join(','),
  sentence: GENERIC_ATTACHMENT_LANES_SENTENCE,
}

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
  if (/\.(tsv|tab)$/i.test(name)) return 'text/tab-separated-values'
  if (/\.xlsx$/i.test(name)) return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
  if (/\.docx$/i.test(name)) return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  if (/\.pptx$/i.test(name)) return 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
  return file.type
}

/**
 * Validate a batch of newly selected files against the per-message rules. Returns
 * `{ error }` with a user-facing message on the first violation, or `{ ok: true }`. The
 * media type is RESOLVED first (so an OS-mislabeled CSV isn't rejected pre-canonicalization),
 * and both the allowlist and size cap run against that resolved type, measured on the
 * original `File.size`.
 *
 * A pre-2007 Office extension is refused first, by name, ahead of the media-type allowlist. A
 * picture has its own size limit; pictures and PDFs together must fit the chat's total, of which
 * `heldBytes` is already used; every other file has the code lane's limit.
 */
export type AttachmentValidationResult = { error: string } | { ok: true }

export function validateAttachmentFiles(
  incoming: File[],
  currentCount = 0,
  heldBytes = 0,
): AttachmentValidationResult {
  if (currentCount + incoming.length > MAX_FILES_PER_MESSAGE) {
    return { error: `You can attach at most ${MAX_FILES_PER_MESSAGE} files per message.` }
  }
  let held = heldBytes
  for (const file of incoming) {
    const legacy = legacyOfficeFormat(file.name)
    if (legacy) {
      return {
        error:
          `"${file.name}" is an older ${legacy.application} format. ` +
          `Open it in ${legacy.application}, re-save it as ${legacy.modern}, and attach it again.`,
      }
    }
    const mediaType = resolveMediaType(file)
    if (!ALLOWED_MEDIA_TYPES.includes(mediaType)) {
      return { error: unsupportedFileMessage(file.name) }
    }
    // Interpolated, never spelled: the figure a citizen is told is the figure enforced.
    const limitMb = fileLimitMb(mediaType)
    if (file.size > limitMb * MIB) return { error: `"${file.name}" exceeds the ${limitMb} MB limit.` }
    held += chatFileWeight(mediaType, file.size)
    if (held > CHAT_FILES_MAX_MB * MIB) return { error: wontFit(file.name) }
  }
  return { ok: true }
}

/** One file's own limit. A PDF's is the chat's whole room, so a new chat is never the advice. */
function fileLimitMb(mediaType: string): number {
  if (!MODEL_LANE_MEDIA_TYPES.includes(mediaType)) return CODE_LANE_MAX_MB
  return IMAGE_MEDIA_TYPES.includes(mediaType) ? IMAGE_MAX_MB : CHAT_FILES_MAX_MB
}

function wontFit(name: string): string {
  return (
    `"${name}" won't fit in this chat — pictures and PDFs can add up to ${CHAT_FILES_MAX_MB} MB. ` +
    'Attach a smaller file or start a new chat.'
  )
}

/**
 * The refusal for a send whose pictures and PDFs would take the chat past its total, or `null`.
 * The picker checks as each file arrives; this catches one picked before the chat's history loaded.
 */
export function chatTotalRefusal(
  messages: readonly ChatMessage[],
  attachments: readonly { name: string; mediaType: string; size: number }[],
): string | null {
  let held = chatFileBytes(messages)
  for (const attachment of attachments) {
    held += chatFileWeight(attachment.mediaType, attachment.size)
    if (held > CHAT_FILES_MAX_MB * MIB) return wontFit(attachment.name)
  }
  return null
}

/** What a file of this type and size takes from the chat's room: pictures and PDFs, nothing else. */
export function chatFileWeight(mediaType: string, size: number): number {
  return MODEL_LANE_MEDIA_TYPES.includes(mediaType) ? size : 0
}

const NO_IDS: ReadonlySet<string> = new Set()

/** Bytes of the pictures and PDFs these messages carry, skipping the attachment ids in `exclude`. */
export function chatFileBytes(messages: readonly ChatMessage[], exclude = NO_IDS): number {
  let total = 0
  for (const message of messages) {
    for (const part of message.parts) {
      if (part.type === 'file' && !exclude.has(part.attachmentId)) {
        total += chatFileWeight(part.mediaType, part.size ?? 0)
      }
    }
  }
  return total
}

interface PixelSize {
  width: number
  height: number
}

// Small on purpose: `imageSize` walks a damaged JPEG header a byte at a time, copying what is left
// at each step, so its cost grows with the square of the bytes it is given.
const HEADER_BYTES = 128 * 1024

/**
 * The refusal for a picture wider or taller than the provider reads, or `null`. Reads the size from
 * the file's header rather than decoding the picture. A header it cannot read is let through: the
 * server says what is wrong.
 */
export async function pixelLimitRefusal(file: File): Promise<string | null> {
  if (!IMAGE_MEDIA_TYPES.includes(resolveMediaType(file))) return null
  const size = (await headerSize(file)) ?? (await jpegFrameSize(file))
  if (!size) return null
  const { width, height } = size
  if (width <= IMAGE_MAX_PIXELS && height <= IMAGE_MAX_PIXELS) return null
  const n = (value: number) => value.toLocaleString('en-US')
  return `"${file.name}" is ${n(width)} × ${n(height)} pixels. Resize it to ${n(IMAGE_MAX_PIXELS)} pixels or less on each side.`
}

async function headerSize(file: File): Promise<PixelSize | null> {
  try {
    return imageSize(new Uint8Array(await file.slice(0, HEADER_BYTES).arrayBuffer()))
  } catch {
    return null
  }
}

const JPEG_START = 0xd8
// Start-of-frame markers carry the size; C4, C8 and CC share the range but are not frames.
const isFrameMarker = (marker: number) =>
  marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc
// A 7 MB picture holds about a hundred full segments; a file of thousands is not a photograph.
const MAX_SEGMENTS = 512

/**
 * A JPEG's size from its frame header when metadata pushes it past the window: each segment
 * states its length, so this hops from one to the next reading nine bytes at a time.
 */
async function jpegFrameSize(file: File): Promise<PixelSize | null> {
  const read = async (at: number, length: number) =>
    new Uint8Array(await file.slice(at, at + length).arrayBuffer())
  const start = await read(0, 2)
  if (start[0] !== 0xff || start[1] !== JPEG_START) return null
  let at = 2
  for (let hop = 0; hop < MAX_SEGMENTS && at + 9 <= file.size; hop += 1) {
    const segment = await read(at, 9)
    if (segment[0] !== 0xff) return null
    if (isFrameMarker(segment[1])) {
      return { height: (segment[5] << 8) | segment[6], width: (segment[7] << 8) | segment[8] }
    }
    at += 2 + ((segment[2] << 8) | segment[3])
  }
  return null
}

/** The pending-composer shape (`chat/runtime/attachmentAdapter.ts` makes them) —
 * transient base64 held client-side until the message sends. */
export interface PendingAttachment {
  id: string
  name: string
  mediaType: string
  size: number
  base64: string
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

/**
 * THE PER-DOCUMENT LIMIT IS GONE, and its server twin with it.
 *
 * It was two, and it existed because a document was charged a flat figure sized to a page cap,
 * so three could not fit one message. The limit bought the citizen a sentence naming it instead
 * of a context refusal telling them to start a new chat, which then refuses the identical
 * message. Nothing prices a document up front any more on either side, and the page cap itself
 * has since gone the same way: the window is measured from what the provider reports for a
 * completed turn.
 *
 * It goes because a citizen attaching five files should not have to know which of them the
 * platform considers expensive. `MAX_FILES_PER_MESSAGE` is now the only per-message count, and
 * it covers every format.
 *
 * What replaced the guarantee is not another count: a message the conversation cannot hold is
 * refused on the room it needs, before it is sent, which is what the removed limit was really
 * standing in for.
 */

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
