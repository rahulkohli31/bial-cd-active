/**
 * Publish, in the builder toolbar — beside Save, where the citizen already is when the build
 * finishes and the preview says "your app is live below".
 *
 * THE SAME CONTROL EXISTS ON THE PROJECT PAGE as a card (`DeployControl`). Two surfaces
 * because they answer different questions: this one is "I just finished, put it out there",
 * that one is "where did my app get to". Both drive `useDeployment`, so neither can drift
 * into a different idea of what publishing means.
 *
 * WHICH NOW INCLUDES THE REVIEW QUEUE. An earlier version of this docstring said the
 * approval lifecycle was a separate lineage this control never read; that is retired, not
 * softened. A publish can ROUTE the app to an administrator instead of shipping it, and
 * while a version waits, this button says so and cannot submit another (R15b). It learns
 * that from the deploy status response, because it is mounted with a project id and no app
 * id — there is no app-scoped call it could make even if it wanted one.
 *
 * Compact by necessity — this sits in a toolbar that already holds device widths, Reload and
 * Save, so it shows a state and an address and nothing more. The failure detail, the routed
 * version, and the withdrawal live on the project page, which is where the link goes.
 */
import { useState } from 'react'
import { CheckCircle2, Clock, ExternalLink, Loader2, Rocket } from 'lucide-react'
import { isLive, stepLabel } from '../utils/deployApi'
import { useDeployment } from '../hooks/useDeployment'
import DataClassificationModal from './DataClassificationModal'
import { REVIEW_STATUS_ANCHOR } from './SubmitControl'

export interface PublishButtonProps {
  projectId: string
}

export default function PublishButton({ projectId }: PublishButtonProps): React.ReactElement {
  const {
    deployment,
    approval,
    running,
    waitingForReview,
    unsaved,
    saving,
    routed,
    onConfirm,
    saveAndPublish,
    dismissUnsaved,
  } = useDeployment(projectId)
  const [showModal, setShowModal] = useState(false)

  // `isLive`, not `status === 'succeeded'`: an admin-unpublished deployment keeps that
  // status (it describes how the attempt ended), so testing the status alone would leave a
  // dead address in the toolbar with nothing to explain it (#113).
  const live = isLive(deployment) && deployment?.url

  // NO failure-code inspection here, deliberately — the card does that because the card
  // renders failures; a toolbar with no room for a failure detail has nothing to suppress.
  // Both shapes of routing reach this surface as `pending` on the shared status read, and
  // `routed` covers the one moment that read cannot: a POST that resolved routed while the
  // follow-up status fetch failed.
  const justRouted = routed !== null

  return (
    <div className="flex items-center gap-2">
      {/* The address, as soon as there is one. A toolbar is exactly where someone looks for
          "so where is it?", and a link beats making them navigate to find out. */}
      {live && (
        <a
          data-testid="publish-url"
          href={deployment.url ?? undefined}
          target="_blank"
          rel="noopener noreferrer"
          title={deployment.url ?? undefined}
          className="hidden lg:inline-flex items-center gap-1 text-[11px] font-worksans text-neutral hover:text-primary transition max-w-[200px]"
        >
          <ExternalLink size={11} className="flex-shrink-0" />
          <span className="truncate">{(deployment.url ?? '').replace(/^https?:\/\//, '')}</span>
        </a>
      )}

      {unsaved && (
        <div
          data-testid="publish-unsaved"
          className="flex items-center gap-1.5"
          role="alert"
        >
          <span className="text-[11px] text-amber-700 max-w-[180px] text-right leading-tight">
            {unsaved}
          </span>
          <button
            type="button"
            data-testid="publish-save-first"
            disabled={saving}
            onClick={() => void saveAndPublish()}
            className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1.5 text-[11px] font-worksans font-semibold bg-primary text-white hover:bg-primary-600 transition disabled:opacity-50"
          >
            {saving && <Loader2 size={11} className="animate-spin" />}
            Save and publish
          </button>
          <button
            type="button"
            disabled={saving}
            onClick={dismissUnsaved}
            className="text-[11px] font-worksans text-neutral hover:text-tertiary px-1.5 disabled:opacity-50"
          >
            Cancel
          </button>
        </div>
      )}

      {/* The routed state, in a toolbar's worth of room: what happened, and where the
          rest of it lives. Informational styling, never the failure treatment — and a
          polite live region, because this arrives while the citizen is looking at the
          preview rather than at this button. */}
      {(waitingForReview || justRouted) && (
        <span
          data-testid="publish-review-pending"
          role="status"
          aria-live="polite"
          className="hidden sm:inline-flex items-center gap-1 text-[11px] font-worksans text-amber-700"
        >
          <Clock size={11} className="flex-shrink-0" aria-hidden />
          <span>Waiting for review</span>
          <a
            data-testid="publish-review-link"
            href={`/projects/${projectId}#${REVIEW_STATUS_ANCHOR}`}
            className="font-semibold underline hover:text-amber-900"
          >
            Track it
          </a>
        </span>
      )}

      {/* R15b: while a version waits, this cannot submit another. */}
      <button
        type="button"
        data-testid="publish-button"
        disabled={running || waitingForReview}
        onClick={() => setShowModal(true)}
        title={
          waitingForReview
            ? 'An administrator is reviewing a version of this app'
            : running
              ? 'Publishing…'
              : 'Publish this app'
        }
        className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-worksans font-semibold transition disabled:opacity-60 ${
          live
            ? 'border border-bial-border bg-white text-neutral'
            : 'bg-primary text-white hover:bg-primary-600'
        }`}
      >
        {waitingForReview ? (
          <Clock size={12} />
        ) : running ? (
          <Loader2 size={12} className="animate-spin" />
        ) : live ? (
          <CheckCircle2 size={12} />
        ) : (
          <Rocket size={12} />
        )}
        {waitingForReview
          ? 'In review'
          : running
            ? stepLabel(deployment?.step ?? null)
            : live
              ? 'Published'
              : 'Publish'}
      </button>

      {showModal && (
        <DataClassificationModal
          projectId={projectId}
          // Same reason as the card: the note is read inside the flow it is about.
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
