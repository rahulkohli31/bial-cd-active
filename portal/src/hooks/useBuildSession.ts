/**
 * The single owner of a build session's lifecycle: it starts / re-attaches / stops /
 * force-ends a C3 session, subscribes to its C7 SSE feed, derives the
 * `BuildSessionStatus`, and runs the frozen keep-alive timers. Every cockpit surface
 * (LivePreview, ActivityFeed, SessionControls) reads from here.
 *
 * KEY BEHAVIOURS (the plan's load-bearing decisions):
 *
 *  - **Status derivation** (C3 §1, from the envelope stream): the first non-terminal
 *    envelope (step|log|error) moves `provisioning → building`; `preview_ready → ready`;
 *    `ended{status:ended} → ended` (graceful — INCLUDING the quota path, which must resolve
 *    ENDED not FAILED); `ended{status:failed} → failed`. The two absorbing terminals are
 *    distinct (C3 §1).
 *  - **Missed `preview_ready`** (KTD-1): `start`/`reattach` seed `previewUrl` from the C3
 *    status response, so a `preview_ready` that fired BEFORE the client connected still
 *    frames the app — readiness comes from authoritative status, not solely the live envelope.
 *  - **Force-end override**: the terminal transition comes from `ForceEndResponse.status`,
 *    overriding the envelope-derived status — a stuck-mid-`building` session may never emit a
 *    terminal `ended` (that is the whole reason force-end exists, C3 §3.4).
 *  - **Keep-alive failure fails closed** (no floating promise): a renew `409 lock_lost`, a
 *    heartbeat `404`, OR any other rejection (5xx / timeout / offline over a long build) all
 *    STOP both timers and reach a terminal state surfaced as `ended` with a distinct `reclaimed`
 *    flag — NOT a 6th `BuildSessionStatus` member (the enum stays frozen at 5). This reconciles
 *    idempotently with the SSE `ended` (whichever fires first wins). It is the only clean in-band
 *    signal for the frozen-tab case (the lock lapses, the reaper tears down + emits `ended` on an
 *    SSE the frozen tab never receives, and the resume reconnect 404s).
 *  - **Feed-disconnected** (KTD-1): a bounded `EventSource` reconnect exhaustion (or an admission
 *    failure) raises a distinct `feedDisconnected` flag with a manual `reconnect()` — heartbeat /
 *    renew may still be succeeding, so nothing else signals the dead feed.
 *
 * `buildLock` is NOT consulted here — its `blockedBy` pre-check lives at the composer (U5); the
 * authoritative barrier is C3 `start`'s 409 (KTD-7), surfaced as `blocked`.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../utils/apiError'
import {
  BuildSessionAlreadyActiveError,
  HEARTBEAT_CADENCE_SECONDS,
  LOCK_RENEW_CADENCE_SECONDS,
  buildSessionClient,
} from '../utils/buildSessionApi'
import type { BuildSessionClient } from '../utils/buildSessionApi'
import { subscribeBuildFeed } from '../utils/buildSessionEvents'
import type { BuildFeedError, BuildFeedSubscription, EventSourceFactory } from '../utils/buildSessionEvents'
import type { BuildSessionStatus, FeedEnvelope, ProgressEnvelope } from '../utils/buildSessionTypes'

/** How long a live `ready` preview may go quiet before the "still iterating" overlay clears (KTD-8b). */
const ITERATION_QUIET_MS = 4000

/** The 409-block state: the caller already holds a live session (carries its id for the reattach/force-end decision). */
export interface BlockedState {
  existingSessionId: string | null
}

/** The graceful-quota terminal surfaced to the banner ("resets at midnight IST"). */
export interface QuotaState {
  limit: number
  used: number
  resetsAt: string
}

/** What `start` resolved to — U5 branches on this (started / blocked→reattach-or-block / error). */
export type StartOutcome =
  | { kind: 'started'; sessionId: string }
  | { kind: 'blocked'; existingSessionId: string | null }
  | { kind: 'error'; message: string }

export interface UseBuildSessionDeps {
  client?: BuildSessionClient
  eventSourceFactory?: EventSourceFactory
}

export interface UseBuildSessionResult {
  sessionId: string | null
  status: BuildSessionStatus | null
  previewUrl: string | null
  envelopes: FeedEnvelope[]
  /** True while a LIVE `ready` preview keeps receiving step/log activity (drives the overlay). */
  iterating: boolean
  /** A graceful stop is in flight — the Stop control shows a pending state until terminal. */
  stopping: boolean
  blocked: BlockedState | null
  reclaimed: boolean
  feedDisconnected: boolean
  quota: QuotaState | null
  error: string | null
  /** ms epoch the current session started, for elapsed-time display in the force-end confirm. */
  startedAt: number | null
  start: (projectId: string, prompt: string) => Promise<StartOutcome>
  reattach: (sessionId: string) => Promise<void>
  stop: () => Promise<void>
  forceEnd: (targetSessionId?: string) => Promise<void>
  reconnect: () => void
  reset: () => void
  clearBlocked: () => void
}

/** Upsert an envelope into the feed store by `seq` (duplicate replaces, never appends) and keep it ordered. */
function upsertBySeq(store: FeedEnvelope[], env: FeedEnvelope): FeedEnvelope[] {
  const next = store.filter((e) => e.seq !== env.seq)
  next.push(env)
  next.sort((a, b) => a.seq - b.seq)
  return next
}

export function useBuildSession(deps: UseBuildSessionDeps = {}): UseBuildSessionResult {
  const client = deps.client ?? buildSessionClient
  const eventSourceFactory = deps.eventSourceFactory

  const [sessionId, setSessionId] = useState<string | null>(null)
  const [status, setStatus] = useState<BuildSessionStatus | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [envelopes, setEnvelopes] = useState<FeedEnvelope[]>([])
  const [iterating, setIterating] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [blocked, setBlocked] = useState<BlockedState | null>(null)
  const [reclaimed, setReclaimed] = useState(false)
  const [feedDisconnected, setFeedDisconnected] = useState(false)
  const [quota, setQuota] = useState<QuotaState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [startedAt, setStartedAt] = useState<number | null>(null)

  // Refs mirror the state that async callbacks (timers, SSE handlers) must read WITHOUT a stale
  // closure. `statusRef` is the source of truth for lifecycle transitions; `settledRef` guards the
  // terminal transition so it runs exactly once (idempotent across SSE-ended / reclaim / force-end).
  const sessionIdRef = useRef<string | null>(null)
  const statusRef = useRef<BuildSessionStatus | null>(null)
  const settledRef = useRef(false)
  const subRef = useRef<BuildFeedSubscription | null>(null)
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const renewRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const quietRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const setPhase = useCallback((next: BuildSessionStatus) => {
    statusRef.current = next
    setStatus(next)
  }, [])

  const teardownTimers = useCallback(() => {
    if (heartbeatRef.current !== null) {
      clearInterval(heartbeatRef.current)
      heartbeatRef.current = null
    }
    if (renewRef.current !== null) {
      clearInterval(renewRef.current)
      renewRef.current = null
    }
    if (quietRef.current !== null) {
      clearTimeout(quietRef.current)
      quietRef.current = null
    }
  }, [])

  const closeFeed = useCallback(() => {
    subRef.current?.close()
    subRef.current = null
  }, [])

  /** The single terminal transition. Idempotent: only the FIRST caller (SSE ended / reclaim / force-end / stop) wins. */
  const finishSession = useCallback(
    (terminal: BuildSessionStatus, opts: { reclaimed?: boolean } = {}) => {
      if (settledRef.current) return
      settledRef.current = true
      teardownTimers()
      closeFeed()
      setPhase(terminal)
      setIterating(false)
      setStopping(false)
      if (opts.reclaimed) setReclaimed(true)
    },
    [teardownTimers, closeFeed, setPhase],
  )

  /** Any keep-alive rejection means we can no longer prove the session is ours — fail closed, reclaim. */
  const reclaim = useCallback(() => {
    finishSession('ended', { reclaimed: true })
  }, [finishSession])

  const startKeepAlive = useCallback(
    (sid: string) => {
      teardownTimers() // defensive: never leak a prior generation's intervals if calls overlap
      // `void` + an attached `.catch` on each tick keeps these off the floating-promise list
      // (`.claude/rules/fail-first-typescript.md`). FENCE on `sid`: clearing the interval on reset
      // cannot cancel a tick whose promise is already in flight, and a stale tick from a session
      // we have since replaced must NOT reclaim the NEW session — so only reclaim when this tick
      // still owns the live session.
      heartbeatRef.current = setInterval(() => {
        void client.heartbeat(sid).catch(() => {
          if (sessionIdRef.current === sid) reclaim()
        })
      }, HEARTBEAT_CADENCE_SECONDS * 1000)
      renewRef.current = setInterval(() => {
        void client.renewLock(sid).catch(() => {
          if (sessionIdRef.current === sid) reclaim()
        })
      }, LOCK_RENEW_CADENCE_SECONDS * 1000)
    },
    [client, reclaim, teardownTimers],
  )

  const markIterating = useCallback(() => {
    if (statusRef.current !== 'ready') return
    setIterating(true)
    if (quietRef.current !== null) clearTimeout(quietRef.current)
    quietRef.current = setTimeout(() => setIterating(false), ITERATION_QUIET_MS)
  }, [])

  const onEnvelope = useCallback(
    (env: ProgressEnvelope) => {
      if (env.type === 'preview_ready') {
        // Routed to preview status ONLY — never a feed row (C7 §3.4). Don't override a terminal.
        setPreviewUrl(env.preview_url)
        if (statusRef.current !== 'ended' && statusRef.current !== 'failed') setPhase('ready')
        return
      }
      // Every other member is a feed row — upsert by seq (duplicate replaces, C3 §4.2).
      setEnvelopes((prev) => upsertBySeq(prev, env))

      if (env.type === 'quota_exceeded') {
        setQuota({ limit: env.limit, used: env.used, resetsAt: env.resets_at })
        return // graceful — the following `ended` (status:ended) resolves the terminal, not this
      }
      if (env.type === 'ended') {
        if (env.reason === 'quota_exceeded') {
          // Defensive: if the quota precursor was missed, still surface the quota banner. The
          // functional update keeps an already-set quota (never clobbers the real limit/used).
          setQuota((q) => q ?? { limit: 0, used: 0, resetsAt: '' })
        }
        finishSession(env.status === 'failed' ? 'failed' : 'ended')
        return
      }
      // step | log | error | escalation — advance provisioning→building; mark iteration if live.
      if (statusRef.current === 'provisioning') setPhase('building')
      markIterating()
    },
    [setPhase, finishSession, markIterating],
  )

  const onFeedError = useCallback(
    (_err: BuildFeedError) => {
      // The feed died (admission failure or bounded-reconnect exhaustion). Heartbeat/renew may
      // still be succeeding, so surface a DISTINCT feed-disconnected state — never let a dead feed
      // masquerade as a slow build (KTD-1). A terminal session ignores it.
      if (settledRef.current) return
      setFeedDisconnected(true)
    },
    [],
  )

  const subscribe = useCallback(
    (sid: string) => {
      closeFeed() // defensive: never leak a prior subscription if calls overlap
      setFeedDisconnected(false)
      subRef.current = subscribeBuildFeed(
        sid,
        { onEnvelope, onError: onFeedError, onOpen: () => setFeedDisconnected(false) },
        eventSourceFactory ? { eventSourceFactory } : {},
      )
    },
    [onEnvelope, onFeedError, eventSourceFactory, closeFeed],
  )

  const reset = useCallback(() => {
    teardownTimers()
    closeFeed()
    settledRef.current = false
    sessionIdRef.current = null
    statusRef.current = null
    setSessionId(null)
    setStatus(null)
    setPreviewUrl(null)
    setEnvelopes([])
    setIterating(false)
    setStopping(false)
    setBlocked(null)
    setReclaimed(false)
    setFeedDisconnected(false)
    setQuota(null)
    setError(null)
    setStartedAt(null)
  }, [teardownTimers, closeFeed])

  const start = useCallback(
    async (projectId: string, prompt: string): Promise<StartOutcome> => {
      reset()
      try {
        const session = await client.start({ projectId, prompt })
        settledRef.current = false
        sessionIdRef.current = session.sessionId
        setSessionId(session.sessionId)
        setPhase(session.status)
        setPreviewUrl(session.previewUrl)
        setStartedAt(Date.now())
        subscribe(session.sessionId)
        startKeepAlive(session.sessionId)
        return { kind: 'started', sessionId: session.sessionId }
      } catch (e) {
        if (e instanceof BuildSessionAlreadyActiveError) {
          setBlocked({ existingSessionId: e.existingSessionId })
          return { kind: 'blocked', existingSessionId: e.existingSessionId }
        }
        const message = e instanceof ApiError ? e.message : 'Could not start the build.'
        setError(message)
        return { kind: 'error', message }
      }
    },
    [client, reset, setPhase, subscribe, startKeepAlive],
  )

  const reattach = useCallback(
    async (sid: string): Promise<void> => {
      reset()
      // Seed from the authoritative status (C3 §2.3) — this is what frames a `preview_ready` that
      // fired before we connected (KTD-1). May throw; U5 handles (falls back to the block banner).
      const st = await client.getStatus(sid)
      settledRef.current = false
      sessionIdRef.current = sid
      setSessionId(sid)
      setPhase(st.status)
      setPreviewUrl(st.previewUrl)
      // Elapsed-time is measured from the session's TRUE start (createdAt), not the moment of
      // reattach — a reload onto a 12-minute-old build must not report it as 0s (force-end confirm).
      const createdMs = Date.parse(st.createdAt)
      setStartedAt(Number.isFinite(createdMs) ? createdMs : Date.now())
      if (st.status === 'ended' || st.status === 'failed') {
        settledRef.current = true // already terminal — nothing to subscribe to
        return
      }
      subscribe(sid)
      startKeepAlive(sid)
    },
    [client, reset, setPhase, subscribe, startKeepAlive],
  )

  const stop = useCallback(async (): Promise<void> => {
    const sid = sessionIdRef.current
    if (!sid || settledRef.current) return
    setStopping(true)
    try {
      await client.stop(sid, {})
      finishSession('ended')
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not stop the build.')
      setStopping(false)
    }
  }, [client, finishSession])

  const forceEnd = useCallback(
    async (targetSessionId?: string): Promise<void> => {
      const own = sessionIdRef.current
      const sid = targetSessionId ?? own
      if (!sid) return
      if (sid === own && settledRef.current) return // own session already terminal — no redundant call
      try {
        const res = await client.forceEnd(sid)
        if (sid === own) {
          // The kill switch's terminal comes from the CONTROL-PLANE response, overriding any
          // envelope-derived status (a stuck build may never emit `ended`) — C3 §3.4.
          finishSession(res.status)
        } else {
          // Force-ended the OTHER (blocking) session from the 409 banner — clear the block so the
          // user can start again.
          setBlocked(null)
        }
      } catch (e) {
        // 403 build_session_forbidden (non-owner) is surfaced fail-closed, never swallowed.
        setError(e instanceof ApiError ? e.message : 'Could not force-end the build.')
      }
    },
    [client, finishSession],
  )

  const reconnect = useCallback(() => {
    const sid = sessionIdRef.current
    if (!sid || settledRef.current) return
    closeFeed()
    subscribe(sid)
  }, [closeFeed, subscribe])

  const clearBlocked = useCallback(() => setBlocked(null), [])

  // Own timer + feed teardown on unmount — no leaked intervals, no zombie SSE.
  useEffect(() => () => {
    teardownTimers()
    closeFeed()
  }, [teardownTimers, closeFeed])

  return {
    sessionId,
    status,
    previewUrl,
    envelopes,
    iterating,
    stopping,
    blocked,
    reclaimed,
    feedDisconnected,
    quota,
    error,
    startedAt,
    start,
    reattach,
    stop,
    forceEnd,
    reconnect,
    reset,
    clearBlocked,
  }
}
