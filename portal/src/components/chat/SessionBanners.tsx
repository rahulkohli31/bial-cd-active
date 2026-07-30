/**
 * The four per-user session lifecycle banners (U15) — relocated from the retired
 * SessionControls cockpit row to just above the composer, where the operator is
 * already looking when they need to act on one. Presentational: every decision is
 * `useBuildSession` state; every action is one of its callbacks. All banners are
 * `aria-live="assertive"` — the operator must not miss them.
 *
 *   - block          — a 409 `build_session_already_active`; offers force-ending the holder.
 *   - reclaimed      — a keep-alive failure reclaimed the session; offers Start-again.
 *   - feed-disconnected — the SSE feed died and the bounded reconnect gave up; offers a
 *                      manual reconnect (heartbeat/renew may still be succeeding, so
 *                      nothing else signals it).
 *   - quota          — the daily token cap was hit; building pauses until it resets.
 */
import { RefreshCw } from 'lucide-react'
import type { BlockedState, QuotaState } from '../../hooks/useBuildSession'
import { formatDailyLimitMessage } from '../../utils/buildSessionTypes'

export interface SessionBannersProps {
  blocked: BlockedState | null
  reclaimed: boolean
  feedDisconnected: boolean
  quota: QuotaState | null
  onForceEnd: (targetSessionId?: string) => void
  onReconnect: () => void
  /** Clear a terminal banner (reclaimed / quota / block) so the operator can start fresh. */
  onStartAgain: () => void
}

const BANNER_BASE = 'rounded-lg border px-3 py-2 text-xs'

export default function SessionBanners({
  blocked,
  reclaimed,
  feedDisconnected,
  quota,
  onForceEnd,
  onReconnect,
  onStartAgain,
}: SessionBannersProps) {
  return (
    <>
      {/* Block banner (409 build_session_already_active) */}
      {blocked && (
        <div role="alert" aria-live="assertive" className={`${BANNER_BASE} border-warning/30 bg-warning/10 text-tertiary`}>
          <p className="font-semibold">You already have a build running.</p>
          <p className="mt-0.5 text-neutral">
            {blocked.existingSessionId === null
              ? 'A previous session is being reclaimed — retry shortly.'
              : 'Only one build runs at a time. Force-end it to start a new one, or switch to the chat that owns it.'}
          </p>
          <div className="mt-1.5 flex items-center gap-2">
            {/* No target session id (a post-restart 409): a force-end would silently no-op, so
                the button is disabled until the server finishes reclaiming (finding #24). */}
            <button
              type="button"
              onClick={() => onForceEnd(blocked.existingSessionId ?? undefined)}
              disabled={blocked.existingSessionId === null}
              title={blocked.existingSessionId === null ? 'A previous session is being reclaimed — retry shortly.' : undefined}
              className="rounded-md bg-danger px-2 py-1 text-[11px] font-semibold text-white transition hover:bg-danger/90 disabled:opacity-40 disabled:cursor-not-allowed"
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
          <p className="font-semibold">{formatDailyLimitMessage(quota.limit, quota.used)}</p>
          <p className="mt-0.5 text-neutral">Building is paused until your limit resets. Contact your administrator if you need a higher plan.</p>
        </div>
      )}
    </>
  )
}
