/**
 * THE COMPOSER BOX — one control, on both screens, built on the library's primitives (plan 002, U5).
 *
 * ═══ WHAT THE BOARDS ASK FOR ═══
 *
 * One bordered box with the attachment control and the send control INSIDE it, not beside it. The
 * project screen and the chat draw the same control; only the placeholder and what sits above and
 * below it differ. Two independently hand-rolled boxes is how they drifted apart, and one shared
 * core is the fix.
 *
 * ═══ DECISION 1, SETTLED BY THE OWNER ON 2026-09-02: ADOPT THE LIBRARY COMPOSER ═══
 *
 * Adopted here: `ComposerPrimitive.Root` (the form, and its click-blank-space-to-focus),
 * `ComposerPrimitive.Input` (the textarea, its autosize, its paste-to-attach and its Enter
 * handling), `ComposerPrimitive.AddAttachment`, `ComposerPrimitive.Attachments` and
 * `ComposerPrimitive.AttachmentDropzone`. The `attachments` capability is turned on and an adapter
 * registered — those three primitives are gated on it and render nothing without one.
 *
 * ═══ ONE THING IS HAND-BUILT, AND THIS IS WHY, AT THE POINT IT HAPPENS ═══
 *
 * THE SEND CONTROL. `ComposerPrimitive.Send` is made by `createActionButton`, which renders
 * `<button disabled={props.disabled || !callback}>` — a REAL `disabled` attribute — and
 * `useComposerSend` returns no callback while `isRunning && !capabilities.queue`. `queue` is never
 * registered here, so the library's Send would carry a hard `disabled` for the whole of every turn.
 * `disabled` on the focused element blurs it to `document.body`, which is the mechanism behind "it
 * blurs mid-sentence and focus never comes back"; this repo has recorded that twice and forbids it.
 * The boards contradict it directly too — they draw a composer that keeps accepting typing while
 * the agent answers, with a line saying so.
 *
 * So Send is ours: `aria-disabled` for the affordance, the reason in its accessible name, and the
 * enforcement in the handler.
 *
 * ═══ AND THE SEND PATH IS OURS, WHICH IS THE PROPERTY THAT MATTERS MOST ═══
 *
 * `composer.send()` sets `_text = ""` BEFORE it awaits anything, and restores it only if the
 * ATTACHMENT tasks throw — never if the append does. That is exactly the defect that destroyed a
 * citizen's typed message and their staged files one day before this plan was written, and this
 * plan deliberately makes a send WAIT LONGER, which widens that window rather than narrowing it.
 *
 * So this box reads the text and the attachments off the runtime at press time, performs the send
 * itself, and clears ONLY once the server has accepted. A refused send leaves everything exactly
 * where it was, and there is therefore no restore path to race with — the whole class of defect
 * stops existing rather than being guarded.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { ComposerPrimitive, useAui, useAuiState } from '@assistant-ui/react'
import { Paperclip, Send, X } from 'lucide-react'

import { payloadsOf } from './runtime/attachmentAdapter'
import { useRefusalSink } from './runtime/stagedAttachments'
import { unsupportedFormatMessage } from '../../utils/attachmentInput'
import type { PendingAttachment } from '../../utils/attachmentInput'
import AttachmentPreview, { type PreviewTarget } from './AttachmentPreview'

/** What one send carries. The box assembles it; the surface performs it. */
export interface ComposerSubmission {
  text: string
  attachments: PendingAttachment[]
  /** The conversation this send belongs to, stamped at press time (R60). */
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
  /** Rendered inside the box, above the input: the offer strip's locked treatment, and nothing else. */
  header?: React.ReactNode
  /** Rendered under the box: the cap counter, the context warning, the kind description. */
  footer?: React.ReactNode
  /** Said out loud when a file is refused, and when a send is. */
  onUrgent: (message: string) => void
}

export default function ComposerBox({
  conversationId,
  placeholder,
  onSubmit,
  unavailableReason,
  header,
  footer,
  onUrgent,
}: ComposerBoxProps) {
  const aui = useAui()
  const [preview, setPreview] = useState<PreviewTarget | null>(null)
  const [sending, setSending] = useState(false)

  // READ THROUGH THE STORE, so the control re-renders when the citizen types or attaches. The
  // VALUES are read again at press time from `getState()` — a render-scoped copy would be one
  // keystroke stale on a fast Enter.
  const stagedCount = useAuiState((s) => s.composer.attachments.length)
  const hasContent = useAuiState((s) => s.composer.text.trim().length > 0) || stagedCount > 0
  const sendUnavailable = unavailableReason !== null || !hasContent || sending

  const doSend = useCallback(async () => {
    // THE ENFORCEMENT, and the whole of it. `aria-disabled` says so; it does not do so. Pressing
    // Enter, clicking a dimmed Send and calling this directly all land here.
    if (unavailableReason !== null || !conversationId || sending) return
    const state = aui.composer.getState()
    const attachments = payloadsOf(state.attachments)
    if (state.text.trim().length === 0 && attachments.length === 0) return

    setSending(true)
    try {
      await onSubmit({ text: state.text, attachments, conversationId })
      // CLEARED ONLY ON SUCCESS. Nothing is optimistically emptied, so a failed send leaves the
      // text and the files exactly where they were and there is no restore path to race with.
      await aui.composer.setText('')
      await aui.composer.clearAttachments()
    } catch (err) {
      // The swallowed press: nothing sent, nothing said, nothing cleared.
      if (err instanceof Error && err.name === 'SendRefusal' && (err as { silent?: boolean }).silent) return
      // THE REFUSAL'S OWN WORDS, but only when they were written to be read. Anything else keeps
      // the generic sentence: an aborted send is already in the surface's own banner in the
      // server's words, and a bug's message is not for this audience.
      onUrgent(
        err instanceof Error && err.name === 'SendRefusal'
          ? err.message
          : 'That message did not send. Everything you typed is still here — try again.',
      )
    } finally {
      setSending(false)
    }
  }, [aui, conversationId, onSubmit, onUrgent, sending, unavailableReason])

  const attachmentComponents = useMemo(
    () => ({ Attachment: () => <AttachmentChip onPreview={setPreview} /> }),
    [],
  )

  /**
   * A REFUSED FILE IS SAID OUT LOUD, and it takes TWO wires because the library refuses in two
   * different places.
   *
   * OUR VALIDATOR — the size caps, the per-message file limit, the per-conversation text budget —
   * runs inside the adapter's `add`, and the library SWALLOWS the throw: both the dropzone and the
   * input's paste handler wrap `addAttachment` in `try { … } catch {}`. So the adapter reports
   * through `useRefusalSink` before it throws.
   *
   * THE FORMAT CHECK NEVER REACHES US AT ALL. The library filters on the adapter's `accept` string
   * BEFORE calling `add`, and emits its own `"File type … is not accepted. Accepted types: …"` —
   * a sentence written for nobody, listing MIME types at someone who dragged in a spreadsheet. So
   * that arm is intercepted here and answered in the words `attachmentInput.ts` already owns,
   * which name what IS accepted rather than what was not.
   *
   * Either way the file is discarded. What these two wires buy is that the citizen is TOLD —
   * without them a dropped file vanishes in silence and they believe the model can see it.
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
      {/* THE DROPZONE IS THE WHOLE BOX, not the input row. The chips and the strip above the row
          are visually "the composer" too, and a drop landing on them would otherwise fall through
          to the browser's default handler — which navigates the tab away and discards both the
          draft and the staged files. */}
      <ComposerPrimitive.AttachmentDropzone data-testid="composer-dropzone">
        <ComposerPrimitive.Root
          data-testid="composer"
          onSubmit={(event) => {
            event.preventDefault()
            void doSend()
          }}
          className="flex w-full flex-col gap-2.5 rounded-[14px] border border-bial-border bg-white px-3 py-[11px]"
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

          {/* NEVER `disabled`, in any state — see the docblock. The library's own `disabled` is
              derived from `thread.isDisabled`, which this project never sets, so the attribute is
              absent rather than false; the guard test asserts the absence directly. */}
          <ComposerPrimitive.Input
            placeholder={placeholder}
            data-testid="composer-input"
            rows={1}
            // NO maxLength. Issue #156 forbids it by name and a test asserts its absence.
            className="max-h-[168px] w-full resize-none bg-transparent text-[13.5px] leading-relaxed text-tertiary placeholder:text-canvas-placeholder focus:outline-none"
          />

          <div className="flex items-center gap-2.5">
            {/* THE ATTACHMENT CONTROL IS INSIDE THE BOX, which is the board's whole point about
                this row. It never goes unavailable: staging a file is composing, not sending. */}
            <ComposerPrimitive.AddAttachment
              data-testid="composer-attach"
              aria-label="Attach a file"
              title="Attach images, PDFs or text files (CSV, TXT), or drop them anywhere in the composer"
              className="inline-flex text-neutral transition hover:text-primary"
            >
              <Paperclip size={16} />
            </ComposerPrimitive.AddAttachment>

            <button
              type="submit"
              aria-disabled={sendUnavailable}
              // The reason rides in the accessible name, so a screen-reader user gets it from the
              // control itself rather than only from a line of text elsewhere on the screen.
              aria-label={unavailableReason ? `Send message — ${unavailableReason}` : 'Send message'}
              title={unavailableReason ?? undefined}
              data-testid="composer-send"
              // THE BOARD'S SEND: a 30px teal circle, not a 36px gold square. Its unavailable
              // treatment is the board's too — a pale ground rather than an opacity, so it still
              // reads as a control rather than as a ghost.
              className={`ms-auto inline-flex h-[30px] w-[30px] flex-shrink-0 items-center justify-center rounded-full transition ${
                sendUnavailable ? 'cursor-default bg-canvas-sendoff text-white' : 'bg-primary text-white hover:bg-primary-600'
              }`}
            >
              <Send size={14} />
            </button>
          </div>
        </ComposerPrimitive.Root>
      </ComposerPrimitive.AttachmentDropzone>

      {footer}

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
