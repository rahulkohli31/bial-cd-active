/**
 * THE LIBRARY'S ATTACHMENT ADAPTER, OVER THIS PROJECT'S OWN PIPELINE (plan 002, U5).
 *
 * ═══ WHY THIS EXISTS AT ALL ═══
 *
 * The library's composer primitives — its add-attachment control, its chip list and its dropzone —
 * are all gated on the `attachments` capability, and that capability is DERIVED from whether an
 * adapter is registered. There is no way to render the library's box and keep the capability off:
 * the add control renders nothing, the chips render nothing, and the dropzone short-circuits.
 *
 * So adopting the box means registering an adapter. What it must NOT mean is handing the library
 * the pipeline.
 *
 * ═══ THE PIPELINE STAYS OURS, AND THAT IS THE WHOLE CONTRACT ═══
 *
 * The library renders a chip; it does not decide which content is re-sent, which binaries are
 * inlined, the cache-breakpoint ceiling, or how fences are escaped. Every one of those decisions
 * lives in `utils/attachmentInput.ts` and the send paths that read it, and none of them moves here.
 *
 * What this adapter does is exactly three things:
 *   · `add`   — runs OUR validation and OUR base64 read, and refuses in OUR words.
 *   · `remove`— nothing to undo. The decoded bytes live in the returned object and die with it;
 *               no upload has happened, so there is nothing on a server to withdraw.
 *   · `send`  — hands back the same payload as a complete attachment. It is on the interface and
 *               therefore has to be honest, but SEE BELOW: this project's send never calls it.
 *
 * ═══ `send` IS NOT ON THIS PROJECT'S SEND PATH, AND THAT IS DELIBERATE ═══
 *
 * `composer.send()` sets `_text = ""` BEFORE it awaits anything and restores it only if the
 * ATTACHMENT tasks throw — never if the append itself does. That is precisely the defect that
 * destroyed a citizen's typed message and their staged files one day before this plan was written
 * (`docs/solutions/logic-errors/refused-send-destroys-composer-message-2026-09-01.md`), and this
 * plan deliberately makes a send wait LONGER, which widens exactly that window.
 *
 * So the composer's own send is not used. `ComposerBox` reads the staged attachments off the
 * runtime at press time, performs the send itself, and clears only once the server has accepted.
 * `send` is implemented here anyway rather than throwing, because an interface member that lies
 * is worse than one that is merely unused — if a future path does route through the library's
 * send, it gets the right payload rather than an exception.
 *
 * ═══ THE PAYLOAD RIDES ON THE ATTACHMENT, NOT BESIDE IT ═══
 *
 * The library's `Attachment` is the only thing that survives from `add` to press time, so the
 * decoded bytes travel on it. They are stashed under one non-enumerated key rather than smuggled
 * into `content`, because `content` is a list of message parts the library may render, and a
 * base64 blob is not something anyone should see rendered.
 */
import {
  fileToBase64,
  newAttachmentId,
  resolveMediaType,
  textAttachmentBytes,
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
   * What is already staged, read at ADD time — the per-message cap counts across the batch.
   *
   * IT LAGS BY A RENDER, WHICH IS WHY IT IS NOT THE WHOLE ANSWER. The runtime appends an
   * attachment only after `add` has resolved, and the ref this reads is written during a React
   * render after that. See the claim list in `createAttachmentAdapter` for the half of the cap
   * this cannot carry.
   */
  staged: () => readonly Attachment[]
  /**
   * SAY A REFUSAL OUT LOUD, because the library will not.
   *
   * Both the dropzone and the input's paste handler wrap `addAttachment` in `try { … } catch {}`,
   * so a file over the size cap, a fifth file past the per-message limit, or a format this
   * platform does not accept would all be dropped in COMPLETE SILENCE — and the citizen would
   * believe the model can see a file it cannot. `add` still throws, so the library discards the
   * file; this is how the reason reaches a screen.
   */
  onRefused: (message: string) => void
}

/** `image` for an image, `document` for a PDF, `file` for the text kinds. The library uses this
 *  only to choose a chip icon; nothing downstream reads it. */
function kindOf(mediaType: string): PendingAttachment['type'] {
  if (mediaType.startsWith('image/')) return 'image'
  if (mediaType === 'application/pdf') return 'document'
  return 'file'
}

export function createAttachmentAdapter({ accept, staged, onRefused }: AttachmentAdapterOptions): AttachmentAdapter {
  /**
   * WHAT THIS ADAPTER HAS ALREADY SAID YES TO IN THE CURRENT GESTURE, and the whole of why the
   * cap survives a multi-file drop.
   *
   * ONE DROP IS N CONCURRENT `add` CALLS. The dropzone, the OS picker and the paste handler all
   * fan out with `Promise.all(files.map(…))`, so every file in one gesture starts its `add` in the
   * SAME tick. `staged()` cannot see any of them: the runtime appends an attachment only after
   * `add` resolves, and the ref is written a render later still. So eight files dropped at once
   * each validated against an empty list and all eight went through — the cap bypass R57 records,
   * reintroduced by the gap between a synchronous check and an asynchronous state update.
   *
   * Counting what THIS adapter has accepted closes it, because the accept and the count happen in
   * the same synchronous stretch — there is no await between them for a sibling to slip through.
   *
   * A GESTURE IS A TICK, AND THE LIST DIES WITH IT. The claims exist only to bridge the gap until
   * `staged()` catches up, so they are dropped at the end of the tick that made them: a later
   * gesture is a later task, by which time the runtime has published everything that landed. That
   * is also what keeps a cancelled add — `clearAttachments()` cancels every in-flight one — from
   * leaving a phantom file permanently occupying a slot in the cap.
   */
  let claimed: { mediaType: string; size: number }[] = []
  let closing = false

  function claim(mediaType: string, size: number): void {
    if (!closing) {
      closing = true
      queueMicrotask(() => {
        claimed = []
        closing = false
      })
    }
    claimed.push({ mediaType, size })
  }

  return {
    accept,

    async add({ file }): Promise<PendingAttachment> {
      // OUR VALIDATION, AGAINST WHAT IS ALREADY STAGED AND WHAT THIS GESTURE HAS ALREADY TAKEN.
      // The per-message file cap and the per-conversation text-byte budget are both cumulative, so
      // the check has to see both lists rather than only the arriving file.
      const mediaType = resolveMediaType(file)
      const current = [...payloadsOf(staged()), ...claimed]
      const verdict = validateAttachmentFiles([file], current.length, textAttachmentBytes(current))
      if ('error' in verdict && verdict.error) {
        onRefused(verdict.error)
        throw new AttachmentRefusal(verdict.error)
      }
      // BEFORE THE READ, NOT AFTER IT. `fileToBase64` is the await the siblings would slip
      // through; claiming the slot first is what makes the check above see them.
      claim(mediaType, file.size)

      const payload: OurAttachment = {
        id: newAttachmentId(),
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
    },

    async remove(): Promise<void> {
      // NOTHING TO UNDO. The decoded bytes live on the object the library is dropping and no
      // upload has happened, so there is no server-side state to withdraw. An empty body here is
      // the correct implementation, not an unfinished one.
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
