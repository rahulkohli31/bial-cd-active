/**
 * The three marks only the browser can make.
 *
 * These are guard tests, and every guard here exists because a counter that double-counts, or
 * that counts a numerator without its denominator, is not a weaker measurement — it is a wrong
 * one. The chat-open ratio is read as `1 − (project_opened_chat / project_opened)`, so a ratio
 * above 1 is not a bias, it is a broken number.
 *
 * Module state IS the guard (one project id per page load), so each test imports a FRESH copy of
 * the module rather than reaching for a reset export that would exist only for tests.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const h = vi.hoisted(() => ({ authFetch: vi.fn() }))
vi.mock('../api', () => ({ authFetch: h.authFetch }))

/** A brand-new page load: fresh module state, fresh call log. */
async function aFreshPageLoad() {
  vi.resetModules()
  h.authFetch.mockReset()
  h.authFetch.mockResolvedValue({ ok: true } as Response)
  return await import('../observe')
}

/** The bodies actually posted, in order. */
function beacons(): unknown[] {
  return h.authFetch.mock.calls.map(([, opts]) => JSON.parse(String(opts.body)))
}

beforeEach(() => {
  h.authFetch.mockReset()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('opening a project', () => {
  it('sends exactly one project_opened, and to the beacon route', async () => {
    const { markProjectOpened } = await aFreshPageLoad()

    markProjectOpened('p1', { hasApp: true })

    expect(beacons()).toEqual([{ name: 'project_opened' }])
    expect(h.authFetch.mock.calls[0][0]).toBe('/api/observations')
    expect(h.authFetch.mock.calls[0][1].method).toBe('POST')
  })

  it('is marked ONCE when the same project is opened twice in one load', async () => {
    // ★ THE STRICTMODE CASE. React double-invokes every effect in development, so a mount-fired
    // beacon double-counts without this guard — and an early read of the chat-open ratio would be
    // skewed by whichever developers happened to be clicking around. It is also the repeat-visit case:
    // one project id per page load is what "a visit" MEANS here.
    const { markProjectOpened } = await aFreshPageLoad()

    markProjectOpened('p1', { hasApp: true })
    markProjectOpened('p1', { hasApp: true })

    expect(beacons()).toEqual([{ name: 'project_opened' }])
  })

  it('counts two different projects in one load separately', async () => {
    const { markProjectOpened } = await aFreshPageLoad()

    markProjectOpened('p1', { hasApp: false })
    markProjectOpened('p2', { hasApp: false })

    expect(beacons()).toEqual([{ name: 'project_opened' }, { name: 'project_opened' }])
  })
})

describe('opening a chat from a project', () => {
  it('sends one project_opened_chat after its project was opened', async () => {
    const { markProjectOpened, markChatOpened } = await aFreshPageLoad()

    markProjectOpened('p1', { hasApp: false })
    markChatOpened('p1')

    expect(beacons()).toEqual([{ name: 'project_opened' }, { name: 'project_opened_chat' }])
  })

  it('counts one visit, not two chats', async () => {
    const { markProjectOpened, markChatOpened } = await aFreshPageLoad()

    markProjectOpened('p1', { hasApp: false })
    markChatOpened('p1')
    markChatOpened('p1')

    expect(beacons()).toEqual([{ name: 'project_opened' }, { name: 'project_opened_chat' }])
  })

  it('★ sends NOTHING for a project this load never opened (the deep-link case)', async () => {
    // A bookmark, a shared link or a browser restore lands straight on `/chat/{id}` and resolves
    // a project whose page was never on screen. Counting it would push the chat-open ratio above
    // 1 — a denominator smaller than its numerator. Removing the guard must fail this.
    const { markChatOpened } = await aFreshPageLoad()

    markChatOpened('p-never-opened')

    expect(beacons()).toEqual([])
  })

  it('invents no denominator either', async () => {
    // The other half of the same guard: the deep link must not be "fixed" by marking the project
    // open on the way past. That would count a project visit that never happened.
    const { markChatOpened, markProjectOpened } = await aFreshPageLoad()

    markChatOpened('p1')
    markProjectOpened('p1', { hasApp: false })
    markChatOpened('p1')

    expect(beacons()).toEqual([{ name: 'project_opened' }, { name: 'project_opened_chat' }])
  })

  it('ignores a chat with no project behind it', async () => {
    const { markChatOpened } = await aFreshPageLoad()

    markChatOpened(null)

    expect(beacons()).toEqual([])
  })
})

describe('first seeing the app', () => {
  it('records the elapsed time between opening the project and the app appearing', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-30T10:00:00Z'))
    const { markProjectOpened, markAppVisible } = await aFreshPageLoad()

    markProjectOpened('p1', { hasApp: true })
    vi.advanceTimersByTime(7321)
    markAppVisible('p1')

    expect(beacons()).toEqual([
      { name: 'project_opened' },
      { name: 'project_to_app_visible_ms', value: 7321 },
    ])
  })

  it('records it ONCE, however many times the frame reveals', async () => {
    // A reload of the same app re-reveals; the second reveal is not a second first-view.
    const { markProjectOpened, markAppVisible } = await aFreshPageLoad()

    markProjectOpened('p1', { hasApp: true })
    markAppVisible('p1')
    markAppVisible('p1')

    expect(beacons().filter((b) => (b as { name: string }).name === 'project_to_app_visible_ms'))
      .toHaveLength(1)
  })

  it('★ starts no clock for a project with nothing built', async () => {
    // A project with no app has no app to first-see, and emitting for it would make this number
    // and the sandbox-first number answer different questions.
    const { markProjectOpened, markAppVisible } = await aFreshPageLoad()

    markProjectOpened('p1', { hasApp: false })
    markAppVisible('p1')

    expect(beacons()).toEqual([{ name: 'project_opened' }])
  })

  it('★ records nothing when the project page was never opened in this load', async () => {
    // The deep-link case from the other end. An implementer who defaults a missing mark to
    // page-load time measures a DIFFERENT journey and pollutes the only `project_to_app_visible_ms`
    // number there is.
    const { markAppVisible } = await aFreshPageLoad()

    markAppVisible('p-deep-link')

    expect(beacons()).toEqual([])
  })
})

describe('a beacon that does not land', () => {
  it('never throws, and never fails the thing it was observing', async () => {
    const { markProjectOpened } = await aFreshPageLoad()
    h.authFetch.mockRejectedValue(new Error('offline'))

    expect(() => markProjectOpened('p1', { hasApp: true })).not.toThrow()
    // Let the rejected promise settle: an unhandled rejection here would fail the run.
    await Promise.resolve()
    await Promise.resolve()
    expect(h.authFetch).toHaveBeenCalledTimes(1)
  })
})

describe('timing a start from its click to its app showing', () => {
  /** The start waits posted, in order — the counts above go to a different route. */
  function startWaits(): unknown[] {
    return h.authFetch.mock.calls
      .filter(([path]) => path === '/api/observations/start-visible')
      .map(([, opts]) => JSON.parse(String(opts.body)))
  }

  async function aClockAt(time: string) {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(time))
    return await aFreshPageLoad()
  }

  it('reports the wait against the start the click began, with nothing but the id and the time', async () => {
    // Labels come from the server's own row; a browser that sent a kind could relabel a start.
    const { markStartClicked, markStartVisible } = await aClockAt('2026-10-05T10:00:00Z')

    const began = markStartClicked('p1')
    vi.advanceTimersByTime(800)
    began('s-1')
    vi.advanceTimersByTime(40_450)
    markStartVisible('p1')

    expect(startWaits()).toEqual([{ startId: 's-1', durationMs: 41_250 }])
    expect(beacons()).toEqual([{ startId: 's-1', durationMs: 41_250 }])
  })

  it('reports it once, however many times the app shows', async () => {
    const { markStartClicked, markStartVisible } = await aFreshPageLoad()

    markStartClicked('p1')('s-1')
    markStartVisible('p1')
    markStartVisible('p1')

    expect(startWaits()).toHaveLength(1)
  })

  it('★ reports nothing for a click the server answered with no start', async () => {
    // An attach: the app was already running, and there is no start for the time to belong to.
    const { markStartClicked, markStartVisible } = await aFreshPageLoad()

    markStartClicked('p1')(null)
    markStartVisible('p1')

    expect(startWaits()).toEqual([])
  })

  it('★ ignores a stop that comes before the server named the start', async () => {
    // The start's container does not exist until after the answer that names it, so whatever
    // showed first was an app from before the click. Mutation check: let an unbound stop report
    // or consume the clock and this goes red.
    const { markStartClicked, markStartVisible } = await aClockAt('2026-10-05T10:00:00Z')

    const began = markStartClicked('p1')
    markStartVisible('p1')
    vi.advanceTimersByTime(2_000)
    began('s-1')
    vi.advanceTimersByTime(30_000)
    markStartVisible('p1')

    expect(startWaits()).toEqual([{ startId: 's-1', durationMs: 32_000 }])
  })

  it('★ keeps the time of the click that began a start when later clicks join it', async () => {
    // Opening the project starts the app; a message sent before it shows attaches to that start,
    // and a second press is joined to it and named by the server. The wait is the open's.
    // Mutation check: let a click naming the timed start re-time it and this goes red.
    const { markStartClicked, markStartVisible } = await aClockAt('2026-10-05T10:00:00Z')

    markStartClicked('p1')('s-1')
    vi.advanceTimersByTime(5_000)
    markStartClicked('p1')(null)
    vi.advanceTimersByTime(1_000)
    markStartClicked('p1')('s-1')
    vi.advanceTimersByTime(20_000)
    markStartVisible('p1')

    expect(startWaits()).toEqual([{ startId: 's-1', durationMs: 26_000 }])
  })

  it('times a second start from the click that began it', async () => {
    // The first start never showed and a sent message began another; the first click's wait
    // includes a failure the second start did not have.
    const { markStartClicked, markStartVisible } = await aClockAt('2026-10-05T10:00:00Z')

    markStartClicked('p1')('s-1')
    vi.advanceTimersByTime(60_000)
    const resend = markStartClicked('p1')
    vi.advanceTimersByTime(500)
    resend('s-2')
    vi.advanceTimersByTime(9_500)
    markStartVisible('p1')

    expect(startWaits()).toEqual([{ startId: 's-2', durationMs: 10_000 }])
  })

  it('★ reports nothing for a start abandoned for another project or by leaving', async () => {
    // Mutation check: make `markStartAbandoned` a no-op and this goes red.
    const { markStartClicked, markStartVisible, markStartAbandoned } = await aFreshPageLoad()

    markStartClicked('p1')('s-1')
    markStartAbandoned('p2')
    markStartVisible('p1')
    markStartClicked('p3')('s-3')
    markStartAbandoned()
    markStartVisible('p3')

    expect(startWaits()).toEqual([])
  })

  it('★ binds nothing from an answer that lands after its project was left', async () => {
    const { markStartClicked, markStartVisible, markStartAbandoned } = await aFreshPageLoad()

    const began = markStartClicked('p1')
    markStartAbandoned('p2')
    began('s-1')
    markStartVisible('p1')

    expect(startWaits()).toEqual([])
  })

  it('keeps the start while the person moves between its own project`s screens', async () => {
    const { markStartClicked, markStartVisible, markStartAbandoned } = await aFreshPageLoad()

    const began = markStartClicked('p1')
    markStartAbandoned('p1')
    began('s-1')
    markStartAbandoned('p1')
    markStartVisible('p1')

    expect(startWaits()).toEqual([{ startId: 's-1', durationMs: expect.any(Number) }])
  })

  it('★ ends a project`s wait when a click starts another project', async () => {
    // One workspace per person: starting another project takes the slot the first was waiting on.
    const { markStartClicked, markStartVisible } = await aFreshPageLoad()

    markStartClicked('p1')('s-1')
    markStartClicked('p2')
    markStartVisible('p1')

    expect(startWaits()).toEqual([])
  })

  it('does not report one project`s start when another project`s app shows', async () => {
    const { markStartClicked, markStartVisible } = await aFreshPageLoad()

    markStartClicked('p1')('s-1')
    markStartVisible('p2')
    markStartVisible(null)

    expect(startWaits()).toEqual([])
    markStartVisible('p1')
    expect(startWaits()).toHaveLength(1)
  })

  it('never throws when the report does not land', async () => {
    const { markStartClicked, markStartVisible } = await aFreshPageLoad()
    h.authFetch.mockRejectedValue(new Error('offline'))

    markStartClicked('p1')('s-1')
    expect(() => markStartVisible('p1')).not.toThrow()
    await Promise.resolve()
    await Promise.resolve()
    expect(h.authFetch).toHaveBeenCalledTimes(1)
  })
})
