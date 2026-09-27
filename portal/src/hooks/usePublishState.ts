/**
 * The publish read and the publish request, behind one lifetime — everything the chip needs
 * to say where an app stands and to act on it.
 *
 * The approval lifecycle rides the same status response, because a surface with no app id
 * (the builder, pre-submit) can show nothing else.
 *
 * WHY THIS EXISTS
 * Two refresh triggers besides the poll. The visibility/focus listeners are the cross-tab
 * story — a publish started elsewhere is picked up when this tab is looked at. The
 * `bial:deployment-changed` CustomEvent is the cross-mount story on one document: the chip
 * (`WorkspaceToolbar`) and `AppStatusPanel` (`WorkspaceRail`) mount together on the workspace
 * screen holding separate reads, and without the nudge a withdrawal in one leaves the other
 * saying "waiting for review" — the bug this closes. Its test renders two hooks explicitly
 * and pins the contract; deleting the nudge as apparently-dead code would reintroduce that
 * bug on a screen where both surfaces are visible at once.
 *
 * The nudge is also raised from outside this hook, by `announceDeploymentChanged`: a publish
 * is no longer the only thing that changes what this read returns — the last-saved row is
 * `savedHead`/`savedAt` off this same response, and the surface that writes them holds no
 * publish read of its own, so it raises the same nudge the chip and the status panel already
 * listen to.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getDeployment,
  startDeploy,
  type ApprovalState,
  type DeployOutcome,
  type DeploymentView,
  type PublishAnswers,
} from '../utils/deployApi'
import { withdrawSubmission } from '../utils/approvalApi'
import { announceReviewQueueChanged } from '../utils/appRegistryApi'
import { fetchSaveState, saveProject } from '../utils/buildSessionApi'
import { ApiError } from '../utils/apiError'

/** How often to ask where the deploy has got to. Five seconds: the pipeline's phases last
 *  tens of seconds to minutes, so anything tighter is load without extra information. */
const POLL_MS = 5000

/** The same-tab nudge every mount watching a project listens for. A CustomEvent on
 *  `window` rather than a store: there is exactly one fact to share ("re-read this
 *  project"), and the re-read already exists. */
const DEPLOYMENT_CHANGED = 'bial:deployment-changed'

interface DeploymentChanged {
  projectId: string
  /** The mount that acted. It has already refreshed synchronously as part of its own
   *  await chain, so it skips its own nudge rather than fetching the same row twice. */
  origin: number
}

let mountCounter = 0

/** The origin carried by a nudge raised from OUTSIDE any mount of this hook. Mount ids come
 *  from `++mountCounter`, so they begin at 1 and this can never be one of them — which is the
 *  whole property: a nudge nobody here owns has no mount to skip, so every mount on the
 *  project re-reads. */
const NO_MOUNT = 0

function dispatchDeploymentChanged(projectId: string, origin: number): void {
  window.dispatchEvent(
    new CustomEvent<DeploymentChanged>(DEPLOYMENT_CHANGED, { detail: { projectId, origin } }),
  )
}

/**
 * SOMETHING OUTSIDE THIS HOOK CHANGED WHAT THIS READ WOULD RETURN.
 *
 * The project screen's Save writes a new bundle, and the LAST SAVED row (`savedHead`, `savedAt`)
 * is two fields of THIS read and of no other. The saving surface holds no publish read at all:
 * the row is `AppStatusPanel`'s, the state the toolbar chip's, each with its own. So the save
 * raises the nudge those two already listen to and both reconcile off one dispatch, the case the
 * nudge was kept alive for. A second deployment fetch inside the workspace's own refresh epoch
 * would instead duplicate a reader and still leave the chip naming the previous version.
 */
export function announceDeploymentChanged(projectId: string): void {
  dispatchDeploymentChanged(projectId, NO_MOUNT)
}

/** What the one button is waiting on while it works: a save before the dialog, or the approved
 *  copy being sent. */
export type PublishPhase = 'saving' | 'publishing'

/**
 * NOTHING HERE MAY GROW A PREDICATE BACK. `running`, `waitingForReview`, `routed` — derived
 * booleans the browser used to compute from raw fields — are gone: each was the browser
 * re-deciding something the server had already decided, and each was a place two surfaces
 * could disagree. `deployment.publishState` alone says all three, the same way to everyone;
 * if it can't say what a consumer needs, the fix belongs in the server that authors it.
 */
export interface UsePublishState {
  deployment: DeploymentView | null
  /** The app's approval lifecycle, off the same status response — null only when the
   *  project has no app yet. It is here for the VERSION ROWS the chip renders (which
   *  commit was submitted, which was approved, and when), never to decide a state. */
  approval: ApprovalState | null
  loadError: string | null
  /** Read it again. The publish surface offers this as its one action when the read
   *  itself failed — a chip that rendered nothing there would be indistinguishable from
   *  a broken page. */
  refresh: () => Promise<void>
  /**
   * THE ONE BUTTON, for every action but taking a submission back. Where the server hands back
   * `approvedRetryCommit` it posts that commit — no save, no review, no dialog — and resolves
   * with the outcome. Otherwise it saves any unsaved work and resolves `'review'`: the caller
   * opens the dialog, which reviews the version saved now. `null` when that failed, with the
   * reason in `publishError` and no dialog.
   */
  publish: () => Promise<DeployOutcome | 'review' | null>
  publishPhase: PublishPhase | null
  publishError: string | null
  /** Hand to the dialog's `onConfirm`: sends the commit it reviewed. Throws so the dialog
   *  renders the refusal itself.
   *
   *  RESOLVES WITH THE OUTCOME, because the two successes are two different answers and only
   *  the caller can say them: `202 started` and `200 routed_for_review` both resolve, and a
   *  surface that could not tell them apart would have to guess which of the server's two
   *  sentences to speak. */
  onConfirm: (commitSha: string, send: PublishAnswers) => Promise<DeployOutcome>
  /** Pull the owner's own pending submission back out of the queue. */
  withdraw: () => Promise<void>
  withdrawing: boolean
  withdrawError: string | null
}

export function usePublishState(projectId: string): UsePublishState {
  const [deployment, setDeployment] = useState<DeploymentView | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [publishPhase, setPublishPhase] = useState<PublishPhase | null>(null)
  const [publishError, setPublishError] = useState<string | null>(null)
  const [withdrawing, setWithdrawing] = useState(false)
  const [withdrawError, setWithdrawError] = useState<string | null>(null)
  // The press itself, read and written synchronously: two presses inside one frame both see
  // the same `publishPhase`, and only a ref settles that race.
  const pressing = useRef(false)

  // One generation token per mount+project. Every async write checks it, so a response for a
  // project the user has already navigated away from can never paint over the current one —
  // React Router reuses component instances across a projectId change.
  const generation = useRef(0)
  // Whether this mount has ever read THIS project's row. A ref rather than derived from
  // `deployment`, because reading that state here would put it in `refresh`'s dependencies and
  // re-run the mount effect — re-subscribing the listeners and re-reading — on every response.
  const everRead = useRef(false)
  // Stable for the life of the mount — identifies whose nudge is whose.
  const mountId = useRef(++mountCounter)

  const refresh = useCallback(async (): Promise<void> => {
    const mine = generation.current
    try {
      const next = await getDeployment(projectId)
      if (generation.current !== mine) return
      setDeployment(next)
      everRead.current = true
      setLoadError(null)
    } catch (err) {
      if (generation.current !== mine) return
      // A re-read that fails keeps the row it already has. `loadError` is rendered first by
      // both surfaces and replaces everything — the pill, every provenance row and the action
      // become one line — so a failed read that follows a save would blank the whole panel on
      // a screen that has just said "Saved". A row naming the previous version is worse than
      // one naming the current one and better than no panel at all, and the citizen still has
      // the state, the dates and the action they had a moment ago.
      //
      // The first read is the exception: a mount that has never had an answer has nothing
      // better to show than the failure, and a blank section there really would be
      // indistinguishable from a broken page.
      if (everRead.current) return
      // Every failed read lands in one place — blanking the surface and reporting nothing is
      // not an option here. This is the only publishing surface the citizen has, so a chip
      // that renders nothing is indistinguishable from a broken page. The server no longer
      // 503s on a storage blip either: it degrades that to the explicit unknown state and
      // answers 200, so special-casing 503 would not catch it, and this read no longer
      // requires a deploy pipeline to exist at all.
      setLoadError(err instanceof ApiError ? err.message : 'Could not read the publish status.')
    }
  }, [projectId])

  // Read once when the project resolves, then again whenever the tab is looked at.
  //
  // The focus listener is what makes the poll below safe to stop. A publish can be started
  // from the other surface or another tab, so this cannot ONLY refetch after its own button
  // press — but the answer to that is to check when someone is actually looking, not to hold
  // a timer open forever. An idle finished deploy costs nothing here.
  useEffect(() => {
    generation.current += 1
    everRead.current = false
    setDeployment(null)
    setPublishError(null)
    setWithdrawError(null)
    void refresh()

    const onVisible = (): void => {
      if (document.visibilityState === 'visible') void refresh()
    }
    // The same-tab counterpart: another mount on this project just changed something, so
    // re-read rather than wait for a tab switch that will never come.
    const mine = mountId.current
    const onChanged = (event: Event): void => {
      const detail = (event as CustomEvent<DeploymentChanged>).detail
      if (detail.projectId !== projectId || detail.origin === mine) return
      void refresh()
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onVisible)
    window.addEventListener(DEPLOYMENT_CHANGED, onChanged)
    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', onVisible)
      window.removeEventListener(DEPLOYMENT_CHANGED, onChanged)
    }
  }, [refresh, projectId])

  const announce = useCallback((): void => {
    dispatchDeploymentChanged(projectId, mountId.current)
  }, [projectId])

  const approval = deployment?.approval ?? null

  // Poll ONLY while something is in flight. A deploy is the only state that changes on its
  // own, so a timer outliving it is pure traffic — a finished deploy left this hitting the
  // API every five seconds for as long as the page stayed open, forever.
  //
  // THE GATE READS THE FIELD, not `status === 'running'` as it used to. Same answer in the
  // ordinary case and a better one at the edges: an app an administrator disabled or a
  // submission that routed while an OLD deployment row still sat `running` used to poll
  // for as long as the page stayed open, because the row alone never settles. The server's
  // own ordering rules those out before it ever says `starting_up`.
  const inFlight = deployment?.publishState === 'starting_up'
  useEffect(() => {
    if (!inFlight) return undefined
    const mine = generation.current
    const timer = window.setInterval(() => {
      if (generation.current === mine) void refresh()
    }, POLL_MS)
    return () => window.clearInterval(timer)
  }, [inFlight, refresh])

  const approvedRetryCommit = deployment?.approvedRetryCommit ?? null
  const publish = useCallback(async (): Promise<DeployOutcome | 'review' | null> => {
    if (pressing.current) return null
    pressing.current = true
    setPublishError(null)
    try {
      if (approvedRetryCommit !== null) {
        setPublishPhase('publishing')
        const outcome = await startDeploy(projectId, { commitSha: approvedRetryCommit })
        await refresh()
        announce()
        return outcome
      }
      // SAVE FIRST, so the review and the send are both about the version that will ship.
      // Unknown is not dirty: with no live workspace the saved version is the only version.
      const saveState = await fetchSaveState(projectId)
      if (saveState.dirty === true) {
        setPublishPhase('saving')
        await saveProject(projectId)
        await refresh()
        announce()
      }
      return 'review'
    } catch (err) {
      setPublishError(
        err instanceof ApiError ? err.message : 'That did not work. Please try again.',
      )
      void refresh()
      return null
    } finally {
      pressing.current = false
      setPublishPhase(null)
    }
  }, [approvedRetryCommit, projectId, refresh, announce])

  // TWO success shapes, and routing is not an error: thrown, the dialog would render "your app
  // was sent for review" in red beside the button, as a failure of the thing just asked for.
  //
  // EVERY REFUSAL REFRESHES BEFORE IT RETHROWS. A 409 here is usually the server telling this
  // surface something it did not know yet — `waiting_for_review` from another tab, or
  // `snapshot_moved` from a save after the dialog opened — and the dialog should not sit on
  // state the server already contradicted until the next poll.
  const onConfirm = useCallback(
    async (commitSha: string, send: PublishAnswers): Promise<DeployOutcome> => {
      try {
        const outcome = await startDeploy(projectId, { commitSha, ...send })
        await refresh()
        announce()
        // An administrator who sends their own app changes the queue their nav counts.
        if (outcome.outcome === 'routed_for_review') announceReviewQueueChanged()
        return outcome
      } catch (err) {
        // Fire-and-forget on purpose: the caller is about to see the error either way, and
        // making them wait on a second round trip to read it would be worse.
        void refresh()
        throw err
      }
    },
    [projectId, refresh, announce],
  )

  // The app id comes off the status response, not a prop: the toolbar surface never had
  // one, and taking it from the same read that says the app is pending is what keeps the
  // withdrawal aimed at the app the citizen is actually looking at.
  const appId = deployment?.appId ?? null
  const withdraw = useCallback(async (): Promise<void> => {
    if (appId === null || withdrawing) return
    setWithdrawing(true)
    setWithdrawError(null)
    try {
      await withdrawSubmission(appId)
      await refresh()
      announce()
      announceReviewQueueChanged()
    } catch (err) {
      // A 409 means an administrator got there first; the server's copy says so.
      setWithdrawError(
        err instanceof ApiError ? err.message : 'Could not withdraw this submission. Try again.',
      )
    } finally {
      setWithdrawing(false)
    }
  }, [appId, withdrawing, refresh, announce])

  return {
    deployment,
    approval,
    loadError,
    refresh,
    publish,
    publishPhase,
    publishError,
    onConfirm,
    withdraw,
    withdrawing,
    withdrawError,
  }
}
