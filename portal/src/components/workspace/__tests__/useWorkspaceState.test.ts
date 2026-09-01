/**
 * THE READ BEHIND THE WORKSPACE STATE (Plan F, U2) — the half that talks to the server.
 *
 * Two of this plan's own scenarios depend on a timer EXISTING, not merely on the map being right:
 * a `starting` read has to reach `running` with no user gesture, and a stay that lapses at thirty
 * minutes has to be noticed rather than left on screen as a lie. So the cadence is asserted here
 * directly, with fake timers, rather than left as an implementation detail.
 *
 * The other half of this file is about COST. `fetchPreviewState` is cheap by contract — one cache
 * read, no container call — and safe on a timer. `fetchSaveState` runs two `git` executions inside
 * the container, and asking a stopped project whether it has unsaved work is a start the screen
 * caused. The gating is a requirement (R3), not an optimisation, so it is pinned.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { PreviewState, SaveState } from '../../../utils/buildSessionApi'

const api = vi.hoisted(() => ({
  fetchPreviewState: vi.fn(),
  fetchSaveState: vi.fn(),
  fetchCompileState: vi.fn(),
  checkWorkspace: vi.fn(),
}))

vi.mock('../../../utils/buildSessionApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../utils/buildSessionApi')>()
  return { ...actual, ...api }
})

const { useWorkspaceState } = await import('../useWorkspaceState')
const { PREVIEW_PROBE_MS } = await import('../workspaceState')

function reading(over: Partial<PreviewState> = {}): PreviewState {
  return {
    state: 'asleep',
    alive: false,
    previewUrl: null,
    occupyingProjectName: null,
    occupyingProjectId: null,
    restorable: null,
    ...over,
  }
}

const SAVE: SaveState = { appId: 'app-1', dirty: false, containerHead: 'abc1234', savedHead: 'abc1234' }

/** The hook, mounted against a project, with the defaults every scenario shares. */
const mount = (projectId: string | null = 'proj-1', projectHasSavedBuild: boolean | null = null) =>
  renderHook(() => useWorkspaceState({ projectId, projectHasSavedBuild }))

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  for (const fn of Object.values(api)) fn.mockReset()
  api.fetchPreviewState.mockResolvedValue(reading())
  api.fetchSaveState.mockResolvedValue(SAVE)
})

afterEach(() => {
  vi.useRealTimers()
})

/** Let the in-flight read settle without leaning on a wall clock. */
const settle = async () => {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('the read runs where the old probe would not', () => {
  it('asks with NO framed URL — the no-frame case is what the pane exists to describe', async () => {
    // The conversation surface's probe returns early on `!framedPreviewUrl`, which is right for a
    // pane catching a framed app being reclaimed underneath it and exactly wrong here: a project
    // whose app is saved and not running has no address at all, and it is the state that carries
    // the product's one start control.
    mount()
    await settle()

    expect(api.fetchPreviewState).toHaveBeenCalledWith('proj-1')
  })

  it('asks nothing at all while the route has not resolved a project', async () => {
    mount(null)
    await settle()

    expect(api.fetchPreviewState).not.toHaveBeenCalled()
  })
})

describe('the cadence — the timer two features depend on', () => {
  it('reaches running from starting with NO user gesture', async () => {
    api.fetchPreviewState.mockResolvedValueOnce(reading({ state: 'starting' }))
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'alive', alive: true }))

    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('starting'))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS + 1)
    })

    expect(result.current.state.name).toBe('running')
  })

  it('notices the stay lapsing under a person who is still reading', async () => {
    // `RELAUNCH_PREVIEW_STAY_SECONDS` is granted at relaunch and extended only by a turn's own
    // deadline writers; start-then-read has no turn. The pane must return to "Your app is saved."
    // with the start offered — one press to recover — rather than showing a dead frame.
    api.fetchPreviewState.mockResolvedValueOnce(reading({ state: 'alive', alive: true }))
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'asleep', restorable: true }))

    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('running'))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS + 1)
    })

    expect(result.current.state.name).toBe('not-running')
    expect(result.current.state.action?.kind).toBe('start')
  })

  it('stops asking on a settled answer whose restore question was decided', async () => {
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'asleep', restorable: true }))
    mount()
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalledTimes(1))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS * 3)
    })

    expect(api.fetchPreviewState).toHaveBeenCalledTimes(1)
  })

  it('keeps asking while `restorable` is still null — a half answer is not terminal', async () => {
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'asleep', restorable: null }))
    mount()
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalledTimes(1))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS + 1)
    })

    expect(api.fetchPreviewState.mock.calls.length).toBeGreaterThan(1)
  })

  it('re-asks on a deliberate refresh, which no batching can erase', async () => {
    const { result } = mount()
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalledTimes(1))

    await act(async () => {
      result.current.refresh()
    })
    await settle()

    expect(api.fetchPreviewState).toHaveBeenCalledTimes(2)
  })
})

describe('what an unreadable answer may and may not do', () => {
  it('an `unknown` after a decided `asleep` leaves the decided value in place', async () => {
    // A blip must not pull a running app off screen, and it must not wipe a settled answer
    // somebody is already reading either.
    api.fetchPreviewState.mockResolvedValueOnce(reading({ state: 'asleep', restorable: true }))
    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('not-running'))

    api.fetchPreviewState.mockResolvedValue(reading({ state: 'unknown' }))
    await act(async () => {
      result.current.refresh()
    })
    await settle()

    expect(result.current.state.name).toBe('not-running')
  })

  it('a read that throws says nothing and leaves the timer running', async () => {
    api.fetchPreviewState.mockResolvedValueOnce(reading({ state: 'starting' }))
    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('starting'))

    api.fetchPreviewState.mockRejectedValue(new Error('network'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS + 1)
    })

    expect(result.current.state.name).toBe('starting')
  })

  it('records "could not read" when it is the ONLY thing we know', async () => {
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'unknown' }))
    const { result } = mount()

    await waitFor(() => expect(result.current.state.name).toBe('could-not-read'))
    expect(result.current.state.action?.kind).toBe('retry')
  })
})

describe('cost — the calls this hook refuses to make (R3)', () => {
  it('never asks a stopped workspace whether it has unsaved work', async () => {
    // Two `git` execs against a dead container is an attach the screen caused.
    for (const state of ['asleep', 'never_built', 'slot_taken', 'starting', 'unknown'] as const) {
      api.fetchSaveState.mockClear()
      api.fetchPreviewState.mockResolvedValue(reading({ state, restorable: true }))
      const { result, unmount } = mount()
      await waitFor(() => expect(result.current.preview?.state).toBe(state))
      await settle()
      expect(api.fetchSaveState, `save state asked while ${state}`).not.toHaveBeenCalled()
      unmount()
    }
  })

  it('asks for the save state only once the workspace is alive', async () => {
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'alive', alive: true }))
    const { result } = mount()

    await waitFor(() => expect(result.current.save).toEqual(SAVE))
    expect(api.fetchSaveState).toHaveBeenCalledWith('proj-1')
  })

  it('drops the save state the moment the workspace stops being alive', async () => {
    // Holding a reading from a container that has since stopped would arm the unsaved-work guard
    // against work that is no longer reachable.
    api.fetchPreviewState.mockResolvedValueOnce(reading({ state: 'alive', alive: true }))
    const { result } = mount()
    await waitFor(() => expect(result.current.save).toEqual(SAVE))

    api.fetchPreviewState.mockResolvedValue(reading({ state: 'asleep', restorable: true }))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS + 1)
    })

    expect(result.current.save).toBeNull()
  })

  it('treats an unreadable save state as no claim rather than as clean', async () => {
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'alive', alive: true }))
    api.fetchSaveState.mockRejectedValue(new Error('exec failed'))
    const { result } = mount()

    await waitFor(() => expect(result.current.state.name).toBe('running'))
    await settle()
    expect(result.current.save).toBeNull()
  })

  it('never calls the two container-exec reads that belong to a live turn', async () => {
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'alive', alive: true }))
    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('running'))
    await settle()

    expect(api.fetchCompileState).not.toHaveBeenCalled()
    expect(api.checkWorkspace).not.toHaveBeenCalled()
  })
})

describe('the start outcome slot', () => {
  it('renders the reported ending and clears it on request', async () => {
    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('never-built'))

    await act(async () => {
      result.current.reportStartOutcome({ kind: 'timed-out' })
    })
    expect(result.current.state.name).toBe('timed-out')

    await act(async () => {
      result.current.reportStartOutcome(null)
    })
    expect(result.current.state.name).toBe('never-built')
  })

  it('reporting an outcome does NOT restart the poll — it is a fact about a press', async () => {
    const { result } = mount()
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalledTimes(1))

    await act(async () => {
      result.current.reportStartOutcome({ kind: 'not-painted' })
    })
    await settle()

    expect(api.fetchPreviewState).toHaveBeenCalledTimes(1)
  })
})
