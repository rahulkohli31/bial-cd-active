/**
 * The pre-submit data-classification questionnaire (V4, task-sheet "Part 1") — six
 * weighted Yes/No questions plus a soft-gated explanation, shown before an app is
 * submitted for review. Confirm submits the app WITH these answers attached in one
 * call (`SubmitControl`'s `onConfirm` prop); Cancel is structural, not conditional —
 * see the note on the Cancel wiring below.
 *
 * FOCUS IS PART OF THE CONTRACT, same as `ReclaimWorkspaceDialog` (whose implementation
 * this follows deliberately): the trap has to survive the busy window, because a submit
 * in flight disables Confirm/Cancel and the browser blurs to `<body>`, where neither Tab
 * cycling nor Escape would fire without parking focus on the card itself.
 *
 * Unanswered vs. No is the load-bearing distinction here (matches the backend, which
 * only ever persists a complete six-of-six set): `answers` starts as six `null`s, never
 * six `false`s, so a reviewer can never mistake "hasn't gotten to this yet" for "the
 * developer said no."
 */
import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Loader2, ShieldAlert } from 'lucide-react'
import {
  DATA_CLASSIFICATION_QUESTIONS,
  NOTES_REQUIRED_AT,
  totalWeight,
  type DataClassificationAnswers,
} from '../utils/approvalApi'

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

interface Props {
  onConfirm: (answers: DataClassificationAnswers) => Promise<void>
  onCancel: () => void
}

export default function DataClassificationModal({ onConfirm, onCancel }: Props): React.ReactElement {
  const [answers, setAnswers] = useState<AnswerState>(UNANSWERED)
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const firstQuestionRef = useRef<HTMLButtonElement>(null)

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

  const allAnswered = Object.values(answers).every((v) => v !== null)
  const total = totalWeight(answers)
  const notesRequired = total >= NOTES_REQUIRED_AT
  const notesBlank = notes.trim() === ''
  const confirmDisabled = busy || !allAnswered || (notesRequired && notesBlank)

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
      setError(e instanceof Error ? e.message : 'Could not submit. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  // Escalating, plain-language warning — shown only once every question has an answer,
  // so "nothing flagged yet" (unanswered) and "flagged nothing" (all six No) never read
  // the same way.
  let warning: string | null = null
  if (allAnswered) {
    if (total >= NOTES_REQUIRED_AT) {
      warning = "This submission involves higher-sensitivity data — please explain how it's handled below."
    } else if (total > 0) {
      warning = 'This submission involves some sensitive data. An explanation is optional but appreciated.'
    } else {
      warning = 'No sensitive data flagged for this submission.'
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
          Before you submit
        </h3>
        <p className="text-sm text-neutral mt-1 leading-relaxed">
          A few questions about the kinds of data this app handles. Every question needs an
          answer before you can continue.
        </p>

        <div className="mt-4 flex flex-col gap-3">
          {DATA_CLASSIFICATION_QUESTIONS.map(([key, label], index) => {
            const value = answers[key]
            return (
              <div key={key} className="flex items-center justify-between gap-3">
                <span className="text-sm text-tertiary">{label}</span>
                <div role="radiogroup" aria-label={label} className="flex gap-1.5 flex-shrink-0">
                  <button
                    ref={index === 0 ? firstQuestionRef : undefined}
                    type="button"
                    role="radio"
                    aria-checked={value === true}
                    disabled={busy}
                    data-testid={`dc-question-${key}-yes`}
                    onClick={() => setAnswers((prev) => ({ ...prev, [key]: true }))}
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
                    onClick={() => setAnswers((prev) => ({ ...prev, [key]: false }))}
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
            )
          })}
        </div>

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

        {error && (
          <p role="alert" className="text-xs text-danger mt-2">
            {error}
          </p>
        )}

        <div className="flex gap-3 mt-5">
          <button
            type="button"
            data-testid="dc-confirm"
            disabled={confirmDisabled}
            onClick={() => void handleConfirm()}
            className="flex-1 flex items-center justify-center gap-2 bg-primary hover:bg-primary/90 disabled:opacity-50 text-white font-semibold py-2.5 rounded-xl transition text-sm"
          >
            {busy && <Loader2 size={15} className="animate-spin" />}
            Confirm
          </button>
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
