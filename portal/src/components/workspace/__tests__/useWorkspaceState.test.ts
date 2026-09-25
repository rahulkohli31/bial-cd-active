/**
 * THE READ BEHIND THE WORKSPACE STATE — the half that talks to the server.
 *
 * Two scenarios depend on a timer EXISTING, not merely on the map being right: a `starting`
 * read has to reach `running` with no user gesture, and a stay that lapses at thirty minutes
 * has to be noticed rather than left on screen as a lie. So the cadence is pinned directly,
 * with fake timers.
 *
 * The other half is COST. `fetchPreviewState` is cheap and safe on a timer; `fetchSaveState`
 * runs two `git` execs inside the container, and asking a stopped project is a start the
 * screen caused. The gating is a requirement, not an optimisation, so it is pinned too.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { PreviewState, SaveState } from '../../../utils/buildSessionApi'
import { ApiError } from '../../../utils/apiError'

const api = vi.hoisted(() => ({
  fetchPreviewState: vi.fn(),
  fetchSaveState: vi.fn(),
  fetchCompileState: vi.fn(),
  checkWorkspace: vi.fn(),
  renewPresence: vi.fn(),
}))

vi.mock('../../../utils/buildSessionApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../utils/buildSessionApi')>()
  return { ...actual, ...api }
})

const { useWorkspaceState } = await import('../useWorkspaceState')
const {
  BACKGROUND_CADENCE,
  HIDDEN_PROBE_MS,
  PREVIEW_PROBE_MS,
  STARTING_PROBE_LIMIT,
  STARTING_PROBE_MS,
  nextProbeCadence,
  spendProbeCadence,
} = await import('../workspaceState')

function reading(over: Partial<PreviewState> = {}): PreviewState {
  return {
    state: 'asleep',
    alive: false,
    previewUrl: null,
    restorable: null,
    startingSince: null,
    startFailure: null,
    ...over,
  }
}

const SAVE: SaveState = { appId: 'app-1', dirty: false, containerHead: 'abc1234', savedHead: 'abc1234' }

/**
 * Put the document out of sight, or bring it back, and fire the event the browser would.
 *
 * `visibilityState` is a read-only getter, so it is redefined rather than assigned — jsdom has no
 * real tab to hide. THIS IS WHY THE HIDDEN-TAB BEHAVIOUR IS ALSO PROVED IN A REAL BROWSER: jsdom
 * will happily keep firing a timer that Chrome throttles and Edge freezes, so everything below
 * proves the CODE asks correctly, and nothing below proves the browser lets it.
 */
function hide(hidden: boolean): void {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => (hidden ? 'hidden' : 'visible'),
  })
  document.dispatchEvent(new Event('visibilitychange'))
}

/** The hook, mounted against a project, with the defaults every scenario shares. */
const mount = (projectId: string | null = 'proj-1', projectHasSavedBuild: boolean | null = null) =>
  renderHook(() => useWorkspaceState({ projectId, projectHasSavedBuild }))

/** The same, but the project arrives as a prop, so a scenario can move the SAME hook to another
 *  one — which is what the screen does, since nothing keys the surface on the project id. */
const mountMovable = (projectId: string) =>
  renderHook(({ id }: { id: string }) => useWorkspaceState({ projectId: id, projectHasSavedBuild: null }), {
    initialProps: { id: projectId },
  })

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  for (const fn of Object.values(api)) fn.mockReset()
  api.fetchPreviewState.mockResolvedValue(reading())
  api.fetchSaveState.mockResolvedValue(SAVE)
  api.renewPresence.mockResolvedValue('renewed')
  hide(false)
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
    // A stay lapses under a reader even though the poll renews it: the renewal cannot push past
    // the absolute age ceiling, and one that fails reports nothing. The pane must return to
    // "Your app is saved." with the start offered — one press to recover — not a dead frame.
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

/**
 * THE PANE LEAVES "GETTING YOUR APP READY." WHEN THE APP IS READY.
 *
 * The measurement: the server flipped to `alive` at t=2.7s and the pane left
 * `starting` at t=45.5s, with nothing animating for the 42.8 seconds in between — so there was no
 * cue that it was not simply hung. `starting` is the only reading whose successor arrives with no
 * gesture from anybody, which is exactly why a cadence tuned for "has anything happened while
 * nobody was looking" is the wrong instrument for it.
 *
 * WHAT THESE SCENARIOS PIN, beyond "it is faster now": the acceleration is bounded at both ends.
 * It is gated STRICTLY on `starting` and reverts on anything else (or the whole product ends up on
 * a three-second poll), and it gives up after a fixed number of reads (or a start that hangs polls
 * for the life of the tab). And it never reclassifies the wait it gives up on — the pane still
 * says a start is happening, because that is still what is true. Reading an elapsed budget as a
 * statement about the container is the mistake that once marked a live sandbox dead and rolled a
 * workspace back to its last Save.
 */
describe('the accelerated cadence while a start is in flight', () => {
  it('leaves `starting` within ONE accelerated read, not one background cadence', async () => {
    api.fetchPreviewState.mockResolvedValueOnce(reading({ state: 'starting' }))
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'alive', alive: true }))

    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('starting'))

    // Three seconds, not forty-five. At the old cadence nothing has fired by here at all, so the
    // pane is still telling somebody their running app is being prepared.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS + 1)
    })

    expect(result.current.state.name).toBe('running')
  })

  it('reverts to the background cadence the moment the reading is not `starting`', async () => {
    api.fetchPreviewState.mockResolvedValueOnce(reading({ state: 'starting' }))
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'alive', alive: true }))

    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('starting'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS + 1)
    })
    expect(result.current.state.name).toBe('running')
    const settled = api.fetchPreviewState.mock.calls.length

    // TEN accelerated intervals over a running workspace buy nothing. The mutation this pins is a
    // window that stays open on `alive`, which puts every idle project screen in the product on a
    // three-second poll — the request volume this fix is explicitly not allowed to change.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS * 10)
    })
    expect(api.fetchPreviewState.mock.calls.length).toBe(settled)

    // ABSENCE, PAIRED WITH LIVENESS: the timer above is quiet because it is slow, not because it
    // is dead — one background cadence later it asks again.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS)
    })
    expect(api.fetchPreviewState.mock.calls.length).toBe(settled + 1)
  })

  it('gives up the accelerated window after the bound — WITHOUT reclassifying the wait', async () => {
    // A start that never readies. The server holds `starting` for up to five minutes
    // (`STARTING_MARKER_TTL_SECONDS`), so this is a real answer and not a fault.
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'starting' }))

    const { result } = mount()
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalledTimes(1))

    // The whole window, read by read: the mount read plus exactly the bound.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS * STARTING_PROBE_LIMIT)
    })
    const spent = api.fetchPreviewState.mock.calls.length
    expect(spent).toBe(1 + STARTING_PROBE_LIMIT)

    // Past it, ten more accelerated intervals buy nothing — the fast timer is gone…
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS * 10)
    })
    expect(api.fetchPreviewState.mock.calls.length).toBe(spent)

    // …and the wait is NOT RECLASSIFIED. The poll's budget elapsing is a fact about our asking,
    // not about the container: still `starting`, still busy, and no verb that assumes the
    // workspace is dead. What the citizen may be offered by now is one that assumes nothing —
    // asking again, which attaches to whatever the start left standing — and the clock that
    // decides when to offer it is the wait's own, not this poll's.
    expect(result.current.state.name).toBe('starting')
    expect(result.current.state.busy).toBe(true)
    expect(result.current.state.action?.kind ?? null).not.toBe('start')

    // And the background poll is still there to correct the pane if the app lands two minutes late.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS)
    })
    expect(api.fetchPreviewState.mock.calls.length).toBeGreaterThan(spent)
  })

  it('changes cadence WITHOUT re-running the effect, so the pane cannot blink', async () => {
    // THE PATH IS THE SUBJECT, not the destination. The tempting implementation — put
    // `preview.state` in the effect's dependency list — arrives at `running` too, and gets there
    // by tearing the poll down and building it again mid-start. That is forbidden here for a
    // reason this file already states ("a start outcome must not restart the poll"), it costs an
    // extra request on every transition, and on the chat surface, whose equivalent effect DOES
    // blank its reading on every re-run, the same mutation flickers the pane through "we could not
    // check" and unframes an app that is running (`ConversationSurface-cadence.test.jsx` holds
    // that half, where the damage is visible).
    //
    // So both halves are asserted: the READ COUNT, which is what a re-armed effect gives itself
    // away by, and the sequence of states, which is what a reader would have seen.
    //
    // ★ AND THE ANSWERS MUST ARRIVE LATE ENOUGH TO BE SEEN AROUND, which is what gives this test
    // its teeth. With `mockResolvedValue` — and, verified here, even with a `setTimeout(…, 0)` — the
    // dep-driven mutant is INERT: the answer lands in the same flush as the effect's own
    // `setPreview(null)`, React coalesces the two into one commit, and the blank verdict is never
    // rendered at all. A real network takes tens of milliseconds, so the `null` commit lands FIRST
    // and the flicker is on screen. Answering half a second later is the difference between this
    // guard and a green test that proves nothing. (Same trap, same fix, as
    // `ConversationSurface-previewaddress.test.tsx`'s loop guard, one flush deeper.)
    const ANSWERS_IN = 500
    const seen: string[] = []
    const later = (value: PreviewState) =>
      new Promise<PreviewState>((resolve) => {
        setTimeout(() => resolve(value), ANSWERS_IN)
      })
    api.fetchPreviewState.mockImplementationOnce(() => later(reading({ state: 'starting' })))
    api.fetchPreviewState.mockImplementation(() => later(reading({ state: 'alive', alive: true })))

    renderHook(() => {
      const held = useWorkspaceState({ projectId: 'proj-1', projectHasSavedBuild: null })
      seen.push(held.state.name)
      return held
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ANSWERS_IN + 1)
    })
    expect(seen).toContain('starting')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS + ANSWERS_IN * 2)
    })

    // Before the first read lands there is genuinely nothing to say, and "could not read" is the
    // honest answer to a question nobody has answered yet — so the sequence is read from the first
    // real verdict onward.
    // TWO READS AND NO MORE: the mount's, and the accelerated tick that found the app serving. A
    // third is an effect that re-armed itself, which is the mutation this pins.
    expect(api.fetchPreviewState).toHaveBeenCalledTimes(2)
    const published = seen.slice(seen.indexOf('starting'))
    expect(published).not.toContain('could-not-read')
    expect(published.at(-1)).toBe('running')
  })

  it('the own-press short-circuit still asks exactly once more, and leaves ONE timer behind', async () => {
    // `ProjectWorkspace`'s `onStartOutcome(null)` calls `refresh()` so a start that reached the app
    // lands on the press rather than on a tick. It bumps the epoch, so the effect tears down and
    // re-runs — and a cadence change that failed to clear the interval it replaced would double
    // every read from here on, invisibly, for the life of the tab.
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'starting' }))

    const { result } = mount()
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalledTimes(1))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS + 1)
    })
    expect(api.fetchPreviewState).toHaveBeenCalledTimes(2)

    await act(async () => {
      result.current.refresh()
    })
    await settle()
    expect(api.fetchPreviewState).toHaveBeenCalledTimes(3)

    // ONE accelerated interval, ONE read.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS + 1)
    })
    expect(api.fetchPreviewState).toHaveBeenCalledTimes(4)
  })

  it('spends no container call to go faster — the acceleration buys cheap reads only', async () => {
    // `fetchSaveState` is two `git` executions INSIDE the container, and it fires on the tick that
    // first sees `alive` — which, in an accelerated window, is a container that came up seconds
    // ago and is still unpacking a snapshot and booting a dev server.
    api.fetchPreviewState.mockResolvedValueOnce(reading({ state: 'starting' }))
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'alive', alive: true }))

    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('starting'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS + 1)
    })

    // ABSENCE: the accelerated read found a live app and asked it nothing…
    expect(result.current.state.name).toBe('running')
    expect(api.fetchSaveState).not.toHaveBeenCalled()

    // …and LIVENESS: the next background tick asks, which is no later than it would have asked
    // without the acceleration at all.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS)
    })
    expect(api.fetchSaveState).toHaveBeenCalledWith('proj-1')
  })
})

/**
 * The cadence decision on its own, for the two rules the hook scenarios above cannot reach without
 * an unreliable server to play against.
 */
describe('nextProbeCadence — what opens a window, what closes it, what spends it', () => {
  it('a `starting` opens the window', () => {
    expect(nextProbeCadence('starting', BACKGROUND_CADENCE)).toEqual({
      delayMs: STARTING_PROBE_MS,
      fastReads: 1,
    })
  })

  it.each(['alive', 'asleep'] as const)(
    'a decided "%s" closes the window and gives the next start a whole one',
    (state) => {
      expect(nextProbeCadence(state, { delayMs: STARTING_PROBE_MS, fastReads: 7 })).toEqual(
        BACKGROUND_CADENCE,
      )
    },
  )

  it('stops accelerating at the bound and never counts past it', () => {
    const exhausted = nextProbeCadence('starting', {
      delayMs: STARTING_PROBE_MS,
      fastReads: STARTING_PROBE_LIMIT,
    })
    expect(exhausted.delayMs).toBe(PREVIEW_PROBE_MS)
    expect(exhausted.fastReads).toBe(STARTING_PROBE_LIMIT)
  })
})

/**
 * ★ THE HALF OF THE BOUND THAT WAS NEVER SPENT — a read that came back with NOTHING.
 *
 * `nextProbeCadence` is only reachable from the success path, and `fetchPreviewState` throws on any
 * non-2xx and on a dropped connection. So the 120-second bound was a ceiling on SUCCESSFUL reads:
 * a workspace that reached `starting` and then met a 500, an expired session or a dead network was
 * asked every three seconds FOR THE LIFE OF THE TAB, on both surfaces, with the counter that exists
 * to stop a hung start never moving a step.
 *
 * THE RULE THESE PIN, and the asymmetry is the whole of it: a failed read SPENDS from the window
 * and DECIDES nothing. It cannot say whether the container is still coming up, so ending the window
 * on it — or reclassifying the reading — would be reading a failure to ask as an answer: the
 * death-certificate mistake that once rolled a live workspace back to its last Save.
 */
describe('spendProbeCadence — what a read that never answered costs the window', () => {
  it('never opens one: a broken server does not buy an acceleration nothing earned', () => {
    expect(spendProbeCadence(BACKGROUND_CADENCE)).toEqual(BACKGROUND_CADENCE)
  })

  it('spends from an open window WITHOUT closing it', () => {
    // The remaining fast reads are still owed to a start that may land the moment the endpoint
    // recovers. Giving up on the first error would put the pane back on a 45-second wait over a
    // sentence that still says a start is happening — the bug above, restored by one 500.
    const spent = spendProbeCadence({ delayMs: STARTING_PROBE_MS, fastReads: 1 })
    expect(spent).toEqual({ delayMs: STARTING_PROBE_MS, fastReads: 2 })
  })

  it('falls back at the bound and never counts past it', () => {
    const exhausted = spendProbeCadence({
      delayMs: STARTING_PROBE_MS,
      fastReads: STARTING_PROBE_LIMIT,
    })
    expect(exhausted.delayMs).toBe(PREVIEW_PROBE_MS)
    expect(exhausted.fastReads).toBe(STARTING_PROBE_LIMIT)
  })

  it('an unbroken run of failures spends the window in exactly the bound and then stops', () => {
    // TERMINATION, PROVED RATHER THAN ASSUMED — the property the whole finding is about. The loop
    // is capped well above the bound so a cadence that never gives up fails as a wrong NUMBER
    // rather than as a hung test nobody can read.
    let cadence = nextProbeCadence('starting', BACKGROUND_CADENCE)
    let readsMade = 1
    while (cadence.delayMs === STARTING_PROBE_MS && readsMade < STARTING_PROBE_LIMIT * 3) {
      cadence = spendProbeCadence(cadence)
      readsMade += 1
    }
    expect(cadence.delayMs).toBe(PREVIEW_PROBE_MS)
    expect(readsMade).toBe(STARTING_PROBE_LIMIT + 1)
  })
})

describe('what an unreadable answer may and may not do', () => {
  it('a 503 after a decided `asleep` leaves the decided value in place', async () => {
    // A blip must not pull a running app off screen, and it must not wipe a settled answer
    // somebody is already reading either. Mutation-check: clear `preview` in the read's `catch`
    // and this answers `could-not-read`.
    api.fetchPreviewState.mockResolvedValueOnce(reading({ state: 'asleep', restorable: true }))
    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('not-running'))

    api.fetchPreviewState.mockRejectedValue(new ApiError('Build coordination is temporarily unavailable.', 503))
    await act(async () => {
      result.current.refresh()
    })
    await settle()

    expect(api.fetchPreviewState).toHaveBeenCalledTimes(2)
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

  it('★ a start that goes dark still spends the window — an erroring endpoint is BOUNDED', async () => {
    // THE FINDING, AT THE HOOK. The mount read opens the accelerated window and every read after it
    // is a 500 — the shape of an expired session, a restarted API, or a gateway that fell over. The
    // bound existed for exactly this, and could not reach it: `nextProbeCadence` was the only thing
    // that could advance `fastReads`, and it lives on the success path, so this tab asked every
    // three seconds forever — twenty requests a minute, for as long as it stayed open.
    const MINE = 'proj-goes-dark'
    let reads = 0
    api.fetchPreviewState.mockImplementation(async (id: string) => {
      if (id !== MINE) return reading()
      reads += 1
      if (reads === 1) return reading({ state: 'starting' })
      throw new Error('500 from preview-state')
    })

    const { result } = mount(MINE)
    await waitFor(() => expect(reads).toBe(1))

    // THE READ COUNT OVER THE WINDOW, not just the state at the end of it — and the window is
    // deliberately FIVE accelerated intervals longer than the bound, so the NUMBER is what fails
    // when a failed read spends nothing: the mount read plus exactly the bound, every one of the
    // latter a failure, and then silence for the rest of the window.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS * (STARTING_PROBE_LIMIT + 5))
    })
    expect(reads).toBe(1 + STARTING_PROBE_LIMIT)
    const spent = reads

    // Past it, eight more accelerated intervals buy nothing — the fast timer is gone…
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS * 8)
    })
    expect(reads).toBe(spent)

    // …AND NOTHING WAS RECLASSIFIED ON THE WAY. Not `could-not-read`, not gone, not a verb that
    // assumes the workspace is dead: a string of failures is not evidence about a container, and
    // the last thing anybody actually told us is that a start is happening. The reading underneath
    // is untouched too. (A wait this long may by now offer to ask again — that is the wait's own
    // clock, not these failures, and it assumes nothing about the container either.)
    expect(result.current.state.name).toBe('starting')
    expect(result.current.state.busy).toBe(true)
    expect(result.current.state.action?.kind ?? null).not.toBe('start')
    expect(result.current.preview?.state).toBe('starting')

    // ABSENCE PAIRED WITH LIVENESS: quiet because it is slow, not because it died. The background
    // poll is still there to correct the pane the moment the endpoint answers again.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS)
    })
    expect(reads).toBeGreaterThan(spent)
  })

  it('★ failures alone never buy an accelerated window nothing earned', async () => {
    // Erroring from the very first read: nothing has ever said `starting`, so there is no window to
    // spend and no reason to go fast. The mutation this pins is a `catch` that OPENS one — which
    // would put every project screen behind a flaky endpoint on a three-second poll.
    const MINE = 'proj-never-answered'
    let reads = 0
    api.fetchPreviewState.mockImplementation(async (id: string) => {
      if (id !== MINE) return reading()
      reads += 1
      throw new Error('500 from preview-state')
    })

    const { result } = mount(MINE)
    await waitFor(() => expect(reads).toBe(1))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS * 10)
    })
    expect(reads).toBe(1)
    expect(result.current.state.name).toBe('could-not-read')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS)
    })
    expect(reads).toBeGreaterThan(1)
  })

  it('★ a failure after a settled answer does not resurrect the poll that correctly stopped', async () => {
    // The visibility backstop stays live after the timer stops, by design — it is the one read a
    // settled workspace can still make. Its failure must not re-arm the interval: a poll that gave
    // up on `asleep` plus a decided `restorable` has heard everything there is to hear, and a
    // rescheduling `catch` would have a broken endpoint start it up again.
    const MINE = 'proj-settled'
    let answering = true
    let reads = 0
    api.fetchPreviewState.mockImplementation(async (id: string) => {
      if (id !== MINE) return reading()
      reads += 1
      if (answering) return reading({ state: 'asleep', restorable: true })
      throw new Error('500 from preview-state')
    })

    const { result } = mount(MINE)
    await waitFor(() => expect(result.current.state.name).toBe('not-running'))

    answering = false
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await settle()
    const spent = reads
    expect(spent).toBeGreaterThan(1) // liveness: the backstop really did fire, and really did fail

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS * 3)
    })
    expect(reads).toBe(spent)
  })

  it('records "could not read" when it is the ONLY thing we know', async () => {
    api.fetchPreviewState.mockRejectedValue(new ApiError('Build coordination is temporarily unavailable.', 503))
    const { result } = mount()

    await waitFor(() => expect(result.current.settled).toBe(true))
    expect(result.current.state.name).toBe('could-not-read')
    expect(result.current.state.action?.kind).toBe('retry')
  })
})

describe('cost — the calls this hook refuses to make', () => {
  it('never asks a stopped workspace whether it has unsaved work', async () => {
    // Two `git` execs against a dead container is an attach the screen caused.
    for (const state of ['asleep', 'starting'] as const) {
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
  it('★ carries the reported ending WITHOUT letting it select a card, and clears it on request', async () => {
    // ★ THIS ASSERTED `timed-out` AS A STATE NAME, and that is the change. A start outcome used to
    // select three whole cards of its own — "your app is up but has not served a page yet", "your
    // app did not answer in time", "we could not start your app" — all three of them sentences
    // about a FETCH rather than about a workspace. The READING decides the card now and the ending
    // contributes at most a `note`.
    //
    // WHAT THE SLOT STILL HAS TO DO, and the reason this scenario survives rather than being
    // deleted: the hook must hold the ending and must let go of it.
    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('never-built'))

    // A refusal names a reason, and it rides in `note`, on the card the reading already chose.
    await act(async () => {
      result.current.reportStartOutcome({ kind: 'failed', reason: 'no image' })
    })
    expect(result.current.state.name).toBe('never-built')
    expect(result.current.state.note).toBe('no image')

    await act(async () => {
      result.current.reportStartOutcome(null)
    })
    expect(result.current.state.name).toBe('never-built')
    expect(result.current.state.note ?? null).toBeNull()
  })

  it('reporting an outcome does NOT restart the poll — it is a fact about a press', async () => {
    const { result } = mount()
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalledTimes(1))

    await act(async () => {
      result.current.reportStartOutcome({ kind: 'failed', reason: 'no image' })
    })
    await settle()

    expect(api.fetchPreviewState).toHaveBeenCalledTimes(1)
  })
})

describe('a wait that looks stuck asks whether the app has stopped', () => {
  /** The server's side of the check: finding the app stopped, it puts the container away, so every
   *  read after the check answers `asleep` with the work restorable. */
  function aServerThatPutsTheAppAway(before: PreviewState) {
    let putAway = false
    api.checkWorkspace.mockImplementation(async () => {
      putAway = true
      return false
    })
    api.fetchPreviewState.mockImplementation(async () =>
      putAway ? reading({ state: 'asleep', restorable: true }) : before,
    )
  }

  it('★ a stalled frame on a running app asks at once, then reads again for the answer', async () => {
    // The reading that prompted the question predates whatever the server did about it, so the
    // check is followed by one more read — which is what moves the pane off the slow card.
    aServerThatPutsTheAppAway(reading({ state: 'alive', alive: true }))
    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('running'))
    // LIVENESS BEFORE ABSENCE: the poll is reading, and no reading so far has asked.
    expect(api.checkWorkspace).not.toHaveBeenCalled()

    await act(async () => {
      result.current.reportFrameStall(true)
    })

    await waitFor(() => expect(result.current.state.name).toBe('not-running'))
    expect(api.checkWorkspace).toHaveBeenCalledTimes(1)
    expect(api.checkWorkspace).toHaveBeenCalledWith('proj-1')
    expect(result.current.state.action?.kind).toBe('start')
  })

  it('★ a start stuck past the accelerated window asks on the first background read, never inside it', async () => {
    aServerThatPutsTheAppAway(reading({ state: 'starting' }))
    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('starting'))

    // THE WHOLE WINDOW buys cheap reads only — the bargain `nextProbeCadence` makes with a start.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS * STARTING_PROBE_LIMIT)
    })
    expect(api.fetchPreviewState.mock.calls.length).toBe(1 + STARTING_PROBE_LIMIT)
    expect(api.checkWorkspace).not.toHaveBeenCalled()
    expect(result.current.state.name).toBe('starting')

    // Past it, the first background read asks, and the read after the answer is the saved app.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS + 1)
    })
    await waitFor(() => expect(result.current.state.name).toBe('not-running'))
    expect(api.checkWorkspace).toHaveBeenCalledTimes(1)
  })

  it('★ a stall does not outlive the app it was about — launched again, a running app is not asked', async () => {
    // A reading that takes the frame away unmounts the pane with no chance to take its stall back,
    // so that reading has to. Mutation check: drop that reset and the relaunched app is asked about
    // on every background read, for as long as the tab stays open.
    let putAway = false
    let launched = false
    api.checkWorkspace.mockImplementation(async () => {
      putAway = true
      return false
    })
    api.fetchPreviewState.mockImplementation(async () =>
      launched || !putAway
        ? reading({ state: 'alive', alive: true })
        : reading({ state: 'asleep', restorable: true }),
    )
    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('running'))
    await act(async () => {
      result.current.reportFrameStall(true)
    })
    await waitFor(() => expect(result.current.state.name).toBe('not-running'))

    // The citizen presses Launch; the start lands and the surface asks again at once.
    launched = true
    await act(async () => {
      result.current.refresh()
    })
    await waitFor(() => expect(result.current.state.name).toBe('running'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS * 2 + 1)
    })

    // LIVENESS: the poll is reading the relaunched app…
    expect(api.fetchPreviewState.mock.calls.length).toBeGreaterThan(4)
    // …and the stall from before the put-away bought it no question.
    expect(api.checkWorkspace).toHaveBeenCalledTimes(1)
  })

  it('a stall the pane has since taken back asks nothing more', async () => {
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'alive', alive: true }))
    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('running'))
    await act(async () => {
      result.current.reportFrameStall(true)
    })
    await waitFor(() => expect(api.checkWorkspace).toHaveBeenCalledTimes(1))

    // A late beacon won: the citizen is looking at their app, so the poll stops asking about it.
    await act(async () => {
      result.current.reportFrameStall(false)
    })
    const readsBefore = api.fetchPreviewState.mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS * 2 + 1)
    })

    // LIVENESS: the poll kept reading…
    expect(api.fetchPreviewState.mock.calls.length).toBeGreaterThan(readsBefore)
    // …and asked nothing more.
    expect(api.checkWorkspace).toHaveBeenCalledTimes(1)
  })
})


describe('presence renewal — what holds the container open', () => {
  it('renews on the ordinary tick, so a screen left open keeps its app', async () => {
    // The whole mechanism in one assertion: nobody is typing, nobody presses anything, and the
    // container stays because a screen that can frame it is still here.
    mount()
    await settle()

    expect(api.renewPresence).toHaveBeenCalledWith('proj-1', 'visible')
  })

  it('renews from a HIDDEN tab, and asks for the longer budget', async () => {
    // A citizen reading the docs for their own app in the next tab has not left. A poll that went
    // silent while hidden would have the platform collect their container out from under them.
    hide(true)
    mount()
    await settle()

    expect(api.renewPresence).toHaveBeenCalledWith('proj-1', 'hidden')
  })

  it('asks a hidden tab for nothing but the read and the renewal', async () => {
    // THE HALF THAT MATTERS. `fetchSaveState` costs two `git` executions inside the container and
    // `checkWorkspace` can restart the app's dev server — doing either with nobody looking is the
    // opposite of what renewing from a hidden tab is for.
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'alive', alive: true }))
    hide(true)
    mount()
    await settle()

    expect(api.renewPresence).toHaveBeenCalled()
    expect(api.fetchSaveState).not.toHaveBeenCalled()
    expect(api.checkWorkspace).not.toHaveBeenCalled()
  })

  it('renews the moment a hidden tab is woken, without waiting for a tick', async () => {
    // A throttled or frozen tab can miss several ticks before it comes back, so the lease may be
    // minutes old at exactly the moment somebody starts looking at the app again.
    hide(true)
    mount()
    await settle()
    api.renewPresence.mockClear()

    hide(false)
    await settle()

    expect(api.renewPresence).toHaveBeenCalledWith('proj-1', 'visible')
  })

  it('polls a hidden tab on the longer cadence, not the visible one', async () => {
    mount()
    await settle()
    hide(true)
    await settle()
    api.renewPresence.mockClear()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS + 1)
    })
    expect(api.renewPresence).not.toHaveBeenCalled()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(HIDDEN_PROBE_MS)
    })
    expect(api.renewPresence).toHaveBeenCalledWith('proj-1', 'hidden')
  })

  it('★ renews throughout a watched start — the window where nothing else holds the container', async () => {
    // THE GAP THIS CLOSES. Accelerated ticks used to renew nothing, on the grounds that a starting
    // container is held by the marker and the lock rather than by a stay. But the marker is written
    // ONCE with a five-minute TTL and the accelerated window is five minutes, so a citizen watching
    // a start sent zero renewals across exactly the window in which both of those lapse — and the
    // sweep runs every five minutes.
    //
    // Renewing here is a no-op when there is no record to renew: the server's write is a
    // compare-and-set on the registry's own `app_name`, and its deadline is a monotonic max, so it
    // can neither conjure a lease nor truncate the longer one a start already granted itself.
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'starting' }))
    mount()
    await settle()
    api.renewPresence.mockClear()

    // Three accelerated ticks, three renewals — not one at the start and silence after it.
    for (let tick = 0; tick < 3; tick += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS + 1)
      })
    }

    expect(api.fetchPreviewState).toHaveBeenCalled()
    expect(api.renewPresence).toHaveBeenCalledTimes(3)
    expect(api.renewPresence).toHaveBeenCalledWith('proj-1', 'visible')
  })

  it('renders nothing and assumes nothing when a renewal cannot be made', async () => {
    // 401, 403 and 503 are facts about the request, not about the container. A screen that painted
    // "your workspace is going away" on one would be over-claiming from an outage; a lease that
    // genuinely lapsed arrives through the preview read instead.
    api.fetchPreviewState.mockResolvedValue(reading({ state: 'alive', alive: true }))
    api.renewPresence.mockResolvedValue(null)

    const { result } = mount()
    await waitFor(() => expect(result.current.state.name).toBe('running'))

    expect(result.current.state.name).toBe('running')
  })

  it('sends nothing at all on unmount — leaving is silence, not a message', async () => {
    const { unmount } = mount()
    await settle()
    api.renewPresence.mockClear()

    unmount()
    await settle()

    expect(api.renewPresence).not.toHaveBeenCalled()
  })
})


describe('★ what a project hop drops', () => {
  it('drops a press that was in flight on the project being left', async () => {
    // Nothing keys this hook on the project, and `startApp`'s own clear is gated on the start
    // still being ours — correctly, or a late clear from the outgoing start would wipe the
    // incoming one's flag. So the hop itself has to drop it, or the next project is drawn
    // mid-start with nobody having touched it.
    //
    // Mutation check: remove `setStartInFlight(false)` from the project-change effect and the
    // last assertion goes red — the incoming project still reads as getting ready.
    const view = mountMovable('proj-1')
    await waitFor(() => expect(view.result.current.preview).not.toBeNull())

    act(() => view.result.current.reportStartPending(true))
    expect(view.result.current.state.name).toBe('starting')

    await act(async () => {
      view.rerender({ id: 'proj-2' })
    })

    expect(view.result.current.state.name).not.toBe('starting')
  })
})
