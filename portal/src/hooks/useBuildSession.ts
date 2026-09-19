/**
 * Owns a build session's lifecycle: RE-ATTACHES to a session, subscribes to its SSE feed, and
 * derives `BuildSessionStatus`. Every cockpit surface (LivePreview, the conversation surface's
 * banners) reads from here.
 *
 * It no longer STARTS or RELAUNCHES a session — a composer send is a TURN and the build
 * lives inside the turn's transaction; the live restore path is `relaunchPreview`, called
 * directly by `StartAppControl`/`RailComposer`. `blocked` went with them (each function's
 * own 409, now unreachable here); `reattach` is the surviving entry point.
 *
 * WHY THIS EXISTS: no client keep-alive extends a sandbox's deadline — no timer, heartbeat,
 * or lock renewal reaches from here into the container. The server renews the lock on every
 * non-terminal progress envelope, so a build in flight renews as fast as it produces frames;
 * save/relaunch/deploy extend the lease as a side effect of the request they already
 * make. Reading without acting for the full lease window loses the container — a bounded,
 * deliberate cost recovered on the next prompt behind a labelled wait.
 *
 * Status derives from the envelope stream (`provisioning → building → ready`; terminal read
 * off `ended.status`, never `reason`). `reattach` seeds `previewUrl` from the status response,
 * so a `preview_ready` fired before connecting still frames the app. This hook SETTLES a session
 * but no longer ENDS one: the session-scoped stop and force-end were both retired with their
 * routes, so a terminal reaches here only as an `ended` envelope. There is no `reclaimed` state
 * either — the frozen-tab case is `LivePreview`'s own `asleep` poll state —
 * and `feedDisconnected` is a distinct, bounded reconnect-exhaustion flag with manual
 * `reconnect()`. `buildLock` is not consulted here: the composer pre-checks it; the 409 barrier
 * is server-side.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { buildSessionClient } from '../utils/buildSessionApi'
import type { BuildSessionClient } from '../utils/buildSessionApi'
import { subscribeBuildFeed } from '../utils/buildSessionEvents'
import type { BuildFeedError, BuildFeedSubscription, EventSourceFactory } from '../utils/buildSessionEvents'
import type { BuildSessionStatus, FeedEnvelope, ProgressEnvelope } from '../utils/buildSessionTypes'

/** How long a live `ready` preview may go quiet before the "still working" overlay clears. */
const ITERATION_QUIET_MS = 4000

/** The graceful-quota terminal surfaced to the banner ("resets at midnight IST"). */
export interface QuotaState {
  limit: number
  used: number
  resetsAt: string
}

export interface UseBuildSessionDeps {
  client?: BuildSessionClient
  eventSourceFactory?: EventSourceFactory
}

export interface UseBuildSessionResult {
  sessionId: string | null
  status: BuildSessionStatus | null
  previewUrl: string | null
  /**
   * WHY the session reached its terminal, from the `ended` envelope's `reason` (or the local
   * action that settled it): 'completed' | 'stopped_by_user' | 'quota_exceeded' | … — null while
   * live, and null when the terminal arrived without a reason (reclaim, reattach onto
   * an already-ended session). 'completed' is the one the preview pane cares about: the
   * server PARDONS a completed build's container (it stays up under an idle lease), so
   * `ended` + 'completed' + `previewUrl` means "done, preview live" — not "no longer running".
   */
  endReason: string | null
  envelopes: FeedEnvelope[]
  /** True while a LIVE `ready` preview keeps receiving step/log activity (drives the overlay). */
  iterating: boolean
  feedDisconnected: boolean
  /**
   * The dev-server PROCESS crashed after the preview was framed (a `preview_reconnecting`
   * envelope). DISTINCT from `feedDisconnected` (the SSE feed dropping): this is the app's own dev
   * process dying, so the pane shows a "reconnecting" visual over the dead frame — never the
   * "building" spinner. Cleared by the next `preview_ready` (the re-frame). Not a 6th
   * `BuildSessionStatus` — the enum stays frozen at five.
   */
  reconnecting: boolean
  quota: QuotaState | null
  /** ms epoch the current session started, read from its `createdAt` — for elapsed-time display. */
  startedAt: number | null
  reattach: (sessionId: string) => Promise<void>
  reconnect: () => void
  reset: () => void
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
  const [endReason, setEndReason] = useState<string | null>(null)
  const [envelopes, setEnvelopes] = useState<FeedEnvelope[]>([])
  const [iterating, setIterating] = useState(false)
  const [feedDisconnected, setFeedDisconnected] = useState(false)
  const [reconnecting, setReconnecting] = useState(false)
  const [quota, setQuota] = useState<QuotaState | null>(null)
  const [startedAt, setStartedAt] = useState<number | null>(null)

  // Refs mirror the state that async callbacks (timers, SSE handlers) must read WITHOUT a stale
  // closure. `statusRef` is the source of truth for lifecycle transitions; `settledRef` guards the
  // terminal transition so it runs exactly once (idempotent across SSE-ended / reclaim / stop).
  // `mountedRef` guards `reattach`: if the component unmounts WHILE its network call is in flight,
  // the unmount cleanup has already run, so a feed subscribed afterwards is never closed. Bail.
  const mountedRef = useRef(true)
  const sessionIdRef = useRef<string | null>(null)
  const statusRef = useRef<BuildSessionStatus | null>(null)
  const settledRef = useRef(false)
  const subRef = useRef<BuildFeedSubscription | null>(null)
  const quietRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const setPhase = useCallback((next: BuildSessionStatus) => {
    statusRef.current = next
    setStatus(next)
  }, [])

  const teardownTimers = useCallback(() => {
    if (quietRef.current !== null) {
      clearTimeout(quietRef.current)
      quietRef.current = null
    }
  }, [])

  const closeFeed = useCallback(() => {
    subRef.current?.close()
    subRef.current = null
  }, [])

  /** The single terminal transition. Idempotent: only the FIRST caller (SSE ended / reclaim) wins — including its `reason`, so a late duplicate can never repaint WHY. */
  const finishSession = useCallback(
    (terminal: BuildSessionStatus, opts: { reason?: string } = {}) => {
      if (settledRef.current) return
      settledRef.current = true
      teardownTimers()
      closeFeed()
      setPhase(terminal)
      setEndReason(opts.reason ?? null)
      setIterating(false)
      setFeedDisconnected(false) // a terminal session clears any lingering "Lost the feed" banner + dead Reconnect
    },
    [teardownTimers, closeFeed, setPhase],
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
        // Routed to preview status ONLY — never a feed row. Don't override a terminal.
        setPreviewUrl(env.preview_url)
        setReconnecting(false) // a fresh frame (re-frame after a crash) clears the reconnecting state
        if (statusRef.current !== 'ended' && statusRef.current !== 'failed') setPhase('ready')
        return
      }
      if (env.type === 'preview_reconnecting') {
        // A preview signal, not a feed row. Kept even past a completed-build terminal so
        // LivePreview can BOUND it (never a forever spinner).
        setReconnecting(true)
        return
      }
      // Every other member is a feed row — upsert by seq (duplicate replaces).
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
        finishSession(env.status === 'failed' ? 'failed' : 'ended', { reason: env.reason })
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
      // masquerade as a slow build. A terminal session ignores it.
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
    setEndReason(null)
    setEnvelopes([])
    setIterating(false)
    setFeedDisconnected(false)
    setReconnecting(false)
    setQuota(null)
    setStartedAt(null)
  }, [teardownTimers, closeFeed])

  const reattach = useCallback(
    async (sid: string): Promise<void> => {
      reset()
      // Seed from the authoritative status — this is what frames a `preview_ready` that
      // fired before we connected. May throw; the composer handles it (falls back to the block banner).
      const st = await client.getStatus(sid)
      // Unmounted mid-flight: don't subscribe a feed the unmount cleanup has already run past.
      if (!mountedRef.current) return
      settledRef.current = false
      sessionIdRef.current = sid
      setSessionId(sid)
      setPhase(st.status)
      setPreviewUrl(st.previewUrl)
      // Elapsed-time is measured from the session's TRUE start (createdAt), not the moment of
      // reattach — a reload onto a 12-minute-old build must not report it as 0s.
      const createdMs = Date.parse(st.createdAt)
      setStartedAt(Number.isFinite(createdMs) ? createdMs : Date.now())
      if (st.status === 'ended' || st.status === 'failed') {
        settledRef.current = true // already terminal — nothing to subscribe to
        return
      }
      subscribe(sid)
    },
    [client, reset, setPhase, subscribe],
  )

  const reconnect = useCallback(() => {
    const sid = sessionIdRef.current
    if (!sid || settledRef.current) return
    closeFeed()
    subscribe(sid)
    // Reseed preview/status from the authoritative getStatus (mirrors reattach): the
    // feed was dead for a while and the fresh EventSource on a LIVE session starts
    // live-from-now, so a `preview_ready` (or a status hop) that fired during the gap would
    // otherwise be lost. Best-effort — a failed reseed leaves the resubscribed live stream.
    // RESIDUAL: the missed feed ROWS need a backend narrative-backfill
    // endpoint to recover; that is deliberately not built here.
    void client.getStatus(sid).then(
      (st) => {
        if (sessionIdRef.current !== sid) return // a newer session replaced this one mid-flight
        setPreviewUrl(st.previewUrl)
        if (st.status === 'ended' || st.status === 'failed') {
          finishSession(st.status) // the session ended while the feed was dead — settle now
          return
        }
        if (!settledRef.current) setPhase(st.status)
      },
      () => {
        // Swallowed by design: the reseed is an enhancement over the resubscribed feed; its
        // failure modes (404 after eviction, network) surface through the feed's own error arm.
      },
    )
  }, [client, closeFeed, subscribe, setPhase, finishSession])

  // Own timer + feed teardown on unmount — no orphaned timeout, no zombie SSE. `mountedRef` also
  // trips here so an in-flight `reattach` bails instead of wiring resources the cleanup can't reach.
  // Re-set `true` on (re)mount: StrictMode double-invokes this effect (mount→cleanup→remount), so a
  // cleanup-only `false` would strand `reattach` as permanently-unmounted after the remount.
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      teardownTimers()
      closeFeed()
    }
  }, [teardownTimers, closeFeed])

  return {
    sessionId,
    status,
    previewUrl,
    endReason,
    envelopes,
    iterating,
    feedDisconnected,
    reconnecting,
    quota,
    startedAt,
    reattach,
    reconnect,
    reset,
  }
}
