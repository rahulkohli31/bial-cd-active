/**
 * The publish state machine, shared by the THREE places a citizen meets it: the compact
 * button in the builder toolbar (where they are the moment a build finishes), the card on
 * the project page (where they go back to check on it), and the review status card beside
 * that card (where they watch a routed version wait for an administrator).
 *
 * Extracted rather than duplicated because the fiddly parts — the generation token, the
 * poll lifetime, treating `unsaved_changes` as a question rather than a failure — are exactly
 * the parts that rot when copied. Three presentations, one behaviour.
 *
 * THE APPROVAL LIFECYCLE COMES THROUGH HERE TOO (U12), off the same status response, and
 * that is what makes the surfaces agree. The status card used to read `/apps/:id/status`
 * itself, once, on mount — so a citizen who pressed Publish and watched their app route
 * into the queue sat there being told it was still a draft. Routing the lifecycle through
 * this hook hands every surface the refresh lifetime that already exists here instead of a
 * second one that rots, and it is the only way the toolbar button can show pending at all:
 * that one is mounted with a project id and no app id.
 *
 * MUTATIONS ANNOUNCE THEMSELVES to every other mount watching the same project. The
 * visibility/focus listeners below already exist because a publish can start from the
 * OTHER surface — but they only fire when the tab is re-entered, and the publish card and
 * the status card sit two inches apart on one screen, where nothing is ever re-entered. A
 * withdrawal in one that left the other saying "waiting for review" is precisely the
 * disagreement this unit exists to remove.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  UNSAVED_CHANGES,
  getDeployment,
  startDeploy,
  type ApprovalState,
  type DataClassificationAnswers,
  type DeploymentView,
  type RoutedForReview,
} from '../utils/deployApi'
import { withdrawSubmission } from '../utils/approvalApi'
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

export interface UseDeployment {
  deployment: DeploymentView | null
  /** The app's approval lifecycle, off the same status response — null only when the
   *  project has no app yet. */
  approval: ApprovalState | null
  running: boolean
  /** A version is in the administrator's queue: publishing is closed until it is approved
   *  or withdrawn (R15b). Both publish surfaces branch on THIS, never on their own idea
   *  of what pending means. */
  waitingForReview: boolean
  loadError: string | null
  /** The server's `unsaved_changes` message, or null. Non-null means the "Save and publish"
   *  choice is outstanding. */
  unsaved: string | null
  saving: boolean
  /** The publish request that ROUTED instead of publishing, until the next attempt. An
   *  outcome, not an error — it resolves, and the surfaces render it informationally. */
  routed: RoutedForReview | null
  /** Hand to the modal's `onConfirm`. Throws so the modal renders the refusal itself. */
  onConfirm: (answers: DataClassificationAnswers) => Promise<void>
  saveAndPublish: () => Promise<void>
  dismissUnsaved: () => void
  /** Pull the owner's own pending submission back out of the queue (P6). */
  withdraw: () => Promise<void>
  withdrawing: boolean
  withdrawError: string | null
}

export function useDeployment(projectId: string): UseDeployment {
  const [deployment, setDeployment] = useState<DeploymentView | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [unsaved, setUnsaved] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [routed, setRouted] = useState<RoutedForReview | null>(null)
  const [withdrawing, setWithdrawing] = useState(false)
  const [withdrawError, setWithdrawError] = useState<string | null>(null)
  // Held here, not in the modal, so the "Save and publish" retry resends exactly what was
  // already declared instead of reopening the questionnaire.
  const pendingAnswers = useRef<DataClassificationAnswers | null>(null)

  // One generation token per mount+project. Every async write checks it, so a response for a
  // project the user has already navigated away from can never paint over the current one —
  // React Router reuses component instances across a projectId change.
  const generation = useRef(0)
  // Stable for the life of the mount — identifies whose nudge is whose.
  const mountId = useRef(++mountCounter)

  const refresh = useCallback(async (): Promise<void> => {
    const mine = generation.current
    try {
      const next = await getDeployment(projectId)
      if (generation.current !== mine) return
      setDeployment(next)
      setLoadError(null)
    } catch (err) {
      if (generation.current !== mine) return
      // 503 means publishing is not switched on for this environment — a configuration fact,
      // not a failure of anything the citizen did, so it renders as no control rather than an
      // error they cannot act on.
      if (err instanceof ApiError && err.status === 503) {
        setDeployment(null)
        setLoadError(null)
        return
      }
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
    setDeployment(null)
    setUnsaved(null)
    setRouted(null)
    setWithdrawError(null)
    pendingAnswers.current = null
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
    window.dispatchEvent(
      new CustomEvent<DeploymentChanged>(DEPLOYMENT_CHANGED, {
        detail: { projectId, origin: mountId.current },
      }),
    )
  }, [projectId])

  const running = deployment?.status === 'running'
  const approval = deployment?.approval ?? null
  const waitingForReview = approval?.status === 'pending'

  // Poll ONLY while something is in flight. A deploy is the only state that changes on its
  // own, so a timer outliving it is pure traffic — a finished deploy left this hitting the
  // API every five seconds for as long as the page stayed open, forever.
  useEffect(() => {
    if (!running) return undefined
    const mine = generation.current
    const timer = window.setInterval(() => {
      if (generation.current === mine) void refresh()
    }, POLL_MS)
    return () => window.clearInterval(timer)
  }, [running, refresh])

  const send = useCallback(
    async (answers: DataClassificationAnswers, saveFirst: boolean): Promise<void> => {
      // TWO success shapes (U9). Routing is not an error and must not be thrown: the
      // modal would render it in red beside the button, and the citizen would read "your
      // app was sent for review" as a failure of the thing they just asked for.
      const outcome = await startDeploy(projectId, { answers, saveFirst })
      pendingAnswers.current = null
      setUnsaved(null)
      setRouted(outcome.outcome === 'routed_for_review' ? outcome : null)
      await refresh()
      announce()
    },
    [projectId, refresh, announce],
  )

  // Errors propagate to the modal, which renders them beside the button while the answers are
  // still on screen. `unsaved_changes` is the exception: not a reason to fail, but a question
  // with a second answer, so it is surfaced as a choice instead.
  //
  // EVERY OTHER ERROR REFRESHES BEFORE IT RETHROWS. A 409 here is usually the server
  // telling this surface something it did not know yet — most often `waiting_for_review`,
  // where another tab (or the other publish control, mounted on a different page) already
  // routed a version while this one still showed the button enabled. R15b relies on the
  // disabled waiting state to stop a second submit, but that state is only as fresh as the
  // last poll. Rethrowing alone left the modal open on state the server had already
  // contradicted, until the next tick happened to correct it.
  const onConfirm = useCallback(
    async (answers: DataClassificationAnswers): Promise<void> => {
      pendingAnswers.current = answers
      try {
        await send(answers, false)
      } catch (err) {
        if (err instanceof ApiError && err.code === UNSAVED_CHANGES) {
          setUnsaved(err.message)
          return
        }
        // Fire-and-forget on purpose: the caller is about to see the error either way, and
        // making them wait on a second round trip to read it would be worse.
        void refresh()
        throw err
      }
    },
    [send, refresh],
  )

  const saveAndPublish = useCallback(async (): Promise<void> => {
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

  const dismissUnsaved = useCallback(() => {
    setUnsaved(null)
    pendingAnswers.current = null
  }, [])

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
      // The routed banner described the submission that just left the queue.
      setRouted(null)
      await refresh()
      announce()
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
    running,
    waitingForReview,
    loadError,
    unsaved,
    saving,
    routed,
    onConfirm,
    saveAndPublish,
    dismissUnsaved,
    withdraw,
    withdrawing,
    withdrawError,
  }
}
