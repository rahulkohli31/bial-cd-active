/**
 * The publish state machine, shared by the two places a citizen can reach it: the compact
 * button in the builder toolbar (where they are the moment a build finishes) and the card on
 * the project page (where they go back to check on it).
 *
 * Extracted rather than duplicated because the fiddly parts — the generation token, the
 * poll lifetime, treating `unsaved_changes` as a question rather than a failure — are exactly
 * the parts that rot when copied. Two presentations, one behaviour.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  UNSAVED_CHANGES,
  getDeployment,
  startDeploy,
  type DataClassificationAnswers,
  type DeploymentView,
} from '../utils/deployApi'
import { ApiError } from '../utils/apiError'

/** How often to ask where the deploy has got to. Five seconds: the pipeline's phases last
 *  tens of seconds to minutes, so anything tighter is load without extra information. */
const POLL_MS = 5000

export interface UseDeployment {
  deployment: DeploymentView | null
  running: boolean
  loadError: string | null
  /** The server's `unsaved_changes` message, or null. Non-null means the "Save and publish"
   *  choice is outstanding. */
  unsaved: string | null
  saving: boolean
  /** Hand to the modal's `onConfirm`. Throws so the modal renders the refusal itself. */
  onConfirm: (answers: DataClassificationAnswers) => Promise<void>
  saveAndPublish: () => Promise<void>
  dismissUnsaved: () => void
}

export function useDeployment(projectId: string): UseDeployment {
  const [deployment, setDeployment] = useState<DeploymentView | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [unsaved, setUnsaved] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  // Held here, not in the modal, so the "Save and publish" retry resends exactly what was
  // already declared instead of reopening the questionnaire.
  const pendingAnswers = useRef<DataClassificationAnswers | null>(null)

  // One generation token per mount+project. Every async write checks it, so a response for a
  // project the user has already navigated away from can never paint over the current one —
  // React Router reuses component instances across a projectId change.
  const generation = useRef(0)

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
    pendingAnswers.current = null
    void refresh()

    const onVisible = (): void => {
      if (document.visibilityState === 'visible') void refresh()
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onVisible)
    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', onVisible)
    }
  }, [refresh])

  const running = deployment?.status === 'running'

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
      await startDeploy(projectId, { answers, saveFirst })
      pendingAnswers.current = null
      setUnsaved(null)
      await refresh()
    },
    [projectId, refresh],
  )

  // Errors propagate to the modal, which renders them beside the button while the answers are
  // still on screen. `unsaved_changes` is the exception: not a reason to fail, but a question
  // with a second answer, so it is surfaced as a choice instead.
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
        throw err
      }
    },
    [send],
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

  return {
    deployment,
    running,
    loadError,
    unsaved,
    saving,
    onConfirm,
    saveAndPublish,
    dismissUnsaved,
  }
}
