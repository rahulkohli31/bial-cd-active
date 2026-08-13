import { useState, useEffect, useCallback, useRef } from 'react'
import {
  Loader2, AlertCircle, RefreshCw, Box, CheckCircle, XCircle, X,
  Power, Trash2, ScrollText, Download, Rocket,
} from 'lucide-react'
import {
  listApps, approveApp, rejectApp, disableApp, enableApp,
  bundleDownloadUrl, markDeployed, deleteApp, fetchAudit,
} from '../../utils/appRegistryApi'
import type { RegistryApp, AppStatus, AuditEvent } from '../../utils/appRegistryApi'

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
  // new Date(null) coerces to new Date(0) (epoch) at runtime — TS's Date overloads
  // just don't accept null directly, so it's spelled out explicitly rather than cast.
  const d = iso === null ? new Date(0) : new Date(iso)
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

/**
 * Review a pending SUBMISSION by its metadata — submitter, submitted-at, commit
 * SHA, submission id — with an audited bundle download for out-of-band
 * inspection (a git bundle is opaque binary; it cannot render here). Approve
 * sends EXACTLY the submission id on display, so the server's reviewed-id guard
 * has something to check: a re-submit since this review 409s, never a silent
 * promotion of an unreviewed build.
 */
interface ReviewModalProps {
  app: RegistryApp
  onClose: () => void
  onApprove: () => Promise<void>
  onReject: (note: string) => Promise<void>
  onToast: (msg: string) => void
}

function ReviewModal({ app, onClose, onApprove, onReject, onToast }: ReviewModalProps) {
  const [mode, setMode] = useState<'reject' | null>(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    try { await fn() } finally { setBusy(false) }
  }
  const download = async () => {
    // Pre-open the tab SYNCHRONOUSLY, inside the click tick, so it rides the user
    // gesture. A tab opened AFTER the awaited network mint is blocked by Safari
    // (any await before window.open) and unreliable on Chrome (round-trip latency).
    const w = window.open('', '_blank', 'noopener')
    if (w === null) {
      onToast('Your browser blocked the download tab — allow pop-ups for this site and try again.')
      return
    }
    setDownloading(true)
    try {
      // The minted URL is a short-TTL bearer credential: it only ever touches the
      // pre-opened tab's location — never rendered, stored, or logged. Audited server-side.
      const minted = await bundleDownloadUrl(app.appId)
      w.location = minted.url
    } catch (e) {
      w.close()
      onToast(e instanceof Error ? e.message : String(e))
    } finally {
      setDownloading(false)
    }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
        <div className="flex items-start justify-between">
          <div>
            <h3 className="text-base font-bold text-tertiary">Review “{app.name || app.appId}”</h3>
            <p className="text-sm text-neutral mt-0.5">Owner: {app.ownerUsername || '—'}</p>
          </div>
          <button onClick={onClose} className="p-1.5 text-neutral hover:text-tertiary rounded-lg hover:bg-bial-bg transition"><X size={18} /></button>
        </div>
        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
          <dt className="text-neutral">Submitted</dt>
          <dd data-testid="review-submitted-at" className="text-tertiary">{fmtWhen(app.submittedAt)}</dd>
          <dt className="text-neutral">Build</dt>
          <dd><code data-testid="review-commit-sha" className="text-tertiary bg-bial-bg rounded px-1 py-0.5">{(app.commitSha || '').slice(0, 12) || '—'}</code></dd>
          <dt className="text-neutral">Submission</dt>
          <dd data-testid="review-submission-id" className="text-tertiary truncate">{app.submissionId || '—'}</dd>
        </dl>
        <p className="text-xs text-neutral mt-3">
          Review happens out-of-band: download the submitted bundle and inspect it, then approve
          exactly what you reviewed. Approval pins this submission; go-live is a manual step the
          platform team performs per the approved-app go-live runbook.
        </p>
        <button
          data-testid="download-bundle"
          onClick={download}
          disabled={downloading}
          className="mt-3 inline-flex items-center gap-1.5 text-xs font-medium px-2.5 py-1.5 rounded-lg border border-bial-border text-tertiary hover:text-primary hover:bg-bial-bg transition disabled:opacity-50"
        >
          {downloading ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />} Download bundle
        </button>
        {mode === 'reject' && (
          <textarea
            data-testid="reject-note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Feedback for the developer (optional)…"
            rows={3}
            className="mt-4 w-full border border-bial-border rounded-xl px-3 py-2.5 text-sm text-tertiary placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary resize-none"
          />
        )}
        <div className="flex gap-3 mt-5">
          {mode !== 'reject' ? (
            <>
              <button data-testid="approve-btn" disabled={busy} onClick={() => run(onApprove)} className="flex-1 flex items-center justify-center gap-2 bg-primary hover:bg-primary/90 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50">
                {busy ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle size={15} />} Approve
              </button>
              <button onClick={() => setMode('reject')} className="flex-1 flex items-center justify-center gap-2 border border-bial-border hover:border-red-300 hover:text-red-600 text-tertiary font-semibold py-2.5 rounded-xl transition text-sm">
                <XCircle size={15} /> Reject
              </button>
            </>
          ) : (
            <>
              <button data-testid="reject-confirm" disabled={busy} onClick={() => run(() => onReject(note))} className="flex-1 bg-red-600 hover:bg-red-700 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50">Send rejection</button>
              <button onClick={() => setMode(null)} className="px-4 border border-bial-border text-neutral hover:text-tertiary py-2.5 rounded-xl transition text-sm">Back</button>
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
          <h2 className="text-base font-bold text-tertiary">Audit — {app.name || app.appId}</h2>
          <button onClick={onClose} className="p-1.5 text-neutral hover:text-tertiary rounded-lg hover:bg-bial-bg transition"><X size={18} /></button>
        </div>
        <div className="flex-1 overflow-y-auto p-4">
          {!events && !err && <p className="text-sm text-neutral flex items-center gap-2"><Loader2 size={14} className="animate-spin" /> Loading…</p>}
          {err && <p className="text-sm text-red-600">{err}</p>}
          {events && events.length === 0 && <p className="text-sm text-neutral">No events yet.</p>}
          {events && events.length > 0 && (
            <ul className="space-y-2">
              {events.map((ev) => (
                <li key={ev.id} className="text-sm border border-bial-border rounded-lg px-3 py-2">
                  <div className="flex items-center justify-between">
                    <span className="font-semibold text-tertiary">{ev.action}</span>
                    <span className="text-[11px] text-neutral">{fmtWhen(ev.createdAt)}</span>
                  </div>
                  <p className="text-[11px] text-neutral mt-0.5">
                    {ev.username || 'anonymous'}{ev.resourceId ? ` · ${ev.resourceId}` : ''}{ev.count != null ? ` · ${ev.count}` : ''}
                  </p>
                </li>
              ))}
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
  const [auditing, setAuditing] = useState<RegistryApp | null>(null)
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
    } catch (e) {
      if (loadSeq.current === seq) setError(e instanceof Error ? e.message : String(e))
    } finally {
      // Only the freshest load owns the spinner — a stale one resolving late must not
      // flip `loading` off under a newer in-flight fetch.
      if (loadSeq.current === seq) setLoading(false)
    }
  }, [tab])

  useEffect(() => { load() }, [load])

  // Run a mutating action with a PER-ROW busy lock + toast, then reload. Returns
  // true only when `fn()` didn't throw, so callers can gate on success.
  const act = async (appId: string, fn: () => Promise<unknown>, okMsg?: string): Promise<boolean> => {
    setBusyIds((s) => new Set(s).add(appId))
    try { await fn(); if (okMsg) onToast(okMsg) ; await load(); return true }
    catch (e) { onToast(e instanceof Error ? e.message : String(e)); return false }
    finally { setBusyIds((s) => { const n = new Set(s); n.delete(appId); return n }) }
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
  const onApprove = (app: RegistryApp) => act(app.appId, () => approveApp(app.appId, app.submissionId as string), `“${app.name || app.appId}” approved`).then((ok) => { if (ok) setReview(null) })
  const onReject = (app: RegistryApp, note: string) => act(app.appId, () => rejectApp(app.appId, note), `“${app.name || app.appId}” rejected`).then((ok) => { if (ok) setReview(null) })
  const onDisable = (app: RegistryApp) => act(app.appId, () => disableApp(app.appId), `“${app.name || app.appId}” disabled`)
  const onEnable = (app: RegistryApp) => act(app.appId, () => enableApp(app.appId), `“${app.name || app.appId}” re-enabled`)
  // The deployed URL is DATA, not automation (R5): the operator pastes what the go-live
  // runbook produced. Prompting (like `onDelete`'s confirm) keeps this on the runbook's
  // own rhythm — mark the deploy the moment it lands, address in hand. Cancel aborts
  // entirely; a blank answer still records the deploy and leaves any existing URL alone,
  // so a re-deploy of the same app needs no re-typing. An invalid URL comes back as the
  // server's 422 copy through `act`'s toast — no duplicated client-side check.
  const onMarkDeployed = (app: RegistryApp) => {
    const answer = window.prompt(
      `Deployed URL for “${app.name || app.appId}” (https://…). Leave blank to record the deploy without changing the URL.`,
      app.deployedUrl || '',
    )
    if (answer === null) return
    const url = answer.trim()
    return act(app.appId, () => markDeployed(app.appId, url), `Deployment recorded for “${app.name || app.appId}”`)
  }
  const onDelete = (app: RegistryApp) => {
    // Names the two things that do not come back. "Data and files" undersold it: the app's
    // own PostgreSQL database is dropped outright — no export, no snapshot, no undo — and
    // the delete is the only place an admin is told so.
    if (!window.confirm(`Permanently delete “${app.name || app.appId}”? Its database is dropped and its files are deleted. This cannot be undone.`)) return
    act(app.appId, () => deleteApp(app.appId), `“${app.name || app.appId}” deleted`)
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
            className={`text-xs font-medium px-3 py-1.5 rounded-md transition ${tab === t ? 'bg-white text-primary shadow-sm border border-bial-border' : 'text-neutral hover:text-primary'}`}
          >
            {STATUS[t].label}
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
                          <p className="font-semibold text-tertiary whitespace-nowrap">{app.name || '(untitled)'}</p>
                          <p className="text-[11px] text-neutral">{app.appId}</p>
                        </div>
                      </div>
                    </td>
                    <td className="py-3 pr-6 text-tertiary whitespace-nowrap">{app.ownerUsername || '—'}</td>
                    <td className="py-3 pr-6"><StatusBadge status={app.status} /></td>
                    <td data-testid={`db-bytes-${app.appId}`} className="py-3 pr-6 text-neutral whitespace-nowrap">{fmtBytes(app.databaseBytes)}</td>
                    <td className="py-3">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        {app.status === 'pending' && (
                          <button data-testid={`review-${app.appId}`} onClick={() => setReview(app)} disabled={busy} className="px-2.5 py-1.5 rounded-lg bg-primary/10 text-primary hover:bg-primary/20 transition text-xs font-medium disabled:opacity-50">Review</button>
                        )}
                        {app.status === 'approved' && app.redeployNeeded && (
                          <span data-testid={`redeploy-needed-${app.appId}`} title="The approved build has not been deployed (or was re-approved since the last deploy) — run the go-live runbook, then mark it deployed" className="inline-flex items-center text-[11px] font-semibold px-2 py-1 rounded-lg bg-amber-100 text-amber-700">Deploy needed</span>
                        )}
                        {app.status === 'approved' && (
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

      {review && <ReviewModal app={review} onClose={() => setReview(null)} onApprove={() => onApprove(review)} onReject={(note) => onReject(review, note)} onToast={onToast} />}
      {auditing && <AuditDrawer app={auditing} onClose={() => setAuditing(null)} />}
    </>
  )
}
