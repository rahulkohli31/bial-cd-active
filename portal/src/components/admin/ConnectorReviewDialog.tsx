/**
 * The administrator's decision dialog — `AdminReview`, opened from `Review` in the queue.
 *
 * THE LARGEST DEPARTURE IN THE PASS IS IN HERE, AND IT IS A GOVERNANCE SHAPE RATHER THAN A
 * STYLE CHOICE. The board draws `YOUR REMARKS` with a permanent `REQUIRED` pill and the sentence
 * `Approving needs a remark as well as declining — Priya reads it either way, and it is kept in
 * the audit log.` Both come off (R10):
 *
 *   · There is no remark on approval AT ALL. The textarea is not in the document at rest, and
 *     the approve button approves immediately with nothing stored. An optional approval remark
 *     would be a write-only column — the audit row carries ids, the citizen is never shown one,
 *     and `ALREADY DECIDED` has no remarks column and no way to reopen a decided row.
 *   · `Decline` REVEALS the box, required, and focuses it. The alternative shape — one textarea
 *     under two outcome buttons, with the outcome chosen after the remark is typed — gives
 *     "required only when declining" no state the pill can ever be in.
 *
 * THE REVEALED STATE IS WHERE A GOVERNANCE MISTAKE WOULD LIVE, so it is specified rather than
 * left to fall out. Once the decline box is showing, the approve button goes `aria-disabled` AND
 * REFUSES TO FIRE: an administrator who has started composing a decline must not be one stray
 * click from granting access to BIAL's operational data instead. `Decline` becomes the submit,
 * `aria-disabled` until the box holds the same minimum the server enforces. `Cancel` closes the
 * whole dialog from either state.
 *
 * PRONOUNS — A DEPARTURE THE PLAN DOES NOT NAME, AND A CORRECTNESS REQUIREMENT RATHER THAN A
 * PREFERENCE. The board's copy reads `One decision, covering every project SHE owns` and
 * `PRIYA reads it either way`, because it was drawn for one fictional person. Shipped copy must
 * not infer anybody's gender from their name, so every sentence here is they/their, and the
 * approve button interpolates the person's own first name off the row. A reviewer holding the
 * board beside this file will find those words changed on purpose.
 *
 * THE CONSENT PANEL SHIPS WHOLE AND SHIPS FROM THE WIRE. All three ticked lines with their bold
 * leads, in the order the server sent them, off `consentLinesApprover` on the row. They are
 * R1-binding promises about what an approval does and does not give somebody, and they live on
 * the registry entry in `backend/src/core/connectors.py` where a test holds them byte-exact
 * against the board. Not one sentence about a particular system is written down in here (R18):
 * the title, the panel and every line come off `request`.
 *
 * BOTH REMARKS ARE PLAIN JSX TEXT. The citizen writes theirs and an administrator reads it; the
 * administrator writes theirs and the citizen reads it. This portal ships a markdown renderer
 * and it must never be pointed at either, and no raw-HTML injection prop may be added here.
 *
 * WHO OWNS WHAT. This dialog owns the two writes, the busy lock and the difference between a
 * refusal worth retrying and a row that is already gone. The panel above it owns the queue: it
 * closes this, reloads both tables and shows the sentence handed to `onSettled`. That split is
 * why a 409 does not render in here — there is nothing left to decide, and a dialog that stayed
 * open over a decided row is a control that can only fail again.
 */
import { useState } from 'react'
import { Check, Loader2, X } from 'lucide-react'
import { Button } from '../ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '../ui/dialog'
import { Textarea } from '../ui/textarea'
import { approveConnectorRequest, declineConnectorRequest } from '../../utils/adminConnectorApi'
import type { ConnectorRequestRow } from '../../utils/adminConnectorApi'
import { ApiError, isRecord, optionalString } from '../../utils/apiError'
import {
  countWords,
  MAX_DELETE_REASON_CHARS,
  MAX_DELETE_REASON_WORDS,
  MIN_DELETE_REASON_WORDS,
} from '../../utils/words'
import { dayMonth } from '../connectors/ConnectorRow'
import { clockTime } from './columns'

/**
 * The box's own furniture — the two headings and the consent panel's title, true of any
 * connector and of any person. Everything else on this screen comes off the row.
 */
const WHO_LABEL = 'WHO'
const REMARKS_LABEL = 'THEIR REMARKS'
const CONSENT_HEADING = 'WHAT APPROVING GIVES THEM'

/**
 * The four sentences `clean_stated_reason` returns, mirrored so the form refuses in the SAME
 * words the API would — a 422 must never be the first news of a rule the counter was already
 * showing.
 *
 * THREE OF THE FOUR. The server's `That reason is too long. Keep it under 2000 characters.` has
 * no mirror because the textarea's `maxLength` is that number: the branch is unreachable from
 * the browser, and an unreachable branch here would be dead code claiming to be a guard. It is
 * still rendered if the server ever returns it, through the ordinary inline error region.
 */
const SAY_WHY = 'Say why you are declining this request.'
const TOO_SHORT = `Give a little more detail — at least ${MIN_DELETE_REASON_WORDS} words.`
const TOO_LONG = `Keep the reason under ${MAX_DELETE_REASON_WORDS} words.`

const RULE_ID = 'connector-decline-rule'
const COUNT_ID = 'connector-decline-count'

/** The small-caps section labels the board sets above `WHO` and `THEIR REMARKS`. */
const SECTION_LABEL = 'text-[10.5px] font-extrabold tracking-[.4px] text-canvas-label mb-[5px]'
/** One action button at the board's metrics, over the shadcn base's own sizing. */
const ACTION = 'h-auto rounded-[9px] px-4 py-[9px] text-[12.5px] [&_svg]:size-[13px]'

/**
 * Why this remark cannot be sent, in the server's own words, or `null` when it can.
 *
 * `countWords(value) === 0` IS THE EMPTY CHECK, NOT `value.trim() === ''`. The server strips by
 * Python's whitespace set and then splits by it, so "blank" there means exactly "splits into no
 * words" — and `words.ts` is built on that set precisely because JS `.trim()` is not it. A
 * `.trim()` here would disagree with the server on a leading BOM and agree with it everywhere
 * else, which is the worst kind of disagreement to own.
 */
function declineRefusal(value: string): string | null {
  const words = countWords(value)
  if (words === 0) return SAY_WHY
  if (words < MIN_DELETE_REASON_WORDS) return TOO_SHORT
  if (words > MAX_DELETE_REASON_WORDS) return TOO_LONG
  return null
}

/**
 * The `409 already_decided` sentence, composed HERE from `error.detail`.
 *
 * THE SERVER NAMES WHO AND WHAT AND CANNOT NAME WHEN. There is no human-readable date formatting
 * anywhere in `backend/src/`, and the console this opens over already formats every date on the
 * screen behind it — so the instant rides `error.detail.decidedAt` and the whole sentence is
 * built where the formatter lives. `dayMonth` and not a clock, deliberately: the decided row this
 * refusal points at renders `2 Sep · you`, and a sentence carrying a time the row does not show
 * would look like a second, different decision.
 *
 * FALLS BACK TO THE SERVER'S OWN MESSAGE if the detail is not there or not readable. That message
 * still names who and what; only the date is lost, which is better than a sentence built around
 * `undefined`.
 */
function alreadyDecidedSentence(error: ApiError): string {
  const detail =
    isRecord(error.details) && isRecord(error.details.detail) ? error.details.detail : null
  if (detail === null) return error.message
  const when = optionalString(detail.decidedAt)
  const what =
    detail.status === 'approved' ? 'approved' : detail.status === 'declined' ? 'declined' : null
  if (when === null || what === null) return error.message
  // `null` when the deciding administrator's account is gone — a decision outlives its decider,
  // and the sentence then reads "An administrator" rather than leaving a hole where a person was.
  const who = optionalString(detail.decidedByName) ?? 'An administrator'
  return `${who} already ${what} this request on ${dayMonth(when)}.`
}

/**
 * The row is gone rather than the request having failed — the sentence to close on, or `null`
 * when this is a refusal worth staying open for.
 *
 * EVERY 409 IS "GONE", AND THE CODE IS WHAT PICKS THE WORDS. `already_decided` and
 * `request_cancelled` are opposite facts wearing one status — another administrator answered
 * first, or the citizen withdrew between the render and the click — and only the first has an
 * `error.detail` to build a date from; the second deliberately carries none, because a cancelled
 * row's decision fields are both null and there is nothing measured to hand over. `not_waiting`
 * is the third arm the server keeps for a status it does not recognise, and its own sentence is
 * the right one to show. A 403, a 422 or a dead network are none of these: they leave the dialog
 * open with the remark still in the box.
 */
function rowIsGone(caught: unknown): string | null {
  if (!(caught instanceof ApiError) || caught.status !== 409) return null
  if (caught.code === 'already_decided') return alreadyDecidedSentence(caught)
  return caught.message
}

/** The name the approve button uses. The first word of the handle the SERVER settled on, so a
 *  person with no display name gets the first token of their work email rather than a blank. */
function firstName(displayName: string): string {
  const [first] = displayName.trim().split(/\s+/)
  return first === undefined || first === '' ? displayName : first
}

export interface ConnectorReviewDialogProps {
  /** The waiting row `Review` was pressed on. Every string on screen comes off it (R18). */
  request: ConnectorRequestRow
  /** Dismissed with nothing written — `Cancel`, the corner X, Escape, or a press outside. */
  onClose: () => void
  /**
   * The queue behind this dialog is now stale, and the panel is what fixes that: close, reload
   * both tables, show `message`. Fired for a decision that landed AND for a row that was gone
   * before it could — only the severity tells those apart, because in both cases the row the
   * administrator was looking at is no longer where it was.
   */
  onSettled: (message: string, severity: 'ok' | 'problem') => void
}

export default function ConnectorReviewDialog({
  request,
  onClose,
  onSettled,
}: ConnectorReviewDialogProps): React.JSX.Element {
  /** `false` until `Decline` is pressed. It is what puts the textarea in the document at all. */
  const [declining, setDeclining] = useState(false)
  const [remarks, setRemarks] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const words = countWords(remarks)
  const refusal = declineRefusal(remarks)
  // The approve path closes the moment the decline box opens. `aria-disabled` says so; this is
  // what makes it true, and `dialog.tsx`'s docblock is why it is not the `disabled` attribute
  // (which throws focus to `<body>` from under whoever is standing on the control).
  const canApprove = !declining && !busy
  const canDecline = declining && refusal === null && !busy

  const clock = clockTime(request.askedAt)
  const asked =
    clock === null ? dayMonth(request.askedAt) : `${dayMonth(request.askedAt)} at ${clock}`
  const person = firstName(request.displayName)
  const invalid = remarks.length > 0 && refusal !== null

  const close = (): void => {
    if (!busy) onClose()
  }

  const run = async (call: () => Promise<void>, landed: string): Promise<void> => {
    setBusy(true)
    setError(null)
    try {
      await call()
      onSettled(landed, 'ok')
    } catch (caught) {
      const gone = rowIsGone(caught)
      if (gone !== null) onSettled(gone, 'problem')
      else setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  const approve = (): void => {
    // The real refusal. `aria-disabled` announces it without enforcing it, and this is the one
    // control where the difference matters: the alternative is a stray click granting access.
    if (!canApprove) return
    void run(
      () => approveConnectorRequest(request.id),
      `${request.displayName} can now reach ${request.connectorDisplayName}.`,
    )
  }

  const decline = (): void => {
    if (!declining) {
      // First press REVEALS. Nothing is sent, and the approve path is closed from here on.
      setDeclining(true)
      setError(null)
      return
    }
    if (!canDecline) return
    void run(
      () => declineConnectorRequest(request.id, remarks),
      `${request.displayName}’s request was declined.`,
    )
  }

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        // Escape, the overlay press and the corner X all arrive here. A write in flight holds it
        // open: the decision lands either way, and a dialog that vanished mid-write would leave
        // an administrator with no idea whether they had decided.
        if (!next && !busy) onClose()
      }}
    >
      <DialogContent
        hideClose
        data-testid="connector-review-dialog"
        // The board's softened scrim (`rgba(15,23,42,.16)` over a 3px blur), as every other
        // canvas-drawn dialog in this portal passes it — not shadcn's flat `bg-black/80`.
        overlayClassName="bg-slate-900/15 backdrop-blur-[3px] [-webkit-backdrop-filter:blur(3px)]"
        // `p-0 gap-0`: the header, the body and the action row each own their padding, because
        // the board gives them three different ones.
        className="font-manrope w-full max-w-[600px] gap-0 rounded-2xl border-0 bg-white p-0 shadow-2xl"
      >
        <div className="flex items-start gap-2.5 px-6 pt-[22px]">
          <div className="min-w-0 flex-1">
            <DialogTitle className="text-base font-extrabold tracking-[-0.2px] text-primary-900">
              Give {request.displayName} access to {request.connectorDisplayName}?
            </DialogTitle>
            {/* `they own`, not the board's `she owns` — see the pronoun paragraph above. */}
            <DialogDescription className="mt-[5px] text-xs leading-[1.6] text-neutral">
              Asked on {asked}. One decision, covering every project they own.
            </DialogDescription>
          </div>
          <button
            type="button"
            onClick={close}
            aria-label="Close"
            className="flex-shrink-0 p-0.5 text-neutral transition hover:text-primary-900"
          >
            <X size={17} />
          </button>
        </div>

        <div className="flex flex-col gap-[15px] px-6 pt-[18px]">
          <div>
            <div className={SECTION_LABEL}>{WHO_LABEL}</div>
            {/* ONE INLINE LINE, which is how the board draws it here — the queue's two-line
                stack belongs to a cell 200px wide, not to a 600px dialog. The work email is in
                place of the board's `department`, which exists nowhere in this product. */}
            <p className="m-0 text-[12.5px] leading-[1.65] text-primary-900">
              {request.displayName} — {request.email}
            </p>
          </div>
          <div>
            <div className={SECTION_LABEL}>{REMARKS_LABEL}</div>
            {/* Quoted, in full, as PLAIN TEXT. One user wrote this and another is deciding on
                it: never the markdown renderer, and never a raw-HTML injection prop. */}
            <p
              data-testid="requester-remarks"
              className="m-0 text-[12.5px] leading-[1.65] text-primary-900"
            >
              “{request.requesterRemarks}”
            </p>
          </div>
        </div>

        <div
          data-testid="approval-consent"
          className="mx-6 mt-4 rounded-xl border border-bial-border bg-canvas-group px-3.5 py-3"
        >
          <div className="mb-1.5 text-[10.5px] font-extrabold tracking-[.5px] text-neutral">
            {CONSENT_HEADING}
          </div>
          {request.consentLinesApprover.map((line) => (
            <div key={line.lead} className="flex items-start gap-2 py-1">
              <span className="mt-0.5 flex-shrink-0">
                <Check size={12} strokeWidth={2.4} className="text-primary-dark" aria-hidden />
              </span>
              <p className="m-0 text-[11.5px] leading-[1.55] text-neutral">
                <b className="font-bold text-primary-900">{line.lead}</b> {line.body}
              </p>
            </div>
          ))}
        </div>

        {declining && (
          <div className="mt-[15px] px-6">
            <div className="mb-1.5 flex items-center gap-2">
              <span className="text-[11px] font-bold tracking-[.3px] text-neutral">
                YOUR REMARKS
              </span>
              {/* The board's pill, kept — but only in the state where it is TRUE. At rest there
                  is no box for it to be required of. */}
              <span className="ml-auto inline-flex items-center rounded-full bg-status-amber-bg px-[7px] py-0.5 text-[9.5px] font-extrabold tracking-[.5px] text-status-amber-fg">
                REQUIRED
              </span>
            </div>
            <Textarea
              // Focused on reveal: `Decline` was a deliberate press, and the next thing the
              // administrator has to do is type. Mounted only now, so this fires on reveal.
              autoFocus
              data-testid="decline-remarks"
              value={remarks}
              onChange={(event) => setRemarks(event.target.value)}
              rows={3}
              // A paste backstop at the server's own character cap, not the rule anybody is told
              // about — that is the word count under the box.
              maxLength={MAX_DELETE_REASON_CHARS}
              aria-label={`Why you are declining ${request.displayName}’s request`}
              aria-describedby={`${RULE_ID} ${COUNT_ID}`}
              className="min-h-[52px] resize-y rounded-[10px] border-bial-border px-[13px] py-[11px] text-[12.5px] leading-[1.65] text-primary-900"
            />
            <div className="mt-1.5 flex items-baseline justify-between gap-3">
              {/* THE BOARD'S STRUCK SENTENCE'S SLOT. It said approving needs a remark too, which
                  is no longer true; what replaces it is the rule the submit actually enforces,
                  and — once anything is typed — the server's own words for why it will not go. */}
              <span
                id={RULE_ID}
                data-testid="decline-rule"
                className={`text-[11px] leading-[1.55] ${
                  invalid ? 'font-semibold text-danger' : 'text-neutral'
                }`}
              >
                {invalid
                  ? refusal
                  : `Between ${MIN_DELETE_REASON_WORDS} and ${MAX_DELETE_REASON_WORDS} words. ${person} reads this, and it is the whole of what they are told.`}
              </span>
              {/* WORDS, COUNTED BY THE FUNCTION THE SERVER COUNTS WITH. The number under the box
                  and the rule that refuses the submit read the same `countWords`, so they cannot
                  disagree — which is the whole reason `words.ts` mirrors `core/words.py`. */}
              <span
                id={COUNT_ID}
                data-testid="decline-count"
                className={`whitespace-nowrap text-[11px] tabular-nums ${
                  invalid ? 'font-semibold text-danger' : 'text-neutral'
                }`}
              >
                {words}/{MAX_DELETE_REASON_WORDS} words
              </span>
            </div>
          </div>
        )}

        {error !== null && (
          <div
            role="alert"
            data-testid="review-error"
            className="mx-6 mt-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2.5"
          >
            <p className="text-xs text-red-600">{error}</p>
          </div>
        )}

        <div className="mt-[18px] flex items-center gap-2.5 px-6 pb-5">
          {/* LEFT, RED-OUTLINED, exactly where the board puts it — and the only caller of the
              `destructive` variant, selected by string literal (see `button.tsx` departure 4). */}
          <Button
            type="button"
            variant="destructive"
            data-testid="review-decline"
            aria-disabled={declining && !canDecline}
            onClick={decline}
            className={`${ACTION} font-bold aria-disabled:opacity-60`}
          >
            {busy && declining ? <Loader2 className="animate-spin" aria-hidden /> : <X aria-hidden />}
            Decline
          </Button>
          <Button
            type="button"
            variant="outline"
            data-testid="review-cancel"
            onClick={close}
            className={`${ACTION} ml-auto font-semibold text-neutral`}
          >
            Cancel
          </Button>
          <Button
            type="button"
            data-testid="review-approve"
            aria-disabled={!canApprove}
            onClick={approve}
            className={`${ACTION} px-[18px] font-bold aria-disabled:opacity-60`}
          >
            {busy && !declining ? (
              <Loader2 className="animate-spin" aria-hidden />
            ) : (
              <Check aria-hidden />
            )}
            Give {person} access
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
