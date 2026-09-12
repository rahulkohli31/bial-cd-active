/**
 * THE LIBRARY'S ATTACHMENT ADAPTER, OVER THIS PROJECT'S OWN PIPELINE.
 *
 * WHY THIS EXISTS
 *
 * The library's composer chrome (add-attachment control, chip list, dropzone) only
 * renders once an adapter is registered — there's no way to get the UI without also
 * accepting an adapter. Registering one must NOT mean handing the library the
 * pipeline: it renders a chip, but never decides what content is re-sent, which
 * binaries are inlined, the cache-breakpoint ceiling, or fence-escaping — those stay
 * in `utils/attachmentInput.ts` and the send paths that read it.
 *
 * This adapter does exactly three things: `add` runs OUR validation and base64 read,
 * refusing in OUR words; `remove` is a no-op (nothing uploaded, nothing to undo);
 * `send` returns the payload honestly but is NEVER CALLED here — `composer.send()`
 * clears the composer text before awaiting anything and restores it only if the
 * attachment tasks throw, never if the append itself does. That window has already
 * destroyed a citizen's typed message and staged files once; `ComposerBox` sends
 * itself instead and clears only after the server accepts. `send` stays implemented
 * so a future caller gets a correct payload, not a lie.
 *
 * The payload rides ON the library's `Attachment` object under one non-enumerated
 * key, never inside `content` — `content` is message parts the library may render,
 * and a base64 blob must never be one of them.
 */
import {
  fileToBase64,
  newAttachmentId,
  resolveMediaType,
  validateAttachmentFiles,
} from '../../../utils/attachmentInput'
import type { PendingAttachment as OurAttachment } from '../../../utils/attachmentInput'
import type { AttachmentAdapter, Attachment, CompleteAttachment, PendingAttachment } from '@assistant-ui/react'

/**
 * WHERE OUR PAYLOAD LIVES ON A LIBRARY ATTACHMENT. One key, one place to look, and a name that
 * says whose it is — a second convention for the same thing is how the two halves drift.
 */
const PAYLOAD = '__bialPayload' as const

type Carried = { [PAYLOAD]?: OurAttachment }

/** Read our payload back off a library attachment. `null` for anything this adapter did not make. */
export function payloadOf(attachment: Attachment): OurAttachment | null {
  return (attachment as Attachment & Carried)[PAYLOAD] ?? null
}

/** Every staged attachment's payload, in order, skipping any the adapter did not make. */
export function payloadsOf(attachments: readonly Attachment[]): OurAttachment[] {
  return attachments.map(payloadOf).filter((p): p is OurAttachment => p !== null)
}

/**
 * A refusal whose message was written for the citizen. The library swallows a throwing `add`
 * silently on paste and surfaces it on the add control, so the composer catches this itself and
 * says it out loud rather than relying on the library to.
 */
export class AttachmentRefusal extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'AttachmentRefusal'
  }
}

export interface AttachmentAdapterOptions {
  /** The MIME/extension list the OS picker and the validator both use. */
  accept: string
  /**
   * What is already staged, read LIVE at ADD time — the per-message cap counts across the batch.
   *
   * IT IS READ, NOT CAPTURED (see `stagedAttachments.tsx`), so it is never older than the last
   * paint. What it cannot see is a file whose read is STILL RUNNING — nothing is staged until then
   * — nor one taken in the same breath as this one, because the composer's own snapshot catches up
   * with the screen. The claim list below is what counts both.
   */
  staged: () => readonly Attachment[]
  /**
   * SAY A REFUSAL OUT LOUD, because the library will not. The dropzone and paste handler
   * both wrap `addAttachment` in `try { … } catch {}`, so an oversized file, a fifth file
   * past the per-message cap, or an unsupported format would otherwise vanish in COMPLETE
   * SILENCE — leaving the citizen believing the model can see a file it cannot. `add` still
   * throws so the library discards it; this callback is how the reason reaches a screen.
   */
  onRefused: (message: string) => void
  /**
   * HOW MANY FILES ARE BEING READ RIGHT NOW, published on every change.
   *
   * A file is not staged until `fileToBase64` has finished with it, and on a large workbook over
   * a slow disk that is a real window with nothing on screen in it. Send is pressable throughout
   * — the composer's own view is "no attachments and maybe some text" — so a citizen who attaches
   * a spreadsheet and presses Enter sends their question WITHOUT the file and gets an answer that
   * never mentions it. Nothing about that reads as a failure.
   *
   * The count is what the composer needs and the claim list already tracks; it is published rather
   * than exposed because the composer must RE-RENDER when it changes, and a ref cannot ask for
   * that.
   */
  onReadingChanged?: (count: number) => void
}

/** `image` for an image, `document` for a PDF, `file` for the text kinds. The library uses this
 *  only to choose a chip icon; nothing downstream reads it. */
function kindOf(mediaType: string): PendingAttachment['type'] {
  if (mediaType.startsWith('image/')) return 'image'
  if (mediaType === 'application/pdf') return 'document'
  return 'file'
}

export function createAttachmentAdapter({ accept, staged, onRefused, onReadingChanged }: AttachmentAdapterOptions): AttachmentAdapter {
  /**
   * A local `claimed` count above `staged()`, because concurrent `add()` calls (one drop
   * gesture is N parallel calls) all read `staged()` before any of them publish — without
   * this, a five-file cap let eight files through. A claim retires when: the composer now
   * holds the file (it's in `staged()`); the user removed the chip (`remove`); or every read
   * has settled with nothing left unpublished (a cancelled `clearAttachments()`). A failed
   * read releases its claim immediately.
   */
  const claimed = new Set<string>()
  let reading = 0

  /** Move the in-flight count and tell whoever is drawing the composer. */
  function readingBy(delta: number): void {
    reading += delta
    onReadingChanged?.(reading)
  }

  /** What the caps must count right now: what the composer holds, plus what is still being read. */
  function countable(): number {
    const stagedNow = payloadsOf(staged())
    if (reading === 0) claimed.clear()
    else for (const p of stagedNow) claimed.delete(p.id)
    return stagedNow.length + claimed.size
  }

  return {
    accept,

    async add({ file }): Promise<PendingAttachment> {
      // OUR VALIDATION, AGAINST WHAT IS ALREADY STAGED AND WHAT THIS GESTURE HAS ALREADY TAKEN.
      // The per-message file cap is cumulative, so the check has to see both lists rather than
      // only the arriving file.
      const mediaType = resolveMediaType(file)
      const verdict = validateAttachmentFiles([file], countable())
      if ('error' in verdict && verdict.error) {
        onRefused(verdict.error)
        throw new AttachmentRefusal(verdict.error)
      }
      // BEFORE THE READ, NOT AFTER IT. `fileToBase64` is the await the siblings would slip
      // through; claiming the slot first is what makes the check above see them.
      //
      // THE ID IS MINTED HERE RATHER THAN AT PAYLOAD TIME, because it is what lets the claim be
      // recognised in the staged list once the composer is holding the file — the claim and the
      // attachment have to be the same thing under the same name, or they are counted twice.
      const id = newAttachmentId()
      claimed.add(id)
      readingBy(1)

      try {
        const payload: OurAttachment = {
          id,
          name: file.name,
          mediaType,
          size: file.size,
          base64: await fileToBase64(file),
        }
        const attachment: PendingAttachment & Carried = {
          id: payload.id,
          type: kindOf(mediaType),
          name: payload.name,
          contentType: mediaType,
          file,
          // `requires-action` / `composer-send` is the library's own vocabulary for "staged, not
          // yet sent". It is the honest status: nothing has been uploaded and nothing will be
          // until the citizen presses send.
          status: { type: 'requires-action', reason: 'composer-send' },
          [PAYLOAD]: payload,
        }
        return attachment
      } catch (err) {
        // THE READ ITSELF FAILED, so nothing will ever be staged under this id and holding the
        // slot would refuse a file the citizen is entitled to attach.
        claimed.delete(id)
        throw err
      } finally {
        readingBy(-1)
      }
    },

    async remove(attachment): Promise<void> {
      // NOTHING TO UNDO ABOUT THE FILE ITSELF. The decoded bytes live on the object the library is
      // dropping and no upload has happened, so there is no server-side state to withdraw.
      //
      // THE COUNTING IS THE PART THAT IS NOT NOTHING. If this file was still holding a claim — it
      // is, whenever no later `add` has run to notice the composer was holding it — that claim
      // would go on occupying a slot in the per-message cap and bytes in the text budget until
      // every read in flight had settled. The id is the one the claim was made under, because the
      // claim and the attachment are minted as the same thing under the same name.
      claimed.delete(attachment.id)
    },

    async send(attachment): Promise<CompleteAttachment> {
      // NOT ON THIS PROJECT'S SEND PATH — see the docblock. Implemented honestly anyway.
      const payload = payloadOf(attachment)
      const complete: CompleteAttachment & Carried = {
        ...attachment,
        status: { type: 'complete' },
        content: [{ type: 'text', text: attachment.name }],
        ...(payload ? { [PAYLOAD]: payload } : {}),
      }
      return complete
    },
  }
}
