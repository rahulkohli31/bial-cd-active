/**
 * The publish hook's OWN behaviour — the parts that are not the chip's.
 *
 * WHY THIS FILE EXISTS AND WHY IT IS SEPARATE. `PublishStatusChip.test.tsx` mocks this hook
 * at the module boundary, so nothing there runs a line of it. The three retired control
 * suites DID exercise it, through the real hook with only `deployApi` mocked — and they are
 * gone. Everything below is a guarantee one of them held, re-established here against the
 * renamed hook, plus the two the swap newly needs.
 *
 * Two of these were proven necessary rather than assumed: deleting the poll's `inFlight`
 * guard, and deleting both generation-token checks, each left the entire portal suite green
 * before this file existed. Those are the two mutants these tests exist to kill, and each
 * one names the incident it protects against.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, cleanup, waitFor } from '@testing-library/react'

import { usePublishState } from '../usePublishState'
import { ApiError } from '../../utils/apiError'
import { REVIEW_QUEUE_CHANGED } from '../../utils/appRegistryApi'
import * as buildSessionApi from '../../utils/buildSessionApi'
import * as deployApi from '../../utils/deployApi'
import type { SaveState } from '../../utils/buildSessionApi'
import type { DeploymentView, PublishAnswers, PublishState } from '../../utils/deployApi'

vi.mock('../../utils/deployApi', async () => {
  const actual = await vi.importActual<typeof deployApi>('../../utils/deployApi')
  return { ...actual, getDeployment: vi.fn(), startDeploy: vi.fn() }
})
vi.mock('../../utils/approvalApi', () => ({ withdrawSubmission: vi.fn() }))
vi.mock('../../utils/buildSessionApi', async () => {
  const actual = await vi.importActual<typeof buildSessionApi>('../../utils/buildSessionApi')
  return { ...actual, fetchSaveState: vi.fn(), saveProject: vi.fn() }
})

const getDeployment = vi.mocked(deployApi.getDeployment)
const startDeploy = vi.mocked(deployApi.startDeploy)
const fetchSaveState = vi.mocked(buildSessionApi.fetchSaveState)
const saveProject = vi.mocked(buildSessionApi.saveProject)

const saveState = (dirty: boolean | null): SaveState => ({
  appId: 'app-1',
  dirty,
  containerHead: null,
  savedHead: null,
})

const SHA = 'a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0'

const view = (publishState: PublishState, over: Partial<DeploymentView> = {}): DeploymentView => ({
  deploymentId: null,
  appId: 'app-1',
  status: null,
  step: null,
  url: null,
  headSha: null,
  failureCode: null,
  failureDetail: null,
  startedAt: null,
  finishedAt: null,
  unpublishedAt: null,
  liveUrl: null,
  approval: null,
  publishState,
  approvedRetryCommit: null,
  savedHead: null,
  savedAt: null,
  // `null` is "the server did not say", which keeps the saved row — the neutral default
  // for suites that are not about the never-saved omission.
  savedState: null,
  ...over,
})

const ANSWERS: PublishAnswers = {
  answers: { ai_usage: false, public_data: true },
  note: 'Staff names only.',
}

const STARTED = {
  outcome: 'started' as const,
  deploymentId: 'd1',
  appId: 'app-1',
  status: 'running',
}

const ROUTED = {
  outcome: 'routed_for_review' as const,
  appId: 'app-1',
  submissionId: 's1',
  commitSha: SHA,
  submittedAt: '2026-08-19T10:00:00Z',
  message: 'Your app was sent to an administrator for review.',
}

beforeEach(() => {
  vi.clearAllMocks()
  getDeployment.mockResolvedValue(view('live_current'))
  fetchSaveState.mockResolvedValue(saveState(false))
  saveProject.mockResolvedValue({ appId: 'app-1', headSha: SHA })
})
afterEach(cleanup)

describe('the poll runs only while something is actually changing on its own', () => {
  // THE INCIDENT: an ungated timer hit the API every five seconds for as long as a page
  // stayed open — 132 requests on one idle project page, from two controls that each ran
  // their own. One chip is half of that and still all of the bug.
  const callsOverTime = async (publishState: PublishState): Promise<number> => {
    getDeployment.mockResolvedValue(view(publishState))
    vi.useFakeTimers()
    try {
      // Fake timers BEFORE render: an interval created under real timers is not moved by
      // advancing a fake clock afterwards.
      const { result } = renderHook(() => usePublishState('p1'))
      // INSIDE `act`, AND THAT IS WHAT MAKES THIS DETERMINISTIC.
      //
      // The hook reads once on mount and only THEN decides whether to poll: the interval is
      // armed by an effect that depends on the state the mount read sets. Advancing the clock
      // outside `act` lets the mock's promise resolve without React having applied that state,
      // so whether the interval existed during the measurement window came down to how busy the
      // machine was — the mount call landed inside the window instead and was counted as a poll.
      // It passed on a quiet run and failed under a full suite, in both directions.
      //
      // Verified rather than assumed: outside `act` the read lands (one call) while the hook's
      // own state is still `undefined`; one `act`-wrapped tick later the state is there and the
      // next thirty seconds produce exactly the six polls the five-second interval owes.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      // LIVENESS: the baseline is only meaningful if the mount read actually landed. Without
      // this, a hook that never read at all would give `0 - 0 == 0` and satisfy three of these
      // four assertions for entirely the wrong reason.
      expect(getDeployment.mock.calls.length).toBeGreaterThan(0)
      expect(result.current.deployment?.publishState).toBe(publishState)
      const afterMount = getDeployment.mock.calls.length
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000)
      })
      return getDeployment.mock.calls.length - afterMount
    } finally {
      vi.useRealTimers()
    }
  }

  it('keeps asking while a publish is starting up', async () => {
    // Mutation receipt: widen or inverse the `inFlight` condition and this goes red.
    expect(await callsOverTime('starting_up')).toBeGreaterThan(0)
  })

  it('stops once the app is simply live', async () => {
    // Mutation receipt: delete `if (!inFlight) return undefined` and this goes red. Before
    // this test existed, that deletion left the whole portal suite green.
    expect(await callsOverTime('live_current')).toBe(0)
  })

  it('stops for every other settled state too, not just the live one', async () => {
    for (const state of ['draft', 'in_review', 'switched_off', 'did_not_start'] as const) {
      expect(await callsOverTime(state)).toBe(0)
      cleanup()
    }
  })
})

describe('a response for a project the citizen has left cannot paint over the current one', () => {
  it('drops a stale read when the project id changes under the same mount', async () => {
    // THE HAZARD: React Router reuses component instances across a projectId change, so
    // without the per-mount generation token a slow response for project A lands after
    // project B's and silently shows B's citizen A's publish state.
    //
    // Mutation receipt: delete either `if (generation.current !== mine) return` in
    // `refresh()` and this goes red. Before this test existed, deleting BOTH left the whole
    // portal suite green.
    let releaseA: (v: DeploymentView) => void = () => {}
    const slowA = new Promise<DeploymentView>((res) => {
      releaseA = res
    })
    getDeployment.mockImplementationOnce(async () => slowA)
    getDeployment.mockResolvedValue(view('draft'))

    const { result, rerender } = renderHook(({ id }) => usePublishState(id), {
      initialProps: { id: 'p1' },
    })

    // Navigate away before p1's read comes back.
    rerender({ id: 'p2' })
    await waitFor(() => expect(result.current.deployment?.publishState).toBe('draft'))

    // p1's answer arrives late, carrying a completely different state.
    await act(async () => {
      releaseA(view('switched_off'))
      await slowA
    })

    expect(result.current.deployment?.publishState).toBe('draft')
  })
})

describe('a press, and what came back', () => {
  it('hands the caller which of the two successes happened', async () => {
    // The surface cannot predict this and must not try: the decision is taken inside the
    // request. So the hook resolves with the answer rather than swallowing it.
    startDeploy.mockResolvedValueOnce(STARTED)
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())

    let outcome
    await act(async () => {
      outcome = await result.current.onConfirm(SHA, ANSWERS)
    })
    expect(outcome).toEqual(STARTED)

    startDeploy.mockResolvedValueOnce(ROUTED)
    await act(async () => {
      outcome = await result.current.onConfirm(SHA, ANSWERS)
    })
    expect(outcome).toEqual(ROUTED)
  })

  it('tells the review queue when a send was routed to an administrator, and only then', async () => {
    const heard = vi.fn()
    window.addEventListener(REVIEW_QUEUE_CHANGED, heard)
    try {
      const { result } = renderHook(() => usePublishState('p1'))
      await waitFor(() => expect(result.current.deployment).not.toBeNull())

      startDeploy.mockResolvedValueOnce(STARTED)
      await act(async () => { await result.current.onConfirm(SHA, ANSWERS) })
      expect(heard).not.toHaveBeenCalled()

      startDeploy.mockResolvedValueOnce(ROUTED)
      await act(async () => { await result.current.onConfirm(SHA, ANSWERS) })
      expect(heard).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener(REVIEW_QUEUE_CHANGED, heard)
    }
  })

  it('sends the commit the dialog reviewed, with its answers and its note', async () => {
    startDeploy.mockResolvedValueOnce(STARTED)
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())

    await act(async () => {
      await result.current.onConfirm(SHA, ANSWERS)
    })

    expect(startDeploy).toHaveBeenCalledWith('p1', {
      commitSha: SHA,
      answers: { ai_usage: false, public_data: true },
      note: 'Staff names only.',
    })
  })

  it('re-reads after a press, so the chip moves without waiting for a poll', async () => {
    startDeploy.mockResolvedValueOnce(STARTED)
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())
    getDeployment.mockClear()

    await act(async () => {
      await result.current.onConfirm(SHA, ANSWERS)
    })

    expect(getDeployment).toHaveBeenCalled()
  })

  it('re-reads before it rethrows a refusal', async () => {
    // A 409 here is usually the server telling this surface something it did not know yet —
    // most often a save that landed after the dialog opened. Rethrowing alone left the surface
    // showing state the server had already contradicted.
    startDeploy.mockRejectedValueOnce(
      new ApiError('Your app was saved again.', 409, 'snapshot_moved'),
    )
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())
    getDeployment.mockClear()

    await act(async () => {
      await expect(result.current.onConfirm(SHA, ANSWERS)).rejects.toThrow(/saved again/)
    })

    await waitFor(() => expect(getDeployment).toHaveBeenCalled())
  })
})

describe('the one button saves first, then hands over the dialog', () => {
  it('★ saves a dirty workspace, re-reads, and only then asks for the dialog', async () => {
    // The dialog must be about the version that will ship, so the save lands before the
    // review starts. Nothing is sent: sending is the dialog's.
    const order: string[] = []
    fetchSaveState.mockImplementation(async () => {
      order.push('save-state')
      return saveState(true)
    })
    saveProject.mockImplementation(async () => {
      order.push('save')
      return { appId: 'app-1', headSha: SHA }
    })
    getDeployment.mockResolvedValue(view('draft'))
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())
    getDeployment.mockImplementation(async () => {
      order.push('re-read')
      return view('draft')
    })

    let next: unknown
    await act(async () => {
      next = await result.current.publish()
    })

    expect(next).toBe('review')
    expect(order).toEqual(['save-state', 'save', 're-read'])
    expect(startDeploy).not.toHaveBeenCalled()
    expect(result.current.publishError).toBeNull()
  })

  it('does not save a clean workspace, or one nobody could check', async () => {
    getDeployment.mockResolvedValue(view('draft'))
    for (const dirty of [false, null]) {
      fetchSaveState.mockResolvedValueOnce(saveState(dirty))
      const { result } = renderHook(() => usePublishState('p1'))
      await waitFor(() => expect(result.current.deployment).not.toBeNull())

      let next: unknown
      await act(async () => {
        next = await result.current.publish()
      })

      expect(next, String(dirty)).toBe('review')
      expect(fetchSaveState, String(dirty)).toHaveBeenCalledWith('p1')
      cleanup()
    }
    expect(saveProject).not.toHaveBeenCalled()
  })

  it('★ opens no dialog when the save fails, and says why', async () => {
    fetchSaveState.mockResolvedValueOnce(saveState(true))
    saveProject.mockRejectedValueOnce(
      new ApiError('Your workspace is not running, so there was nothing to save.', 409),
    )
    getDeployment.mockResolvedValue(view('draft'))
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())

    let next: unknown = 'unset'
    await act(async () => {
      next = await result.current.publish()
    })

    expect(next).toBeNull()
    expect(result.current.publishError).toBe(
      'Your workspace is not running, so there was nothing to save.',
    )
    expect(startDeploy).not.toHaveBeenCalled()
    expect(result.current.publishPhase).toBeNull()
  })

  it('★ posts the approved commit straight away when the server hands one back', async () => {
    // No save, no review, no dialog: the approval already decided this version.
    const RETRY = 'f9e8d7c6b5a4f9e8d7c6b5a4f9e8d7c6b5a4f9e8'
    getDeployment.mockResolvedValue(view('did_not_start', { approvedRetryCommit: RETRY }))
    startDeploy.mockResolvedValueOnce(STARTED)
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())

    let next: unknown
    await act(async () => {
      next = await result.current.publish()
    })

    expect(next).toEqual(STARTED)
    expect(startDeploy).toHaveBeenCalledWith('p1', { commitSha: RETRY })
    expect(fetchSaveState).not.toHaveBeenCalled()
    expect(saveProject).not.toHaveBeenCalled()
  })

  it('names what it is waiting on while it works, and lets go afterwards', async () => {
    let release: () => void = () => {}
    fetchSaveState.mockResolvedValueOnce(saveState(true))
    saveProject.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = () => resolve({ appId: 'app-1', headSha: SHA })
        }),
    )
    getDeployment.mockResolvedValue(view('draft'))
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())

    let pending: Promise<unknown> = Promise.resolve()
    act(() => {
      pending = result.current.publish()
    })
    await waitFor(() => expect(result.current.publishPhase).toBe('saving'))

    // A second press while the first is in flight does nothing.
    let second: unknown = 'unset'
    await act(async () => {
      second = await result.current.publish()
    })
    expect(second).toBeNull()
    expect(saveProject).toHaveBeenCalledTimes(1)

    await act(async () => {
      release()
      await pending
    })
    expect(result.current.publishPhase).toBeNull()
  })
})

describe('a failed read is reported, never swallowed', () => {
  it('surfaces the server sentence rather than blanking the surface', async () => {
    getDeployment.mockRejectedValue(new ApiError('Publishing is not switched on.', 503))
    const { result } = renderHook(() => usePublishState('p1'))

    // THE RETIRED 503 ARM: this used to null the deployment and report nothing at all, so
    // the whole publish affordance vanished. It is now the only publishing surface there
    // is, and a surface that renders nothing is indistinguishable from a broken page.
    await waitFor(() => expect(result.current.loadError).toBe('Publishing is not switched on.'))
    expect(result.current.deployment).toBeNull()
  })

  it('★ keeps what it already had when a LATER read fails, rather than blanking the surface', async () => {
    /* THE RULE NOBODY WROTE DOWN, and the first read is deliberately exempt from it. A surface
       renders the error branch instead of the status — pill, every provenance row and the action
       replaced by one line — so a 500 on the read that FOLLOWS a save would blank the whole panel
       on a screen that has just said "Saved". A stale row is worse than a fresh one and far
       better than no panel at all.

       MUTATION RECEIPT: delete `if (everRead.current) return` from the hook's catch — restoring
       the blanking branch — and this goes red on `deployment`, which becomes null. */
    getDeployment.mockResolvedValue(view('draft'))
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment?.publishState).toBe('draft'))

    getDeployment.mockRejectedValue(new ApiError('Could not read it.', 500))
    await act(async () => {
      await result.current.refresh()
    })

    expect(result.current.deployment?.publishState).toBe('draft')
    // …and it does not put a failure sentence over a panel that is still showing real rows.
    expect(result.current.loadError).toBeNull()
  })

  it('clears the error once a later read succeeds', async () => {
    getDeployment.mockRejectedValueOnce(new ApiError('Could not read it.', 500))
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.loadError).not.toBeNull())

    getDeployment.mockResolvedValue(view('draft'))
    await act(async () => {
      await result.current.refresh()
    })

    expect(result.current.loadError).toBeNull()
    expect(result.current.deployment?.publishState).toBe('draft')
  })
})

describe('taking a submission back out of the queue', () => {
  it('withdraws the app the read named, then re-reads', async () => {
    // The app id comes off the status response, not a prop — the builder's mount never had
    // one, and taking it from the same read that says the app is pending is what keeps the
    // withdrawal aimed at the app the citizen is looking at.
    const { withdrawSubmission } = await import('../../utils/approvalApi')
    getDeployment.mockResolvedValue(view('in_review', { appId: 'app-42' }))
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())
    getDeployment.mockClear()

    await act(async () => {
      await result.current.withdraw()
    })

    expect(vi.mocked(withdrawSubmission)).toHaveBeenCalledWith('app-42')
    expect(getDeployment).toHaveBeenCalled()
    expect(result.current.withdrawError).toBeNull()
  })

  it('tells the review queue it lost an entry', async () => {
    const heard = vi.fn()
    window.addEventListener(REVIEW_QUEUE_CHANGED, heard)
    try {
      getDeployment.mockResolvedValue(view('in_review', { appId: 'app-42' }))
      const { result } = renderHook(() => usePublishState('p1'))
      await waitFor(() => expect(result.current.deployment).not.toBeNull())

      await act(async () => { await result.current.withdraw() })

      expect(result.current.withdrawError).toBeNull()
      expect(heard).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener(REVIEW_QUEUE_CHANGED, heard)
    }
  })

  it('renders a refused withdrawal in the server own words', async () => {
    const { withdrawSubmission } = await import('../../utils/approvalApi')
    vi.mocked(withdrawSubmission).mockRejectedValueOnce(
      new ApiError('An administrator has already decided this one.', 409),
    )
    getDeployment.mockResolvedValue(view('in_review', { appId: 'app-42' }))
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())

    await act(async () => {
      await result.current.withdraw()
    })

    expect(result.current.withdrawError).toBe('An administrator has already decided this one.')
    expect(result.current.withdrawing).toBe(false)
  })
})

describe('the hook hands out no predicate over the deployment fields', () => {
  it('returns the publish state and nothing derived from it', async () => {
    // Its own verification: `running`, `waitingForReview` and `routed` were three ways of
    // saying what `publishState` now says once, and every one of them was a place two
    // surfaces reading one response could still disagree.
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())

    const keys = Object.keys(result.current)
    expect(keys).not.toContain('running')
    expect(keys).not.toContain('waitingForReview')
    expect(keys).not.toContain('routed')
    expect(keys.filter((k) => /^(is|has)[A-Z]/.test(k))).toEqual([])
  })
})
