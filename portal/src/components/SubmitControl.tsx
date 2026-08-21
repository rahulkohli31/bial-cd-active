/**
 * The review status card on the project page: where a citizen watches a version that has
 * been sent to an administrator, and the one place they can pull it back.
 *
 * IT HAS NO SUBMIT BUTTON, and that absence is the feature (R15a). There is exactly one
 * route into the review queue, and it is the publish flow: a declaration that flags
 * anything weighted routes the app itself, carrying both answer sets and the citizen's
 * explanation. The button that used to live here posted to a route that attached none of
 * that, which is how a queue item could reach an administrator with nothing to read. It
 * was retired backend-side in U8; what is left here is the STATUS, plus withdrawal (P6) —
 * a citizen may pull their own pending submission back, but may never submit over one.
 *
 * THE TWO LINEAGES ARE NOT SEPARATE ANY MORE. An earlier version of this docstring, and
 * `DeployControl`'s, both asserted that publishing and approval "never read each other's
 * state". That was true and is now false by design: the publish gate resolves approval
 * before it ships anything, and this card reads its lifecycle off the very same deploy
 * status response the publish surfaces poll (`useDeployment`). One source, one refresh
 * lifetime — this card used to fetch once on mount and never again, so a citizen who
 * pressed Publish and watched their app route into the queue was left being told it was
 * still a draft.
 *
 * NOBODY DEPLOYS AN APPROVED APP FOR THE CITIZEN. The retired copy here promised a
 * platform team would; R17 gives them the button instead — an approval names the exact
 * version they may publish themselves, and they publish it from the card above this one.
 *
 * The status switch ends in `assertNever`, making a future lifecycle status a compile
 * error here rather than a silently unlabelled badge.
 */
import { CheckCircle, Clock, Loader2, ShieldOff, XCircle } from 'lucide-react'
import { useDeployment } from '../hooks/useDeployment'
import { assertNever } from '../utils/assertNever'
import type { ApprovalRoute, AppStatus } from '../utils/projectApi'

/** Where both publish surfaces point when they say "watch it here". */
export const REVIEW_STATUS_ANCHOR = 'review-status'

export interface SubmitControlProps {
  projectId: string
}

interface StatusMeta {
  label: string
  cls: string
  Icon: typeof Clock | null
  /** One sentence, announced politely on every transition — this is where a citizen
   *  learns their app routed, was approved, or came back with changes. */
  sentence: string
}

function statusMeta(status: AppStatus, route: ApprovalRoute | null): StatusMeta {
  switch (status) {
    case 'draft':
      return {
        label: 'Nothing waiting',
        cls: 'text-neutral bg-surface-muted',
        Icon: null,
        sentence:
          'Nothing is waiting for review. If a version you publish handles sensitive data, it comes here first.',
      }
    case 'pending':
      return {
        label: 'Waiting for review',
        cls: 'text-amber-700 bg-amber-100',
        Icon: Clock,
        sentence:
          'This version is with an administrator. You can withdraw it, but you cannot send another until this one is decided.',
      }
    case 'approved':
      return {
        label: 'Approved',
        cls: 'text-green-700 bg-green-100',
        Icon: CheckCircle,
        // THE LINEAGE DECIDES WHAT THIS APPROVAL BUYS, so the sentence has to read it.
        // Only a `self_publish` approval satisfies the publish gate's override; one from
        // the earlier out-of-band route does not (P5 — the cutover backfilled every
        // pre-existing approval to `runbook`), and neither does an absent or unreadable
        // lineage. Promising self-publishing to those apps would be copy asserting
        // behaviour the platform does not have: the citizen presses Publish, the gate
        // declines the override, and the version routes to an administrator instead.
        sentence:
          route === 'self_publish'
            ? 'An administrator approved this version. You can publish it yourself, above.'
            : 'An administrator approved this version through the earlier review process, '
              + 'which does not cover publishing on its own. Press Publish above and this '
              + 'version will be sent for approval once more.',
      }
    case 'rejected':
      return {
        label: 'Changes requested',
        cls: 'text-red-700 bg-red-100',
        Icon: XCircle,
        sentence:
          'An administrator sent this back with changes. Read the note below, then publish again when you have made them.',
      }
    case 'disabled':
      return {
        label: 'Disabled by admin',
        cls: 'text-gray-600 bg-gray-200',
        Icon: ShieldOff,
        sentence: 'An administrator has disabled this app. Publishing is closed until they re-enable it.',
      }
    default:
      return assertNever(status)
  }
}

function formatTimestamp(iso: string): string {
  const parsed = new Date(iso)
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString()
}

export default function SubmitControl({ projectId }: SubmitControlProps): React.ReactElement {
  const { approval, loadError, withdraw, withdrawing, withdrawError } = useDeployment(projectId)

  const meta = approval ? statusMeta(approval.status, approval.approvalRoute) : null
  const pending = approval?.status === 'pending'
  // The pinned version is whichever one this state is ABOUT: the submission in the queue
  // while one is waiting, the approved commit once one is approved. Showing the submitted
  // sha after approval would name a version the approval may no longer cover.
  const version = pending ? approval.submittedSha : (approval?.approvedCommitSha ?? null)

  return (
    <section
      id={REVIEW_STATUS_ANCHOR}
      data-testid="submit-control"
      className="bg-white border border-bial-border rounded-2xl p-5 scroll-mt-24"
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
          {/* The transitions a citizen is waiting on — routed, approved, sent back —
              arrive while they are looking at something else on the page, so they
              announce rather than only appear (carrying U11's commitment across). */}
          <p
            data-testid="submit-announce"
            role="status"
            aria-live="polite"
            className="text-xs text-neutral leading-relaxed"
          >
            {meta?.sentence ?? ''}
          </p>

          {version && (
            <p className="text-xs text-neutral mt-2">
              {pending ? 'Version sent' : 'Version approved'}{' '}
              <code data-testid="commit-sha" className="bg-bial-bg rounded px-1 py-0.5">
                {version.slice(0, 12)}
              </code>
            </p>
          )}
          {pending && approval.submittedAt && (
            <p className="text-xs text-neutral mt-1" data-testid="submitted-at">
              Sent {formatTimestamp(approval.submittedAt)}
            </p>
          )}

          {approval?.rejectionNote && (
            <p
              id="review-rejection-note"
              data-testid="rejection-note"
              className="text-xs text-red-600 mt-2 leading-relaxed whitespace-pre-wrap break-words"
            >
              “{approval.rejectionNote}”
            </p>
          )}

          {withdrawError && (
            <p className="text-xs text-danger mt-2" role="alert">
              {withdrawError}
            </p>
          )}

          {pending && (
            <button
              type="button"
              data-testid="withdraw-submission"
              onClick={() => void withdraw()}
              disabled={withdrawing}
              aria-label="Withdraw this submission from review"
              className="mt-3 inline-flex items-center gap-1.5 border border-bial-border text-tertiary hover:bg-bial-bg disabled:opacity-50 text-xs font-semibold px-3 py-1.5 rounded-lg transition"
            >
              {withdrawing && <Loader2 size={12} className="animate-spin" />}
              Withdraw
            </button>
          )}
        </>
      )}
    </section>
  )
}
