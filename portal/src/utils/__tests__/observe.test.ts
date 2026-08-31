/**
 * The three marks only the browser can make (U4; R104, R105).
 *
 * These are guard tests, and every guard here exists because a counter that double-counts, or
 * that counts a numerator without its denominator, is not a weaker measurement — it is a wrong
 * one. R105 is read as `1 − (project_opened_chat / project_opened)`, so a ratio above 1 is not a
 * bias, it is a broken number.
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
    // beacon double-counts without this guard — and an early read of R105 would be skewed by
    // whichever developers happened to be clicking around. It is also the repeat-visit case:
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
    // a project whose page was never on screen. Counting it would push R105's ratio above 1 — a
    // denominator smaller than its numerator. Removing the guard must fail this.
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
    // page-load time measures a DIFFERENT journey and pollutes the only R104 number there is.
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
