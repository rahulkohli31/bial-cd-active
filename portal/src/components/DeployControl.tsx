/**
 * The Publish card on the project page: state, the questionnaire, the address, and the
 * failure detail when one goes wrong.
 *
 * IT SAYS "PUBLISH" ON SCREEN AND "DEPLOY" IN THE CODE, and that split is deliberate rather
 * than an oversight half-corrected. The people using this are airport staff describing an
 * app in plain English — "deploy" is a word from our world, not theirs, while "publish" is
 * what they already mean. The code keeps `deploy` because that is what the route, the table,
 * and the service are called, and renaming those to chase the copy would leave the API and
 * the UI disagreeing in a worse place. Change the strings freely; leave the identifiers.
 *
 * THE SAME CONTROL EXISTS IN THE BUILDER TOOLBAR (`PublishButton`), compact, for the moment a
 * build finishes. This one is the fuller view: it has room for the failure detail, which a
 * toolbar does not. Both drive `useDeployment`.
 *
 * THIS PATH RUNS THROUGH ADMIN APPROVAL NOW, and the sentence that used to stand here —
 * "the two are separate lineages and neither reads the other's state" — is retired rather
 * than quietly edited, because it was load-bearing and is now false. The publish gate
 * resolves the app's approval state before it ships anything: a declaration that flags
 * sensitive data ROUTES this app into the administrator's queue instead of publishing,
 * and an approval naming exactly the version being published is what lets it through.
 * `SubmitControl` beside this one is the status card for that queue, and it reads the same
 * `useDeployment` view this component does — one source, so the two cannot disagree.
 *
 * THE SERVER DECIDES. The modal shows a running total, but this component sends whatever was
 * answered and renders whatever comes back — including the routed outcome, whose message is
 * the server's own copy. There is no client-side threshold check before the call, because a
 * client that refused on its own would be enforcing a hand-synced duplicate of the weights.
 *
 * ROUTING IS NOT FAILING. A routed publish did exactly what the button said it would, so it
 * renders informationally and links to the status card — never the red badge. Two shapes
 * reach here: the immediate one (the POST resolves with `outcome: "routed_for_review"`) and
 * the drift one (the pipeline stopped and queued a newer version, arriving as a failed row
 * whose code `isRoutedForReview` recognises). Both land in the same block.
 */
import { useState } from 'react'
import {
  CheckCircle2,
  ExternalLink,
  Loader2,
  Rocket,
  Send,
  ShieldOff,
  XCircle,
} from 'lucide-react'
import { isLive, isRoutedForReview, stepLabel } from '../utils/deployApi'
import { useDeployment } from '../hooks/useDeployment'
import DataClassificationModal from './DataClassificationModal'
import { REVIEW_STATUS_ANCHOR } from './SubmitControl'

export interface DeployControlProps {
  projectId: string
}

export default function DeployControl({ projectId }: DeployControlProps): React.ReactElement {
  const {
    deployment,
    approval,
    running,
    waitingForReview,
    loadError,
    unsaved,
    saving,
    routed,
    onConfirm,
    saveAndPublish,
    dismissUnsaved,
  } = useDeployment(projectId)
  const [showModal, setShowModal] = useState(false)

  // The pipeline stopped because this version went to an administrator, not because
  // anything broke (ASM20). One named predicate, one lookup — U10's drift code joins the
  // set in `deployApi.ts` and lands here with no branch to re-reason about.
  const routedFailure = deployment?.status === 'failed' && isRoutedForReview(deployment.failureCode)
  // Either shape of routing: the POST that just resolved this way, or a pipeline that
  // ended this way.
  const routedMessage = routed?.message ?? (routedFailure ? deployment?.failureDetail : null)
  // An unpublished deployment still reads `succeeded` — that is how the attempt ended, and
  // it does not change. `isLive` is the only thing that answers "is it up right now", so
  // every affordance that implies a reachable app branches on it, not on the status (#113).
  const live = isLive(deployment)
  // `unpublishedAt` ALONE, never `status === 'succeeded' && …`: the route stamps whichever
  // deployment is NEWEST, whatever its status (`store.latest_for_app`), precisely because the
  // pipeline creates the container at step 5 and only then awaits the revision — a run that
  // settles FAILED at step 6 still leaves `pub-<app_id>` serving, and taking THAT down is the
  // case the lever most obviously exists for. Gating this on `succeeded` would stamp the row
  // server-side and then tell the citizen only that their deploy failed, with nothing on
  // screen saying an administrator acted.
  const takenDown = Boolean(deployment?.unpublishedAt)

  return (
    <div className="bg-white border border-bial-border rounded-2xl p-5" data-testid="deploy-control">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-bold text-tertiary">Publish</h3>
        {/* `!takenDown` for the same reason the `failed` badge below carries it: `takenDown` is
            status-agnostic (the takedown route stamps whichever row is newest, whatever its
            status), so a RUNNING row can wear a stamp too — and rendering both pills is the
            duplicate-testid bug, plus two contradictory answers to one question. */}
        {running && !takenDown && (
          <span
            data-testid="deploy-status"
            className="text-xs font-semibold px-2.5 py-1 rounded-full text-amber-700 bg-amber-100 flex items-center gap-1.5"
          >
            <Loader2 size={12} className="animate-spin" />
            {stepLabel(deployment?.step ?? null)}
          </span>
        )}
        {live && (
          <span
            data-testid="deploy-status"
            className="text-xs font-semibold px-2.5 py-1 rounded-full text-green-700 bg-green-100 flex items-center gap-1.5"
          >
            <CheckCircle2 size={12} />
            Live
          </span>
        )}
        {takenDown && (
          <span
            data-testid="deploy-status"
            className="text-xs font-semibold px-2.5 py-1 rounded-full text-neutral bg-bial-surface flex items-center gap-1.5"
          >
            <ShieldOff size={12} />
            Taken down
          </span>
        )}
        {/* `!takenDown` keeps this mutually exclusive with the badge above — both conditions
            are true at once for a FAILED run whose container an admin then removed, and two
            `deploy-status` nodes is both a duplicate-testid bug and two contradictory answers
            to one question. The takedown is the LATER, admin-initiated fact, so it wins the
            badge; the failure detail below still renders and explains why the run failed. */}
        {deployment?.status === 'failed' && !routedFailure && !takenDown && (
          <span
            data-testid="deploy-status"
            className="text-xs font-semibold px-2.5 py-1 rounded-full text-red-700 bg-red-100 flex items-center gap-1.5"
          >
            <XCircle size={12} />
            Didn&apos;t publish
          </span>
        )}
      </div>

      {/* One sentence, and it changes with the state a citizen is waiting on — so the
          routed and pending transitions are announced, not merely rendered (U11's
          accessibility commitment, carried across rather than left at one unit). */}
      <p
        data-testid="deploy-announce"
        role="status"
        aria-live="polite"
        className="text-xs text-neutral mt-1.5 leading-relaxed"
      >
        {waitingForReview
          ? 'A version of this app is with an administrator. You can’t publish again until they decide.'
          : running
            ? 'This takes a few minutes. You can leave this page — it keeps going.'
            : 'Publish the last version you saved. Your app gets its own address on the BIAL network.'}
      </p>

      {live && deployment?.url && (
        <a
          data-testid="deploy-url"
          href={deployment.url}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-3 inline-flex items-center gap-1.5 text-xs font-semibold text-primary hover:underline break-all"
        >
          <ExternalLink size={13} className="flex-shrink-0" />
          {deployment.url}
        </a>
      )}

      {/* No link, and a reason. The URL is deliberately not rendered as a dead link: it
          would 404, and a citizen has no way to tell that from an app that has broken. */}
      {takenDown && (
        <p data-testid="deploy-taken-down" className="text-xs text-neutral mt-3 leading-relaxed">
          An administrator has taken this app offline. Publishing again puts it back at the
          same address.
        </p>
      )}

      {/* THE ROUTED STATE, informational — never `role="alert"`, never red. The platform
          did what the dialog's "Send for review" button promised, and the citizen's next
          move is to watch it, not to fix something. The link is the one place that state
          lives, so both publish surfaces point at the same card. */}
      {(routedMessage || waitingForReview) && (
        <div
          data-testid="deploy-routed"
          className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5"
        >
          <p className="text-xs text-amber-800 leading-relaxed flex items-start gap-1.5">
            <Send size={13} className="flex-shrink-0 mt-0.5" aria-hidden />
            <span>
              {routedMessage ??
                'This app is waiting for an administrator to review the version you sent.'}
            </span>
          </p>
          {approval?.submittedSha && (
            <p className="text-xs text-amber-800 mt-1">
              Version{' '}
              <code className="bg-white/70 rounded px-1 py-0.5">
                {approval.submittedSha.slice(0, 12)}
              </code>
            </p>
          )}
          <a
            data-testid="deploy-routed-link"
            href={`#${REVIEW_STATUS_ANCHOR}`}
            className="mt-1.5 inline-block text-xs font-semibold text-amber-900 hover:underline"
          >
            Track it in Review &amp; approval
          </a>
        </div>
      )}

      {/* `!routedFailure`: a routed run is not a thing that went wrong, and its sentence
          already rendered above. Leaving this un-guarded would print the same words a
          second time, in red, under an alert role. */}
      {deployment?.status === 'failed' && deployment.failureDetail && !routedFailure && (
        <p
          data-testid="deploy-failure"
          role="alert"
          className="mt-3 text-xs text-danger leading-relaxed whitespace-pre-wrap break-words"
        >
          {deployment.failureDetail}
        </p>
      )}

      {unsaved && (
        <div data-testid="deploy-unsaved" className="mt-3">
          <p className="text-xs text-amber-700 leading-relaxed">{unsaved}</p>
          <div className="flex gap-2 mt-2">
            <button
              type="button"
              data-testid="deploy-save-first"
              disabled={saving}
              onClick={() => void saveAndPublish()}
              className="flex items-center gap-1.5 text-xs font-semibold bg-primary hover:bg-primary/90 disabled:opacity-50 text-white px-3 py-1.5 rounded-lg transition"
            >
              {saving && <Loader2 size={12} className="animate-spin" />}
              Save and publish
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={dismissUnsaved}
              className="text-xs font-semibold text-neutral hover:text-tertiary px-3 py-1.5 rounded-lg border border-bial-border transition disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {loadError && (
        <p role="alert" className="mt-3 text-xs text-danger">
          {loadError}
        </p>
      )}

      {/* R15b: while a version waits, this cannot submit again. Disabled rather than
          hidden — a control that vanishes reads as a bug, and the sentence above says
          why this one is closed. */}
      <button
        type="button"
        data-testid="deploy-button"
        disabled={running || waitingForReview}
        onClick={() => setShowModal(true)}
        className="mt-4 w-full flex items-center justify-center gap-2 bg-primary hover:bg-primary/90 disabled:opacity-50 text-white font-semibold py-2.5 rounded-xl transition text-sm"
      >
        <Rocket size={15} />
        {waitingForReview
          ? 'Waiting for review'
          : deployment?.status === 'succeeded'
            ? 'Publish again'
            : 'Publish'}
      </button>

      {showModal && (
        <DataClassificationModal
          projectId={projectId}
          // A citizen who presses Publish after a rejection reads why BEFORE anything
          // else happens — the note belongs in the flow they are actually in, not only
          // on a card beside it that they may never scroll to.
          rejectionNote={approval?.status === 'rejected' ? approval.rejectionNote : null}
          onConfirm={async (answers) => {
            await onConfirm(answers)
            setShowModal(false)
          }}
          onCancel={() => setShowModal(false)}
        />
      )}
    </div>
  )
}
