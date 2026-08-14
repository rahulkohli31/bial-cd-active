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
 * NO ADMIN APPROVAL ON THIS PATH. `SubmitControl` sits beside this one and still drives the
 * submit/approve/reject lifecycle; the two are separate lineages and neither reads the
 * other's state. A self-published app stays `draft`, so nothing here moves that badge.
 *
 * THE SERVER DECIDES. The modal shows a running total, but this component sends whatever was
 * answered and renders whatever comes back — including the refusal, whose message is the
 * server's own copy naming the score, the threshold, and what was not declared. There is no
 * client-side threshold check before the call, because a client that refused on its own would
 * be enforcing a hand-synced duplicate of the weights.
 */
import { useState } from 'react'
import { CheckCircle2, ExternalLink, Loader2, Rocket, ShieldOff, XCircle } from 'lucide-react'
import { CLASSIFICATION_REFUSED, isLive, stepLabel } from '../utils/deployApi'
import { useDeployment } from '../hooks/useDeployment'
import DataClassificationModal from './DataClassificationModal'

export interface DeployControlProps {
  projectId: string
}

export default function DeployControl({ projectId }: DeployControlProps): React.ReactElement {
  const {
    deployment,
    running,
    loadError,
    unsaved,
    saving,
    onConfirm,
    saveAndPublish,
    dismissUnsaved,
  } = useDeployment(projectId)
  const [showModal, setShowModal] = useState(false)

  const failedOnClassification = deployment?.failureCode === CLASSIFICATION_REFUSED
  // An unpublished deployment still reads `succeeded` — that is how the attempt ended, and
  // it does not change. `isLive` is the only thing that answers "is it up right now", so
  // every affordance that implies a reachable app branches on it, not on the status (#113).
  const live = isLive(deployment)
  const takenDown = deployment?.status === 'succeeded' && Boolean(deployment.unpublishedAt)

  return (
    <div className="bg-white border border-bial-border rounded-2xl p-5" data-testid="deploy-control">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-bold text-tertiary">Publish</h3>
        {running && (
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
        {deployment?.status === 'failed' && !failedOnClassification && (
          <span
            data-testid="deploy-status"
            className="text-xs font-semibold px-2.5 py-1 rounded-full text-red-700 bg-red-100 flex items-center gap-1.5"
          >
            <XCircle size={12} />
            Didn&apos;t publish
          </span>
        )}
      </div>

      <p className="text-xs text-neutral mt-1.5 leading-relaxed">
        {running
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

      {deployment?.status === 'failed' && deployment.failureDetail && (
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

      <button
        type="button"
        data-testid="deploy-button"
        disabled={running}
        onClick={() => setShowModal(true)}
        className="mt-4 w-full flex items-center justify-center gap-2 bg-primary hover:bg-primary/90 disabled:opacity-50 text-white font-semibold py-2.5 rounded-xl transition text-sm"
      >
        <Rocket size={15} />
        {deployment?.status === 'succeeded' ? 'Publish again' : 'Publish'}
      </button>

      {showModal && (
        <DataClassificationModal
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
