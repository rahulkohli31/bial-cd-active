/**
 * The citizen's Publish control: a button, the data-classification questionnaire, live
 * progress, and the address the app ends up at.
 *
 * IT SAYS "PUBLISH" ON SCREEN AND "DEPLOY" IN THE CODE, and that split is deliberate rather
 * than an oversight half-corrected. The people using this are airport staff describing an
 * app in plain English — "deploy" is a word from our world, not theirs, while "publish" is
 * what they already mean. The code keeps `deploy` because that is what the route, the table,
 * and the service are called, and renaming those to chase the copy would leave the API and
 * the UI disagreeing in a worse place. Change the strings freely; leave the identifiers.
 *
 * NO ADMIN APPROVAL ON THIS PATH. `SubmitControl` sits beside this one and still drives the
 * submit/approve/reject lifecycle; the two are separate on purpose and neither reads the
 * other's state. A self-deployed app stays `draft`, so nothing here changes the badge that
 * control renders.
 *
 * THE SERVER DECIDES. `DataClassificationModal` shows a running total, but this component
 * sends whatever the citizen answered and renders whatever comes back — including the 409
 * refusal, whose message is the server's own copy naming the score, the threshold, and what
 * was not declared. There is no client-side threshold check before the call, because a
 * client that refused on its own would be enforcing a hand-synced duplicate of the weights.
 *
 * THE REFUSAL IS SHOWN INSIDE THE MODAL, not after it closes. It arrives as a thrown
 * `ApiError` from `onConfirm`, which the modal catches and renders next to Deploy while the
 * answers are still on screen — which is the only place it is actionable, since the fix is
 * to change an answer. Only a 202 closes the modal.
 *
 * POLLING, NOT STREAMING. A deploy runs for minutes and the route returns 202 immediately,
 * so progress is read from `GET /deployment`. The poll is torn down on unmount and guarded
 * by a per-run token, because a citizen can leave the page and come back, or switch
 * projects, while one is still running.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { CheckCircle2, ExternalLink, Loader2, Rocket, XCircle } from 'lucide-react'
import {
  CLASSIFICATION_REFUSED,
  UNSAVED_CHANGES,
  getDeployment,
  startDeploy,
  stepLabel,
  type DataClassificationAnswers,
  type DeploymentView,
} from '../utils/deployApi'
import { ApiError } from '../utils/apiError'
import DataClassificationModal from './DataClassificationModal'

export interface DeployControlProps {
  projectId: string
}

/** How often to ask where the deploy has got to. Five seconds: the pipeline's phases last
 *  tens of seconds to minutes, so anything tighter is load without extra information. */
const POLL_MS = 5000

export default function DeployControl({ projectId }: DeployControlProps): React.ReactElement {
  const [deployment, setDeployment] = useState<DeploymentView | null>(null)
  const [showModal, setShowModal] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  // The answers are held here, not in the modal, so the "Save and deploy" retry can resend
  // exactly what the citizen already declared instead of reopening the questionnaire.
  const pendingAnswers = useRef<DataClassificationAnswers | null>(null)
  const [unsaved, setUnsaved] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const running = deployment?.status === 'running'

  // One generation token per mount+project. Every async write checks it, so a response for
  // a project the user has already navigated away from can never paint over the current one
  // — React Router reuses this instance across a projectId change.
  const generation = useRef(0)

  const refresh = useCallback(async (): Promise<DeploymentView | null> => {
    const mine = generation.current
    try {
      const next = await getDeployment(projectId)
      if (generation.current !== mine) return null
      setDeployment(next)
      setLoadError(null)
      return next
    } catch (err) {
      if (generation.current !== mine) return null
      // A 503 means publishing is not switched on for this environment. That is a
      // configuration state, not a failure of anything the citizen did, so the control
      // simply does not offer a button rather than showing them an error they cannot act on.
      if (err instanceof ApiError && err.status === 503) {
        setDeployment(null)
        setLoadError(null)
        return null
      }
      setLoadError(
        err instanceof ApiError ? err.message : 'Could not read the publish status.',
      )
      return null
    }
  }, [projectId])

  useEffect(() => {
    generation.current += 1
    const mine = generation.current
    setDeployment(null)
    setUnsaved(null)
    pendingAnswers.current = null
    void refresh()
    // The interval keeps running for the life of the mount rather than being started and
    // stopped as the status changes: a deploy can also be started from another tab, and a
    // control that only polled after ITS OWN button press would show a stale "not deployed"
    // for as long as the page stayed open.
    const timer = window.setInterval(() => {
      if (generation.current === mine) void refresh()
    }, POLL_MS)
    return () => window.clearInterval(timer)
  }, [refresh])

  const send = useCallback(
    async (answers: DataClassificationAnswers, saveFirst: boolean): Promise<void> => {
      await startDeploy(projectId, { answers, saveFirst })
      pendingAnswers.current = null
      setUnsaved(null)
      setShowModal(false)
      await refresh()
    },
    [projectId, refresh],
  )

  // Thrown errors propagate to the modal, which renders them beside Deploy. The one
  // exception is `unsaved_changes`: that is not a reason to fail, it is a question with a
  // second answer, so the modal closes and the choice is offered where the citizen can see
  // what it means for their work.
  const onConfirm = useCallback(
    async (answers: DataClassificationAnswers): Promise<void> => {
      pendingAnswers.current = answers
      try {
        await send(answers, false)
      } catch (err) {
        if (err instanceof ApiError && err.code === UNSAVED_CHANGES) {
          setShowModal(false)
          setUnsaved(err.message)
          return
        }
        throw err
      }
    },
    [send],
  )

  const saveAndDeploy = useCallback(async (): Promise<void> => {
    const answers = pendingAnswers.current
    if (!answers) return
    setSaving(true)
    try {
      await send(answers, true)
    } catch (err) {
      setUnsaved(
        err instanceof ApiError ? err.message : 'Could not save and publish. Please try again.',
      )
    } finally {
      setSaving(false)
    }
  }, [send])

  const failedOnClassification = deployment?.failureCode === CLASSIFICATION_REFUSED

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
        {deployment?.status === 'succeeded' && (
          <span
            data-testid="deploy-status"
            className="text-xs font-semibold px-2.5 py-1 rounded-full text-green-700 bg-green-100 flex items-center gap-1.5"
          >
            <CheckCircle2 size={12} />
            Live
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

      {deployment?.status === 'succeeded' && deployment.url && (
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
              onClick={() => void saveAndDeploy()}
              className="flex items-center gap-1.5 text-xs font-semibold bg-primary hover:bg-primary/90 disabled:opacity-50 text-white px-3 py-1.5 rounded-lg transition"
            >
              {saving && <Loader2 size={12} className="animate-spin" />}
              Save and publish
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={() => {
                setUnsaved(null)
                pendingAnswers.current = null
              }}
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
        <DataClassificationModal onConfirm={onConfirm} onCancel={() => setShowModal(false)} />
      )}
    </div>
  )
}
