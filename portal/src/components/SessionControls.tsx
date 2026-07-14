/**
 * The operator's start / stop / force-end controls over a build session, plus the four
 * per-user banners the session lifecycle can raise. Presentational — every decision is
 * driven by `useBuildSession`'s state; every action is one of its callbacks.
 *
 * State matrix (prevents double-starts / stale-enabled controls):
 *   - Start   — visible & enabled ONLY when no session is active (status null / terminal),
 *               and never while a quota lock or a 409 block is showing.
 *   - Stop    — visible only during provisioning / building / ready; shows a PENDING state
 *               until the session reaches a terminal status.
 *   - Force-end — same visibility; CONFIRMS first (it kills in-progress work) and surfaces
 *               the session's elapsed time.
 *
 * Banners (all `aria-live="assertive"` — the operator must not miss them):
 *   - block          — a 409 `build_session_already_active`; offers force-ending the holder.
 *   - reclaimed      — a keep-alive failure reclaimed the session (lock lost / heartbeat 404 /
 *                      generic drop); offers Start-again.
 *   - feed-disconnected — the SSE feed died and the bounded reconnect gave up; offers a manual
 *                      reconnect (heartbeat/renew may still be succeeding, so nothing else signals it).
 *   - quota          — the daily token cap was hit; Start stays disabled until it resets.
 */
import { Ban, Loader2, Play, RefreshCw, Square } from 'lucide-react'
import { useState } from 'react'
import type { BlockedState, QuotaState } from '../hooks/useBuildSession'
import type { BuildSessionStatus } from '../utils/buildSessionTypes'

export interface SessionControlsProps {
  status: BuildSessionStatus | null
  stopping: boolean
  blocked: BlockedState | null
  reclaimed: boolean
  feedDisconnected: boolean
  quota: QuotaState | null
  startedAt: number | null
  /** Optional — in the builder the composer's Send starts a build, so Start may be omitted. */
  onStart?: () => void
  onStop: () => void
  onForceEnd: (targetSessionId?: string) => void
  onReconnect: () => void
  /** Clear a terminal banner (reclaimed / quota / block) so the operator can start fresh. */
  onStartAgain: () => void
}

function isActive(status: BuildSessionStatus | null): boolean {
  return status === 'provisioning' || status === 'building' || status === 'ready'
}

function formatElapsed(startedAt: number | null): string {
  if (startedAt === null) return ''
  const secs = Math.max(0, Math.round((Date.now() - startedAt) / 1000))
  const m = Math.floor(secs / 60)
  const s = secs % 60
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}

function quotaMessage(quota: QuotaState): string {
  const cap = quota.limit > 0 ? `${quota.limit.toLocaleString('en-US')} tokens` : 'daily token limit'
  return `You've hit your daily limit of ${cap}. It resets at midnight IST.`
}

const BANNER_BASE = 'rounded-lg border px-3 py-2 text-xs'

export default function SessionControls({
  status,
  stopping,
  blocked,
  reclaimed,
  feedDisconnected,
  quota,
  startedAt,
  onStart,
  onStop,
  onForceEnd,
  onReconnect,
  onStartAgain,
}: SessionControlsProps) {
  const [confirmingForceEnd, setConfirmingForceEnd] = useState(false)
  const active = isActive(status)
  const startDisabled = active || quota !== null || blocked !== null

  return (
    <div className="space-y-2">
      {/* Controls row */}
      <div className="flex items-center gap-2">
        {onStart && !active && (
          <button
            type="button"
            onClick={onStart}
            disabled={startDisabled}
            className="inline-flex items-center gap-1.5 rounded-lg bg-secondary px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-secondary-600 disabled:opacity-40"
          >
            <Play size={12} /> Start build
          </button>
        )}

        {active && !confirmingForceEnd && (
          <>
            <button
              type="button"
              onClick={onStop}
              disabled={stopping}
              className="inline-flex items-center gap-1.5 rounded-lg border border-bial-border bg-white px-3 py-1.5 text-xs font-semibold text-tertiary transition hover:border-primary hover:text-primary disabled:opacity-50"
            >
              {stopping ? <Loader2 size={12} className="animate-spin" /> : <Square size={12} />}
              {stopping ? 'Stopping…' : 'Stop'}
            </button>
            <button
              type="button"
              onClick={() => setConfirmingForceEnd(true)}
              className="inline-flex items-center gap-1.5 rounded-lg border border-danger/30 bg-white px-3 py-1.5 text-xs font-semibold text-danger transition hover:bg-danger/5"
            >
              <Ban size={12} /> Force-end
            </button>
          </>
        )}

        {active && confirmingForceEnd && (
          <div className="flex items-center gap-2 rounded-lg border border-danger/30 bg-danger/5 px-2.5 py-1.5">
            <span className="text-xs text-danger">
              Force-end this build? It kills in-progress work{startedAt !== null ? ` (running ${formatElapsed(startedAt)})` : ''}.
            </span>
            <button
              type="button"
              onClick={() => {
                setConfirmingForceEnd(false)
                onForceEnd()
              }}
              className="rounded-md bg-danger px-2 py-1 text-[11px] font-semibold text-white transition hover:bg-danger/90"
            >
              Force-end
            </button>
            <button
              type="button"
              onClick={() => setConfirmingForceEnd(false)}
              className="rounded-md border border-bial-border px-2 py-1 text-[11px] font-semibold text-neutral transition hover:text-tertiary"
            >
              Cancel
            </button>
          </div>
        )}
      </div>

      {/* Block banner (409 build_session_already_active) */}
      {blocked && (
        <div role="alert" aria-live="assertive" className={`${BANNER_BASE} border-warning/30 bg-warning/10 text-tertiary`}>
          <p className="font-semibold">You already have a build running.</p>
          <p className="mt-0.5 text-neutral">Only one build runs at a time. Force-end it to start a new one, or switch to the chat that owns it.</p>
          <div className="mt-1.5 flex items-center gap-2">
            <button
              type="button"
              onClick={() => onForceEnd(blocked.existingSessionId ?? undefined)}
              className="rounded-md bg-danger px-2 py-1 text-[11px] font-semibold text-white transition hover:bg-danger/90"
            >
              Force-end it
            </button>
            <button
              type="button"
              onClick={onStartAgain}
              className="rounded-md border border-bial-border px-2 py-1 text-[11px] font-semibold text-neutral transition hover:text-tertiary"
            >
              Dismiss
            </button>
          </div>
        </div>
      )}

      {/* Reclaimed banner (keep-alive failure) */}
      {reclaimed && (
        <div role="alert" aria-live="assertive" className={`${BANNER_BASE} border-danger/20 bg-danger/5 text-tertiary`}>
          <p className="font-semibold text-danger">Your build session was reclaimed.</p>
          <p className="mt-0.5 text-neutral">We lost contact with the sandbox (it may have gone idle). Start again to pick up where you left off.</p>
          <button
            type="button"
            onClick={onStartAgain}
            className="mt-1.5 rounded-md bg-secondary px-2 py-1 text-[11px] font-semibold text-white transition hover:bg-secondary-600"
          >
            Start again
          </button>
        </div>
      )}

      {/* Feed-disconnected banner (bounded reconnect exhausted) */}
      {feedDisconnected && (
        <div role="alert" aria-live="assertive" className={`${BANNER_BASE} border-bial-border bg-bial-bg text-tertiary`}>
          <p className="font-semibold">Lost the build activity feed.</p>
          <p className="mt-0.5 text-neutral">The build may still be running — reconnect to resume the activity stream.</p>
          <button
            type="button"
            onClick={onReconnect}
            className="mt-1.5 inline-flex items-center gap-1.5 rounded-md border border-bial-border bg-white px-2 py-1 text-[11px] font-semibold text-primary transition hover:border-primary"
          >
            <RefreshCw size={11} /> Reconnect
          </button>
        </div>
      )}

      {/* Quota banner (daily token cap) */}
      {quota && (
        <div role="alert" aria-live="assertive" className={`${BANNER_BASE} border-warning/30 bg-warning/10 text-tertiary`}>
          <p className="font-semibold">{quotaMessage(quota)}</p>
          <p className="mt-0.5 text-neutral">Building is paused until your limit resets. Contact your administrator if you need a higher plan.</p>
        </div>
      )}
    </div>
  )
}
