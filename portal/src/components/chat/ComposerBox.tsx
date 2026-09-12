/**
 * THE COMPOSER BOX — one control, on both screens, built on the library's primitives.
 *
 * WHY THIS EXISTS — the project screen and the chat used to hand-roll two independently
 * drifting boxes; this is the shared `ComposerPrimitive` core instead, gated on the
 * registered `attachments` adapter, so only the placeholder and what surrounds it differ.
 *
 * NO CONTROL HERE IS EVER GIVEN A REAL `disabled` — not the textarea, attach or Send, in
 * any state, including a running turn, a waiting offer or a spent budget: it blurs the
 * focused element to `document.body` and drops keyboard focus, the recurring "blurs
 * mid-sentence" defect. Unavailability is worn as `aria-disabled` with the reason in the
 * accessible name, enforced in the handler; Send is hand-built for the same reason, since
 * the library's own button hard-disables whenever `queue` (never registered) leaves it
 * with no send callback.
 *
 * THE SEND PATH IS OURS, the property that matters most: `composer.send()` clears text
 * BEFORE awaiting and restores it only if the attachment tasks throw, never if the append
 * does — the defect that once destroyed a citizen's message and staged files. So this box
 * reads text/attachments at press time, sends them itself, and clears ONLY once the
 * server accepts — no restore path exists to race with.
 *
 * AND ONLY OVER THE CHAT IT WAS SENT IN: this box is not remounted per conversation, so
 * an accepted send can land while a sibling chat is on screen — what it clears is then
 * decided against the chat being typed into, not the one it belongs to (`liveConversation`).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ComposerPrimitive, useAui, useAuiState } from '@assistant-ui/react'
import { Paperclip, Send, X } from 'lucide-react'

import { payloadsOf } from './runtime/attachmentAdapter'
import { usePendingAttachmentReads, useRefusalSink } from './runtime/stagedAttachments'
import { unsupportedFormatMessage } from '../../utils/attachmentInput'
import type { PendingAttachment } from '../../utils/attachmentInput'
import AttachmentPreview, { type PreviewTarget } from './AttachmentPreview'
import { SendRefusal } from './sendRefusal'

/** What one send carries. The box assembles it; the surface performs it. */
export interface ComposerSubmission {
  text: string
  attachments: PendingAttachment[]
  /** The conversation this send belongs to, stamped at press time. */
  conversationId: string
}

export interface ComposerBoxProps {
  /** The conversation a send is stamped with. `null` disables sending — nothing to send TO. */
  conversationId: string | null
  placeholder: string
  /**
   * Perform the send. Resolves on server confirmation; REJECTS on failure, and the box keeps
   * everything it was holding.
   */
  onSubmit: (submission: ComposerSubmission) => Promise<void>
  /**
   * Why Send must wait, in one sentence, or `null` when it need not. Typing is never blocked —
   * only sending — so this is the box's whole notion of unavailability.
   */
  unavailableReason: string | null
  /**
   * THE BOX IS WAITING ON AN ANSWER THAT IS NOT A MESSAGE — a whole treatment (pale
   * ground, dimmed paperclip, reason where the placeholder was), not a greyed send circle
   * (`PlanReady`, `PlanRevised`). SEPARATE FROM `unavailableReason`: Send waits for four
   * reasons, three of which (too-long text, a reply arriving, a build running) leave the
   * box white — only a pending question locks it.
   */
  locked?: boolean
  /**
   * WHAT THE BOX KEPT for that chat once an accepted send is reconciled: the
   * conversation stamped on the send, and whatever text still stands in the box.
   * Exists because the box does not always empty — a citizen who rewrites it mid-request
   * keeps their words (see `doSend`), so a caller holding a second draft copy must be told
   * what survived; empty when the citizen has moved on, a real answer, not a shrug.
   */
  onAccepted?: (conversationId: string, remainingText: string) => void
  /** Rendered inside the box, above the input: the offer strip's locked treatment, and nothing else. */
  header?: React.ReactNode
  /**
   * Rendered under the box: the cap counter, the context warning, the kind description. It sits
   * INSIDE the dropzone, because a drop that lands on it must be claimed rather than falling
   * through to the browser.
   */
  footer?: React.ReactNode
  /** Said out loud when a file is refused, and when a send is. */
  onUrgent: (message: string) => void
}

export default function ComposerBox({
  conversationId,
  placeholder,
  onSubmit,
  unavailableReason,
  locked = false,
  onAccepted,
  header,
  footer,
  onUrgent,
}: ComposerBoxProps) {
  const aui = useAui()
  const [preview, setPreview] = useState<PreviewTarget | null>(null)
  const [sending, setSending] = useState(false)

  /**
   * WHICH CHAT IS ON SCREEN NOW, vs. the one Send was pressed in — what makes the
   * reconciliation below safe. This box is ONE LONG-LIVED INSTANCE (flat routing swaps
   * `conversationId`, no remount): a send still out when the citizen steps to a sibling
   * finishes over somebody else's composing, since `doSend`'s press-time closure holds its
   * own id while this ref holds what's being typed into. WRITTEN DURING RENDER, not an
   * effect, since an accepted send can resolve before a passive effect flush.
   */
  const liveConversation = useRef(conversationId)
  liveConversation.current = conversationId

  // READ THROUGH THE STORE, so the control re-renders when the citizen types or attaches. The
  // VALUES are read again at press time from `getState()` — a render-scoped copy would be one
  // keystroke stale on a fast Enter.
  const stagedCount = useAuiState((s) => s.composer.attachments.length)
  const hasContent = useAuiState((s) => s.composer.text.trim().length > 0) || stagedCount > 0
  /**
   * FILES TAKEN BUT NOT YET STAGED.
   *
   * `add` reads the file to base64 before the runtime appends anything, so between the drop and
   * the chip there is a window in which the composer's own view is "no attachments" — and on a
   * four-megabyte workbook that window is long enough to press Enter in. The send that resulted
   * carried the question and not the file, and the answer that came back simply never mentioned
   * it. Nothing about that reads as a failure to the person who attached it.
   *
   * SO IT IS A REASON SEND WAITS, and it is worded like every other one: the box says why. This
   * is also the only unavailability the citizen resolves by doing nothing at all, which is why it
   * is phrased as an in-progress fact rather than as an instruction.
   */
  const pendingReads = usePendingAttachmentReads()
  const attachmentsArriving = pendingReads > 0
  const waitingForFiles = attachmentsArriving
    ? pendingReads === 1
      ? 'Adding your file…'
      : `Adding your files… (${pendingReads})`
    : null
  const sendReason = unavailableReason ?? waitingForFiles
  const sendUnavailable = sendReason !== null || !hasContent || sending
  /**
   * LOOK vs. WILL-SEND are different questions — collapsing them was a departure from
   * fourteen boards. The canvas paints the send circle `#D6DDE4` only where a pending
   * offer LOCKS the composer (`PlanReady`, `PlanRevised`); every other board draws it teal
   * even empty, so pale ground means "you may not send," not "you haven't typed yet" — and
   * greying it for an empty box would also fail contrast (white on `#D6DDE4` ~1.4:1).
   * `sendUnavailable` still governs `aria-disabled` and the refusal in `doSend` untouched.
   *
   * ARRIVING FILES DO NOT LOCK THE BOX EITHER. The lock is the treatment for a
   * pending QUESTION, and a file a few hundred milliseconds from being staged is not one. It
   * greys the send circle through `sendUnavailable` and says why in the accessible name; the
   * box stays white.
   */
  const sendLocked = unavailableReason !== null || sending

  const doSend = useCallback(async () => {
    // THE ENFORCEMENT, and the whole of it. `aria-disabled` says so; it does not do so. Pressing
    // Enter, clicking a dimmed Send and calling this directly all land here.
    if (unavailableReason !== null || !conversationId || sending) return
    // THE FILES ARE NOT ALL IN YET. Enforced here as well as drawn, for the same
    // reason every other refusal is: `aria-disabled` says so, it does not do so, and pressing
    // Enter lands here. Sending now would send the question without the file.
    if (attachmentsArriving) return
    const state = aui.composer.getState()
    const attachments = payloadsOf(state.attachments)
    if (state.text.trim().length === 0 && attachments.length === 0) return

    // WHAT THIS PRESS IS SENDING, snapshotted — and the reason the clear below is not a `reset()`.
    // The box stays typable and the paperclip stays live for the whole in-flight window, on
    // purpose: waiting on the server is not a reason to stop composing. That makes an
    // unconditional clear a DELETE of whatever the citizen added while the request was out —
    // exactly the loss this file's docblock says it exists to prevent, arriving through the
    // affordance rather than through the failure path.
    const sentText = state.text
    const sentIds = new Set(state.attachments.map((a) => a.id))

    // WHETHER THE SERVER TOOK IT, for the catch below. The clearing has to stay INSIDE this `try`
    // so that `sending` is only released once the box has actually been reconciled — release it
    // first and a fast second press re-enters with the sent text still in the box and sends it
    // twice. But that means a slip while CLEARING lands in the same catch as a failed send, and
    // those are not the same event and must not read the same.
    let accepted = false

    setSending(true)
    try {
      await onSubmit({ text: sentText, attachments, conversationId })
      accepted = true

      // THE CHAT MOVED ON WHILE THE REQUEST WAS OUT, and then there is nothing here to reconcile:
      // the box on screen belongs to a sibling, and every rule below would be applied to somebody
      // else's composing. The prefix slice is the one that bites — send "hi" here, step next
      // door, type "hi there", and the tail rule would cut the sibling's line down to " there",
      // because it reads as an append to a message that was never typed in this chat.
      //
      // THE FILES ARE THE SAME QUESTION. `clearAttachments()` empties the whole composer, so
      // tidying up after this send would take the sibling's staged files with it and then put
      // them back through a second decode — a chip that blinks out and returns, in a chat that
      // sent nothing.
      //
      // THE SEND ITSELF STILL SUCCEEDED, and is reported as such: `accepted` is already true, so
      // nothing downstream says a message failed, and the chat it was sent in is told its box
      // kept nothing — because nothing on screen is its any more.
      if (liveConversation.current !== conversationId) {
        onAccepted?.(conversationId, '')
        return
      }

      // CLEARED ONLY ON SUCCESS, AND ONLY WHAT WAS SENT. Nothing is optimistically emptied, so a
      // failed send leaves everything exactly where it was and there is no restore path to race
      // with; and anything added since the press survives, because it was never sent.
      const after = aui.composer.getState()
      // ONE BRANCH, NOT TWO. `startsWith` is true when the two are equal and the slice is then
      // empty, so an `after.text === sentText` arm ahead of this would be the same statement twice.
      //
      // Anything else is an edit we cannot reconcile — the citizen rewrote the box while the
      // request was out. Leaving their words alone is the only safe answer; a stale copy of a
      // sent line is a nuisance, a deleted paragraph is the bug.
      const kept = after.text.startsWith(sentText) ? after.text.slice(sentText.length) : after.text
      if (kept !== after.text) await aui.composer.setText(kept)
      // SAID OUT LOUD, because a caller holding a second copy of this text cannot see which of the
      // two branches ran. Reported before the attachment tidying below, which can throw: the words
      // are safe either way, and the draft is about the words.
      onAccepted?.(conversationId, kept)

      // THE FILES, SAME RULE. `clearAttachments()` is all-or-nothing and the composer exposes no
      // per-file removal, so anything staged during the send is cleared with the rest and then put
      // back. Re-adding runs it through the adapter's validation a second time, which is harmless:
      // it passed once already, and the alternative is either deleting a file the citizen chose or
      // sending it twice.
      //
      // `a.file` IS ASSERTED, NOT TESTED. The library types `file` optional because a
      // `CompleteAttachment` — produced only by the adapter's own `send()`, which this app never
      // calls — may lack it. Everything reaching here is a `PendingAttachment` and carries its
      // file. A bare `if (a.file)` would drop one SILENTLY on the day that stops being true, which
      // is the exact loss the rest of this function exists to prevent.
      const keptFiles = after.attachments.filter((a) => !sentIds.has(a.id))
      await aui.composer.clearAttachments()
      for (const a of keptFiles) {
        if (!a.file) throw new Error(`Staged attachment ${a.id} has no file to restore after a send.`)
        await aui.composer.addAttachment(a.file)
      }
    } catch (err) {
      // THE SEND WORKED AND THE TIDYING DID NOT. Saying "that message did not send" here would be
      // false twice over: it sent, and the files were cleared a few lines above. `addAttachment`
      // can genuinely throw — the adapter raises `AttachmentRefusal` on a failed verdict.
      if (accepted) {
        onUrgent('Your message was sent. The composer could not be tidied up afterwards — reload if it looks wrong.')
        return
      }
      // The swallowed press: nothing sent, nothing said, nothing cleared.
      if (err instanceof SendRefusal && err.silent) return
      // THE REFUSAL'S OWN WORDS, but only when they were written to be read. Anything else keeps
      // the generic sentence: an aborted send is already in the surface's own banner in the
      // server's words, and a bug's message is not for this audience.
      onUrgent(
        err instanceof SendRefusal
          ? err.message
          : 'That message did not send. Everything you typed is still here — try again.',
      )
    } finally {
      setSending(false)
    }
  }, [attachmentsArriving, aui, conversationId, onAccepted, onSubmit, onUrgent, sending, unavailableReason])

  const attachmentComponents = useMemo(
    () => ({ Attachment: () => <AttachmentChip onPreview={setPreview} /> }),
    [],
  )

  /**
   * A REFUSED FILE IS SAID OUT LOUD — TWO wires, since the library refuses in two places.
   * Our validator (size/limit caps) runs inside the adapter's `add`, but the library
   * SWALLOWS that throw (dropzone + paste wrap `addAttachment` in `try {} catch {}`), so
   * the adapter reports via `useRefusalSink` first. THE FORMAT CHECK NEVER REACHES US: the
   * library filters on `accept` before `add` and emits its own MIME-type sentence,
   * intercepted here and answered in `attachmentInput.ts`'s words instead.
   */
  useRefusalSink(onUrgent)
  useEffect(
    () =>
      aui.on('composer.attachmentAddError', (event) => {
        // NO FILE NAME IS AVAILABLE on this arm — the library filters before the adapter is
        // called and its event carries a reason, not the file. The advice is the part that
        // matters, and it is the same advice, from the same author.
        if (event.reason === 'not-accepted') onUrgent(`That file ${unsupportedFormatMessage()}`)
      }),
    [aui, onUrgent],
  )

  return (
    <>
      {/* THE DROPZONE IS EVERYTHING THAT READS AS THE COMPOSER, not the bordered box.
          The chips, the strip above the row and — the part this had wrong — the FOOTER under the
          box are all visually "the composer" to the person aiming at it. The footer is the gate
          note, the context warning, the plan chat's standing line and the character counter: on a
          running turn there is always a live strip of text there, and a drop landing a few pixels
          low fell through to the browser's default handler, which navigates the tab to the file
          and takes every staged attachment with it. The library only prevents that inside its own
          element, so the footer has to be inside it. */}
      <ComposerPrimitive.AttachmentDropzone
        data-testid="composer-dropzone"
        // The column and its gap were the frame's; they move here with the footer so the box, the
        // notes and the counter still stack exactly as they did.
        className="flex w-full flex-col gap-1.5"
      >
        <ComposerPrimitive.Root
          data-testid="composer"
          onSubmit={(event) => {
            event.preventDefault()
            void doSend()
          }}
          // THE BORDER ANSWERS TO THE HEADER. `PlanReady` draws the box `#CDE9EA` while the offer
          // strip is fixed to its top, so the strip and the box read as one card rather than as a
          // banner sitting on an unrelated control. The header slot's only occupant is that strip.
          //
          // THE GROUND ANSWERS TO THE LOCK, which is a narrower question — a spent offer keeps the
          // teal border and gets its box back. `#F8FAFC` is the board's, and it is a ground rather
          // than an opacity so the box still reads as a control rather than as a ghost.
          className={`flex w-full flex-col gap-2.5 rounded-[14px] border px-3 py-[11px] ${
            locked ? 'bg-canvas-offerlock' : 'bg-white'
          } ${header ? 'border-canvas-offeredge' : 'border-bial-border'}`}
        >
          {header}

          {/* THE TESTID IS KEPT DELIBERATELY. The chips are the library's list now rather than our
              own map, but they are the same chips saying the same thing, and several assertions
              still describe them truthfully. Renaming the handle would have retired those as
              collateral of a component swap. */}
          {/* RENDERED ONLY WHEN THERE IS SOMETHING IN IT. An always-present empty row is an
              element a test cannot tell apart from a chip that failed to clear — and the clearing
              is the property that matters here, because a staged file must not follow the reader
              into the next chat. */}
          {stagedCount > 0 && (
            <div data-testid="composer-chips" className="flex flex-wrap gap-1.5">
              <ComposerPrimitive.Attachments components={attachmentComponents} />
            </div>
          )}

          {/* THE FILE THAT IS COMING BUT IS NOT A CHIP YET.
              The library's chip list can only draw what the runtime holds, and the runtime holds
              nothing until the read finishes — so a large workbook spends its whole read with no
              mark on the screen at all. Without this the box is indistinguishable from one where
              the drop was ignored, which is what a citizen concludes and what makes them press
              Send. `aria-live` because the same news has to reach someone who cannot see the row;
              `polite` rather than `assertive` because it resolves on its own. */}
          {attachmentsArriving && (
            <div
              data-testid="composer-pending"
              aria-live="polite"
              className="text-[12px] leading-relaxed text-neutral"
            >
              {waitingForFiles}
            </div>
          )}

          {/* NEVER `disabled`, in any state — see the docblock. The library's own `disabled` is
              derived from `thread.isDisabled`, which this project never sets, so the attribute is
              absent rather than false; the guard test asserts the absence directly. */}
          <ComposerPrimitive.Input
            // THE REASON GOES WHERE THE PLACEHOLDER WAS while the box is locked, because that is
            // where both boards draw it — inside the box, at the point the citizen is aiming at
            // when they try to type. A hint to describe a change is not the true thing to say to
            // someone who cannot send one.
            placeholder={locked && unavailableReason ? unavailableReason : placeholder}
            data-testid="composer-input"
            rows={1}
            // NO maxLength — deliberately forbidden, and a test asserts its absence.
            className="max-h-[168px] w-full resize-none bg-transparent text-[13.5px] leading-relaxed text-tertiary placeholder:text-canvas-placeholder focus:outline-none"
          />

          <div className="flex items-center gap-2.5">
            {/* THE ATTACHMENT CONTROL IS INSIDE THE BOX, which is the board's whole point about
                this row. It never goes unavailable: staging a file is composing, not sending.

                `ms-auto` IS ON THE PAPERCLIP, not on Send, and the two then ride together at the
                right edge on the row's own `gap-2.5`. Every board that draws a composer carries
                `margin-left:auto` on the paperclip; with the auto margin on Send instead, the two
                controls were pushed to opposite ends of the box — 308px apart on the project rail
                at 1440, and 932px apart at 1024, which is one at each end of the screen. */}
            <ComposerPrimitive.AddAttachment
              data-testid="composer-attach"
              aria-label="Attach a file"
              title="Attach images, PDFs or text files (CSV, TXT), or drop them anywhere in the composer"
              // DIMMED WITH THE BOX, never unavailable. The board draws it at 40% while an offer
              // waits, and it stays pressable at 40%: staging a file is composing, not answering.
              className={`ms-auto inline-flex text-neutral transition hover:text-primary${locked ? ' opacity-40' : ''}`}
            >
              <Paperclip size={16} />
            </ComposerPrimitive.AddAttachment>

            <button
              type="submit"
              aria-disabled={sendUnavailable}
              // The reason rides in the accessible name, so a screen-reader user gets it from the
              // control itself rather than only from a line of text elsewhere on the screen.
              aria-label={sendReason ? `Send message — ${sendReason}` : 'Send message'}
              title={sendReason ?? undefined}
              data-testid="composer-send"
              // THE BOARD'S SEND: a 30px teal circle, not a 36px gold square. Its LOCKED treatment
              // is the board's too — a pale ground rather than an opacity, so it still reads as a
              // control rather than as a ghost — and it is reserved for being locked. See
              // `sendLocked`: an empty box is not a lock, it is Tuesday.
              className={`inline-flex h-[30px] w-[30px] flex-shrink-0 items-center justify-center rounded-full transition ${
                sendLocked ? 'cursor-default bg-canvas-sendoff text-white' : 'bg-primary text-white hover:bg-primary-600'
              }`}
            >
              <Send size={14} />
            </button>
          </div>
        </ComposerPrimitive.Root>

        {footer}
      </ComposerPrimitive.AttachmentDropzone>

      <AttachmentPreview target={preview} onClose={() => setPreview(null)} />
    </>
  )
}

/**
 * ONE STAGED FILE. Preview and remove are SEPARATE TARGETS, and remove is a SIBLING at a higher
 * layer rather than a nested interactive descendant — the project page's own invariant, kept.
 */
function AttachmentChip({ onPreview }: { onPreview: (target: PreviewTarget) => void }) {
  const aui = useAui()
  const attachment = useAuiState((s) => s.attachment)
  const name = attachment.name
  const payload = useMemo(() => payloadsOf([attachment])[0] ?? null, [attachment])

  return (
    <span className="relative inline-flex items-center gap-1.5 rounded-lg border border-bial-border bg-bial-bg px-2 py-1 text-xs text-tertiary">
      <button
        type="button"
        onClick={() => {
          if (!payload) return
          onPreview({
            name: payload.name,
            mediaType: payload.mediaType,
            dataUrl: `data:${payload.mediaType};base64,${payload.base64}`,
          })
        }}
        className="max-w-[10rem] truncate hover:text-primary"
        title={`Open ${name}`}
      >
        {name}
      </button>
      <button
        type="button"
        onClick={() => void aui.attachment.remove()}
        aria-label={`Remove ${name}`}
        className="text-neutral transition hover:text-danger"
      >
        <X size={11} />
      </button>
    </span>
  )
}
