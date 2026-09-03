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
import * as deployApi from '../../utils/deployApi'
import type { DataClassificationAnswers, DeploymentView, PublishState } from '../../utils/deployApi'

vi.mock('../../utils/deployApi', async () => {
  const actual = await vi.importActual<typeof deployApi>('../../utils/deployApi')
  return { ...actual, getDeployment: vi.fn(), startDeploy: vi.fn() }
})
vi.mock('../../utils/approvalApi', () => ({ withdrawSubmission: vi.fn() }))

const getDeployment = vi.mocked(deployApi.getDeployment)
const startDeploy = vi.mocked(deployApi.startDeploy)

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
  approval: null,
  publishState,
  savedHead: null,
  savedAt: null,
  ...over,
})

const ANSWERS: DataClassificationAnswers = {
  credentialsSecrets: false,
  healthData: false,
  personalInformation: true,
  financialData: false,
  confidentialBusinessData: false,
  publicData: false,
  notes: 'Staff names only.',
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
      outcome = await result.current.onConfirm(ANSWERS)
    })
    expect(outcome).toEqual(STARTED)

    startDeploy.mockResolvedValueOnce(ROUTED)
    await act(async () => {
      outcome = await result.current.onConfirm(ANSWERS)
    })
    expect(outcome).toEqual(ROUTED)
  })

  it('re-reads after a press, so the chip moves without waiting for a poll', async () => {
    startDeploy.mockResolvedValueOnce(STARTED)
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())
    getDeployment.mockClear()

    await act(async () => {
      await result.current.onConfirm(ANSWERS)
    })

    expect(getDeployment).toHaveBeenCalled()
  })

  it('treats unsaved work as a QUESTION with a second answer, not as a failure', async () => {
    // The one refusal that is not a failure. It must not throw — a thrown 409 would render
    // in red beside the button as though the citizen had done something wrong.
    startDeploy.mockRejectedValueOnce(
      new ApiError('You have changes that are not saved yet.', 409, 'unsaved_changes'),
    )
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())

    let outcome: unknown = 'unset'
    await act(async () => {
      outcome = await result.current.onConfirm(ANSWERS)
    })

    expect(outcome).toBeNull()
    expect(result.current.unsaved).toBe('You have changes that are not saved yet.')
  })

  it('re-sends the SAME declaration on Save and publish, with saveFirst set', async () => {
    // The guarantee: the retry publishes what the citizen actually declared. Re-asking, or
    // sending a fresh/empty declaration, would put a different answer through the gate than
    // the one they read and agreed to.
    //
    // Mutation receipt: clear `pendingAnswers.current` before the retry, or pass `false`
    // for saveFirst, and this goes red.
    startDeploy.mockRejectedValueOnce(
      new ApiError('You have changes that are not saved yet.', 409, 'unsaved_changes'),
    )
    startDeploy.mockResolvedValueOnce(STARTED)
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())

    await act(async () => {
      await result.current.onConfirm(ANSWERS)
    })
    await act(async () => {
      await result.current.saveAndPublish()
    })

    expect(startDeploy).toHaveBeenCalledTimes(2)
    expect(startDeploy.mock.calls[0]?.[1]).toEqual({ answers: ANSWERS, saveFirst: false })
    expect(startDeploy.mock.calls[1]?.[1]).toEqual({ answers: ANSWERS, saveFirst: true })
    expect(result.current.unsaved).toBeNull()
  })

  it('re-reads before it rethrows any other refusal', async () => {
    // A 409 here is usually the server telling this surface something it did not know yet —
    // most often that a version is already in the queue, routed from another tab. Rethrowing
    // alone left the surface showing state the server had already contradicted.
    startDeploy.mockRejectedValueOnce(
      new ApiError('This version is already waiting for review.', 409, 'waiting_for_review'),
    )
    const { result } = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(result.current.deployment).not.toBeNull())
    getDeployment.mockClear()

    await act(async () => {
      await expect(result.current.onConfirm(ANSWERS)).rejects.toThrow(/already waiting/)
    })

    await waitFor(() => expect(getDeployment).toHaveBeenCalled())
    // Not a question — the questionnaire renders this one itself, beside its own button.
    expect(result.current.unsaved).toBeNull()
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
    // U5's own verification. `running`, `waitingForReview` and `routed` were three ways of
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
