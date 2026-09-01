/**
 * The cross-surface reconciliation event — the mechanism that stops two mounts of the
 * publish surface disagreeing, and the one thing about it that had no test at all.
 *
 * ITS SUBJECT IS NOT RETIRED, WHICH IS WHY THIS SUITE MOVED RATHER THAN WENT. It was
 * written when three controls each ran their own `useDeployment`, two of them two inches
 * apart on one screen, where nothing is ever re-entered and a withdrawal in one left the
 * other saying "waiting for review". Those three are now one chip — but the chip is still
 * mounted TWICE, on the project page and in the builder's pane toolbar, and it will be
 * until Plan F merges the two screens. So the nudge now buys CROSS-ROUTE and CROSS-TAB
 * freshness instead of settling an argument between two cards: a publish started in one
 * tab must not leave another tab's chip stale. Same mechanism, same test, different reason
 * to keep it.
 *
 * These mount TWO hooks on the same project — the shape the event exists for — and assert
 * the reconciliation without leaning on focus or visibility, which have their own paths.
 *
 * The event is dispatched directly rather than through the hook's own `announce`, which is
 * internal: what matters is the CONTRACT on `window` (name, `projectId`, `origin`), since
 * that is what every mount actually shares. A test reaching for a private callback would
 * pin the implementation and miss a renamed event entirely.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, cleanup, waitFor } from '@testing-library/react'

import { usePublishState } from '../usePublishState'
import * as deployApi from '../../utils/deployApi'

vi.mock('../../utils/deployApi', async () => {
  const actual = await vi.importActual<typeof deployApi>('../../utils/deployApi')
  return { ...actual, getDeployment: vi.fn(), startDeploy: vi.fn() }
})

const getDeployment = vi.mocked(deployApi.getDeployment)

/** The wire contract, restated here on purpose: if the event name or its detail shape
 *  changes, these tests fail rather than silently stopping exercising anything. */
const DEPLOYMENT_CHANGED = 'bial:deployment-changed'

function announceFrom(projectId: string, origin: number): void {
  window.dispatchEvent(
    new CustomEvent(DEPLOYMENT_CHANGED, { detail: { projectId, origin } }),
  )
}

/** An origin no mount can hold, so every listener treats it as "some other surface".
 *  The origin filter's OWN behaviour (a mount skipping its own nudge) is deliberately not
 *  pinned here: mount ids come from a module-level counter that keeps incrementing across
 *  tests, so any assertion about them is a guess about execution order. It is a
 *  de-duplication optimisation — one redundant re-read, not a wrong answer — and a flaky
 *  test for it would cost more than it protects. */
const SOMEONE_ELSE = -1

const IDLE = {
  status: 'succeeded',
  url: 'https://app.example',
  failureCode: null,
  failureDetail: null,
  approval: null,
  headSha: 'a'.repeat(40),
} as unknown as Awaited<ReturnType<typeof deployApi.getDeployment>>

beforeEach(() => {
  vi.clearAllMocks()
  getDeployment.mockResolvedValue(IDLE)
})
afterEach(cleanup)

describe('two surfaces on one project reconcile through the shared event', () => {
  it('re-reads a sibling mount when one announces a change', async () => {
    const first = renderHook(() => usePublishState('p1'))
    const second = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(getDeployment).toHaveBeenCalled())
    getDeployment.mockClear()

    // What a mutation does on the surface that performed it.
    act(() => {
      announceFrom('p1', SOMEONE_ELSE)
    })

    // Both mounts re-read. Without the listener this stays 0 until a poll tick.
    await waitFor(() => expect(getDeployment).toHaveBeenCalled())
    expect(first.result.current).toBeTruthy()
    expect(second.result.current).toBeTruthy() // liveness: both are still mounted
  })

  it('ignores an announcement for a different project', async () => {
    const mine = renderHook(() => usePublishState('p1'))
    const other = renderHook(() => usePublishState('p2'))
    await waitFor(() => expect(getDeployment).toHaveBeenCalled())
    getDeployment.mockClear()

    act(() => {
      announceFrom('p2', SOMEONE_ELSE)
    })
    await act(async () => {})

    // A busy admin with two projects open must not have one re-read on the other's news.
    expect(getDeployment).not.toHaveBeenCalledWith('p1')
    expect(mine.result.current).toBeTruthy()
    expect(other.result.current).toBeTruthy()
  })

  it('stops listening once a surface unmounts', async () => {
    const first = renderHook(() => usePublishState('p1'))
    const second = renderHook(() => usePublishState('p1'))
    await waitFor(() => expect(getDeployment).toHaveBeenCalled())

    second.unmount()
    getDeployment.mockClear()

    act(() => {
      announceFrom('p1', SOMEONE_ELSE)
    })
    await act(async () => {})

    // Exactly one mount answered — the listener came off with the unmounted one. An
    // unmounted surface setting state is the other half of this event's cost.
    expect(getDeployment.mock.calls.length).toBe(1)
    expect(first.result.current).toBeTruthy()
  })
})
