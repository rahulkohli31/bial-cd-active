/**
 * The one-build-per-project lock.
 *
 * The load-bearing test here is the CROSS-TAB one: two independent managers over two real
 * BroadcastChannel instances. Had the lock kept a module-level claim map, two managers in one
 * document would see each other's claims without a channel round-trip — the wire, and the only
 * reason this module exists, would go entirely unexercised. The factory prevents that.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createBuildLock, openBuildLockChannel, CLAIM_TTL_MS, BUILD_LOCK_CHANNEL } from '../buildLock'

/**
 * Let queued BroadcastChannel messages be delivered. jsdom dispatches them on a task, and a
 * single `setTimeout(0)` is not reliably enough under load — a one-tick flush made this suite
 * flake. Poll for the condition instead of guessing at the number of ticks.
 */
const flush = async (): Promise<void> => {
  for (let i = 0; i < 5; i += 1) await new Promise<void>((resolve) => setTimeout(resolve, 0))
}

/** Wait until `predicate` holds, draining the channel between attempts. */
const until = async (predicate: () => boolean): Promise<void> => {
  for (let i = 0; i < 50 && !predicate(); i += 1) await new Promise<void>((resolve) => setTimeout(resolve, 1))
  if (!predicate()) throw new Error('condition never held — the channel never delivered')
}

// BroadcastChannel delivery is queued, and a message posted at the very end of one test can
// land inside the next one — a retract for `chat-A` bleeding forward would silently cancel the
// announce a later test just made for the same id. Give every test its own channel NAME so the
// bus cannot carry anything across a test boundary. The production name is still exercised, by
// `openBuildLockChannel` below.
let channelName = BUILD_LOCK_CHANNEL
let channelSeq = 0
const openChannels: BroadcastChannel[] = []
function channel(): BroadcastChannel {
  const c = new BroadcastChannel(channelName)
  openChannels.push(c)
  return c
}

beforeEach(() => {
  channelSeq += 1
  channelName = `${BUILD_LOCK_CHANNEL}:test-${channelSeq}`
})

afterEach(() => {
  openChannels.splice(0).forEach((c) => c.close())
  vi.useRealTimers()
})

describe('buildLock — same manager', () => {
  it('acquires, then blocks a second builder chat in the same project', () => {
    const lock = createBuildLock()
    expect(lock.acquire('p1', 'chat-A')).toBeNull()

    const blocker = lock.acquire('p1', 'chat-B')
    expect(blocker).not.toBeNull()
    expect(blocker?.conversationId).toBe('chat-A') // names the chat that is building
    lock.dispose()
  })

  it('releasing lets the next builder chat through', () => {
    const lock = createBuildLock()
    lock.acquire('p1', 'chat-A')
    lock.release('chat-A')
    expect(lock.acquire('p1', 'chat-B')).toBeNull()
    lock.dispose()
  })

  it('does not block a builder chat in a DIFFERENT project', () => {
    const lock = createBuildLock()
    lock.acquire('p1', 'chat-A')
    expect(lock.acquire('p2', 'chat-B')).toBeNull()
    lock.dispose()
  })

  it('re-acquiring for the same conversation is not self-blocking (a retry must not deadlock)', () => {
    const lock = createBuildLock()
    lock.acquire('p1', 'chat-A')
    expect(lock.acquire('p1', 'chat-A')).toBeNull()
    lock.dispose()
  })

  it('dispose() drops every claim it held', () => {
    const lock = createBuildLock()
    lock.acquire('p1', 'chat-A')
    lock.dispose()
    expect(lock.blockedBy('p1', 'chat-B')).toBeNull()
  })

  it('a late acquire() on a DISPOSED lock is a no-op — no claim, no restarted heartbeat', () => {
    // BuilderPage re-acquires after an awaited start()/reattach(); if the page unmounted
    // mid-await, that acquire lands on a disposed lock — it must not restart a heartbeat
    // interval that nothing can ever clear (a zombie timer for the SPA lifetime).
    const lock = createBuildLock({ channel: null })
    lock.dispose()
    const intervalSpy = vi.spyOn(globalThis, 'setInterval')
    expect(lock.acquire('p1', 'chat-A')).toBeNull() // advisory: a dead manager never blocks…
    expect(lock.blockedBy('p1', 'chat-B')).toBeNull() // …and it claimed nothing
    expect(intervalSpy).not.toHaveBeenCalled()
    intervalSpy.mockRestore()
  })

  it('dispose() CLOSES the channel — every builder-route entry opens one', async () => {
    // Detaching the listener without closing orphans the handle on the page's channel bus
    // for the life of the document. A closed channel throws on postMessage, which is the
    // observable signal that close() actually ran.
    const c = channel()
    const lock = createBuildLock({ channel: c })
    lock.acquire('p1', 'chat-A')
    lock.dispose()
    expect(() => c.postMessage({ type: 'poll' })).toThrow()
  })

  it('dispose() retracts BEFORE closing, so other tabs are not left blocked', async () => {
    const tabA = createBuildLock({ channel: channel() })
    const tabB = createBuildLock({ channel: channel() })
    tabA.acquire('p1', 'chat-A')
    await until(() => tabB.blockedBy('p1', 'chat-B') !== null)

    tabA.dispose() // retract must reach B before the channel shuts
    await until(() => tabB.blockedBy('p1', 'chat-B') === null)

    expect(tabB.blockedBy('p1', 'chat-B')).toBeNull()
    tabB.dispose()
  })
})

describe('buildLock — cross-tab, over two real BroadcastChannels', () => {
  it('A’s announce blocks B, and A’s retract unblocks B', async () => {
    // THIS is the guarantee. Two managers, two channels, one document — exactly the shape
    // of two browser tabs, and the only test that actually drives the message path.
    const tabA = createBuildLock({ channel: channel() })
    const tabB = createBuildLock({ channel: channel() })

    expect(tabA.acquire('p1', 'chat-A')).toBeNull()
    await flush()

    const blocker = tabB.acquire('p1', 'chat-B')
    expect(blocker?.conversationId).toBe('chat-A')

    tabA.release('chat-A')
    await flush()

    expect(tabB.acquire('p1', 'chat-B')).toBeNull()
    tabA.dispose()
    tabB.dispose()
  })

  it('a tab that opens AFTER a build started still learns it is blocked', async () => {
    const tabA = createBuildLock({ channel: channel() })
    tabA.acquire('p1', 'chat-A')
    await flush()

    // tabB opens now: it polls, tabA re-announces, tabB blocks without waiting a heartbeat.
    const tabB = createBuildLock({ channel: channel() })
    await flush()

    expect(tabB.acquire('p1', 'chat-B')?.conversationId).toBe('chat-A')
    tabA.dispose()
    tabB.dispose()
  })

  it('a cross-tab claim does not block a different project', async () => {
    const tabA = createBuildLock({ channel: channel() })
    const tabB = createBuildLock({ channel: channel() })
    tabA.acquire('p1', 'chat-A')
    await flush()
    expect(tabB.acquire('p2', 'chat-B')).toBeNull()
    tabA.dispose()
    tabB.dispose()
  })

  it('a stale REMOTE claim (heartbeat stopped — a crashed tab) expires and stops blocking', async () => {
    // Drive expiry against a claim delivered over the channel, not a local one: a stale lock
    // that never clears is worse than no lock, and only the remote path can go stale.
    let clock = 1_000_000
    const tabA = createBuildLock({ channel: channel(), now: () => clock })
    const tabB = createBuildLock({ channel: channel(), now: () => clock })

    tabA.acquire('p1', 'chat-A')
    await flush()
    expect(tabB.acquire('p1', 'chat-B')).not.toBeNull() // blocked while A is beating

    // Tab A crashes: no more heartbeats. Time passes beyond the TTL.
    tabA.dispose()
    clock += CLAIM_TTL_MS + 1
    // (dispose already retracted; simulate a hard crash by re-announcing a stale claim)
    const ghost = createBuildLock({ channel: channel(), now: () => clock - CLAIM_TTL_MS - 1 })
    ghost.acquire('p1', 'chat-ghost')
    await flush()

    expect(tabB.acquire('p1', 'chat-B')).toBeNull() // the ghost's claim is too old to matter
    ghost.dispose()
    tabB.dispose()
  })
})

describe('buildLock — no BroadcastChannel', () => {
  it('degrades to a same-tab-only lock and does not throw', () => {
    const lock = createBuildLock({ channel: null })
    expect(lock.acquire('p1', 'chat-A')).toBeNull()
    expect(lock.acquire('p1', 'chat-B')?.conversationId).toBe('chat-A')
    lock.release('chat-A')
    expect(lock.acquire('p1', 'chat-B')).toBeNull()
    lock.dispose()
  })

  it('openBuildLockChannel returns null when the constructor is absent', () => {
    const original = globalThis.BroadcastChannel
    // @ts-expect-error — deliberately removing a global to exercise the fallback
    delete globalThis.BroadcastChannel
    try {
      expect(openBuildLockChannel()).toBeNull()
    } finally {
      globalThis.BroadcastChannel = original
    }
  })

  it('openBuildLockChannel returns a usable channel when it exists', () => {
    const c = openBuildLockChannel()
    expect(c).not.toBeNull()
    c?.close()
  })
})

describe('buildLock — advisory only (KTD-7)', () => {
  it('blockedBy stays the fast cross-tab pre-check, but the module enforces nothing — C3 start’s 409 is authoritative', () => {
    const lock = createBuildLock({ channel: null })
    // A local claim gives the instant "another chat is building" signal (the toast pre-check)...
    expect(lock.acquire('p1', 'chat-A')).toBeNull()
    expect(lock.blockedBy('p1', 'chat-B')?.conversationId).toBe('chat-A')

    // ...but it is ADVISORY: nothing here can gate a real build. The authoritative barrier moved
    // server-side (C3 start's 409). A stale/lost local claim must never be the thing that blocks a
    // start — that decision belongs to the backend per-user lock, mirrored (not enforced) here.
    lock.release('chat-A')
    expect(lock.blockedBy('p1', 'chat-B')).toBeNull() // the mirror clears; the server remains the source of truth
    lock.dispose()
  })
})
