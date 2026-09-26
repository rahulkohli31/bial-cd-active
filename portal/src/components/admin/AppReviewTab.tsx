import { useState } from 'react'
import { ShieldAlert } from 'lucide-react'
import type { RegistryApp } from '../../utils/appRegistryApi'
import { Button } from '../ui/button'
import { Textarea } from '../ui/textarea'
import { BusyGlyph } from '../ui/Waiting'
import { readDeclaration, shortSha, MIN_REJECTION_NOTE } from './declaration'

/** The one thing this screen is for, said out loud. An administrator who thinks
 *  they are code-reviewing will either approve everything or block everything. */
const THE_CRITERION =
  'Decide whether an app holding this kind of data is acceptable to publish. You are not ' +
  'checking whether the code is correct.'

const NO_DECLARATION_COPY =
  'This submission carries no data declaration — it was queued before the pre-publish ' +
  'check existed. Decide from the submission details above, or ask the developer to ' +
  're-submit from the app’s Publish button.'

const NO_REVIEW_COPY =
  'No automatic check informed this submission — the developer’s own answers are the ' +
  'only ones on record. That is the most common reason an app arrives here, and it is ' +
  'not itself a problem: it means nobody but the developer has looked at what this app holds.'

const NOTHING_IN_DISPUTE_COPY =
  'The automatic check and the developer agreed on every category. What follows is what ' +
  'they both said.'

const LIVE_REJECTION_COPY =
  'This app is live. Rejecting removes it from the Marketplace but leaves it running at its URL, ' +
  'and only its owner can undo that by submitting again. To take it down, use Unpublish instead.'

const fmtWhen = (iso: string | null): string => {
  // Null is its own answer: `new Date(0)` would print the epoch as if it were a fact.
  if (iso === null) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

interface AppReviewTabProps {
  app: RegistryApp
  /** The number of the send under review, once History has said. */
  number: number | null
  /** The owner pulled this submission back while the panel was open. Non-null replaces the
   *  actions: there is nothing left to decide, and a button that can only fail is worse. */
  withdrawn: string | null
  onClose: () => void
  onApprove: () => Promise<void>
  onReject: (note: string) => Promise<void>
}

/**
 * The pending submission's review: disputes first, then the automatic check's reasoning, then the
 * developer's explanation, above a footer that is always in reach. Approve sends the submission id
 * on display, so a re-submit since this review is refused instead of promoting an unseen build.
 * Evidence locations are never rendered: they live in a document no call reaching this screen makes.
 */
export default function AppReviewTab({ app, number, withdrawn, onClose, onApprove, onReject }: AppReviewTabProps) {
  const [rejecting, setRejecting] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const declaration = readDeclaration(app.declaration)
  const trimmedNote = note.trim()
  const noteTooShort = trimmedNote.length < MIN_REJECTION_NOTE
  const version = `${number === null ? '' : `v${number} · `}${shortSha(app.commitSha)}`

  // `onApprove`/`onReject` never reject — the panel owns every failure and its toast — so this
  // only drives the button's spinner.
  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    try {
      await fn()
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div data-testid="review-scroll" className="flex min-h-0 flex-1 flex-col gap-3.5 overflow-y-auto px-6 py-4">
        <p
          data-testid="review-criterion"
          className="rounded-xl border border-bial-border bg-bial-bg px-3 py-2.5 text-xs leading-relaxed text-tertiary"
        >
          {THE_CRITERION}
        </p>

        <div data-testid="review-status" role="status" aria-live="polite" className="empty:hidden">
          {withdrawn !== null && (
            <p
              data-testid="review-withdrawn"
              className="flex items-start gap-1.5 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs leading-relaxed text-amber-700"
            >
              <ShieldAlert size={13} className="mt-0.5 flex-shrink-0" />
              {withdrawn}
            </p>
          )}
          {withdrawn === null && !declaration.present && (
            <p data-testid="review-no-declaration" className="text-xs leading-relaxed text-neutral">
              {NO_DECLARATION_COPY}
            </p>
          )}
          {withdrawn === null && declaration.present && declaration.noReviewAtAll && (
            <p data-testid="review-no-review" className="text-xs leading-relaxed text-amber-700">
              {NO_REVIEW_COPY}
            </p>
          )}
        </div>

        {declaration.present && (
          <>
            {declaration.disputes.length > 0 ? (
              <div data-testid="review-disputes">
                <h4 className="text-[10px] font-bold uppercase tracking-wider text-neutral">In dispute</h4>
                <ul className="mt-2 flex flex-col gap-3">
                  {declaration.disputes.map((row) => (
                    <li key={row.key} data-testid={`dispute-${row.key}`} className="rounded-xl border border-bial-border px-3 py-2.5">
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-sm font-semibold text-tertiary">{row.label}</span>
                        <span
                          className={`flex-shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${row.mergedYes ? 'bg-amber-100 text-amber-700' : 'bg-gray-100 text-gray-500'}`}
                        >
                          {row.mergedYes ? 'Recorded as Yes' : 'Recorded as No'}
                        </span>
                      </div>
                      <p className="mt-1 text-[11px] text-neutral">
                        Developer said {row.citizenYes === null ? '—' : row.citizenYes ? 'Yes' : 'No'}
                        {' · '}
                        Automatic check said{' '}
                        {row.reviewVerdict === null
                          ? 'nothing'
                          : row.reviewVerdict === 'unanswered'
                            ? 'it could not tell'
                            : row.reviewVerdict === 'yes'
                              ? 'Yes'
                              : 'No'}
                      </p>
                      {row.notes.map((copy) => (
                        <p key={copy} className="mt-1 text-[11px] leading-relaxed text-tertiary">
                          {copy}
                        </p>
                      ))}
                      {/* Multi-line prose in a whitespace-preserving element: the shared markdown
                          renderer collapses single newlines. */}
                      {row.reason !== null && (
                        <p
                          data-testid={`dispute-reason-${row.key}`}
                          className="mt-1.5 whitespace-pre-wrap break-words text-xs leading-relaxed text-neutral"
                        >
                          {row.reason}
                        </p>
                      )}
                      {declaration.drift && row.newlyRaised && (
                        <p data-testid={`dispute-unexplained-${row.key}`} className="mt-1.5 text-[11px] font-semibold text-amber-700">
                          Not covered by the explanation below — the developer never saw this finding.
                        </p>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              !declaration.noReviewAtAll && (
                <p data-testid="review-no-dispute" className="text-xs leading-relaxed text-neutral">
                  {NOTHING_IN_DISPUTE_COPY}
                </p>
              )
            )}

            {declaration.citizenAnswers.length > 0 && (
              <div data-testid="review-citizen-answers">
                <h4 className="text-[10px] font-bold uppercase tracking-wider text-neutral">What the developer declared</h4>
                <ul className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-2">
                  {declaration.citizenAnswers.map((row) => (
                    <li key={row.key} data-testid={`citizen-answer-${row.key}`} className="flex items-center justify-between gap-3 text-xs">
                      <span className="text-tertiary">{row.label}</span>
                      <span className={`font-semibold ${row.yes ? 'text-amber-700' : 'text-neutral'}`}>{row.yes ? 'Yes' : 'No'}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div>
              <h4 className="text-[10px] font-bold uppercase tracking-wider text-neutral">The developer’s explanation</h4>
              <p data-testid="review-explanation" className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-tertiary">
                {declaration.explanation ?? 'No explanation was recorded with this submission.'}
              </p>
              {declaration.drift && (
                <p data-testid="review-drift" className="mt-2 text-[11px] leading-relaxed text-amber-700">
                  This explanation was written about version{' '}
                  <code className="rounded bg-bial-bg px-1">{shortSha(declaration.answeredAbout)}</code>, but version{' '}
                  <code className="rounded bg-bial-bg px-1">{shortSha(declaration.shippingCommit)}</code> is what was
                  submitted. Anything marked above as not covered was raised after they wrote it.
                </p>
              )}
            </div>
          </>
        )}

        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 border-t border-bial-border pt-4 text-xs">
          <dt className="text-neutral">Submitted</dt>
          <dd data-testid="review-submitted-at" className="text-tertiary">
            {fmtWhen(app.submittedAt)}
          </dd>
          <dt className="text-neutral">Build</dt>
          <dd>
            <code data-testid="review-commit-sha" className="rounded bg-bial-bg px-1 py-0.5 text-tertiary">
              {(app.commitSha || '').slice(0, 12) || '—'}
            </code>
          </dd>
          <dt className="text-neutral">Login</dt>
          <dd className="text-tertiary">{app.loginRequired ? 'Required' : 'Off'} — adjust it from the row before approving if needed.</dd>
        </dl>
      </div>

      <div className="flex-shrink-0 border-t border-bial-border bg-white px-6 py-3.5">
        {withdrawn !== null ? (
          <div className="flex justify-end">
            <Button data-testid="withdrawn-close" variant="outline" onClick={onClose} className="h-9 rounded-lg border-bial-border px-4 text-[13.5px] font-semibold text-tertiary">
              Close
            </Button>
          </div>
        ) : (
          <>
            {rejecting && (
              <div className="mb-3">
                <label htmlFor="reject-note" className="block text-xs font-semibold text-tertiary">
                  Why are you rejecting this? <span className="text-danger">(required)</span>
                </label>
                <Textarea
                  id="reject-note"
                  data-testid="reject-note"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  aria-required="true"
                  aria-describedby="reject-note-help"
                  placeholder="What would make this app acceptable to publish?"
                  rows={3}
                  className="mt-1 resize-none"
                />
                <p id="reject-note-help" data-testid="reject-note-help" className={`mt-1 text-[11px] ${noteTooShort ? 'text-danger' : 'text-neutral'}`}>
                  {noteTooShort
                    ? `This is the only thing the developer gets back — write at least ${MIN_REJECTION_NOTE} characters (${trimmedNote.length} so far).`
                    : 'This goes straight back to the developer.'}
                </p>
                {app.liveVersion !== null && (
                  <p data-testid="reject-delists-warning" className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
                    {LIVE_REJECTION_COPY}
                  </p>
                )}
              </div>
            )}
            <div className="flex items-center gap-2.5">
              <p data-testid="review-publish-note" className="flex-grow text-[11.5px] text-neutral">
                Approving publishes exactly {version}.
              </p>
              {rejecting ? (
                <>
                  <Button variant="outline" onClick={() => setRejecting(false)} className="h-9 rounded-lg border-bial-border px-3.5 text-[13.5px] font-semibold text-neutral">
                    Back
                  </Button>
                  <Button
                    data-testid="reject-confirm"
                    disabled={busy || noteTooShort}
                    onClick={() => run(() => onReject(trimmedNote))}
                    className="h-9 rounded-lg bg-red-600 px-[18px] text-[13.5px] font-semibold text-white hover:bg-red-700"
                  >
                    Send rejection
                  </Button>
                </>
              ) : (
                <>
                  <Button
                    data-testid="reject-btn"
                    variant="outline"
                    onClick={() => setRejecting(true)}
                    className="h-9 rounded-lg border-red-500/35 bg-white px-3.5 text-[13.5px] font-semibold text-red-700 shadow-none hover:bg-red-50 hover:text-red-700"
                  >
                    Reject…
                  </Button>
                  <Button
                    data-testid="approve-btn"
                    disabled={busy}
                    onClick={() => run(onApprove)}
                    className="h-9 rounded-lg px-[18px] text-[13.5px] font-semibold shadow-none"
                  >
                    {busy && <BusyGlyph size={15} />} Approve and publish
                  </Button>
                </>
              )}
            </div>
          </>
        )}
      </div>
    </>
  )
}
