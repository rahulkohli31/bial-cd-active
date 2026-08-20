/**
 * The pre-publish form — six weighted Yes/No questions, pre-filled by an automatic
 * review of the app's last SAVED version, plus a gated explanation. Opening the dialog
 * ensures a review exists for that version (`ensureClassificationReview`); while one
 * runs, the dialog polls and the citizen can already answer. Confirm hands the answers
 * to the caller's `onConfirm`, which posts them to the deploy route; the SERVER re-reads
 * the stored review there and merges, taking the stricter of the two — nothing this
 * dialog learned rides in as authority.
 *
 * THE RUNNING TOTAL HERE DECIDES NOTHING. It drives the action's label ("Send for
 * review" when a weighted Yes is present, "Publish" otherwise), the explanation gate,
 * and the score line — affordances only, recomputed from a hand-synced copy of the
 * weights the server also enforces. If the two drift, the server is right.
 *
 * MERGE, NEVER CLOBBER. Verdicts arriving from the review land only on questions the
 * citizen has NOT touched; a touched question keeps their answer and shows the review's
 * verdict alongside as a disagreement. Silently reverting someone's answer on the screen
 * built to make this trustworthy is the named failure to avoid. Responses are also
 * filtered by the version stamp this dialog asked about (`askedShaRef`), so a second
 * tab's newer review can never paint answers for a version this dialog never named.
 *
 * ESCAPE AND CANCEL STAY AVAILABLE WHILE THE REVIEW RUNS — the result is stored against
 * the version, so closing loses nothing and the waiting copy says so (OD-A). The
 * existing rule that blocks them applies to a SUBMIT in flight (`busy`) only, and must
 * not be widened to a window that can last a minute.
 *
 * REASONS ARE MULTI-LINE PROSE, rendered in whitespace-preserving plain elements — never
 * through the shared markdown renderer, which collapses single newlines (documented repo
 * bug: docs/solutions/ui-bugs/chat-markdown-single-newline-collapse-2026-08-10.md).
 *
 * FOCUS IS PART OF THE CONTRACT, same as `ReclaimWorkspaceDialog` (whose implementation
 * this follows deliberately): the trap has to survive the busy window, because a request
 * in flight disables Confirm/Cancel and the browser blurs to `<body>`, where neither Tab
 * cycling nor Escape would fire without parking focus on the card itself. The dialog's
 * contents now ARRIVE after it opens, so progress, arrival, and the failure fall-through
 * announce through a polite live region (`dc-review-status`).
 *
 * Unanswered vs. No is the load-bearing distinction here (matches the backend, which only
 * ever accepts a complete six-of-six set): `answers` starts as six `null`s, never six
 * `false`s, so "hasn't gotten to this yet" can never be recorded as "the developer said
 * no" — and a question the REVIEW left unanswered stays null until the citizen answers
 * it, visibly marked as needing them.
 */
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Loader2, ShieldAlert } from 'lucide-react'
import {
  AUTO_DEPLOY_MAX_SCORE,
  DATA_CLASSIFICATION_QUESTIONS,
  totalWeight,
  type DataClassificationAnswers,
} from '../utils/deployApi'
import {
  ensureClassificationReview,
  getClassificationReview,
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

// The waiting copy tells the truth about closing (OD-A): the result is stored against
// the version, so closing loses nothing — ask them to wait without claiming a loss
// that does not happen, and set the real time expectation.
const WAITING_COPY =
  'We’re checking your saved app for the kinds of data it handles. This usually takes ' +
  'about 20 seconds — sometimes up to a minute. You can close this and come back; the ' +
  'result is kept for this version.'

const ARRIVAL_COPY =
  'The automatic check has finished. Each question below starts from what it found — ' +
  'you can change any answer.'

// R21: no saved code — nothing for a review to read, and nothing to publish either.
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
  onConfirm: (answers: DataClassificationAnswers) => Promise<void>
  onCancel: () => void
}

export default function DataClassificationModal({
  projectId,
  rejectionNote = null,
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

  // Opening the dialog is what asks for the review (R1).
  useEffect(() => {
    void ask()
  }, [ask])

  // Poll ONLY while a run is in flight, on the deploy hook's cadence. The GET never
  // starts a run; a response stamped a version this dialog never asked about is
  // ignored, so a second tab's newer review cannot paint answers here.
  const polling = phase.kind === 'ready' && phase.review.status === 'running'
  useEffect(() => {
    if (!polling) return undefined
    const mine = generation.current
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const next = await getClassificationReview(projectId)
          if (generation.current !== mine) return
          if (next.reviewedSha !== askedShaRef.current) return
          applyReview(next)
        } catch (err) {
          if (generation.current !== mine) return
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
  // R21: no saved code — the questions, the score, and the action all go with it.
  const nothingSaved = review?.status === 'nothing_to_review'
  // The ask in flight, or a run in flight. Either way the review hasn't landed, so the
  // action stays disabled: submitting now would route regardless (the server's rule 4),
  // and the label could not yet say which of its two things it will do.
  const reviewPending = phase.kind === 'asking' || review?.status === 'running'

  // R21 removes every question from the DOM, and with them the element holding focus —
  // the browser would blur to `<body>`, where the Tab/Escape trap can't hear. Park focus
  // on the card, the same move the busy window makes below.
  useEffect(() => {
    if (nothingSaved) cardRef.current?.focus()
  }, [nothingSaved])

  const allAnswered = Object.values(answers).every((v) => v !== null)
  const total = totalWeight(answers)
  // A weighted Yes anywhere means this submission is a review request, not a publish —
  // the action's label says so (R11), and the same condition compels the explanation
  // (R10, issue #117 follow-up): a routed app is never unexplained, and an explanation
  // is never compelled on a declaration that was going to pass anyway.
  const sendForReview = total > AUTO_DEPLOY_MAX_SCORE
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
  // Two states, not three (issue #117 follow-up): notes-required and needs-a-human are now
  // the same condition (`notesRequired` above), so there is no longer a middle band that
  // handles some sensitive data but isn't refused — every nonzero total is both.
  let warning: string | null = null
  if (allAnswered) {
    warning = notesRequired
      ? "This app handles sensitive data — please explain how it's handled below."
      : 'No sensitive data flagged for this app.'
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
              <Loader2 size={13} className="animate-spin flex-shrink-0 mt-0.5" aria-hidden />
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
              // The review left this one to the citizen (R5) — visibly distinct from a
              // No, and it blocks the submit through `allAnswered` until answered.
              const needsAnswer = reviewVerdict === 'unanswered' && value === null
              // The citizen's current answer differs from the review's verdict. Theirs
              // stays — merge, never clobber — and the review's is shown alongside (R8:
              // both answer sets are kept and recorded).
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
                      The automatic check said {reviewVerdict === 'yes' ? 'Yes' : 'No'} — your
                      answer is kept, and both are recorded.
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
                {total <= AUTO_DEPLOY_MAX_SCORE
                  ? 'no sensitive data declared — this can publish automatically'
                  : 'sensitive data declared — this app will be sent to an administrator for review'}
              </span>
            </p>
          )}

          {warning && (
            <p
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
          {/* The label states which of the two things this will do (R11): a weighted Yes
              makes the submission a review request, anything else publishes. Hidden
              entirely when there is nothing saved — publishing already refuses (R21). */}
          {!nothingSaved && (
            <button
              type="button"
              data-testid="dc-confirm"
              disabled={confirmDisabled}
              onClick={() => void handleConfirm()}
              className="flex-1 flex items-center justify-center gap-2 bg-primary hover:bg-primary/90 disabled:opacity-50 text-white font-semibold py-2.5 rounded-xl transition text-sm"
            >
              {busy && <Loader2 size={15} className="animate-spin" />}
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
