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
import { useCallback, useEffect, useState } from 'react'
import { CheckCircle, Clock, Loader2, Rocket, XCircle } from 'lucide-react'
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

  const refresh = useCallback(async (): Promise<void> => {
    try {
      setStatus(await getApprovalStatus(appId))
      setLoadError(null)
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : 'Could not load the app status.')
    }
  }, [appId])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const handleSubmit = async (): Promise<void> => {
    if (busy) return
    setBusy(true)
    setSubmitError(null)
    try {
      await submitForReview(appId)
      await refresh()
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
            Submitting captures your latest build for admin review. An approved app is
            deployed by the platform team.
          </p>
        </>
      )}
    </section>
  )
}
