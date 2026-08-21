import { useState, useEffect, useCallback, useRef } from 'react'
import {
  Loader2, AlertCircle, RefreshCw, Box, CheckCircle, XCircle, X,
  ShieldCheck, ShieldOff, Power, Trash2, ScrollText, Rocket, ShieldAlert,
} from 'lucide-react'
import {
  listApps, approveApp, rejectApp, patchApp, disableApp, enableApp,
  markDeployed, deleteApp, fetchAudit, fetchAppStatusCounts,
} from '../../utils/appRegistryApi'
import type { RegistryApp, AppStatus, AuditEvent } from '../../utils/appRegistryApi'
import { ApiError } from '../../utils/apiError'
import WaitingCountBadge from './WaitingCountBadge'
import { readDeclaration, shortSha, MIN_REJECTION_NOTE } from './declaration'
import type { ReadDeclaration } from './declaration'
import { auditLabel } from './auditLabels'

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
// Admin reviews these statuses (draft is builder-side and hidden here).
const TABS: AppStatus[] = ['pending', 'approved', 'rejected', 'disabled']

const fmtWhen = (iso: string | null): string => {
  // NULL IS ITS OWN ANSWER, and it cannot be routed through Date. `new Date(0)` is the
  // epoch, whose getTime() is 0 — not NaN — so folding null into it rendered a pending
  // row with no submittedAt as "1/1/1970" directly above the Approve button, which reads
  // as a fact about the submission rather than as missing data.
  if (iso === null) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}
// Advisory on-disk size of the app's own database (ADR-0028). Null is a real value —
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

/** The one thing this screen is for (P3), said out loud. An administrator who thinks
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
 * Review a pending SUBMISSION.
 *
 * READING ORDER (R15): what is in DISPUTE first, then the automatic check's reason for
 * each, then the developer's explanation. The disagreement is the thing to read first —
 * the metadata is provenance, and the criterion (P3) is what the whole screen is for.
 *
 * Approve sends EXACTLY the submission id on display, so the server's reviewed-id guard
 * has something to check: a re-submit since this review 409s, never a silent promotion of
 * an unreviewed build. A WITHDRAWAL between opening this and clicking is answered by
 * purpose-written copy rendered IN PLACE OF the actions (`submission_withdrawn`), because
 * "conflict" describes a column and the administrator needs to know what happened.
 *
 * THE SCROLL CONTRACT. This card was a fixed-width box in a centred overlay with no
 * max-height and no overflow, and the page behind an overlay does not scroll either — so
 * anything taller than the viewport was simply unreachable. It now takes a max-height and
 * splits into three: a header, a MIDDLE THAT SCROLLS (the disputes and the explanation,
 * which is the part that grows without bound), and an action row OUTSIDE that scroll
 * region, so Approve and Reject are reachable with a full six-category dispute and a long
 * explanation on screen. `min-h-0` on the scrolling child is load-bearing: a flex item's
 * default `min-height:auto` refuses to shrink below its content, which silently restores
 * the original bug.
 *
 * EVIDENCE LOCATIONS ARE NEVER RENDERED (OD-B) — and structurally cannot be: they live in
 * a separate document that no call reaching this screen makes.
 */
interface ReviewModalProps {
  app: RegistryApp
  /** The developer pulled this submission back while the modal was open (P6). Set by the
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
            <button onClick={onClose} className="p-1.5 text-neutral hover:text-tertiary rounded-lg hover:bg-bial-bg transition"><X size={18} /></button>
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
            {app.approvalRoute === 'self_publish' ? (
              // R17a: for this lineage there IS no runbook, and the previous copy sent the
              // administrator to run one — instructing exactly what R17a forbids.
              <span data-testid="review-self-publish-note">
                Approving does not publish it — the developer publishes this approved version
                themselves, and there is no go-live runbook for you to run.
              </span>
            ) : (
              <span data-testid="review-runbook-note">
                Approving does not deploy it — this submission is on the manual go-live route, so
                the row shows <strong>Deploy needed</strong> until an admin runs the go-live
                runbook and clicks <strong>Mark deployed</strong>.
              </span>
            )}
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
                </div>
              )}
              <div className="flex gap-3">
                {mode !== 'reject' ? (
                  <>
                    <button data-testid="approve-btn" disabled={busy} onClick={() => run(onApprove)} className="flex-1 flex items-center justify-center gap-2 bg-primary hover:bg-primary/90 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50">
                      {busy ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle size={15} />} Approve
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
          {!events && !err && <p className="text-sm text-neutral flex items-center gap-2"><Loader2 size={14} className="animate-spin" /> Loading…</p>}
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
  onToast: (msg: string) => void
}

export default function AppRegistryPanel({ onToast }: AppRegistryPanelProps) {
  const [tab, setTab] = useState<AppStatus>('pending')
  const [apps, setApps] = useState<RegistryApp[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [review, setReview] = useState<RegistryApp | null>(null)
  // Non-null once the developer withdraws the submission under review (P6). Cleared
  // whenever a different item is opened, so one race can never haunt the next review.
  const [withdrawn, setWithdrawn] = useState<string | null>(null)
  const [auditing, setAuditing] = useState<RegistryApp | null>(null)
  // The waiting count, mirrored from the nav badge onto the Pending tab (P1). `null` =
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

  // Run a mutating action with a PER-ROW busy lock + toast, then reload. Returns the
  // FAILURE, or null when `fn()` didn't throw — so callers can both gate on success and
  // inspect which failure it was. (It used to return a bare boolean; the withdrawal race
  // needs the error's `code`, and re-throwing after already toasting would have made the
  // one caller that cares wrap every call in a second try.)
  const act = async (appId: string, fn: () => Promise<unknown>, okMsg?: string): Promise<unknown> => {
    setBusyIds((s) => new Set(s).add(appId))
    try { await fn(); if (okMsg) onToast(okMsg) ; await load(); return null }
    catch (e) { onToast(e instanceof Error ? e.message : String(e)); return e }
    finally { setBusyIds((s) => { const n = new Set(s); n.delete(appId); return n }) }
  }

  /** Close the review modal on success; on the withdrawal race, keep it open and let it
   *  say what happened instead. Every other failure is already a toast and leaves the
   *  modal alone — on the D5 409 the admin still needs the submission metadata. */
  const settleReview = (failure: unknown): void => {
    if (failure === null) { setReview(null); setWithdrawn(null); return }
    if (failure instanceof ApiError && failure.code === 'submission_withdrawn') {
      setWithdrawn(failure.message)
    }
  }

  // Approve carries the submission id ON DISPLAY (the reviewed-id guard's input):
  // the server 409s with "re-submitted since you reviewed it" copy, which `act`
  // surfaces verbatim via the toast — never a generic failure. Close the modal ONLY
  // on success: on the D5 409 the admin needs the submission metadata to re-review.
  //
  // app.submissionId is nullable in the general RegistryApp schema, but the Review
  // button (and so this call) only ever fires for a 'pending' app, which always
  // carries the submission that made it pending. Unchecked pass-through, matching
  // pre-migration behavior exactly (no null guard existed before either).
  const onApprove = (app: RegistryApp) => act(app.appId, () => approveApp(app.appId, app.submissionId as string), `“${appLabel(app)}” approved`).then(settleReview)
  const onReject = (app: RegistryApp, note: string) => act(app.appId, () => rejectApp(app.appId, note), `“${appLabel(app)}” rejected`).then(settleReview)
  const onToggleLogin = (app: RegistryApp) => act(app.appId, () => patchApp(app.appId, { loginRequired: !app.loginRequired }), `Login ${app.loginRequired ? 'disabled' : 'required'} for “${appLabel(app)}”`)
  const onDisable = (app: RegistryApp) => act(app.appId, () => disableApp(app.appId), `“${appLabel(app)}” disabled`)
  const onEnable = (app: RegistryApp) => act(app.appId, () => enableApp(app.appId), `“${appLabel(app)}” re-enabled`)
  // The deployed URL is DATA, not automation (R5): the operator pastes what the go-live
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
  const onDelete = (app: RegistryApp) => {
    // Names the two things that do not come back. "Data and files" undersold it: the app's
    // own PostgreSQL database is dropped outright — no export, no snapshot, no undo — and
    // the delete is the only place an admin is told so.
    if (!window.confirm(`Permanently delete “${appLabel(app)}”? Its database is dropped and its files are deleted. This cannot be undone.`)) return
    act(app.appId, () => deleteApp(app.appId), `“${appLabel(app)}” deleted`)
  }

  if (loading) {
    return <div className="flex items-center justify-center gap-2 py-16 text-neutral text-sm"><Loader2 size={16} className="animate-spin" /> Loading apps…</div>
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
            data-testid={`apps-tab-${t}`}
            onClick={() => setTab(t)}
            className={`text-xs font-medium px-3 py-1.5 rounded-md transition inline-flex items-center gap-1.5 ${tab === t ? 'bg-white text-primary shadow-sm border border-bial-border' : 'text-neutral hover:text-primary'}`}
          >
            {STATUS[t].label}
            {/* Mirrors the nav badge (P1), same component and same accessible name. */}
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
            <thead>
              <tr className="border-b border-bial-border">
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">App</th>
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Owner</th>
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Login</th>
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Status</th>
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
                    <td data-testid={`db-bytes-${app.appId}`} className="py-3 pr-6 text-neutral whitespace-nowrap">{fmtBytes(app.databaseBytes)}</td>
                    <td className="py-3">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        {app.status === 'pending' && (
                          <button data-testid={`review-${app.appId}`} onClick={() => { setWithdrawn(null); setReview(app) }} disabled={busy} className="px-2.5 py-1.5 rounded-lg bg-primary/10 text-primary hover:bg-primary/20 transition text-xs font-medium disabled:opacity-50">Review</button>
                        )}
                        {app.status === 'approved' && app.redeployNeeded && (
                          <span data-testid={`redeploy-needed-${app.appId}`} title="The approved build has not been deployed (or was re-approved since the last deploy) — run the go-live runbook, then mark it deployed" className="inline-flex items-center text-[11px] font-semibold px-2 py-1 rounded-lg bg-amber-100 text-amber-700">Deploy needed</span>
                        )}
                        {/* R17a: the self-publish lineage has NO runbook step, so it gets
                            neither the prompt above (the server already forces
                            `redeployNeeded` false for it) nor this control — which the
                            server refuses anyway. An affordance whose only outcome is a
                            refusal is a bug, not a safety net. */}
                        {app.status === 'approved' && app.approvalRoute !== 'self_publish' && (
                          <button data-testid={`mark-deployed-${app.appId}`} onClick={() => onMarkDeployed(app)} disabled={busy} title="Record that the go-live runbook was run for the approved build" className="inline-flex items-center gap-1 text-xs font-medium px-2 py-1 rounded-lg border border-bial-border text-neutral hover:text-primary hover:bg-bial-bg transition disabled:opacity-50"><Rocket size={12} /> Mark deployed</button>
                        )}
                        {app.status === 'approved' && (
                          <button onClick={() => onDisable(app)} disabled={busy} title="Disable (kill switch)" className="p-1.5 rounded-lg border border-bial-border text-amber-600 hover:bg-amber-50 transition disabled:opacity-50"><Power size={13} /></button>
                        )}
                        {app.status === 'disabled' && (
                          <button onClick={() => onEnable(app)} disabled={busy} title="Re-enable" className="p-1.5 rounded-lg border border-bial-border text-green-600 hover:bg-green-50 transition disabled:opacity-50"><Power size={13} /></button>
                        )}
                        <button data-testid={`audit-${app.appId}`} onClick={() => setAuditing(app)} disabled={busy} title="View audit" className="p-1.5 rounded-lg border border-bial-border text-neutral hover:text-primary hover:bg-bial-bg transition disabled:opacity-50"><ScrollText size={13} /></button>
                        <button onClick={() => onDelete(app)} disabled={busy} title="Delete app" className="p-1.5 rounded-lg border border-bial-border text-red-600 hover:bg-red-50 transition disabled:opacity-50"><Trash2 size={13} /></button>
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
    </>
  )
}
