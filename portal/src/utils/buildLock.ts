/**
 * One build at a time, per project — as far as a browser can enforce it.
 *
 * A project's `app_registry.current_code` is last-write-wins across sessions: two builder
 * chats streaming into the same project silently destroy each other's code. The backend has
 * no lock (a Redis lock or an optimistic-concurrency version on `current_code` is the real
 * fix, and is a backend change). This closes the realistic window — two tabs of one browser
 * — and leaves the cross-device case documented and accepted.
 *
 * Planning chats are never blocked. Only builds write code.
 *
 * WHY A FACTORY, NOT A MODULE SINGLETON. A module-level claim map would let two BuilderPage
 * instances in one document see each other's claims WITHOUT a channel round-trip, so a
 * two-page test would prove only the same-tab path and leave the cross-tab path — the one
 * this module exists for — entirely unexercised. With a factory, every manager owns its own
 * channel and the wire is the ONLY way claims travel: the same code path in one tab or six.
 * It also lets a test construct two managers over two real BroadcastChannels and drive the
 * message path directly.
 *
 * A crashed tab must not wedge the project forever, so a claim is renewed by a heartbeat and
 * expires without one. A lock that never clears is worse than no lock.
 */

/** A live build claim, as broadcast between tabs. */
export interface BuildClaim {
  projectId: string
  conversationId: string
  /** Monotonic ms timestamp of the last heartbeat, as seen by the CLAIMANT's clock. */
  beatAt: number
}

interface ClaimMessage {
  type: 'announce' | 'retract' | 'poll'
  claim?: BuildClaim
}

export interface BuildLock {
  /** Claim the project for `conversationId`. Returns null on success, or the claim that blocks it. */
  acquire(projectId: string, conversationId: string): BuildClaim | null
  /** Drop this conversation's claim, if it holds one. */
  release(conversationId: string): void
  /** The live claim blocking a build in `projectId` for `conversationId`, or null. */
  blockedBy(projectId: string, conversationId: string): BuildClaim | null
  /** Stop the heartbeat, retract every local claim, and detach from the channel. */
  dispose(): void
}

export const BUILD_LOCK_CHANNEL = 'bial:build-lock'

/** A claim with no heartbeat for this long is treated as a crashed tab's leftovers. */
export const CLAIM_TTL_MS = 15_000
const HEARTBEAT_MS = 5_000

export interface BuildLockOptions {
  /**
   * The channel to gossip claims over. Pass `null` to degrade to a same-tab-only lock —
   * which is what happens in an environment without `BroadcastChannel`.
   */
  channel?: BroadcastChannel | null
  /** Injectable clock, so a test can drive expiry with fake timers. */
  now?: () => number
}

function isClaim(value: unknown): value is BuildClaim {
  if (typeof value !== 'object' || value === null) return false
  const c = value as Record<string, unknown>
  return typeof c.projectId === 'string' && typeof c.conversationId === 'string' && typeof c.beatAt === 'number'
}

function isClaimMessage(value: unknown): value is ClaimMessage {
  if (typeof value !== 'object' || value === null) return false
  const m = value as Record<string, unknown>
  return m.type === 'announce' || m.type === 'retract' || m.type === 'poll'
}

/** Open the shared channel, or null where BroadcastChannel does not exist. */
export function openBuildLockChannel(): BroadcastChannel | null {
  if (typeof BroadcastChannel === 'undefined') return null
  try {
    return new BroadcastChannel(BUILD_LOCK_CHANNEL)
  } catch {
    return null // degrade to same-tab-only rather than break the builder
  }
}

export function createBuildLock({ channel = null, now = () => Date.now() }: BuildLockOptions = {}): BuildLock {
  // Claims made by THIS manager. `remote` holds claims heard over the channel.
  const local = new Map<string, BuildClaim>() // conversationId -> claim
  const remote = new Map<string, BuildClaim>() // conversationId -> claim

  const post = (message: ClaimMessage): void => {
    try {
      channel?.postMessage(message)
    } catch {
      // A closed channel must not break a build turn; the lock degrades to same-tab.
    }
  }

  const fresh = (claim: BuildClaim): boolean => now() - claim.beatAt < CLAIM_TTL_MS

  /** Sweep expired remote claims — a crashed tab stops beating and stops blocking. */
  const sweep = (): void => {
    for (const [id, claim] of remote) if (!fresh(claim)) remote.delete(id)
  }

  const onMessage = (event: MessageEvent): void => {
    const message: unknown = event.data
    if (!isClaimMessage(message)) return
    if (message.type === 'poll') {
      // A newcomer asked who is building; re-announce so it learns about us immediately
      // rather than waiting a full heartbeat.
      for (const claim of local.values()) post({ type: 'announce', claim })
      return
    }
    if (!isClaim(message.claim)) return
    if (message.type === 'announce') remote.set(message.claim.conversationId, message.claim)
    else remote.delete(message.claim.conversationId)
  }

  channel?.addEventListener('message', onMessage)

  // Renew our claims so other tabs keep seeing them as live, and forget claims from tabs
  // that stopped beating. Runs only while we hold something (heartbeat starts on acquire).
  let heartbeat: ReturnType<typeof setInterval> | null = null

  const beat = (): void => {
    sweep()
    for (const claim of local.values()) {
      claim.beatAt = now()
      post({ type: 'announce', claim })
    }
    if (local.size === 0 && heartbeat !== null) {
      clearInterval(heartbeat)
      heartbeat = null
    }
  }

  const startHeartbeat = (): void => {
    if (heartbeat !== null) return
    heartbeat = setInterval(beat, HEARTBEAT_MS)
  }

  const blockedBy: BuildLock['blockedBy'] = (projectId, conversationId) => {
    sweep()
    for (const claim of [...local.values(), ...remote.values()]) {
      if (claim.projectId === projectId && claim.conversationId !== conversationId && fresh(claim)) return claim
    }
    return null
  }

  // Ask any tab already building to announce itself, so a page that opens second learns
  // about an in-flight build without waiting for the holder's next heartbeat.
  post({ type: 'poll' })

  return {
    acquire(projectId, conversationId) {
      const blocker = blockedBy(projectId, conversationId)
      if (blocker) return blocker
      const claim: BuildClaim = { projectId, conversationId, beatAt: now() }
      local.set(conversationId, claim)
      post({ type: 'announce', claim })
      startHeartbeat()
      return null
    },

    release(conversationId) {
      const claim = local.get(conversationId)
      if (!claim) return
      local.delete(conversationId)
      post({ type: 'retract', claim })
      if (local.size === 0 && heartbeat !== null) {
        clearInterval(heartbeat)
        heartbeat = null
      }
    },

    blockedBy,

    dispose() {
      for (const claim of local.values()) post({ type: 'retract', claim })
      local.clear()
      remote.clear()
      if (heartbeat !== null) {
        clearInterval(heartbeat)
        heartbeat = null
      }
      channel?.removeEventListener('message', onMessage)
    },
  }
}
