import { useState } from 'react'
import type { ReactNode } from 'react'
import { AlertTriangle, ShieldAlert } from 'lucide-react'
import type { RegistryApp } from '../../utils/appRegistryApi'
import { assertNever } from '../../utils/assertNever'
import { dayMonth, dayMonthTime } from '../../utils/projectDates'
import { cn } from '../../lib/utils'
import { Alert, AlertDescription, AlertTitle } from '../ui/alert'
import { Button } from '../ui/button'
import { Textarea } from '../ui/textarea'
import { BusyGlyph } from '../ui/Waiting'
import AnswersTable from './AnswersTable'
import { handle } from './columns'
import { agentFinding, readDeclaration, shortSha, verdictWord, MIN_REJECTION_NOTE } from './declaration'
import type { ClassDeclaration } from './declaration'

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
  'and only its owner can undo that by submitting again. To take it down as well, disable it ' +
  'from its row’s menu after rejecting.'

const SECTION_LABEL = 'text-[10.5px] font-bold uppercase tracking-[0.6px] text-neutral'

function Commit({ number, sha }: { number: number | null; sha: string | null }) {
  return (
    <>
      {number !== null && `v${number} · `}
      <code className="rounded bg-[#EEF2F6] px-[5px] py-px text-xs">{shortSha(sha)}</code>
    </>
  )
}

function Fact({ label, value, detail }: { label: string; value: ReactNode; detail: string | null }) {
  return (
    <div className="px-3.5 py-2.5">
      <div className={SECTION_LABEL}>{label}</div>
      <div className="mt-[3px] text-[13px] font-semibold text-tertiary">{value}</div>
      {detail !== null && <div className="mt-0.5 text-[11.5px] text-neutral">{detail}</div>}
    </div>
  )
}

/** When the version under review was sent and by whom, which version it is and when it was saved,
 *  when the agent checked it, and what is live. A time the declaration did not record is left out. */
function VersionFacts({ app, number, declaration }: { app: RegistryApp; number: number | null; declaration: ClassDeclaration }) {
  const live = app.liveVersion
  const { savedAt, checkedAt } = declaration
  return (
    <div
      data-testid="review-version"
      className={cn(
        'grid divide-x divide-bial-border rounded-[10px] border border-bial-border bg-surface-muted',
        checkedAt === null ? 'grid-cols-3' : 'grid-cols-4',
      )}
    >
      <Fact
        label="Sent"
        value={dayMonthTime(app.submittedAt)}
        detail={app.ownerUsername === null ? null : `by ${handle(app.ownerUsername)}`}
      />
      <Fact
        label="Version"
        value={<Commit number={number} sha={app.commitSha} />}
        detail={savedAt === null ? null : `Saved ${dayMonthTime(savedAt)}`}
      />
      {checkedAt !== null && <Fact label="Checked" value={dayMonthTime(checkedAt)} detail="by the agent" />}
      {live === null ? (
        <Fact label="Live now" value="Not live yet" detail={number === 1 ? 'First version' : null} />
      ) : (
        <Fact
          label="Live now"
          value={<Commit number={live.number} sha={live.commitSha} />}
          detail={live.since === null ? null : `since ${dayMonth(live.since)}`}
        />
      )}
    </div>
  )
}

interface Why {
  blocked: boolean
  title: string
  details: string[]
}

function whyItIsHere(declaration: ClassDeclaration): Why | null {
  const { reason, found, score, threshold } = declaration
  switch (reason) {
    case null:
      return null
    case 'hard_block':
      return {
        blocked: true,
        title: agentFinding(declaration),
        details: found.flatMap((entry) =>
          entry.reason === null ? [] : [found.length === 1 ? `Agent: ${entry.reason}` : `Agent, on ${entry.title}: ${entry.reason}`],
        ),
      }
    case 'over_threshold':
      return {
        blocked: false,
        title: score === null || threshold === null ? 'Score over the threshold' : `Score ${score}/100 is over ${threshold}`,
        details: [],
      }
    case 'review_unfinished':
      return {
        blocked: false,
        title: "The agent's review did not finish",
        details: ['No answers were recorded, so there is no score.'],
      }
    case 'rejection_standing':
      return {
        blocked: false,
        title: 'An earlier version was rejected',
        details: ['A rejection stands until an administrator approves a version.'],
      }
    default:
      return assertNever(reason)
  }
}

function WhyItIsHere({ why }: { why: Why }) {
  const ink = why.blocked ? 'text-red-900' : 'text-amber-900'
  return (
    <Alert
      data-testid="review-why"
      className={cn(
        'rounded-[10px] border-0 px-3.5 py-[11px] [&>svg]:left-3.5 [&>svg]:top-3 [&>svg~*]:pl-[30px]',
        why.blocked
          ? 'bg-red-50 shadow-[inset_0_0_0_1px_rgba(185,28,28,0.2)] [&>svg]:text-red-700'
          : 'bg-amber-50 shadow-[inset_0_0_0_1px_rgba(217,119,6,0.25)] [&>svg]:text-amber-700',
      )}
    >
      <AlertTriangle size={18} aria-hidden />
      <AlertTitle className={cn('mb-0 text-[13.5px] font-bold leading-[normal] tracking-normal', ink)}>
        Why it is here: {why.title}
      </AlertTitle>
      {why.details.map((detail) => (
        <AlertDescription key={detail} className={cn('mt-[3px] whitespace-pre-wrap break-words text-xs leading-normal', ink)}>
          {detail}
        </AlertDescription>
      ))}
    </Alert>
  )
}

function ScoreLine({ declaration }: { declaration: ClassDeclaration }) {
  const { reviewerScore, score, threshold, ownersCanChangeAnswers, decidedAt } = declaration
  let scored = 'not recorded'
  if (reviewerScore !== null) {
    scored =
      ownersCanChangeAnswers === true && score !== null
        ? `agent ${reviewerScore} · owner ${score} (of 100)`
        : `agent ${reviewerScore} (of 100)`
  }
  const owners = ownersCanChangeAnswers === null ? '—' : ownersCanChangeAnswers ? 'Yes' : 'No'
  return (
    <div data-testid="review-score" className="flex flex-wrap gap-5 text-xs text-neutral">
      <div>
        <b className="text-primary-900">Score</b> {scored}
      </div>
      <div>
        <b className="text-primary-900">Publish without review up to</b> {threshold ?? '—'}
      </div>
      <div>
        <b className="text-primary-900">Owners can change answers</b> {owners}
      </div>
      {decidedAt !== null && <div className="ml-auto">Policy as of {dayMonthTime(decidedAt)}</div>}
    </div>
  )
}

/** A version decided by the classes, read from the declaration stored with that decision. */
function ClassReview({ app, number, declaration }: { app: RegistryApp; number: number | null; declaration: ClassDeclaration }) {
  const why = whyItIsHere(declaration)
  return (
    <>
      <VersionFacts app={app} number={number} declaration={declaration} />
      {why !== null && <WhyItIsHere why={why} />}
      {declaration.note !== null && (
        <div>
          <h4 className={cn(SECTION_LABEL, 'mb-1.5')}>Owner's note</h4>
          <p
            data-testid="review-note"
            className="whitespace-pre-wrap break-words rounded-[10px] border border-bial-border bg-surface-muted px-3.5 py-2.5 text-[13px] leading-relaxed text-primary-900"
          >
            {declaration.note}
          </p>
        </div>
      )}
      <div>
        <h4 className={cn(SECTION_LABEL, 'mb-1.5')}>Answers</h4>
        <AnswersTable classes={declaration.classes} />
      </div>
      <ScoreLine declaration={declaration} />
    </>
  )
}

interface AppReviewTabProps {
  app: RegistryApp
  /** The number of the send under review, once History has said. */
  number: number | null
  /** The submission was withdrawn, re-submitted or decided elsewhere while the panel was open.
   *  Non-null replaces the actions: there is nothing left to decide here, and a button that can
   *  only fail, or act on a version nobody reviewed, is worse. */
  overtaken: string | null
  /** Why the last Approve or Reject failed, said beside the actions it came from. */
  problem: string | null
  /** An Approve or Reject for this app is in flight, even if it began before this tab mounted. */
  busy: boolean
  onClose: () => void
  onApprove: () => Promise<void>
  onReject: (note: string) => Promise<void>
}

/**
 * The pending submission's review, above a footer that is always in reach. A version decided by the
 * classes reads as the agent and the owner answered it; a six-question declaration reads as it was
 * sent. Approve sends the submission id on display, so a re-submit since this review is refused
 * instead of promoting an unseen build. No evidence location reaches this screen.
 */
export default function AppReviewTab({ app, number, overtaken, problem, busy, onClose, onApprove, onReject }: AppReviewTabProps) {
  const [rejecting, setRejecting] = useState(false)
  const [note, setNote] = useState('')
  const declaration = readDeclaration(app.declaration)
  const trimmedNote = note.trim()
  const noteTooShort = trimmedNote.length < MIN_REJECTION_NOTE
  const version = `${number === null ? '' : `v${number} · `}${shortSha(app.commitSha)}`

  return (
    <>
      <div data-testid="review-scroll" className="flex min-h-0 flex-1 flex-col gap-3.5 overflow-y-auto px-6 py-4">
        {declaration.version === 1 && (
          <p
            data-testid="review-criterion"
            className="rounded-xl border border-bial-border bg-bial-bg px-3 py-2.5 text-xs leading-relaxed text-tertiary"
          >
            {THE_CRITERION}
          </p>
        )}

        <div data-testid="review-status" role="status" aria-live="polite" className="empty:hidden">
          {overtaken !== null && (
            <p
              data-testid="review-overtaken"
              className="flex items-start gap-1.5 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs leading-relaxed text-amber-700"
            >
              <ShieldAlert size={13} className="mt-0.5 flex-shrink-0" />
              {overtaken}
            </p>
          )}
          {overtaken === null && declaration.version === 1 && !declaration.present && (
            <p data-testid="review-no-declaration" className="text-xs leading-relaxed text-neutral">
              {NO_DECLARATION_COPY}
            </p>
          )}
          {overtaken === null && declaration.version === 1 && declaration.present && declaration.noReviewAtAll && (
            <p data-testid="review-no-review" className="text-xs leading-relaxed text-amber-700">
              {NO_REVIEW_COPY}
            </p>
          )}
        </div>

        {declaration.version === 2 && <ClassReview app={app} number={number} declaration={declaration} />}

        {declaration.version === 1 && declaration.present && (
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
                        Automatic check said {verdictWord(row.reviewVerdict, 'review')}
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

        {declaration.version === 1 && (
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 border-t border-bial-border pt-4 text-xs">
            <dt className="text-neutral">Submitted</dt>
            <dd data-testid="review-submitted-at" className="text-tertiary">
              {dayMonthTime(app.submittedAt)}
            </dd>
            <dt className="text-neutral">Build</dt>
            <dd>
              <code data-testid="review-commit-sha" className="rounded bg-bial-bg px-1 py-0.5 text-tertiary">
                {shortSha(app.commitSha)}
              </code>
            </dd>
            <dt className="text-neutral">Login</dt>
            <dd className="text-tertiary">{app.loginRequired ? 'Required' : 'Off'} — adjust it from the row before approving if needed.</dd>
          </dl>
        )}
      </div>

      <div className="flex-shrink-0 border-t border-bial-border bg-white px-6 py-3.5">
        {overtaken !== null ? (
          <div className="flex justify-end">
            <Button data-testid="overtaken-close" variant="outline" onClick={onClose} className="h-9 rounded-lg border-bial-border px-4 text-[13.5px] font-semibold text-tertiary">
              Close
            </Button>
          </div>
        ) : (
          <>
            {problem !== null && (
              <p
                data-testid="review-problem"
                role="alert"
                className="mb-3 flex items-start gap-1.5 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-700"
              >
                <AlertTriangle size={13} className="mt-0.5 flex-shrink-0" aria-hidden />
                {problem}
              </p>
            )}
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
                    onClick={() => void onReject(trimmedNote)}
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
                    onClick={() => void onApprove()}
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
