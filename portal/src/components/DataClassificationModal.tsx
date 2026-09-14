/**
 * The pre-publish form — six weighted Yes/No questions, pre-filled from an automatic
 * review of the app's last SAVED version, plus a gated explanation.
 *
 * WHY THIS EXISTS
 * Opening the dialog ensures a review exists for that version; while one runs, the
 * citizen can already answer. Confirm posts the answers to the deploy route, where the
 * SERVER re-reads the stored review and merges, taking the stricter of the two — nothing
 * this dialog computes rides in as authority. The running total shown here decides
 * nothing: it only drives the action's label, the explanation gate, and the score line.
 * MERGE, NEVER CLOBBER: a review verdict lands only on an untouched question, never
 * overwriting an answer the citizen already gave — a touched question instead shows the
 * verdict alongside as a disagreement, scoped to the version this dialog asked about.
 * ESCAPE AND CANCEL STAY AVAILABLE WHILE THE REVIEW RUNS — closing loses nothing since the
 * result is stored against the version; the block on them applies only to a SUBMIT in
 * flight. Reasons render as whitespace-preserving plain text, never through the shared
 * markdown renderer, which collapses single newlines.
 * FOCUS IS PART OF THE CONTRACT, as in `ReclaimWorkspaceDialog`: the trap must survive the
 * busy window, since a request in flight disables Confirm/Cancel and blurs focus to
 * `<body>`. Its contents ARRIVE after opening, so progress, arrival, and failure all
 * announce through a polite live region (`dc-review-status`).
 *
 * Unanswered vs. No is load-bearing too (the backend only accepts a complete six-of-six
 * set): `answers` starts as six `null`s, never `false`s, so "not yet answered" can never
 * read as "said no" — and a question the review left unanswered stays null until the
 * citizen acts.
 */
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { ShieldAlert } from 'lucide-react'
import { BusyGlyph } from './ui/Waiting'
import {
  AUTO_DEPLOY_MAX_SCORE,
  DATA_CLASSIFICATION_QUESTIONS,
  totalWeight,
  type DataClassificationAnswers,
} from '../utils/deployApi'
import {
  ensureClassificationReview,
  getClassificationReview,
  mergeWithReview,
  type ClassificationReview,
} from '../utils/classificationApi'
import { ApiError } from '../utils/apiError'
import { assertNever } from '../utils/assertNever'

type CategoryKey = (typeof DATA_CLASSIFICATION_QUESTIONS)[number][0]
type AnswerState = Record<CategoryKey, boolean | null>

const UNANSWERED: AnswerState = {
  credentialsSecrets: null,
  healthData: null,
  personalInformation: null,
  financialData: null,
  confidentialBusinessData: null,
  publicData: null,
}

/** Mirrors the deploy hook's cadence: the review's phases last tens of seconds, so
 *  anything tighter is load without extra information. */
const REVIEW_POLL_MS = 5000

/**
 * Where the dialog's review has got to. `asking` is the ensure-POST in flight;
 * `ready` is any server-shaped state (including `running`, which the poll advances);
 * `unreachable` is a transport-level failure of the ask itself — the server never
 * answered, so there is no stored state to show.
 */
type ReviewPhase =
  | { kind: 'asking' }
  | { kind: 'ready'; review: ClassificationReview }
  | { kind: 'unreachable'; message: string; retryable: boolean }

// The waiting copy tells the truth about closing: the result is stored against
// the version, so closing loses nothing — ask them to wait without claiming a loss
// that does not happen, and set the real time expectation.
const WAITING_COPY =
  'We’re checking your saved app for the kinds of data it handles. This usually takes ' +
  'about 20 seconds — sometimes up to a minute. You can close this and come back; the ' +
  'result is kept for this version.'

const ARRIVAL_COPY =
  'The automatic check has finished. Each question below starts from what it found — ' +
  'you can change any answer.'

// No saved code means nothing for a review to read, and nothing to publish either.
// The server's nothing-to-review response carries no sentence, so this copy is the
// client's, matching the server's own taxonomy sentence for the same state.
const NOTHING_SAVED_COPY = 'There’s nothing saved to check yet — press Save first.'

// A GET-only state in the wire contract; the ensure-POST this dialog opens with always
// claims a run, so this renders only defensively.
const NOT_REVIEWED_COPY =
  'The automatic check hasn’t run for this version yet. Answer the questions below yourself.'

function unreachableFrom(err: unknown): ReviewPhase {
  return {
    kind: 'unreachable',
    message:
      err instanceof ApiError ? err.message : 'We couldn’t reach the server. Please try again.',
    // The server's 503 (storage down) is worth a re-check — so is a network blip. A 4xx
    // is not: asking again with the same request cannot change the answer.
    retryable: !(err instanceof ApiError) || err.status >= 500,
  }
}

function formatSavedAt(iso: string): string {
  const parsed = new Date(iso)
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString()
}

interface Props {
  projectId: string
  /**
   * The administrator's note from the last rejection, when the app is sitting rejected.
   * Rendered FIRST, above the questions: a citizen who presses Publish after a rejection
   * has to read why before anything else happens, and a note that lives only on a card
   * beside this dialog is a note they can publish straight past. Null when there is
   * nothing to say — a caller passing `undefined` gets the same nothing.
   */
  rejectionNote?: string | null
  /**
   * An administrator has already approved the version this dialog asks about, so the
   * server publishes it whatever the answers score — the approval pins that exact commit
   * and outranks the declaration. The dialog has to know, or it promises a review the
   * server will not perform and compels an explanation for a decision already taken.
   */
  alreadyApproved?: boolean
  onConfirm: (answers: DataClassificationAnswers) => Promise<void>
  onCancel: () => void
}

export default function DataClassificationModal({
  projectId,
  rejectionNote = null,
  alreadyApproved = false,
  onConfirm,
  onCancel,
}: Props): React.ReactElement {
  const [answers, setAnswers] = useState<AnswerState>(UNANSWERED)
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [phase, setPhase] = useState<ReviewPhase>({ kind: 'asking' })
  const cardRef = useRef<HTMLDivElement>(null)
  const firstQuestionRef = useRef<HTMLButtonElement>(null)

  // The version stamp this dialog asked about, latched from each ensure-POST response.
  // Poll responses are filtered against it — see the module comment's stamp rule.
  const askedShaRef = useRef<string | null>(null)
  // Questions the citizen has clicked. A review's verdicts land only on the others.
  const touchedRef = useRef<Set<CategoryKey>>(new Set())
  // One generation per mount; bumped on unmount and on every fresh ask, so a stale
  // response can never paint over a newer state (the deploy hook's discipline).
  const generation = useRef(0)
  useEffect(
    () => () => {
      generation.current += 1
    },
    [],
  )

  /** Accept a server response: record it, and merge its verdicts into the untouched
   *  questions — merge, never clobber. */
  const applyReview = useCallback((review: ClassificationReview): void => {
    setPhase({ kind: 'ready', review })
    const verdicts = review.verdicts
    if (!verdicts) return
    setAnswers((prev) => {
      const next = { ...prev }
      for (const [key] of DATA_CLASSIFICATION_QUESTIONS) {
        if (touchedRef.current.has(key)) continue
        const verdict = verdicts[key].verdict
        next[key] = verdict === 'yes' ? true : verdict === 'no' ? false : null
      }
      return next
    })
  }, [])

  /** The ensure-POST — on open, and again on "Check again" (there is no separate
   *  retry verb). Latches the version stamp the response names. */
  const ask = useCallback(async (): Promise<void> => {
    const mine = ++generation.current
    setPhase({ kind: 'asking' })
    try {
      const first = await ensureClassificationReview(projectId)
      if (generation.current !== mine) return
      askedShaRef.current = first.headSha
      applyReview(first)
    } catch (err) {
      if (generation.current !== mine) return
      setPhase(unreachableFrom(err))
    }
  }, [projectId, applyReview])

  // Opening the dialog is what asks for the review.
  useEffect(() => {
    void ask()
  }, [ask])

  // Poll ONLY while a run is in flight, on the deploy hook's cadence. The GET never
  // starts a run.
  //
  // TWO GUARDS, and they answer different questions. `generation` says "is this dialog
  // still the one that asked?" — it survives a close/reopen. `tick` says "is this the
  // NEWEST response?" — the interval fires without waiting for the previous request, so
  // two polls overlap whenever one is slow, and without an ordering guard a late
  // `running` landing after a `complete` walked the dialog backwards and re-disabled the
  // button the citizen was about to press.
  const polling = phase.kind === 'ready' && phase.review.status === 'running'
  useEffect(() => {
    if (!polling) return undefined
    const mine = generation.current
    let latest = 0
    let applied = 0
    const timer = window.setInterval(() => {
      const tick = ++latest
      void (async () => {
        try {
          const next = await getClassificationReview(projectId)
          if (generation.current !== mine || tick <= applied) return
          if (next.reviewedSha !== askedShaRef.current) {
            // The app's ONE review row was re-stamped for another version — a second tab,
            // another device, or the deploy pipeline's own drift re-check. The review this
            // dialog is waiting for no longer exists and never will, so polling on was an
            // immortal spinner: Confirm stayed disabled because the review read as
            // pending, and "Check again" never appeared because the status was not
            // `failed`. Only Cancel got the citizen out, and nothing on screen said so.
            applied = tick
            setPhase({
              kind: 'unreachable',
              message:
                'This app was saved again while the check was running, so this check no ' +
                'longer applies to the version you are publishing.',
              retryable: true,
            })
            return
          }
          applied = tick
          applyReview(next)
        } catch (err) {
          if (generation.current !== mine || tick <= applied) return
          applied = tick
          setPhase(unreachableFrom(err))
        }
      })()
    }, REVIEW_POLL_MS)
    return () => window.clearInterval(timer)
  }, [polling, projectId, applyReview])

  const returnFocusRef = useRef<Element | null>(null)
  useEffect(() => {
    returnFocusRef.current = document.activeElement
    firstQuestionRef.current?.focus()
    return () => {
      const target = returnFocusRef.current
      if (target instanceof HTMLElement && document.contains(target)) target.focus()
    }
  }, [])

  useEffect(() => {
    if (busy) cardRef.current?.focus()
  }, [busy])

  const review = phase.kind === 'ready' ? phase.review : null
  const verdicts = review?.verdicts ?? null
  // No saved code means the questions, the score, and the action all go with it.
  const nothingSaved = review?.status === 'nothing_to_review'
  // The ask in flight, or a run in flight. Either way the review hasn't landed, so the
  // action stays disabled: submitting now would route regardless (the server's rule 4),
  // and the label could not yet say which of its two things it will do.
  const reviewPending = phase.kind === 'asking' || review?.status === 'running'

  // The nothing-saved state removes every question from the DOM, and with them the
  // element holding focus — the browser would blur to `<body>`, where the Tab/Escape
  // trap can't hear. Park focus on the card, the same move the busy window makes below.
  useEffect(() => {
    if (nothingSaved) cardRef.current?.focus()
  }, [nothingSaved])

  const allAnswered = Object.values(answers).every((v) => v !== null)
  // THE ANSWER OF RECORD, not the developer's answers alone. The server merges the two
  // sets before it scores anything, so scoring only this side told the developer a number
  // the server would not agree with — 0 and a Publish button on a declaration the server
  // was about to score 45 and route. `mergeWithReview` is the mirror of that merge.
  const recorded = mergeWithReview(
    answers,
    verdicts,
    DATA_CLASSIFICATION_QUESTIONS.map(([key]) => key),
  )
  const total = totalWeight(recorded)
  // A weighted Yes anywhere means this submission is a review request, not a publish —
  // the action's label says so, and the same condition compels the explanation: a routed
  // app is never unexplained, and an explanation is never compelled on a declaration
  // that was going to pass anyway. An APPROVAL OF THIS VERSION outranks the score, exactly
  // as the server does: it publishes the pinned commit, so routing it again is an outcome
  // this dialog cannot deliver and must not name.
  const sensitiveRecorded = total > AUTO_DEPLOY_MAX_SCORE
  const sendForReview = sensitiveRecorded && !alreadyApproved
  const notesRequired = sendForReview
  const notesBlank = notes.trim() === ''
  const confirmDisabled =
    busy || reviewPending || !allAnswered || (notesRequired && notesBlank)

  // What the live region says. One sentence per state; the transitions running→complete
  // (arrival) and running→failed (fall-through) are announced by the text changing.
  let statusSentence: string
  if (phase.kind === 'asking') {
    statusSentence = WAITING_COPY
  } else if (phase.kind === 'unreachable') {
    statusSentence = phase.message
  } else {
    switch (phase.review.status) {
      case 'running':
        statusSentence = WAITING_COPY
        break
      case 'complete':
        statusSentence = ARRIVAL_COPY
        break
      case 'failed':
        // The narrower guarantees a failed review carries its citizen sentence.
        statusSentence = phase.review.failureMessage ?? ''
        break
      case 'nothing_to_review':
        statusSentence = NOTHING_SAVED_COPY
        break
      case 'not_reviewed':
        statusSentence = NOT_REVIEWED_COPY
        break
      default:
        statusSentence = assertNever(phase.review.status)
    }
  }

  // Only a retryable failure offers a re-check (the taxonomy's retry column, already
  // AND-ed with the attempt cap server-side), and the re-check is the ensure-POST again.
  const recheckOffered =
    (phase.kind === 'unreachable' && phase.retryable) ||
    (review?.status === 'failed' && review.retryable)

  // Cancel is the ONLY thing the backdrop, Escape, and the Cancel button call — none of
  // them can reach `onConfirm`/the submit call, structurally, not by a runtime check. A
  // request in flight still blocks Escape (matching `ReclaimWorkspaceDialog`): closing
  // mid-submit would leave the caller unable to learn what happened.
  const onKeyDownTrap = (e: KeyboardEvent<HTMLDivElement>): void => {
    if (e.key === 'Escape') {
      if (!busy) onCancel()
      return
    }
    if (e.key !== 'Tab') return
    const focusables = cardRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), textarea:not([disabled])',
    )
    if (!focusables || focusables.length === 0) {
      e.preventDefault()
      cardRef.current?.focus()
      return
    }
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }

  const handleConfirm = async (): Promise<void> => {
    if (confirmDisabled) return
    setBusy(true)
    setError(null)
    try {
      // Safe: `confirmDisabled` already proved every category is non-null.
      await onConfirm({ ...(answers as Record<CategoryKey, boolean>), notes: notes.trim() || null })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not publish. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  // Plain-language warning — shown only once every question has an answer, so "nothing
  // flagged yet" (unanswered) and "flagged nothing" (all six No) never read the same way.
  // Two states, not three: notes-required and needs-a-human are now
  // the same condition (`notesRequired` above), so there is no longer a middle band that
  // handles some sensitive data but isn't refused — every nonzero total is both.
  let warning: string | null = null
  if (allAnswered) {
    if (notesRequired) {
      warning = "This app handles sensitive data — please explain how it's handled below."
    } else if (sensitiveRecorded) {
      // Approved, and the data is still sensitive: saying nothing was flagged would
      // contradict the Yes answers on the same screen.
      warning = 'This app handles sensitive data, and an administrator approved this version.'
    } else {
      // "Nothing flagged" has to be true of the RECORDED answers, not just the
      // developer's: the check's own Yes verdicts count, and this line used to say
      // nothing was flagged while the check had flagged two things on the same screen.
      warning = 'Nothing sensitive was recorded — by you or by the automatic check.'
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 font-manrope"
      role="dialog"
      aria-modal="true"
      aria-labelledby="data-classification-title"
      data-testid="data-classification-modal"
    >
      <div className="absolute inset-0 bg-black/40" onClick={busy ? undefined : onCancel} />
      <div
        ref={cardRef}
        tabIndex={-1}
        onKeyDown={onKeyDownTrap}
        className="relative bg-white rounded-2xl shadow-2xl w-full max-w-lg p-6 focus:outline-none max-h-[90vh] overflow-y-auto"
      >
        <h3 id="data-classification-title" className="text-base font-bold text-tertiary">
          Before you publish
        </h3>
        <p className="text-sm text-neutral mt-1 leading-relaxed">
          An automatic check reads your saved app and fills in what it finds. Every question
          needs an answer before you can continue — change any answer you disagree with.
        </p>

        {/* BEFORE ANYTHING ELSE, including the version line: an administrator sent this
            back, and the reason is the first thing the citizen needs. A real heading and
            whitespace-preserving prose — never the shared markdown renderer, which
            collapses the single newlines an administrator's note is full of. */}
        {rejectionNote && (
          <section
            aria-labelledby="dc-rejection-heading"
            data-testid="dc-rejection-note"
            className="mt-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2.5"
          >
            <h4 id="dc-rejection-heading" className="text-xs font-bold text-red-800">
              An administrator sent this back
            </h4>
            <p className="mt-1 text-xs text-red-800 leading-relaxed whitespace-pre-wrap break-words">
              {rejectionNote}
            </p>
          </section>
        )}

        {/* State next (the specified reading order): which version this is about, and
            where the check has got to. The status line is a polite live region because
            the dialog's contents arrive after it opens — progress, arrival, and the
            failure fall-through are announced by its text changing. */}
        <div className="mt-3 rounded-xl border border-bial-border bg-bial-bg px-3 py-2.5">
          {review?.headSha && (
            <p data-testid="dc-review-version" className="text-xs font-semibold text-tertiary">
              Version {review.headSha.slice(0, 7)}
              {review.savedAt && (
                <span className="font-normal text-neutral">
                  {' '}
                  · saved {formatSavedAt(review.savedAt)}
                </span>
              )}
            </p>
          )}
          <div
            data-testid="dc-review-status"
            role="status"
            aria-live="polite"
            className="mt-1 flex items-start gap-1.5 text-xs text-neutral leading-relaxed"
          >
            {reviewPending && (
              <BusyGlyph size={13} className="flex-shrink-0 mt-0.5" />
            )}
            <span>{statusSentence}</span>
          </div>
          {recheckOffered && (
            <button
              type="button"
              data-testid="dc-recheck"
              disabled={busy}
              onClick={() => void ask()}
              className="mt-2 text-xs font-semibold text-primary hover:underline disabled:opacity-50"
            >
              Check again
            </button>
          )}
        </div>

        {!nothingSaved && (
          <>
          <div className="mt-4 flex flex-col gap-3">
            {DATA_CLASSIFICATION_QUESTIONS.map(([key, label], index) => {
              const value = answers[key]
              const reviewQuestion = verdicts ? verdicts[key] : null
              const reviewVerdict = reviewQuestion?.verdict ?? null
              // The review left this one to the citizen — visibly distinct from a
              // No, and it blocks the submit through `allAnswered` until answered.
              const needsAnswer = reviewVerdict === 'unanswered' && value === null
              // The citizen's current answer differs from the review's verdict. Theirs
              // stays — merge, never clobber — and the review's is shown alongside
              // (both answer sets are kept and recorded).
              const disagreement =
                (reviewVerdict === 'yes' || reviewVerdict === 'no') &&
                value !== null &&
                (reviewVerdict === 'yes') !== value
              return (
                <div key={key} className="flex flex-col gap-1">
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-sm text-tertiary flex items-center gap-2 min-w-0">
                      {label}
                      {needsAnswer && (
                        <span
                          data-testid={`dc-unanswered-${key}`}
                          className="text-[10px] font-semibold uppercase tracking-wide text-amber-700 bg-amber-100 px-1.5 py-0.5 rounded-full flex-shrink-0"
                        >
                          Needs your answer
                        </span>
                      )}
                    </span>
                    <div
                      role="radiogroup"
                      aria-label={label}
                      aria-describedby={reviewQuestion ? `dc-reason-${key}` : undefined}
                      className="flex gap-1.5 flex-shrink-0"
                    >
                    <button
                      ref={index === 0 ? firstQuestionRef : undefined}
                      type="button"
                      role="radio"
                      aria-checked={value === true}
                      disabled={busy}
                      data-testid={`dc-question-${key}-yes`}
                      onClick={() => {
                        touchedRef.current.add(key)
                        setAnswers((prev) => ({ ...prev, [key]: true }))
                      }}
                      className={`text-xs font-semibold px-3 py-1.5 rounded-lg border transition disabled:opacity-50 ${
                        value === true
                          ? 'bg-primary text-white border-primary'
                          : 'border-bial-border text-neutral hover:bg-bial-bg'
                      }`}
                    >
                      Yes
                    </button>
                    <button
                      type="button"
                      role="radio"
                      aria-checked={value === false}
                      disabled={busy}
                      data-testid={`dc-question-${key}-no`}
                      onClick={() => {
                        touchedRef.current.add(key)
                        setAnswers((prev) => ({ ...prev, [key]: false }))
                      }}
                      className={`text-xs font-semibold px-3 py-1.5 rounded-lg border transition disabled:opacity-50 ${
                        value === false
                          ? 'bg-primary text-white border-primary'
                          : 'border-bial-border text-neutral hover:bg-bial-bg'
                      }`}
                    >
                      No
                    </button>
                    </div>
                  </div>
                  {disagreement && (
                    <p
                      data-testid={`dc-disagreement-${key}`}
                      className="text-xs text-amber-700 leading-relaxed"
                    >
                      {/* THE TWO DIRECTIONS ARE NOT THE SAME SENTENCE, and saying they were
                          was the bug: one line claimed "your answer is kept" for both, but a
                          review Yes stands OVER a No — the developer's answer is the one that
                          does not survive. The administrator's screen has always said "The Yes
                          stands"; this now agrees with it instead of contradicting it. */}
                      {reviewVerdict === 'yes' ? (
                        <>
                          The automatic check found this in your code. Its Yes is what goes on
                          record, and an administrator will see that you answered No.
                        </>
                      ) : (
                        <>
                          The automatic check did not find this. Your Yes is what goes on
                          record, and an administrator will see that the check disagreed.
                        </>
                      )}
                    </p>
                  )}
                  {/* The review's reason: multi-line PROSE in a whitespace-preserving
                      plain element — NEVER the shared markdown renderer, which collapses
                      single newlines (documented repo bug). */}
                  {reviewQuestion && (
                    <p
                      id={`dc-reason-${key}`}
                      data-testid={`dc-reason-${key}`}
                      className="text-xs text-neutral leading-relaxed whitespace-pre-wrap break-words"
                    >
                      {reviewQuestion.reason}
                    </p>
                  )}
                </div>
              )
            })}
          </div>

          {/* What the answers add up to. Shown only once every question is answered — a
              partial total would read as a verdict on an incomplete form. It is informational:
              the button below stays enabled regardless, and the server decides. The old
              "ask an administrator" dead end is RETIRED copy: sending it for review is now
              exactly what the button below does. */}
          {allAnswered && (
            <p data-testid="dc-score" className="mt-4 flex items-baseline gap-2 text-xs text-neutral">
              <span className="text-lg font-bold text-tertiary tabular-nums">{total}</span>
              <span>
                {/* The SAME predicates the action label and the explanation prompt use —
                    read off `sendForReview`/`sensitiveRecorded` rather than re-compared
                    against the threshold, so this sentence cannot end up contradicting the
                    button two rows below it if the rule ever moves. */}
                {sendForReview
                  ? 'sensitive data recorded — this app will be sent to an administrator for review'
                  : sensitiveRecorded
                    ? 'sensitive data recorded — an administrator approved this version, so it publishes'
                    : 'nothing sensitive recorded — this can publish without review'}
              </span>
            </p>
          )}

          {warning && (
            <p
              id="dc-warning"
              data-testid="dc-warning"
              className={`mt-4 text-xs leading-relaxed flex items-start gap-1.5 ${
                notesRequired ? 'text-amber-700' : 'text-neutral'
              }`}
            >
              {notesRequired && <ShieldAlert size={13} className="flex-shrink-0 mt-0.5" />}
              {warning}
            </p>
          )}

          <label htmlFor="dc-notes" className="block text-xs font-semibold text-tertiary mt-3">
            Explanation {notesRequired ? <span className="text-danger">(required)</span> : '(optional)'}
          </label>
          <textarea
            id="dc-notes"
            data-testid="dc-notes"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            disabled={busy}
            aria-required={notesRequired}
            // The warning IS this field's error copy when an explanation is obliged, so
            // point at it rather than leaving it as unassociated text a screen reader
            // reaches only by chance — the reject-note field does the same.
            aria-describedby={warning ? 'dc-warning' : undefined}
            aria-invalid={notesRequired && notesBlank}
            rows={3}
            placeholder="How is this data handled?"
            className="mt-1 w-full border border-bial-border rounded-xl px-3 py-2.5 text-sm text-tertiary placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary resize-none disabled:opacity-50"
          />
          </>
        )}

        {error && (
          <p role="alert" className="text-xs text-danger mt-2">
            {error}
          </p>
        )}

        <div className="flex gap-3 mt-5">
          {/* The label states which of the two things this will do: a weighted Yes
              makes the submission a review request, anything else publishes. Hidden
              entirely when there is nothing saved — publishing already refuses. */}
          {!nothingSaved && (
            <button
              type="button"
              data-testid="dc-confirm"
              disabled={confirmDisabled}
              onClick={() => void handleConfirm()}
              className="flex-1 flex items-center justify-center gap-2 bg-primary hover:bg-primary/90 disabled:opacity-50 text-white font-semibold py-2.5 rounded-xl transition text-sm"
            >
              {busy && <BusyGlyph size={15} />}
              {sendForReview ? 'Send for review' : 'Publish'}
            </button>
          )}
          {/* Structurally isolated from `onConfirm` — see the module comment. */}
          <button
            type="button"
            data-testid="dc-cancel"
            disabled={busy}
            onClick={onCancel}
            className="px-4 border border-bial-border text-neutral hover:text-tertiary py-2.5 rounded-xl transition text-sm disabled:opacity-50"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}
