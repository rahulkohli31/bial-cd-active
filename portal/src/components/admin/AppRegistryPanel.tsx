import { useState, useEffect, useCallback, useRef } from 'react'
import {
  AlertCircle, RefreshCw, Box, CheckCircle, XCircle, X,
  ShieldCheck, ShieldOff, Power, Trash2, ScrollText, Rocket, ShieldAlert,
} from 'lucide-react'
import { BusyGlyph } from '../ui/Waiting'
import {
  listApps, approveApp, rejectApp, patchApp, disableApp, enableApp,
  markDeployed, deleteApp, fetchAudit, fetchAppStatusCounts,
} from '../../utils/appRegistryApi'
import type { RegistryApp, AppStatus, AuditEvent } from '../../utils/appRegistryApi'
import { ApiError } from '../../utils/apiError'
import WaitingCountBadge from './WaitingCountBadge'
import { relativeTimeVerbose } from '../../utils/relativeTime'
import { readDeclaration, shortSha, MIN_REJECTION_NOTE } from './declaration'
import type { ReadDeclaration } from './declaration'
import { auditLabel } from './auditLabels'
import {
  countWords,
  MIN_DELETE_REASON_WORDS,
  MAX_DELETE_REASON_WORDS,
  MAX_DELETE_REASON_CHARS,
} from '../../utils/words'
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog'
import { Textarea } from '../ui/textarea'

/** What to call an app on screen. The internal id used to stand in for a missing name, but
 *  a UUID is not a name — it identifies the row for the platform, not the app for a person,
 *  and an administrator cannot do anything with it. An untitled app says so instead. */
const appLabel = (app: RegistryApp): string => app.name || '(untitled app)'

// Registry status vocabulary (NOT the old mock active/under_review/flagged set).
const STATUS: Record<AppStatus, { label: string; cls: string }> = {
  draft: { label: 'Draft', cls: 'bg-gray-100 text-gray-500' },
  pending: { label: 'Pending Review', cls: 'bg-amber-100 text-amber-700' },
  approved: { label: 'Approved', cls: 'bg-green-100 text-green-700' },
  rejected: { label: 'Rejected', cls: 'bg-red-100 text-red-700' },
  disabled: { label: 'Disabled', cls: 'bg-gray-200 text-gray-600' },
}
// Draft used to be hidden here as "builder-side", which was true of the REVIEW flow and
// false of the ops one: a self-published app is a draft (one-click deploy never writes a
// status), so the ordinary live app in the marketplace had no row on this screen at all —
// and the kill switch below can now reach it. A lever nobody can get to is not a
// lever. Pending stays the default tab; this only adds a place to stand.
const TABS: AppStatus[] = ['pending', 'draft', 'approved', 'rejected', 'disabled']

// `STATUS_TRANSITIONS[DISABLED]` on the server (`db/models/app_registry.py`), mirrored so
// the control appears exactly where the transition is legal. PENDING is absent on purpose:
// an app waiting for review is REJECTED, not switched off, and the server refuses it — an
// affordance whose only outcome is a refusal is a bug, not a safety net.
const CAN_DISABLE: readonly AppStatus[] = ['approved', 'draft', 'rejected']

const fmtWhen = (iso: string | null): string => {
  // NULL IS ITS OWN ANSWER, and it cannot be routed through Date. `new Date(0)` is the
  // epoch, whose getTime() is 0 — not NaN — so folding null into it rendered a pending
  // row with no submittedAt as "1/1/1970" directly above the Approve button, which reads
  // as a fact about the submission rather than as missing data.
  if (iso === null) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}
/** The queue's Submitted cell: the visible age, with the exact moment riding underneath
 *  as a `title` and a machine-readable `datetime`. `fmtWhen` owns the one question that
 *  decides whether an age exists at all — a row whose submittedAt is missing or
 *  unparseable takes its guarded placeholder rather than an age counted from 1970. */
function SubmittedCell({ iso }: { iso: string | null }) {
  const exact = fmtWhen(iso)
  if (iso === null || exact === '—') return <>—</>
  return <time dateTime={iso} title={exact}>{relativeTimeVerbose(iso)}</time>
}

// Advisory on-disk size of the app's own database. Null is a real value —
// "no number to show" (never provisioned, not yet ready, or the cluster was unreachable) —
// and renders as "—", never "0 B", which would read as an empty database.
const fmtBytes = (n: number | null): string => {
  if (n == null) return '—'
  const b = Number(n)
  if (!Number.isFinite(b)) return '—'
  if (b < 1024) return `${b} B`
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`
  return `${(b / 1024 / 1024).toFixed(1)} MB`
}
function StatusBadge({ status }: { status: AppStatus }) {
  const s = STATUS[status] || STATUS.draft
  return <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${s.cls}`}>{s.label}</span>
}

/** The one thing this screen is for, said out loud. An administrator who thinks
 *  they are code-reviewing will either approve everything or block everything. */
const THE_CRITERION =
  'Decide whether an app holding this kind of data is acceptable to publish. You are not ' +
  'checking whether the code is correct.'

const NO_DECLARATION_COPY =
  'This submission carries no data declaration — it was queued before the pre-publish ' +
  'check existed, or it came in through the manual go-live route. Decide from the ' +
  'submission details above, or ask the developer to re-submit from the app’s Publish button.'

const NO_REVIEW_COPY =
  'No automatic check informed this submission — the developer’s own answers are the ' +
  'only ones on record. That is the most common reason an app arrives here, and it is ' +
  'not itself a problem: it means nobody but the developer has looked at what this app holds.'

const NOTHING_IN_DISPUTE_COPY =
  'The automatic check and the developer agreed on every category. What follows is what ' +
  'they both said.'

/**
 * Review a pending SUBMISSION. Reading order: disputes first, then the automatic check's
 * reasoning, then the developer's explanation — the disagreement matters most. Approve sends the
 * exact submission id on display, so a re-submit since this review 409s instead of silently
 * promoting an unseen build; a withdrawal mid-review swaps the actions for explanatory copy. The
 * card splits header / scroll / action row so a long dispute list can't push Approve/Reject
 * off-screen — `min-h-0` on the scroll child is load-bearing (flex won't shrink below content).
 * EVIDENCE LOCATIONS ARE NEVER RENDERED, and structurally cannot be: they live in a separate
 * document that no call reaching this screen makes.
 */
interface ReviewModalProps {
  app: RegistryApp
  /** The developer pulled this submission back while the modal was open. Set by the
   *  panel, which is the only thing that sees the failure; non-null replaces the actions
   *  entirely, because there is nothing left to decide and a button that can only fail
   *  again is worse than a sentence saying so. */
  withdrawn: string | null
  onClose: () => void
  onApprove: () => Promise<void>
  onReject: (note: string) => Promise<void>
}

function ReviewModal({ app, withdrawn, onClose, onApprove, onReject }: ReviewModalProps) {
  const [mode, setMode] = useState<'reject' | null>(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const declaration: ReadDeclaration = readDeclaration(app.declaration)
  const trimmedNote = note.trim()
  const noteTooShort = trimmedNote.length < MIN_REJECTION_NOTE

  // `onApprove`/`onReject` never reject — the panel's `act` owns every failure and its
  // toast — so this only drives the button's spinner.
  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    try { await fn() } finally { setBusy(false) }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-labelledby="admin-review-title">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-2xl max-h-[90vh] flex flex-col">
        {/* Header — fixed, outside the scroll region. */}
        <div className="p-6 pb-4 flex-shrink-0">
          <div className="flex items-start justify-between">
            <div>
              <h3 id="admin-review-title" className="text-base font-bold text-tertiary">Review “{appLabel(app)}”</h3>
              <p className="text-sm text-neutral mt-0.5">Owner: {app.ownerUsername || '—'}</p>
            </div>
            {/* NAMED, because it is an icon on its own: without the label this dismiss control
                reads as "button" to a screen reader, and it is the route out of the dialog that
                the focus restore below is measured on. */}
            <button aria-label="Close" onClick={onClose} className="p-1.5 text-neutral hover:text-tertiary rounded-lg hover:bg-bial-bg transition"><X size={18} /></button>
          </div>
          <p data-testid="review-criterion" className="mt-3 text-xs text-tertiary bg-bial-bg border border-bial-border rounded-xl px-3 py-2.5 leading-relaxed">
            {THE_CRITERION}
          </p>
        </div>

        {/* THE SCROLLING MIDDLE — everything that grows with the submission. */}
        <div data-testid="review-scroll" className="flex-1 min-h-0 overflow-y-auto px-6">
          {/* State changes announce here: the withdrawal, and the two declaration states
              that replace the dispute list rather than leaving blanks behind. */}
          <div data-testid="review-status" role="status" aria-live="polite">
            {withdrawn !== null && (
              <p data-testid="review-withdrawn" className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-xl px-3 py-2.5 leading-relaxed flex items-start gap-1.5">
                <ShieldAlert size={13} className="flex-shrink-0 mt-0.5" />
                {withdrawn}
              </p>
            )}
            {withdrawn === null && !declaration.present && (
              <p data-testid="review-no-declaration" className="text-xs text-neutral leading-relaxed">
                {NO_DECLARATION_COPY}
              </p>
            )}
            {withdrawn === null && declaration.present && declaration.noReviewAtAll && (
              <p data-testid="review-no-review" className="text-xs text-amber-700 leading-relaxed">
                {NO_REVIEW_COPY}
              </p>
            )}
          </div>

          {declaration.present && (
            <>
              {/* THE DISPUTE, FIRST. Reason directly beneath each category. */}
              {declaration.disputes.length > 0 ? (
                <div data-testid="review-disputes" className="mt-4">
                  <h4 className="text-[10px] font-bold uppercase tracking-wider text-neutral">In dispute</h4>
                  <ul className="mt-2 flex flex-col gap-3">
                    {declaration.disputes.map((row) => (
                      <li key={row.key} data-testid={`dispute-${row.key}`} className="border border-bial-border rounded-xl px-3 py-2.5">
                        <div className="flex items-center justify-between gap-3">
                          <span className="text-sm font-semibold text-tertiary">{row.label}</span>
                          <span className={`text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-full flex-shrink-0 ${row.mergedYes ? 'bg-amber-100 text-amber-700' : 'bg-gray-100 text-gray-500'}`}>
                            {row.mergedYes ? 'Recorded as Yes' : 'Recorded as No'}
                          </span>
                        </div>
                        <p className="text-[11px] text-neutral mt-1">
                          Developer said {row.citizenYes === null ? '—' : row.citizenYes ? 'Yes' : 'No'}
                          {' · '}
                          Automatic check said {row.reviewVerdict === null ? 'nothing' : row.reviewVerdict === 'unanswered' ? 'it could not tell' : row.reviewVerdict === 'yes' ? 'Yes' : 'No'}
                        </p>
                        {row.notes.map((copy) => (
                          <p key={copy} className="text-[11px] text-tertiary mt-1 leading-relaxed">{copy}</p>
                        ))}
                        {/* The check's own words. Multi-line PROSE in a whitespace-preserving
                            plain element — never the shared markdown renderer, which
                            collapses single newlines (documented repo bug). */}
                        {row.reason !== null && (
                          <p data-testid={`dispute-reason-${row.key}`} className="text-xs text-neutral mt-1.5 leading-relaxed whitespace-pre-wrap break-words">
                            {row.reason}
                          </p>
                        )}
                        {declaration.drift && row.newlyRaised && (
                          <p data-testid={`dispute-unexplained-${row.key}`} className="text-[11px] font-semibold text-amber-700 mt-1.5">
                            Not covered by the explanation below — the developer never saw this finding.
                          </p>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : (
                !declaration.noReviewAtAll && (
                  <p data-testid="review-no-dispute" className="text-xs text-neutral mt-4 leading-relaxed">
                    {NOTHING_IN_DISPUTE_COPY}
                  </p>
                )
              )}

              {/* THE DEVELOPER'S ANSWERS. Always shown — an item with no review must never
                  render blanks where a dispute would be. */}
              {declaration.citizenAnswers.length > 0 && (
                <div data-testid="review-citizen-answers" className="mt-4">
                  <h4 className="text-[10px] font-bold uppercase tracking-wider text-neutral">What the developer declared</h4>
                  <ul className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1">
                    {declaration.citizenAnswers.map((row) => (
                      <li key={row.key} data-testid={`citizen-answer-${row.key}`} className="flex items-center justify-between gap-3 text-xs">
                        <span className="text-tertiary">{row.label}</span>
                        <span className={`font-semibold ${row.yes ? 'text-amber-700' : 'text-neutral'}`}>{row.yes ? 'Yes' : 'No'}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {/* THE EXPLANATION, LAST. */}
              <div className="mt-4">
                <h4 className="text-[10px] font-bold uppercase tracking-wider text-neutral">The developer’s explanation</h4>
                <p data-testid="review-explanation" className="mt-1 text-xs text-tertiary leading-relaxed whitespace-pre-wrap break-words">
                  {declaration.explanation ?? 'No explanation was recorded with this submission.'}
                </p>
                {declaration.drift && (
                  <p data-testid="review-drift" className="mt-2 text-[11px] text-amber-700 leading-relaxed">
                    This explanation was written about version{' '}
                    <code className="bg-bial-bg rounded px-1">{shortSha(declaration.answeredAbout)}</code>, but version{' '}
                    <code className="bg-bial-bg rounded px-1">{shortSha(declaration.shippingCommit)}</code> is what was submitted. Anything marked above as
                    not covered was raised after they wrote it.
                  </p>
                )}
              </div>
            </>
          )}

          {/* PROVENANCE, LAST — it is what the approval pins, not what it is about. */}
          <dl className="mt-5 mb-5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs border-t border-bial-border pt-4">
            <dt className="text-neutral">Submitted</dt>
            <dd data-testid="review-submitted-at" className="text-tertiary">{fmtWhen(app.submittedAt)}</dd>
            <dt className="text-neutral">Build</dt>
            <dd><code data-testid="review-commit-sha" className="text-tertiary bg-bial-bg rounded px-1 py-0.5">{(app.commitSha || '').slice(0, 12) || '—'}</code></dd>
            {/* The submission's own id used to be listed here. It is what the approval pins,
                but it is an internal identifier no administrator can act on, and the Build
                above already names the version in a form that means something. It is still
                sent with the approval — it just is not read off the screen. */}
            <dt className="text-neutral">Login</dt>
            <dd className="text-tertiary">{app.loginRequired ? 'Required' : 'Off'} — adjust it from the row before approving if needed.</dd>
          </dl>
          <p className="text-xs text-neutral mb-5 leading-relaxed">
            Approving pins exactly this submission: if it's been re-submitted since you opened
            this review, the server refuses the approval rather than silently promoting a build
            you never saw.{' '}
            <span data-testid="review-publish-note">
              Approving publishes it: this exact submission goes live for its developer, with
              nothing more for you or them to do.
            </span>
          </p>
        </div>

        {/* THE ACTION ROW — outside the scroll region, always reachable. */}
        <div className="p-6 pt-4 border-t border-bial-border flex-shrink-0">
          {withdrawn !== null ? (
            <button data-testid="withdrawn-close" onClick={onClose} className="w-full border border-bial-border text-tertiary hover:bg-bial-bg font-semibold py-2.5 rounded-xl transition text-sm">
              Close
            </button>
          ) : (
            <>
              {mode === 'reject' && (
                <div className="mb-4">
                  <label htmlFor="reject-note" className="block text-xs font-semibold text-tertiary">
                    Why are you rejecting this? <span className="text-danger">(required)</span>
                  </label>
                  <textarea
                    id="reject-note"
                    data-testid="reject-note"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    aria-required="true"
                    aria-describedby="reject-note-help"
                    placeholder="What would make this app acceptable to publish?"
                    rows={3}
                    className="mt-1 w-full border border-bial-border rounded-xl px-3 py-2.5 text-sm text-tertiary placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary resize-none"
                  />
                  <p id="reject-note-help" data-testid="reject-note-help" className={`mt-1 text-[11px] ${noteTooShort ? 'text-danger' : 'text-neutral'}`}>
                    {noteTooShort
                      ? `This is the only thing the developer gets back — write at least ${MIN_REJECTION_NOTE} characters (${trimmedNote.length} so far).`
                      : 'This goes straight back to the developer.'}
                  </p>
                  {/* Only when the app is actually SERVING. Rejecting sets a standing
                      rejection, which the marketplace reads — so a live app vanishes from
                      the catalog while its URL keeps working, and only the OWNER can
                      re-submit to undo it. An admin rejecting a re-submission of an
                      already-approved app had no way to know that. */}
                  {app.deployedUrl && (
                    <p data-testid="reject-delists-warning" className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 mt-2">
                      This app is live. Rejecting removes it from the Marketplace but leaves
                      it running at its URL, and only its owner can undo that by submitting
                      again. To take it down, use Unpublish instead.
                    </p>
                  )}
                </div>
              )}
              <div className="flex gap-3">
                {mode !== 'reject' ? (
                  <>
                    <button data-testid="approve-btn" disabled={busy} onClick={() => run(onApprove)} className="flex-1 flex items-center justify-center gap-2 bg-primary hover:bg-primary/90 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50">
                      {busy ? <BusyGlyph size={15} /> : <CheckCircle size={15} />} Approve and publish
                    </button>
                    <button data-testid="reject-btn" onClick={() => setMode('reject')} className="flex-1 flex items-center justify-center gap-2 border border-bial-border hover:border-red-300 hover:text-red-600 text-tertiary font-semibold py-2.5 rounded-xl transition text-sm">
                      <XCircle size={15} /> Reject
                    </button>
                  </>
                ) : (
                  <>
                    <button data-testid="reject-confirm" disabled={busy || noteTooShort} onClick={() => run(() => onReject(trimmedNote))} className="flex-1 bg-red-600 hover:bg-red-700 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50">Send rejection</button>
                    <button onClick={() => setMode(null)} className="px-4 border border-bial-border text-neutral hover:text-tertiary py-2.5 rounded-xl transition text-sm">Back</button>
                  </>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

interface AuditDrawerProps {
  app: RegistryApp
  onClose: () => void
}

/** Read-only audit trail for one app. */
function AuditDrawer({ app, onClose }: AuditDrawerProps) {
  const [events, setEvents] = useState<AuditEvent[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    fetchAudit(app.appId).then((e) => { if (live) setEvents(e) }).catch((e) => { if (live) setErr(e instanceof Error ? e.message : String(e)) })
    return () => { live = false }
  }, [app.appId])
  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/40" onClick={onClose} />
      <div className="w-full max-w-md bg-white h-full flex flex-col shadow-2xl">
        <div className="px-6 py-4 border-b border-bial-border flex items-center justify-between">
          <h2 className="text-base font-bold text-tertiary">Audit — {appLabel(app)}</h2>
          <button onClick={onClose} className="p-1.5 text-neutral hover:text-tertiary rounded-lg hover:bg-bial-bg transition"><X size={18} /></button>
        </div>
        <div className="flex-1 overflow-y-auto p-4">
          {!events && !err && <p className="text-sm text-neutral flex items-center gap-2"><BusyGlyph size={14} /> Loading…</p>}
          {err && <p className="text-sm text-red-600">{err}</p>}
          {events && events.length === 0 && <p className="text-sm text-neutral">No events yet.</p>}
          {events && events.length > 0 && (
            <ul className="space-y-2">
              {events.map((ev) => {
                // The stored action is a machine token; `auditLabel` is the only place it
                // becomes words. The app's id is deliberately not repeated on every row —
                // every event in this drawer is about the one app named in the header.
                const label = auditLabel(ev.action)
                return (
                  <li key={ev.id} data-testid={`audit-event-${ev.action}`} className="text-sm border border-bial-border rounded-lg px-3 py-2">
                    <div className="flex items-start justify-between gap-3">
                      <span className="font-semibold text-tertiary">{label.title}</span>
                      <span className="text-[11px] text-neutral whitespace-nowrap">{fmtWhen(ev.createdAt)}</span>
                    </div>
                    {label.description && (
                      <p className="text-[11px] text-neutral mt-1 leading-relaxed">{label.description}</p>
                    )}
                    <p className="text-[11px] text-neutral mt-1">
                      by {ev.username || 'the platform'}{ev.count != null ? ` · ${ev.count}` : ''}
                    </p>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * Admin "App Registry" panel — the real apps surface (replaces the mock AppTable).
 * Status sub-tabs over the registry vocabulary; approve / reject / disable /
 * enable / toggle-login / delete / view-audit, all backed by
 * the admin-gated /api/admin/apps endpoints. Loads via useCallback+useEffect.
 */
export interface AppRegistryPanelProps {
  // severity is optional (default 'ok' on the AdminPage side) so a plain confirmation
  // call reads exactly as it always has — only `act()`'s catch branch below passes 'problem'.
  onToast: (msg: string, severity?: 'ok' | 'problem') => void
}

export default function AppRegistryPanel({ onToast }: AppRegistryPanelProps) {
  const [tab, setTab] = useState<AppStatus>('pending')
  const [apps, setApps] = useState<RegistryApp[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [review, setReview] = useState<RegistryApp | null>(null)
  /** The app awaiting a delete reason, or null. See `onDelete`. */
  const [deleting, setDeleting] = useState<RegistryApp | null>(null)
  const [deleteReason, setDeleteReason] = useState('')
  // Non-null once the developer withdraws the submission under review. Cleared
  // whenever a different item is opened, so one race can never haunt the next review.
  const [withdrawn, setWithdrawn] = useState<string | null>(null)
  const [auditing, setAuditing] = useState<RegistryApp | null>(null)
  // The waiting count, mirrored from the nav badge onto the Pending tab. `null` =
  // not asked yet or the ask failed; never rendered as a number.
  const [waiting, setWaiting] = useState<number | null>(null)
  // A SET of in-flight app ids, not one shared lock: acting on row A must never
  // re-enable row B's still-pending buttons (which a single busyId did, opening the
  // door to duplicate concurrent mutations + duplicate audit rows).
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set())
  // Staleness guard for overlapping loads (tab-switch / Refresh clobber): a stale
  // response must not overwrite fresher state. Ref-token variant of the `let live`
  // idiom, since `load` is also called imperatively (Refresh, act's reload).
  const loadSeq = useRef(0)
  // The queue's own tab — the nearest stable, always-mounted landmark on this panel, and
  // the fallback for the review below when the row that opened it is gone.
  const queueTabRef = useRef<HTMLButtonElement>(null)
  // The Review control that opened the modal, CAPTURED AT PRESS TIME rather than read back
  // off `document.activeElement`: a click focuses the button in a browser but not under
  // `fireEvent`, so reading it back would make the restore untestable and — worse — silently
  // correct in the suite while landing on `<body>` for the citizen. Doubles as the "a review
  // was opened at some point" flag the effect below keys off.
  const reviewTriggerRef = useRef<HTMLButtonElement | null>(null)

  /**
   * PUT FOCUS SOMEWHERE REAL WHEN THE REVIEW CLOSES.
   *
   * The review modal is hand-rolled — no Radix `DialogContent`, so no `FocusScope`, so nothing
   * captures the element that had focus and nothing restores it. Closing it dropped focus on
   * `<body>`, where the next Tab restarts at the top of the document.
   *
   * IT RESTORES THE ROW'S OWN Review BUTTON, which is where an administrator who dismissed a
   * review belongs — three rows down a queue of forty, not back at the top of it.
   *
   * AND IT FALLS BACK, because approving or rejecting DESTROYS that button: the app leaves the
   * pending queue, so the reload this panel does on success takes the whole row with it. That is
   * the same detached-trigger case `ProjectsPage`'s delete path documents, and it takes the same
   * remedy — the nearest stable landmark, here the queue's own tab.
   *
   * IN AN EFFECT, NOT IN THE CLOSE HANDLERS, AND THE SUITE CANNOT TELL THE TWO APART — which is
   * exactly why this note exists. Success closes the modal from an async continuation, and both
   * of the questions asked below are questions about the DOM: whether the trigger is still
   * attached, and whether the tab is there to fall back to. In a browser that continuation is a
   * microtask and React's commit is a scheduled task, so the answers describe the PREVIOUS render
   * — mid-reload, where `loading` has replaced this whole panel with a spinner, that is a
   * detached trigger AND a null tab ref, and focus stays on `<body>`. An effect runs after the
   * commit, so it asks the DOM the citizen actually has. Under RTL both readings pass, because
   * `act` flushes the commit before the continuation resumes: moving this into `settleReview`
   * leaves the tests green and the browser broken.
   */
  useEffect(() => {
    if (review !== null) return
    const trigger = reviewTriggerRef.current
    if (trigger === null) return // no review has been opened yet — nothing to restore
    reviewTriggerRef.current = null
    if (document.contains(trigger)) trigger.focus()
    else queueTabRef.current?.focus()
  }, [review])

  const load = useCallback(async () => {
    const seq = ++loadSeq.current
    setLoading(true); setError(null)
    try {
      const rows = await listApps(tab)
      if (loadSeq.current === seq) setApps(rows)
      // The tab badge rides the same load the table does, so acting on a row updates
      // both. Its own failure must not fail the queue — a missing count renders as no
      // badge, which is the honest reading of "we don't know".
      const counts = await fetchAppStatusCounts().catch(() => null)
      if (loadSeq.current === seq) setWaiting(counts === null ? null : counts.pending)
    } catch (e) {
      if (loadSeq.current === seq) setError(e instanceof Error ? e.message : String(e))
    } finally {
      // Only the freshest load owns the spinner — a stale one resolving late must not
      // flip `loading` off under a newer in-flight fetch.
      if (loadSeq.current === seq) setLoading(false)
    }
  }, [tab])

  useEffect(() => { load() }, [load])

  // Run a mutating action with a per-row busy lock + toast, then reload. Returns the FAILURE,
  // or null on success — never a bare boolean, because the withdrawal race needs the error's
  // `code` and re-throwing after already toasting would force every caller into a second try.
  // The toast is one channel for both a confirmation and a raw failure (okMsg vs. e.message), so
  // the two must NOT render alike — an administrator left to tell them apart by reading the words
  // cannot know whether the action they just took worked, which is why the catch branch, and only
  // it, passes a 'problem' severity.
  const act = async (appId: string, fn: () => Promise<unknown>, okMsg?: string): Promise<unknown> => {
    setBusyIds((s) => new Set(s).add(appId))
    try { await fn(); if (okMsg) onToast(okMsg) ; await load(); return null }
    catch (e) { onToast(e instanceof Error ? e.message : String(e), 'problem'); return e }
    finally { setBusyIds((s) => { const n = new Set(s); n.delete(appId); return n }) }
  }

  /** Close the review modal on success; on the withdrawal race, keep it open and let it
   *  say what happened instead. Every other failure is already a toast and leaves the
   *  modal alone — on the 409 the admin still needs the submission metadata. */
  const settleReview = (failure: unknown): void => {
    if (failure === null) { setReview(null); setWithdrawn(null); return }
    if (failure instanceof ApiError && failure.code === 'submission_withdrawn') {
      setWithdrawn(failure.message)
    }
  }

  // Approve sends the on-display submission id (the reviewed-id guard's input); a stale
  // review 409s with copy `act` surfaces verbatim via toast, and the modal closes only on
  // success so a 409 leaves the metadata visible to re-review. `submissionId` is nullable in
  // the schema but always present once an app is 'pending' — the `as string` below is an
  // unchecked pass-through matching pre-migration behavior, not a missed null check.
  const onApprove = (app: RegistryApp) => act(app.appId, () => approveApp(app.appId, app.submissionId as string), `“${appLabel(app)}” approved`).then(settleReview)
  const onReject = (app: RegistryApp, note: string) => act(app.appId, () => rejectApp(app.appId, note), `“${appLabel(app)}” rejected`).then(settleReview)
  const onToggleLogin = (app: RegistryApp) => act(app.appId, () => patchApp(app.appId, { loginRequired: !app.loginRequired }), `Login ${app.loginRequired ? 'disabled' : 'required'} for “${appLabel(app)}”`)
  const onDisable = (app: RegistryApp) => act(app.appId, () => disableApp(app.appId), `“${appLabel(app)}” disabled`)
  const onEnable = (app: RegistryApp) => act(app.appId, () => enableApp(app.appId), `“${appLabel(app)}” re-enabled`)
  // The deployed URL is DATA, not automation: the operator pastes what the go-live
  // runbook produced. Prompting (like `onDelete`'s confirm) keeps this on the runbook's
  // own rhythm — mark the deploy the moment it lands, address in hand. Cancel aborts
  // entirely; a blank answer still records the deploy and leaves any existing URL alone,
  // so a re-deploy of the same app needs no re-typing. An invalid URL comes back as the
  // server's 422 copy through `act`'s toast — no duplicated client-side check.
  const onMarkDeployed = (app: RegistryApp) => {
    const answer = window.prompt(
      `Deployed URL for “${appLabel(app)}” (https://…). Leave blank to record the deploy without changing the URL.`,
      app.deployedUrl || '',
    )
    if (answer === null) return
    const url = answer.trim()
    return act(app.appId, () => markDeployed(app.appId, url), `Deployment recorded for “${appLabel(app)}”`)
  }
  // THE DELETE ASKS WHY, AND A `window.confirm` COULD NOT.
  //
  // The route now REQUIRES a word-bounded reason, so a confirm-and-send would 422 every time. The
  // reason rides the `app:delete` audit row, which is written before destruction and has no
  // foreign key to the app — so it outlives the thing it describes, which is the whole point.
  //
  // It uses the SAME word rule as the citizen's own project delete (`utils/words.ts`, mirrored
  // at `src/core/words.py`), because the harsher act — destroying somebody else's work — should
  // not ask for less than the gentler one.
  const onDelete = (app: RegistryApp) => {
    // THE REASON IS PER-APP AND MUST NOT TRAVEL. `deleteReason` lives on the panel, so a
    // justification typed for one app and abandoned would open pre-filled on the next one —
    // and if it happened to be valid, one press away from destroying a different citizen's
    // work under words that were never about it. Cleared on OPEN rather than only on close,
    // because close is the path a mid-flight failure deliberately does not take.
    setDeleteReason('')
    setDeleting(app)
  }

  // Pending is the only tab that is a REVIEW QUEUE — the only one ordered oldest-first,
  // and the only one whose rows carry a submittedAt (it is null everywhere else). One
  // <thead>/<tbody> serves all four tabs, so both the Submitted column and the ordering
  // caption hang off this: on Approved, an "oldest first" caption would be a lie and a
  // Submitted column would be a stripe of em-dashes.
  const isQueue = tab === 'pending'

  if (loading) {
    return <div className="flex items-center justify-center gap-2 py-16 text-neutral text-sm"><BusyGlyph size={16} /> Loading apps…</div>
  }
  if (error) {
    return (
      <div className="text-center py-16">
        <AlertCircle size={20} className="text-red-500 mx-auto mb-3" />
        <p className="text-sm text-tertiary font-semibold">Couldn’t load apps</p>
        <p className="text-xs text-neutral mt-1">{error}</p>
        <button onClick={load} className="mt-4 inline-flex items-center gap-1.5 px-4 py-2 rounded-xl border border-bial-border text-sm font-medium text-tertiary hover:bg-bial-bg transition"><RefreshCw size={14} /> Retry</button>
      </div>
    )
  }

  return (
    <>
      <div className="flex items-center gap-1 mb-4 bg-bial-bg rounded-lg p-1 w-fit">
        {TABS.map((t) => (
          <button
            key={t}
            // The review can only be opened from the queue, so the queue's tab is the landmark
            // focus comes back to when the row it was opened from is gone.
            ref={t === 'pending' ? queueTabRef : undefined}
            data-testid={`apps-tab-${t}`}
            onClick={() => setTab(t)}
            className={`text-xs font-medium px-3 py-1.5 rounded-md transition inline-flex items-center gap-1.5 ${tab === t ? 'bg-white text-primary shadow-sm border border-bial-border' : 'text-neutral hover:text-primary'}`}
          >
            {STATUS[t].label}
            {/* Mirrors the nav badge, same component and same accessible name. */}
            {t === 'pending' && <WaitingCountBadge count={waiting} where="tab" />}
          </button>
        ))}
        <button onClick={load} title="Refresh" className="ml-1 p-1.5 text-neutral hover:text-primary"><RefreshCw size={13} /></button>
      </div>

      {apps.length === 0 ? (
        <div className="text-center py-16">
          <div className="w-12 h-12 rounded-2xl bg-bial-bg flex items-center justify-center mx-auto mb-3"><Box size={20} className="text-neutral" /></div>
          <p className="text-sm text-neutral">No {STATUS[tab].label.toLowerCase()} apps.</p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            {/* The ordering is a GUARANTEE the backend makes and pins with a test,
                and until now it was invisible: nothing on screen told an administrator
                that top means oldest, so the queue read as an arbitrary list. Said out
                loud, the position becomes information. Deliberately NOT a sort control —
                a handle that let someone reorder the review queue would turn a reporting
                gap into a real defect. Pending only: every other tab is newest-first. */}
            {isQueue && (
              <caption data-testid="queue-order-note" className="caption-top text-left text-xs text-neutral pb-3">
                Oldest first — the next app to review is at the top.
              </caption>
            )}
            <thead>
              <tr className="border-b border-bial-border">
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">App</th>
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Owner</th>
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Login</th>
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Status</th>
                {isQueue && <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Submitted</th>}
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Database</th>
                <th className="pb-3 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-bial-border">
              {apps.map((app) => {
                const busy = busyIds.has(app.appId)
                return (
                  <tr key={app.appId} data-testid={`app-row-${app.appId}`} className="hover:bg-bial-bg/50 transition">
                    <td className="py-3 pr-6">
                      <div className="flex items-center gap-2">
                        <div className="w-7 h-7 rounded-lg bg-primary/10 flex items-center justify-center flex-shrink-0"><Box size={13} className="text-primary" /></div>
                        <div>
                          <p className="font-semibold text-tertiary whitespace-nowrap">{appLabel(app)}</p>
                        </div>
                      </div>
                    </td>
                    <td className="py-3 pr-6 text-tertiary whitespace-nowrap">{app.ownerUsername || '—'}</td>
                    <td className="py-3 pr-6">
                      <button
                        onClick={() => onToggleLogin(app)}
                        disabled={busy}
                        title="Toggle required login"
                        className={`inline-flex items-center gap-1 text-xs font-medium px-2 py-1 rounded-lg border transition disabled:opacity-50 ${app.loginRequired ? 'border-primary/30 text-primary bg-primary/5' : 'border-bial-border text-neutral'}`}
                      >
                        {app.loginRequired ? <ShieldCheck size={12} /> : <ShieldOff size={12} />}
                        {app.loginRequired ? 'Required' : 'Off'}
                      </button>
                    </td>
                    <td className="py-3 pr-6"><StatusBadge status={app.status} /></td>
                    {isQueue && (
                      <td data-testid={`submitted-${app.appId}`} className="py-3 pr-6 text-neutral whitespace-nowrap">
                        <SubmittedCell iso={app.submittedAt} />
                      </td>
                    )}
                    <td data-testid={`db-bytes-${app.appId}`} className="py-3 pr-6 text-neutral whitespace-nowrap">{fmtBytes(app.databaseBytes)}</td>
                    <td className="py-3">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        {app.status === 'pending' && (
                          <button data-testid={`review-${app.appId}`} onClick={(e) => { reviewTriggerRef.current = e.currentTarget; setWithdrawn(null); setReview(app) }} disabled={busy} className="px-2.5 py-1.5 rounded-lg bg-primary/10 text-primary hover:bg-primary/20 transition text-xs font-medium disabled:opacity-50">Review</button>
                        )}
                        {app.status === 'approved' && app.redeployNeeded && (
                          <span data-testid={`redeploy-needed-${app.appId}`} title="The approved build has not been deployed (or was re-approved since the last deploy) — run the go-live runbook, then mark it deployed" className="inline-flex items-center text-[11px] font-semibold px-2 py-1 rounded-lg bg-amber-100 text-amber-700">Deploy needed</span>
                        )}
                        {/* The self-publish lineage has NO runbook step, so it gets
                            neither the prompt above (the server already forces
                            `redeployNeeded` false for it) nor this control — which the
                            server refuses anyway. An affordance whose only outcome is a
                            refusal is a bug, not a safety net. */}
                        {app.status === 'approved' && app.approvalRoute !== 'self_publish' && (
                          <button data-testid={`mark-deployed-${app.appId}`} onClick={() => onMarkDeployed(app)} disabled={busy} title="Record that the go-live runbook was run for the approved build" className="inline-flex items-center gap-1 text-xs font-medium px-2 py-1 rounded-lg border border-bial-border text-neutral hover:text-primary hover:bg-bial-bg transition disabled:opacity-50"><Rocket size={12} /> Mark deployed</button>
                        )}
                        {CAN_DISABLE.includes(app.status) && (
                          <button data-testid={`disable-${app.appId}`} onClick={() => onDisable(app)} disabled={busy} title="Disable (kill switch)" className="p-1.5 rounded-lg border border-bial-border text-amber-600 hover:bg-amber-50 transition disabled:opacity-50"><Power size={13} /></button>
                        )}
                        {app.status === 'disabled' && (
                          <button data-testid={`enable-${app.appId}`} onClick={() => onEnable(app)} disabled={busy} title="Re-enable" className="p-1.5 rounded-lg border border-bial-border text-green-600 hover:bg-green-50 transition disabled:opacity-50"><Power size={13} /></button>
                        )}
                        <button data-testid={`audit-${app.appId}`} onClick={() => setAuditing(app)} disabled={busy} title="View audit" className="p-1.5 rounded-lg border border-bial-border text-neutral hover:text-primary hover:bg-bial-bg transition disabled:opacity-50"><ScrollText size={13} /></button>
                        <button data-testid={`delete-${app.appId}`} onClick={() => onDelete(app)} disabled={busy} title="Delete app" className="p-1.5 rounded-lg border border-bial-border text-red-600 hover:bg-red-50 transition disabled:opacity-50"><Trash2 size={13} /></button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {review && <ReviewModal app={review} withdrawn={withdrawn} onClose={() => { setReview(null); setWithdrawn(null) }} onApprove={() => onApprove(review)} onReject={(note) => onReject(review, note)} />}
      {auditing && <AuditDrawer app={auditing} onClose={() => setAuditing(null)} />}
      {deleting && (
        <DeleteAppDialog
          app={deleting}
          reason={deleteReason}
          onReason={setDeleteReason}
          busy={busyIds.has(deleting.appId)}
          onClose={() => { setDeleting(null); setDeleteReason('') }}
          onConfirm={async () => {
            const target = deleting
            const outcome = await act(target.appId, () => deleteApp(target.appId, deleteReason), `“${appLabel(target)}” deleted`)
            // Close only on success — a 422 on the reason must leave the words on screen to fix,
            // not throw them away behind a dialog that has already gone.
            if (!(outcome instanceof Error)) { setDeleting(null); setDeleteReason('') }
          }}
        />
      )}
    </>
  )
}

/**
 * THE ADMIN DELETE'S REASON.
 *
 * ON THE VENDORED RADIX `Dialog`, like every other dialog in this portal — and this one was
 * hand-rolled when it first landed, which reintroduced in the admin panel the exact defect a
 * vendored dialog exists to close: a `fixed inset-0` div with `role="dialog"` gives no focus
 * trap, no Escape, and no focus restored to the trash control that opened it. Its sibling
 * review modal in this same file carries an explicit `reviewTriggerRef` restore for that
 * reason. The most destructive control on this screen must not be the one with the weakest
 * keyboard contract. Radix gives the trap, Escape and the overlay click (both routed through
 * `onOpenChange`, so `busy` guards them the way the hand-rolled overlay only guarded its own
 * click), and the restore — via `useFocusBackstop` in `ui/dialog.tsx`, because this dialog is
 * rendered conditionally like the rest.
 *
 * A `window.confirm` stood here before that. It could not collect anything, and the route now
 * REQUIRES a word-bounded justification — so the old control would 422 on every press. The words ride the
 * `app:delete` audit row, which is written before destruction and carries no foreign key to
 * the app, so it is still readable long after what it describes is gone.
 *
 * SAME WORD RULE AS THE CITIZEN'S OWN DELETE, from the shared `utils/words.ts` (mirrored at
 * `src/core/words.py`): the harsher act — an administrator destroying work that is not theirs,
 * with no undo and no export — should not ask for less than the gentler one. The client keeps
 * the person inside the bounds; the server is what enforces them.
 */
function DeleteAppDialog({ app, reason, onReason, busy, onClose, onConfirm }: {
  app: RegistryApp
  reason: string
  onReason: (value: string) => void
  busy: boolean
  onClose: () => void
  onConfirm: () => void
}) {
  const words = countWords(reason)
  const valid = words >= MIN_DELETE_REASON_WORDS && words <= MAX_DELETE_REASON_WORDS

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        // Radix routes Escape, the overlay click and its own close through here, and `busy`
        // holds it open mid-request — the same guard the hand-rolled overlay carried, now
        // covering the two exits it never did.
        if (!next && !busy) onClose()
      }}
    >
      <DialogContent
        hideClose
        // The scrim this dialog already used, kept exactly — the vendored default is
        // `bg-black/80`, which is a different design.
        overlayClassName="bg-black/40"
        className="font-manrope w-full max-w-md rounded-2xl bg-white p-6 shadow-xl gap-0 border-0"
      >
        <DialogTitle className="text-base font-bold text-tertiary">Delete “{appLabel(app)}”?</DialogTitle>
        {/* Names the two things that do not come back. "Data and files" undersold it: the app's
            own PostgreSQL database is dropped outright — no export, no snapshot, no undo. */}
        <p className="mt-2 text-sm text-neutral">
          Its database is dropped and its files are deleted. This cannot be undone.
        </p>
        <label className="block mt-4">
          <span className="text-xs font-semibold text-tertiary">Why are you deleting this app?</span>
          {/* The SHARED `Textarea`, like the citizen dialog this one models itself on down to
              the word rule — two dialogs with the same job drifting apart on their input is how
              a design system stops being one. `maxLength` mirrors the server's own 2,000-char
              backstop (`clean_deletion_reason`): a paste guard, not the rule a person is told
              about, which is the word count below. */}
          <Textarea
            data-testid="admin-delete-reason"
            value={reason}
            onChange={(e) => onReason(e.target.value)}
            rows={3}
            maxLength={MAX_DELETE_REASON_CHARS}
            aria-describedby="admin-delete-reason-count"
            className="mt-1.5 resize-y"
          />
          <span id="admin-delete-reason-count" className="mt-1 block text-xs text-neutral">
            Between {MIN_DELETE_REASON_WORDS} and {MAX_DELETE_REASON_WORDS} words. Kept on the audit record.{' '}
            {words}/{MAX_DELETE_REASON_WORDS} words
          </span>
        </label>
        <div className="flex gap-3 mt-5">
          <button
            type="button"
            onClick={() => { if (!busy) onClose() }}
            aria-disabled={busy}
            className={`rounded-xl border border-bial-border px-4 py-2 text-sm font-semibold text-neutral transition hover:bg-bial-bg ${busy ? 'cursor-not-allowed opacity-50' : ''}`}
          >
            Cancel
          </button>
          <button
            type="button"
            data-testid="admin-delete-confirm"
            // `aria-disabled`, never `disabled`, and this is the control it matters most on:
            // it is the one holding focus at the instant it goes busy, because the citizen just
            // pressed it. A real `disabled` attribute throws focus to the document body from
            // under them, mid-request, which is the defect `ui/dialog.tsx`'s backstop exists to
            // clean up after — better not to cause it. The refusal is enforced in the handler,
            // the only place it can be once the attribute is gone.
            aria-disabled={!valid || busy}
            onClick={() => {
              if (!valid || busy) return
              onConfirm()
            }}
            className={`flex-1 flex items-center justify-center gap-2 bg-red-600 hover:bg-red-700 text-white font-semibold py-2.5 rounded-xl transition text-sm ${
              !valid || busy ? 'opacity-50 cursor-not-allowed' : ''
            }`}
          >
            {busy ? <BusyGlyph size={15} /> : null} Delete app
          </button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
