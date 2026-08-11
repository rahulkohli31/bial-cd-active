/**
 * The four per-user session lifecycle banners (U15) — relocated from the retired
 * SessionControls cockpit row to just above the composer, where the operator is
 * already looking when they need to act on one. Presentational: every decision is
 * `useBuildSession` state; every action is one of its callbacks.
 *
 * ASSERTIVE IS FOR THINGS THAT WENT WRONG. Three of these interrupt the operator
 * (`role="alert"` / `aria-live="assertive"`) because something is genuinely blocked or
 * broken. The fourth — a workspace that went to sleep — does NOT: it is ordinary
 * housekeeping with a one-click way back, and announcing it as an emergency taught
 * citizens that a platform behaving correctly was failing them (R17).
 *
 *   - block          — a 409 `build_session_already_active`; offers force-ending the holder.
 *   - reclaimed      — the workspace went to sleep; offers Start-again. POLITE + neutral.
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

      {/* Workspace-asleep banner (the keep-alive stopped getting an answer).
          RE-TONED, NOT RETIRED (R17). It used to be a red `role="alert"` /
          `aria-live="assertive"` "Your build session was reclaimed." — the platform reporting
          its own housekeeping as the citizen's emergency. A reclaimed container is a sleeping
          workspace whose work is on durable storage; the next prompt brings it back. So:
          neutral styling, `role="status"` / `polite`, copy about sleep rather than failure.
          The BUTTON stays, and that is not cosmetic — `reclaimed` LATCHES (the only
          `setReclaimed(false)` lives inside `reset()`, whose sole caller is this button), so
          removing it would leave the collapsed-panel attention dot lit forever with nothing on
          screen to dismiss. This banner's source is also `onKeepAliveSettled`, not the preview
          poll, so it is the only surface that condition has. */}
      {reclaimed && (
        <div role="status" aria-live="polite" className={`${BANNER_BASE} border-bial-border bg-bial-bg text-tertiary`}>
          <p className="font-semibold">Your workspace went to sleep.</p>
          <p className="mt-0.5 text-neutral">It stopped answering while it was idle. Your work is saved — start again and it comes back where you left it.</p>
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
