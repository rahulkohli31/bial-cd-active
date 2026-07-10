/**
 * The one-build-per-project lock.
 *
 * The load-bearing test here is the CROSS-TAB one: two independent managers over two real
 * BroadcastChannel instances. A test that mounts two BuilderPages in one document would
 * share a module-level map and short-circuit before any channel round-trip — proving only
 * the same-tab path, and leaving the path this module exists for entirely unexercised.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { createBuildLock, openBuildLockChannel, CLAIM_TTL_MS, BUILD_LOCK_CHANNEL } from '../buildLock'

/** Let queued BroadcastChannel messages be delivered (jsdom dispatches them as microtasks). */
const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0))

const openChannels: BroadcastChannel[] = []
function channel(): BroadcastChannel {
  const c = new BroadcastChannel(BUILD_LOCK_CHANNEL)
  openChannels.push(c)
  return c
}

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
