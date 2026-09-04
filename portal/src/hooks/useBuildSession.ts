/**
 * The single owner of a build session's lifecycle: it RE-ATTACHES to a C3 session, stops or
 * force-ends it, subscribes to its C7 SSE feed and derives the `BuildSessionStatus`. Every cockpit
 * surface (LivePreview, the conversation surface's banners) reads from here.
 *
 * IT NO LONGER STARTS ONE, AND IT NO LONGER RELAUNCHES ONE. `start()` went with the client wrapper
 * it called: a composer send is a TURN, and the build lives inside the turn's own transaction.
 * `relaunch()` went because its only caller was wired to `LivePreview`'s `onRelaunch`, a prop the
 * pane accepts and never reads — the live restore path is `relaunchPreview` called directly by
 * `StartAppControl` / `RailComposer`. Both took the `blocked` state with them: it had two producers
 * (each function's own 409) and no reachable one, so the banner it fed and that banner's force-end
 * button are gone too. `reattach` is the surviving entry point.
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
 *  - **There is no `reclaimed` flag, and this paragraph used to describe one.** It was raised by
 *    the blind keep-alive loop's failure arm — a renew `409 lock_lost`, a heartbeat `404` — and
 *    U13 deleted that loop, taking the only producer with it. The state, the banner it fed and
 *    the attention dot it lit all survived it, unreachable, which reads as coverage for the
 *    frozen-tab case rather than the absence it was. What actually covers that case now is the
 *    preview poll's `asleep` state in `LivePreview`, which has a live producer.
 *  - **Feed-disconnected** (KTD-1): a bounded `EventSource` reconnect exhaustion (or an admission
 *    failure) raises a distinct `feedDisconnected` flag with a manual `reconnect()` — heartbeat /
 *    renew may still be succeeding, so nothing else signals the dead feed.
 *
 * `buildLock` is NOT consulted here — its `blockedBy` pre-check lives at the composer (U5); the
 * authoritative barrier is the server's 409 (KTD-7). This hook no longer surfaces that 409 at
 * all: `blocked` went with the `start` client above, so the two callers that can still provoke
 * one — `relaunchPreview` and the turn stream — each render it in their own surface's words.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../utils/apiError'
import { buildSessionClient } from '../utils/buildSessionApi'
import type { BuildSessionClient } from '../utils/buildSessionApi'
import { subscribeBuildFeed } from '../utils/buildSessionEvents'
import type { BuildFeedError, BuildFeedSubscription, EventSourceFactory } from '../utils/buildSessionEvents'
import type { BuildSessionStatus, FeedEnvelope, ProgressEnvelope } from '../utils/buildSessionTypes'

/** How long a live `ready` preview may go quiet before the "still working" overlay clears (KTD-8b). */
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
   * live, and null when the terminal arrived without a reason (reclaim, force-end, reattach onto
   * an already-ended session). 'completed' is the one the preview pane cares about (#13/R2): the
   * server PARDONS a completed build's container (it stays up under an idle lease), so
   * `ended` + 'completed' + `previewUrl` means "done, preview live" — not "no longer running".
   */
  endReason: string | null
  envelopes: FeedEnvelope[]
  /** True while a LIVE `ready` preview keeps receiving step/log activity (drives the overlay). */
  iterating: boolean
  /** A graceful stop is in flight — the Stop control shows a pending state until terminal. */
  stopping: boolean
  feedDisconnected: boolean
  /**
   * F8/U5 — the dev-server PROCESS crashed after the preview was framed (a `preview_reconnecting`
   * envelope). DISTINCT from `feedDisconnected` (the SSE feed dropping): this is the app's own dev
   * process dying, so the pane shows a "reconnecting" visual over the dead frame — never the
   * "building" spinner. Cleared by the next `preview_ready` (the re-frame). Not a 6th
   * `BuildSessionStatus` — the C3 enum stays frozen at five.
   */
  reconnecting: boolean
  quota: QuotaState | null
  error: string | null
  /** ms epoch the current session started, for elapsed-time display in the force-end confirm. */
  startedAt: number | null
  reattach: (sessionId: string) => Promise<void>
  /** Graceful stop. Resolves `false` when the stop FAILED and the session is still live (the caller must not start over it). */
  stop: () => Promise<boolean>
  /**
   * The owner-only kill switch (C3 §3.4), and it has no control on any surface since the block
   * banner's Force-end button went. Kept because it is the only thing that can settle a session
   * stuck mid-`building` that never emits a terminal `ended`, which is the whole reason the lock op
   * exists; retiring the portal's client for it belongs to the stop lineage, not to this sweep.
   */
  forceEnd: (targetSessionId?: string) => Promise<void>
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
  const [stopping, setStopping] = useState(false)
  const [feedDisconnected, setFeedDisconnected] = useState(false)
  const [reconnecting, setReconnecting] = useState(false)
  const [quota, setQuota] = useState<QuotaState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [startedAt, setStartedAt] = useState<number | null>(null)

  // Refs mirror the state that async callbacks (timers, SSE handlers) must read WITHOUT a stale
  // closure. `statusRef` is the source of truth for lifecycle transitions; `settledRef` guards the
  // terminal transition so it runs exactly once (idempotent across SSE-ended / reclaim / force-end).
  // `mountedRef` guards `start`/`reattach`: if the component unmounts WHILE their network call is in
  // flight, the unmount cleanup already ran, so wiring up an SSE feed + keep-alive timers afterwards
  // would leak them (a zombie heartbeat holds the one-per-user lock). Bail instead — a server session
  // with no heartbeat is reaped by TTL, far cheaper than a zombie.
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

  /** The single terminal transition. Idempotent: only the FIRST caller (SSE ended / reclaim / force-end / stop) wins — including its `reason`, so a late duplicate can never repaint WHY. */
  const finishSession = useCallback(
    (terminal: BuildSessionStatus, opts: { reason?: string } = {}) => {
      if (settledRef.current) return
      settledRef.current = true
      teardownTimers()
      closeFeed()
      setPhase(terminal)
      setEndReason(opts.reason ?? null)
      setIterating(false)
      setStopping(false)
      setFeedDisconnected(false) // a terminal session clears any lingering "Lost the feed" banner + dead Reconnect
    },
    [teardownTimers, closeFeed, setPhase],
  )

  /**
   * DELETED IN U13 — the blind keep-alive loop, and it is worth recording why rather than just
   * why-not, because it was live code and not dead code.
   *
   * It ran `heartbeat` and `renewLock` on a bare `setInterval` for as long as the tab existed,
   * with no interaction gate of any kind. That made AN OPEN TAB a keep-alive writer: a browser
   * left on a project overnight renewed the lock and the heartbeat until morning, and the
   * container behind it could never be reclaimed by anything. R13 names the writers that may
   * extend a sandbox's deadline and this is not one of them — tab visibility, a framed preview
   * and an open connection deliberately do NOT extend.
   *
   * Nothing replaces it here, and nothing needs to. A turn in flight is covered server-side by
   * the R10 wall-clock lease (U12), which outranks every other writer and is legible to the
   * sweep in another process — which this loop never was. Save / stop / relaunch / deploy each
   * already call a project-scoped endpoint, so a builder acting extends the deadline as a side
   * effect of the request they were making anyway. And an app actually being used reports
   * itself (R14).
   *
   * A builder who reads for thirty-five minutes without acting does lose the container, and
   * gets it back on their next prompt behind the labelled wait R16 guarantees. That is a
   * bounded, designed-for cost, taken deliberately against the unbounded one of a signal that
   * trickles in while nobody is working.
   */

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
        setReconnecting(false) // a fresh frame (re-frame after a crash) clears the reconnecting state
        if (statusRef.current !== 'ended' && statusRef.current !== 'failed') setPhase('ready')
        return
      }
      if (env.type === 'preview_reconnecting') {
        // F8/U5 — the dev-server PROCESS crashed after framing. A distinct preview signal, NOT a
        // feed row and NOT the "building" spinner; the following `preview_ready` clears it. Kept
        // even past a completed-build terminal so LivePreview can BOUND it (never a forever spinner).
        setReconnecting(true)
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
    setEndReason(null)
    setEnvelopes([])
    setIterating(false)
    setStopping(false)
    setFeedDisconnected(false)
    setReconnecting(false)
    setQuota(null)
    setError(null)
    setStartedAt(null)
  }, [teardownTimers, closeFeed])

  const reattach = useCallback(
    async (sid: string): Promise<void> => {
      reset()
      // Seed from the authoritative status (C3 §2.3) — this is what frames a `preview_ready` that
      // fired before we connected (KTD-1). May throw; U5 handles (falls back to the block banner).
      const st = await client.getStatus(sid)
      // Unmounted mid-flight: same as start() — don't wire a feed/timers the cleanup can't reach (FIX 1).
      if (!mountedRef.current) return
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
    },
    [client, reset, setPhase, subscribe],
  )

  const stop = useCallback(async (): Promise<boolean> => {
    const sid = sessionIdRef.current
    if (!sid || settledRef.current) return true // nothing live to stop — safe to proceed
    setStopping(true)
    try {
      await client.stop(sid, {})
      // The reason is set locally (the SSE feed closes with the settle): a user stop TEARS the
      // container down server-side, so this must never read as the pardoned 'completed' state.
      finishSession('ended', { reason: 'stopped_by_user' })
      return true
    } catch (e) {
      if (settledRef.current) return true // a concurrent SSE-ended / reclaim already finished it — don't paint a stale error
      setError(e instanceof ApiError ? e.message : 'Could not stop the build.')
      setStopping(false)
      return false // the session is STILL LIVE — a caller must not start over it (finding #19)
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
        // The kill switch's terminal comes from the CONTROL-PLANE response, overriding any
        // envelope-derived status (a stuck build may never emit `ended`) — C3 §3.4. A caller may
        // still name ANOTHER session by id, in which case there is nothing local to settle.
        if (sid === own) finishSession(res.status)
      } catch (e) {
        if (sid === own && settledRef.current) return // our own session was concurrently settled — no stale error
        // 403 build_session_forbidden (non-owner) is surfaced fail-closed, never swallowed. A failed
        // force-end of ANOTHER session still surfaces (only the own-session case is guarded above).
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
    // Reseed preview/status from the authoritative getStatus (mirrors reattach, KTD-1): the
    // feed was dead for a while and the fresh EventSource on a LIVE session starts
    // live-from-now, so a `preview_ready` (or a status hop) that fired during the gap would
    // otherwise be lost. Best-effort — a failed reseed leaves the resubscribed live stream.
    // RESIDUAL (finding #18): the missed feed ROWS need a backend narrative-backfill
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

  // Own timer + feed teardown on unmount — no leaked intervals, no zombie SSE. `mountedRef` also
  // trips here so an in-flight start()/reattach() bails instead of wiring resources we can't reach.
  // Re-set `true` on (re)mount: StrictMode double-invokes this effect (mount→cleanup→remount), so a
  // cleanup-only `false` would strand start()/reattach() as permanently-unmounted after the remount.
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
    stopping,
    feedDisconnected,
    reconnecting,
    quota,
    error,
    startedAt,
    reattach,
    stop,
    forceEnd,
    reconnect,
    reset,
  }
}
