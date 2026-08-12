/**
 * The citizen-dev's submit-for-review control (APPROVAL R12) — status badge,
 * submission metadata (submitted-at + commit SHA), the rejection note when
 * present, and the Submit button.
 *
 * Self-contained: loads its own status on mount and refreshes after a submit.
 * Errors render inline (`role="alert"`) with the server's own copy — the three
 * 409 reasons (build session running / nothing to submit / illegal state) arrive
 * as distinct, self-describing `ApiError` messages, so no client-side string
 * matching. The status switch ends in `assertNever`, making a future lifecycle
 * status a compile error here rather than a silently unlabelled badge.
 */
import { useEffect, useState } from 'react'
import { CheckCircle, Clock, ExternalLink, Loader2, Rocket, XCircle } from 'lucide-react'
import { getApprovalStatus, submitForReview } from '../utils/approvalApi'
import type { AppApprovalStatus } from '../utils/approvalApi'
import { ApiError } from '../utils/apiError'
import { assertNever } from '../utils/assertNever'
import type { AppStatus } from '../utils/projectApi'

export interface SubmitControlProps {
  appId: string
}

interface StatusMeta {
  label: string
  cls: string
  Icon: typeof Clock | null
}

function statusMeta(status: AppStatus): StatusMeta {
  switch (status) {
    case 'draft':
      return { label: 'Not submitted', cls: 'text-neutral bg-surface-muted', Icon: null }
    case 'pending':
      return { label: 'Pending admin review', cls: 'text-amber-700 bg-amber-100', Icon: Clock }
    case 'approved':
      return { label: 'Approved', cls: 'text-green-700 bg-green-100', Icon: CheckCircle }
    case 'rejected':
      return { label: 'Changes requested', cls: 'text-red-700 bg-red-100', Icon: XCircle }
    case 'disabled':
      return { label: 'Disabled by admin', cls: 'text-gray-600 bg-gray-200', Icon: XCircle }
    default:
      return assertNever(status)
  }
}

function formatSubmittedAt(iso: string): string {
  const parsed = new Date(iso)
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString()
}

export default function SubmitControl({ appId }: SubmitControlProps) {
  const [status, setStatus] = useState<AppApprovalStatus | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Per-request staleness guard (`let live`): a stale-`appId` or post-unmount status
  // response must not clobber the currently-displayed app's state. React Router reuses
  // this instance across a projectId change, so an `appId` prop swap can leave the prior
  // fetch in flight — only the live one may call `setStatus`/`setLoadError`.
  useEffect(() => {
    let live = true
    getApprovalStatus(appId)
      .then((next) => {
        if (live) {
          setStatus(next)
          setLoadError(null)
        }
      })
      .catch((err) => {
        if (live) {
          setLoadError(err instanceof ApiError ? err.message : 'Could not load the app status.')
        }
      })
    return () => {
      live = false
    }
  }, [appId])

  const handleSubmit = async (): Promise<void> => {
    if (busy) return
    setBusy(true)
    setSubmitError(null)
    try {
      // Update local status from the POST's OWN result (submit always clears the
      // rejection note server-side) — a bare re-fetch here would let a transient
      // follow-up GET failure hide the submit's success behind the load-error screen.
      const result = await submitForReview(appId)
      // Submit does NOT undeploy: the live app keeps serving the last-deployed build
      // until the platform team re-deploys, so the deploy marker carries forward from
      // the previous status rather than being dropped by this spread.
      setStatus((prev) => ({
        deployedAt: prev?.deployedAt ?? null,
        deployedUrl: prev?.deployedUrl ?? null,
        ...result,
        rejectionNote: null,
      }))
      setLoadError(null)
    } catch (err) {
      // The server's copy is self-describing per 409 reason — render it verbatim.
      setSubmitError(err instanceof ApiError ? err.message : 'Could not submit. Try again.')
    } finally {
      setBusy(false)
    }
  }

  const meta = status ? statusMeta(status.status) : null

  return (
    <section
      data-testid="submit-control"
      className="bg-white border border-bial-border rounded-2xl p-5"
    >
      <div className="flex items-center justify-between gap-2 mb-3">
        <h2 className="text-sm font-bold text-tertiary">Review &amp; approval</h2>
        {meta && (
          <span
            data-testid="submit-status"
            className={`inline-flex items-center gap-1.5 text-xs font-semibold px-2.5 py-1 rounded-full ${meta.cls}`}
          >
            {meta.Icon && <meta.Icon size={12} />} {meta.label}
          </span>
        )}
      </div>

      {loadError ? (
        <p className="text-xs text-danger" role="alert">
          {loadError}
        </p>
      ) : (
        <>
          {status?.submittedAt && (
            <p className="text-xs text-neutral mb-1" data-testid="submitted-at">
              Submitted {formatSubmittedAt(status.submittedAt)}
            </p>
          )}
          {status?.commitSha && (
            <p className="text-xs text-neutral mb-1">
              Build{' '}
              <code data-testid="commit-sha" className="bg-bial-bg rounded px-1 py-0.5">
                {status.commitSha.slice(0, 12)}
              </code>
            </p>
          )}
          {status?.deployedUrl && (
            <p className="text-xs text-neutral mb-1" data-testid="live-link">
              <a
                href={status.deployedUrl}
                target="_blank"
                // noreferrer alongside noopener: the deployed app is a separate origin
                // and has no business reading this portal's URL out of the referrer.
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 font-semibold text-green-700 hover:underline"
              >
                <span className="inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-green-100 text-green-700">
                  Live
                </span>
                Open your app
                <ExternalLink size={11} />
              </a>
              {status.deployedAt && (
                <span className="ml-1.5 text-neutral">
                  · deployed {formatSubmittedAt(status.deployedAt)}
                </span>
              )}
            </p>
          )}
          {status?.rejectionNote && (
            <p
              data-testid="rejection-note"
              className="text-xs text-red-600 mt-1"
              title={status.rejectionNote}
            >
              “{status.rejectionNote}”
            </p>
          )}
          {submitError && (
            <p className="text-xs text-danger mt-2" role="alert">
              {submitError}
            </p>
          )}
          <button
            type="button"
            data-testid="submit-for-review"
            onClick={() => void handleSubmit()}
            disabled={busy || status === null}
            className="mt-3 inline-flex items-center gap-1.5 bg-primary hover:bg-primary/90 disabled:opacity-50 text-white text-xs font-semibold px-3 py-1.5 rounded-lg transition"
          >
            {busy ? <Loader2 size={12} className="animate-spin" /> : <Rocket size={12} />}
            {status && status.status !== 'draft' ? 'Submit update for review' : 'Submit for review'}
          </button>
          <p className="text-[11px] text-neutral mt-2">
            {status?.deployedUrl
              ? // Once it IS live, "an approved app is deployed by the platform team" is
                // stale news — the useful thing to say is what a NEW submit does to the
                // app already serving users.
                'Your app is live. Submitting an update captures your latest build for admin review; the live app keeps running until the platform team deploys the new version.'
              : 'Submitting captures your latest build for admin review. An approved app is deployed by the platform team.'}
          </p>
        </>
      )}
    </section>
  )
}
