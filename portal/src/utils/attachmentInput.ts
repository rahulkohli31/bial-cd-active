/**
 * Pure helpers for the chat attachment composer. Validation + base64 reading +
 * ref-building live here so the composer logic is testable without a DOM render. The real trust boundary is
 * the server (media-type allowlist + magic-byte check); these checks are UX.
 */
/**
 * THE LINE IS A RULE, NOT A LIST: images/PDFs upload as themselves; text (CSV/TXT) rides
 * inline as a fenced block, so it shares this allowlist despite not being binary. Anything
 * needing conversion — Word/Excel/PowerPoint — is gone client-side: the server used to
 * extract them to Markdown, so a citizen asking about a spreadsheet's layout was describing
 * something the model never saw. That extraction machinery still exists server-side,
 * deliberately out of scope here — a reachable-but-unreferenced path, unshipped not removed.
 */
export const ALLOWED_MEDIA_TYPES = [
  // The MODEL lane — it reads these bytes itself.
  'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'application/pdf',
  // The CODE lane — a reader in the workspace opens these and reports what it found.
  // `text/plain` is deliberately NOT here: it works today and stops, because no client
  // requirement names it and every format costs a reader arm, refusal copy, a test and a line
  // in the help page. Scope Boundaries records it as a withdrawal rather than a format never
  // added.
  'text/csv', 'text/tab-separated-values',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
]
// THE INLINE TEXT LANE IS GONE, AND SO IS ITS LAST TRACE. A CSV used to be read in the
// browser and pushed into the prompt as a fenced text block; every attachment is now an uploaded
// file with a stored identity, which is what lets a chip be rebuilt on reload for EVERY format by
// one fix — the inline lane could never have produced an identity to rebuild from.
//
// A `TEXT_MEDIA_TYPES` set stood here, deliberately EMPTIED rather than deleted, so its three
// call sites could move onto the uploaded path one at a time instead of all at once. They have
// all moved. What was left was a set that answered `false` to everything, two byte caps only it
// could reach, and a `textAttachmentBytes` helper that could only ever return 0 — dead code with
// a live test asserting the zero, which is the residue this pass exists to remove.

// WHAT CAN BE SHOWN AS TEXT, which is a different question from how a file travels, and
// the reason the set above could not simply be reused for it: `AttachmentPreview` asks whether
// pressing a chip can render the file in place. A CSV is still perfectly readable in a browser,
// and losing that preview would be a real regression smuggled in by a transport change.
//
// Office formats are absent on purpose: a spreadsheet, document or deck cannot be rendered in a
// browser without a converter this platform does not host, so their chips return the file
// instead.
export const TEXT_PREVIEW_MEDIA_TYPES = new Set(['text/csv', 'text/tab-separated-values'])
// Extension tokens let the OS picker show these even when it reports an inconsistent or empty
// MIME — which it does constantly for Office and delimited files (see `resolveMediaType`).
//
// `.tab` IS HERE BECAUSE THE SERVER ACCEPTS IT. `resolveMediaType` already maps a `.tab` to
// `text/tab-separated-values` and the TSV door admits that suffix by name — but the picker filters
// on this list, so a citizen browsing for `movements.tab` could not select it at all, and the file
// they were told was supported was invisible. The drag path worked; the picker path failed silently.
export const ACCEPT_ATTR = [
  ...ALLOWED_MEDIA_TYPES, '.csv', '.tsv', '.tab', '.xlsx', '.docx', '.pptx',
].join(',')

// ONE SIZE FOR EVERY FORMAT, matching the server's `ATTACHMENT_MAX_BYTES` exactly; a test
// holds the two equal. Measured on the original `File.size`, so a citizen learns a file is too
// large before it is read, encoded and sent.
export const MAX_FILE_SIZE = 10 * 1024 * 1024
export const MAX_FILE_SIZE_MB = MAX_FILE_SIZE / (1024 * 1024)
export const MAX_FILES_PER_MESSAGE = 5
// Cumulative cap across a whole conversation (all turns). Distinct from the
// per-message cap above and the per-user 50 MB object-store cap (enforced
// server-side); checked at send time where the full conversation is visible.
export const MAX_ATTACHMENTS_PER_CONVERSATION = 20

/**
 * ADVICE IS ONLY HONEST WHILE IT LEADS SOMEWHERE: the two legacy reject messages said "save
 * as .docx"/"save as .pptx", but both stopped being followable once those formats were
 * refused too — a citizen who complied got rejected again, told nothing new. So there is one
 * refusal now, naming what IS accepted — the reasoning this file already used for the
 * flag-off deck case, applied to the permanent one.
 */
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
 * ONE SENTENCE, EVERYWHERE. The composer, the help page and every unsupported-format
 * refusal say this and nothing else — three sentences that drift is how the removed rule failed.
 *
 * IT DESCRIBES WHAT HAPPENS, NOT WHICH EXTENSIONS ARE ON A LIST. A list of ten formats is the
 * shape the old copy failed as: it goes stale the moment the allowlist moves, and it tells a
 * citizen nothing about why a spreadsheet behaves differently from a photograph.
 */
export function unsupportedFormatMessage(): string {
  return `isn't supported. ${ATTACHMENT_LANES_SENTENCE}`
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
 * TWO QUESTIONS, NOT THREE. A per-file text cap and a running text-byte budget used to sit here
 * for the inline lane; nothing is inlined now, so both bounded a population that is always empty.
 * Every file takes one path and one size rule.
 */
export type AttachmentValidationResult = { error: string } | { ok: true }

export function validateAttachmentFiles(
  incoming: File[],
  currentCount = 0,
): AttachmentValidationResult {
  if (currentCount + incoming.length > MAX_FILES_PER_MESSAGE) {
    return { error: `You can attach at most ${MAX_FILES_PER_MESSAGE} files per message.` }
  }
  for (const file of incoming) {
    // ONE refusal, for every unsupported format. There is no longer a special case for a legacy
    // `.doc` or `.ppt`: with the OOXML formats refused as well, "save as .docx" led nowhere, and a
    // single message that names what IS accepted is both true and followable.
    const mediaType = resolveMediaType(file)
    if (!ALLOWED_MEDIA_TYPES.includes(mediaType)) {
      return { error: unsupportedFileMessage(file.name) }
    }
    // Interpolated, never spelled: the figure a citizen is told is the figure enforced.
    if (file.size > MAX_FILE_SIZE) {
      return { error: `"${file.name}" exceeds the ${MAX_FILE_SIZE_MB} MB limit.` }
    }
  }
  return { ok: true }
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
 * message (#194). Nothing prices a document up front any more on either side, and the page cap
 * itself has since gone the same way: the window is measured from what the provider reports for
 * a completed turn.
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
