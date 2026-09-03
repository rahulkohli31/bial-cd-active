/**
 * ONE COMPOSER (R40, R41, R42, R43, R45, R55, R57–R60, R64, R72).
 *
 * There were three, with three different growth, draft and restore behaviours: BuilderPage's fixed
 * two-row textarea with a sessionStorage draft and a send that holds the draft until the server
 * confirms; ChatPage's autogrowing input with no draft persistence and a blind restore; and
 * ProjectBuilder's five-row box with a REAL `disabled` on its start button. This replaces the first
 * two. The third is Plan F's to retire onto this same component, which is why the verification
 * counts IMPLEMENTATIONS rather than call sites — a call-site count goes stale the moment F lands.
 *
 * ══ NOTHING HERE IS EVER `disabled` (R45, R64) ══
 *
 * Not the textarea, not attach, not Send — in any state, including while a turn runs, while an
 * offer is pending, and when today's budget is spent. `disabled` on the currently-focused element
 * blurs it to `document.body`, which is the mechanism behind "it blurs mid-sentence and focus never
 * comes back". This codebase records that twice already and it is not a style preference.
 *
 * Send carries `aria-disabled` with the reason in its accessible name, and THE ENFORCEMENT IS IN
 * THE HANDLER. The attribute is affordance only. A test queries the whole subtree for a real
 * `disabled` attribute and expects none, which is the mechanical form of "the library's Send is not
 * used here".
 *
 * The library's Send could not have been used: `createActionButton` renders
 * `<button disabled={props.disabled || !callback}>` and `useComposerSend` returns no callback while
 * `isRunning && !capabilities.queue` — and `queue` is never registered. So it is guaranteed to ship
 * the exact bug R45 forbids, for the whole of every turn.
 *
 * ══ THE DRAFT IS CONSUMED, NOT OWNED (R-7) ══
 *
 * `utils/composerDraft.ts` is the one store, and this is its ONE WRITER. `sessionStorage`, per
 * conversation, last-writer-wins across tabs, cleared only on a SUCCESSFUL send, throw-wrapped
 * because `sessionStorage` genuinely throws in Safari private mode rather than degrading.
 *
 * A second writer is the interleaving hazard R58 is about, so there is exactly one.
 *
 * ══ ISSUE #154's FOUR DEFECTS ARE PROPERTIES HERE, NOT PATCHES (R57–R60) ══
 *
 * R58 — THE DRAFT OVERWRITE. `ChatPage` did a blind `setText(rawText)` on a failed send, and
 *   because the input was fully controlled the browser's undo stack could not recover what it
 *   replaced. The better design already existed in the other file: BuilderPage HOLDS the draft
 *   until the server confirms, and therefore needs no restore at all. That is what this does — the
 *   text is never cleared optimistically, so there is nothing to put back and no race to guard.
 * R59 — DROPPED IN-FLIGHT READS. A `fileToBase64` still resolving when the composer cleared was
 *   discarded silently, and the citizen believed the model could see a file it could not. Because
 *   nothing clears until the server confirms, a read that lands late lands on the send it belongs
 *   to; if the send has already gone, the reader is TOLD rather than left with silence.
 * R57 — THE CAP BYPASS lived in `usePendingAttachments.restorePending`, which merged a restored
 *   batch with no per-message clamp. It is fixed there, and this composer additionally never
 *   CALLS it: nothing is cleared optimistically, so there is nothing to restore. Both halves
 *   matter — the clamp because the function is still exported and reachable, and the non-call
 *   because a defect you cannot reach is better than one you have guarded.
 * R60 — CROSS-CHAT LEAKAGE is guarded by the send path stamping the conversation it is sending to
 *   and comparing on completion.
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type FC,
  type KeyboardEvent,
} from 'react'
import { Paperclip, Send, X } from 'lucide-react'
import TextareaAutosize from 'react-textarea-autosize'

import { ACCEPT_ATTR, type PendingAttachment } from '../../utils/attachmentInput'
import { capState, MAX_COMPOSER_CHARS } from '../../utils/composerCap'
import { clearDraft, readDraft, writeDraft } from '../../utils/composerDraft'
import { usePendingAttachments } from '../../hooks/usePendingAttachments'
import Announcer from './Announcer'
import AttachmentPreview, { type PreviewTarget } from './AttachmentPreview'
import OfferStrip, { OFFER_GATE_NOTE, type OfferStripProps } from './OfferStrip'
import StopTurnControl, { type StopTurnControlProps } from './StopTurnControl'

/** What one send carries. The composer assembles it; the surface performs it. */
/**
 * A refusal whose message was WRITTEN FOR THE CITIZEN and is therefore safe to show.
 *
 * The type is the permission. `onSubmit` can reject for reasons that are nobody's business on
 * screen — a `TypeError` from a bug, an aborted send the surface has already explained in its own
 * banner with the server's wording — and showing `err.message` for those would put developer text,
 * or a second differently-worded copy of the banner, in front of someone asking for an app. Only
 * this class means "say this out loud".
 *
 * `silent` covers the press the surface swallowed because an identical one is already in flight:
 * the citizen did not knowingly make it, so there is nothing to report — but it must still reject,
 * because resolving would empty the composer for a press that sent nothing.
 */
export class SendRefusal extends Error {
  readonly silent: boolean
  constructor(message: string, opts: { silent?: boolean } = {}) {
    super(message)
    this.name = 'SendRefusal'
    this.silent = opts.silent ?? false
  }
}

export interface ComposerSubmission {
  text: string
  attachments: PendingAttachment[]
  /** The conversation this send belongs to, stamped at press time (R60). */
  conversationId: string
}

export interface ComposerProps {
  conversationId: string | null
  /** Placeholder — the only copy that differs between the two kinds, and it is a hint, not a mode. */
  placeholder?: string
  /**
   * Perform the send. Resolves on server confirmation; REJECTS on failure, and the composer keeps
   * everything it was holding.
   */
  onSubmit: (submission: ComposerSubmission) => Promise<void>
  /** True while a turn is in flight — Send waits, typing does not. */
  isRunning: boolean
  /** Any other reason Send must wait, with the sentence that explains it. */
  gate?: { blocked: boolean; reason: string } | undefined
  /** R55 — the relocated stop, given its permanent home on this chrome. */
  stop?: Omit<StopTurnControlProps, 'onStopFailed'> | undefined
  /** R29 — the pending plan offer, rendered on the composer rather than in the transcript. */
  offer?: Omit<OfferStripProps, 'onFailed'> | undefined
  /**
   * The "this chat is getting long" line, or null/absent when it is not.
   *
   * A SENTENCE, NOT A NUMBER AND NOT A STATE. The surface owns the transcript and therefore
   * owns the estimate (`utils/contextLimits.ts`); the composer's job is to show the line where
   * a citizen will read it. Passing the computed sentence rather than the messages is what
   * keeps this component free of a second opinion about how long a conversation is — the
   * failure mode this whole guardrail is being rebuilt out of.
   *
   * IT IS NOT A GATE. Send stays available past the soft threshold; the hard boundary is the
   * server's refusal, which arrives as an ordinary turn error.
   */
  contextWarning?: string | null | undefined
  /** Urgent sentences go to the assertive slot the surface owns. */
  onUrgent: (message: string) => void
}

const MIN_ROWS = 2
const MAX_ROWS = 12

const Composer: FC<ComposerProps> = ({
  conversationId,
  placeholder = 'Ask for another change…',
  onSubmit,
  isRunning,
  gate,
  stop,
  offer,
  contextWarning,
  onUrgent,
}) => {
  const [text, setText] = useState('')
  const [preview, setPreview] = useState<PreviewTarget | null>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const {
    pendingAttachments,
    handleFileSelect,
    removePending,
    clearPending,
    attachToast,
    draggingFiles,
    dragHandlers,
  } = usePendingAttachments()

  // Hydrate the draft for whichever conversation is mounted, and re-hydrate on a switch. The store
  // is keyed per conversation, so this is what makes a draft follow its own chat rather than
  // leaking into a sibling.
  //
  // THE STAGED FILES GO WITH IT, and they are dropped rather than restored because — unlike the
  // draft — they live only in memory as decoded bytes, so there is nothing to hydrate them back
  // from. Leaving them was the worse of the two: this composer is NOT remounted per chat (flat
  // routing keeps one instance), so a file picked in one chat stayed staged in the next, counted
  // against that chat's attachment and text budgets, and would have been sent into a conversation
  // the citizen never attached it to.
  useEffect(() => {
    setText(readDraft(conversationId))
    clearPending()
  }, [conversationId, clearPending])

  const cap = useMemo(() => capState(text), [text])
  const hasContent = text.trim().length > 0 || pendingAttachments.length > 0
  const offerPending = Boolean(offer && !offer.spent && offer.toolCallId)

  /**
   * Why Send is unavailable — one sentence, always stated, with a distinct reason per cause.
   * Plain language: a citizen reading this asked for an app and is watching it get made.
   */
  const unavailableReason = useMemo<string | null>(() => {
    // THE ORDER IS BY IMMEDIACY, and the offer is LAST on purpose. All four block Send, but the
    // first three describe something happening right now — the citizen's own text is too long, a
    // reply is arriving, their app is being built — while a pending offer describes a question
    // still waiting. The offer also stays pending for the whole of the round trip its own Build
    // press starts, so putting it first told a citizen to "choose one of the two above" while the
    // build they had just chosen was starting.
    if (cap.over) return cap.message
    if (isRunning) return 'Replying — keep typing if you like; send unlocks when it’s done.'
    if (gate?.blocked) return gate.reason
    if (offerPending) return OFFER_GATE_NOTE
    return null
  }, [offerPending, cap.over, cap.message, isRunning, gate])

  const sendUnavailable = unavailableReason !== null || !hasContent

  const doSend = useCallback(async () => {
    // THE ENFORCEMENT, and the whole of it. `aria-disabled` on the button says so; it does not do
    // so. Pressing Enter, clicking a visually-dimmed Send and calling this directly all land here.
    if (unavailableReason !== null || !hasContent || !conversationId) return

    const submission: ComposerSubmission = {
      text,
      attachments: pendingAttachments,
      // R60 — stamped at PRESS time. If the reader moves to another chat mid-send, the completion
      // is compared against this and never writes into whichever chat they are now looking at.
      conversationId,
    }

    try {
      await onSubmit(submission)
      // Cleared ONLY on success (R58/R59). Nothing is optimistically emptied, so a failed send
      // leaves the text and the files exactly where they were and there is no restore path to race
      // with — which is how the whole class of defect stops existing rather than being guarded.
      setText('')
      clearDraft(conversationId)
      clearPending()
    } catch (err) {
      // The swallowed press: nothing sent, nothing said, nothing cleared.
      if (err instanceof SendRefusal && err.silent) return
      // THE REFUSAL'S OWN WORDS, but only when they were written to be read. `onSubmit` throws a
      // distinct sentence per reason — a build running here, the daily cap, the attachment cap, no
      // project open — and collapsing all of them into one generic line told a citizen "that did
      // not send" while withholding the only thing that would let them act on it. Anything else
      // that rejects keeps the generic sentence: an aborted send is already in the surface's own
      // banner in the server's words, and a bug's message is not for this audience.
      onUrgent(
        err instanceof SendRefusal
          ? err.message
          : 'That message did not send. Everything you typed is still here — try again.',
      )
    }
  }, [
    unavailableReason,
    hasContent,
    conversationId,
    text,
    pendingAttachments,
    onSubmit,
    onUrgent,
    clearPending,
  ])

  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        void doSend()
      }
    },
    [doSend],
  )

  const onChange = useCallback(
    (e: ChangeEvent<HTMLTextAreaElement>) => {
      // NO truncation, ever, at any length. The text is stored exactly as typed or pasted — R42's
      // whole point is that a citizen whose paste was silently cut believes it all went in.
      setText(e.target.value)
      writeDraft(conversationId, e.target.value)
    },
    [conversationId],
  )

  return (
    <div
      data-testid="composer"
      // The drop-target state as an ATTRIBUTE, not only as a ring class. A colour change is not a
      // thing a test can assert without pinning a Tailwind string, and it is not a thing a screen
      // reader can perceive at all — so the state is exposed here and the ring is the visual half
      // of the same fact. Carried over from the surface this composer replaces, where it was the
      // one assertion the drag-and-drop suites actually discriminated on.
      data-dragging={draggingFiles || undefined}
      {...dragHandlers}
      // The drop target is the WRAPPER, not the inner row: the chips and the gate note above the
      // row are visually "the composer" too, and a drop landing on them would otherwise fall
      // through to the browser's default handler — which navigates the tab away and discards both
      // the draft and the staged files.
      className={`flex flex-col gap-1 border-t border-bial-border bg-bial-surface px-3 py-2 transition ${
        draggingFiles ? 'ring-2 ring-inset ring-primary/40' : ''
      }`}
    >
      {/* ONE polite region for the attachment toast — permanently mounted, so its text is
          announced when it arrives rather than being injected together with its region. */}
      <Announcer message={attachToast} />

      {offer && (
        <OfferStrip
          {...offer}
          onFailed={onUrgent}
        />
      )}

      {pendingAttachments.length > 0 && (
        <div data-testid="composer-chips" className="flex flex-wrap gap-1.5 pb-1">
          {pendingAttachments.map((a) => (
            // Preview and remove are SEPARATE TARGETS, and remove is a SIBLING at a higher layer
            // rather than a nested interactive descendant — the project page's own invariant.
            <span
              key={a.id}
              className="relative inline-flex items-center gap-1.5 rounded-lg border border-bial-border bg-bial-bg px-2 py-1 text-xs text-tertiary"
            >
              <button
                type="button"
                onClick={() =>
                  setPreview({
                    name: a.name,
                    mediaType: a.mediaType,
                    dataUrl: `data:${a.mediaType};base64,${a.base64}`,
                  })
                }
                className="max-w-[10rem] truncate hover:text-primary"
                title={`Open ${a.name}`}
              >
                {a.name}
              </button>
              <button
                type="button"
                onClick={() => removePending(a.id)}
                aria-label={`Remove ${a.name}`}
                className="text-neutral transition hover:text-danger"
              >
                <X size={11} />
              </button>
            </span>
          ))}
        </div>
      )}

      {unavailableReason && (
        <p data-testid="composer-gate-note" role="status" className="text-xs text-neutral">
          {unavailableReason}
        </p>
      )}

      {/* R55 — stop's permanent home. U3 built it and mounted it into the block this replaces; if
          it were not re-mounted here it would have been deleted with `BuildProgress`, and the
          surface would ship with a running build and no way to stop it. */}
      {stop && (
        <div className="flex justify-end pb-1">
          <StopTurnControl {...stop} onStopFailed={onUrgent} />
        </div>
      )}

      <div className="flex items-end gap-2">
        <input
          ref={fileInputRef}
          type="file"
          accept={ACCEPT_ATTR}
          multiple
          onChange={handleFileSelect}
          className="hidden"
          data-testid="composer-file-input"
        />
        {/* Attach NEVER goes unavailable. Staging a file is composing, not sending. */}
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          title="Attach images, PDFs or text files (CSV, TXT), or drop them anywhere in the composer"
          aria-label="Attach a file"
          className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl border border-bial-border bg-bial-bg text-neutral transition hover:bg-surface-muted hover:text-primary"
        >
          <Paperclip size={13} />
        </button>

        {/* NEVER disabled, NEVER removed — in any state. Growth is bounded and then it scrolls. */}
        <TextareaAutosize
          ref={inputRef}
          value={text}
          onChange={onChange}
          onKeyDown={onKeyDown}
          minRows={MIN_ROWS}
          maxRows={MAX_ROWS}
          placeholder={placeholder}
          data-testid="composer-input"
          // NO maxLength. Issue #156 forbids it by name and a test asserts its absence.
          className="flex-1 resize-none rounded-xl border border-bial-border bg-bial-bg px-3 py-2 text-sm text-tertiary transition placeholder:text-gray-400 focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/20"
        />

        <button
          type="button"
          onClick={() => void doSend()}
          aria-disabled={sendUnavailable}
          // The reason rides in the accessible name, so a screen-reader user gets it from the
          // control itself rather than only from a line of text elsewhere on the screen.
          aria-label={unavailableReason ? `Send message — ${unavailableReason}` : 'Send message'}
          title={unavailableReason ?? undefined}
          data-testid="composer-send"
          className={`flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl bg-primary text-white transition ${
            sendUnavailable ? 'cursor-default opacity-40' : 'hover:bg-primary-600'
          }`}
        >
          <Send size={13} />
        </button>
      </div>

      {/* The context guardrail's SOFT half: advisory, non-blocking, and deliberately not
          `disabled` anything — see the top-of-file rule. Send still works past this line; what
          stops a turn is the server, and it says so itself. `role="status"` so it is announced
          once when it appears rather than interrupting. */}
      {contextWarning && (
        <p
          role="status"
          data-testid="composer-context-warning"
          className="self-stretch text-xs leading-relaxed text-neutral"
        >
          {contextWarning}
        </p>
      )}

      {/* R43 — silent until it is useful, and then exact. See `composerCap.ts` for why the number
          is code points rather than String.length. */}
      {cap.showCounter && (
        <p
          data-testid="composer-counter"
          className={`self-end text-xs tabular-nums ${cap.over ? 'font-semibold text-danger' : 'text-neutral'}`}
        >
          {cap.count.toLocaleString()} / {MAX_COMPOSER_CHARS.toLocaleString()}
        </p>
      )}

      <AttachmentPreview target={preview} onClose={() => setPreview(null)} />
    </div>
  )
}

export default Composer
