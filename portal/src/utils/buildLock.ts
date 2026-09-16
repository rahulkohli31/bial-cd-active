/**
 * One build at a time, per project, as far as a browser can enforce it. This closes the
 * realistic window — two tabs of one browser — and leaves the cross-device case documented
 * and accepted. Planning chats are never blocked; only builds write code.
 *
 * WHY THIS EXISTS
 * DEMOTED TO ADVISORY. The AUTHORITATIVE one-build-per-user barrier lives server-side: the
 * build-session routes answer `409 build_session_already_active`, reaching two live surfaces
 * (`relaunchPreview`'s `BuildSessionAlreadyActiveError`, and `turnStreamApi.ts`'s carried
 * code). This module keeps only the FAST LOCAL UX role — `blockedBy` is the instant cross-tab
 * pre-check `ConversationSurface.tsx` calls before the network round trip. `acquire`/`release`
 * are advisory mirrors only: a stale or lost local claim never blocks the real server start.
 *
 * A FACTORY, not a module singleton, so two managers in one test see each other's claims only
 * through the wire (never an in-process shortcut) — the same code path in one tab or six. A
 * crashed tab must not wedge the project forever, so a claim is renewed by heartbeat and
 * expires without one.
 */

import { isRecord } from './apiError'

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
  /**
   * Advisory claim. Announces this conversation's build to other tabs and returns null,
   * or the claim that blocks it. NOT the enforcement boundary — the server `start`'s 409 is
   * authoritative; a stale/lost claim here never blocks the real start.
   */
  acquire(projectId: string, conversationId: string): BuildClaim | null
  /** Advisory release of this conversation's claim, if it holds one (the server owns the real lock). */
  release(conversationId: string): void
  /** The live claim blocking a build in `projectId` for `conversationId`, or null — the fast cross-tab UX pre-check. */
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

// Channel traffic is untrusted input like any other boundary: narrow it, never cast it.
function isClaim(value: unknown): value is BuildClaim {
  return (
    isRecord(value) &&
    typeof value.projectId === 'string' &&
    typeof value.conversationId === 'string' &&
    typeof value.beatAt === 'number'
  )
}

function isClaimMessage(value: unknown): value is ClaimMessage {
  return isRecord(value) && (value.type === 'announce' || value.type === 'retract' || value.type === 'poll')
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

  // Set by dispose(). A late async caller — the conversation surface re-acquires after an
  // awaited start()/reattach() that may resolve post-unmount — must not restart the heartbeat
  // on a dead manager: nothing could ever clear it again (a zombie interval for the SPA lifetime).
  let disposed = false

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
    if (disposed || heartbeat !== null) return
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
      if (disposed) return null // advisory no-op: a disposed manager never claims (nor blocks)
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
      disposed = true // fail-safe every late async acquire (see the flag above)
      // Retract BEFORE closing — a closed channel cannot post, and other tabs would keep
      // seeing this claim until its heartbeat expired.
      for (const claim of local.values()) post({ type: 'retract', claim })
      local.clear()
      remote.clear()
      if (heartbeat !== null) {
        clearInterval(heartbeat)
        heartbeat = null
      }
      channel?.removeEventListener('message', onMessage)
      // Closing is not optional. Every entry into a builder chat opens a channel; detaching
      // the listener without closing orphans the handle on the page's channel bus for the
      // life of the document.
      channel?.close()
    },
  }
}
