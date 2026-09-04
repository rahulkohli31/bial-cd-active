/**
 * The two per-user session lifecycle banners (U15) — relocated from the retired
 * SessionControls cockpit row to just above the composer, where the operator is
 * already looking when they need to act on one. Presentational: every decision is
 * `useBuildSession` state; every action is one of its callbacks.
 *
 * ASSERTIVE IS FOR THINGS THAT WENT WRONG. Both of these interrupt the operator
 * (`role="alert"` / `aria-live="assertive"`) because something is genuinely blocked or
 * broken.
 *
 * THERE WERE FOUR, AND TWO ARE GONE — each because its producer was, not because it was re-toned.
 *
 *   · "your workspace went to sleep" was raised only by the blind keep-alive loop U13 deleted, so
 *     nothing had passed `reclaimed: true` since. The R17 argument it carried — a reclaimed
 *     container is a sleeping workspace, not an emergency — lives on in `LivePreview`'s `asleep`
 *     state, which has a live producer in the preview poll.
 *   · "you already have a build running", with its Force-end button and its Dismiss, was raised by
 *     the session hook's `blocked` state. That had TWO producers — `start`'s 409 and `relaunch`'s —
 *     and neither was reachable: `start` lost its caller when the build moved inside the turn
 *     transaction, and `relaunch`'s one caller hung off `LivePreview`'s `onRelaunch`, a prop the
 *     pane accepts and never reads. The live 409 today comes off `relaunchPreview`, called directly
 *     by `StartAppControl`, and is reported as a workspace sentence in the pane instead.
 *
 * A banner nothing can raise is worse than a missing one: it reads as covered.
 *
 *   - feed-disconnected — the SSE feed died and the bounded reconnect gave up; offers a
 *                      manual reconnect (heartbeat/renew may still be succeeding, so
 *                      nothing else signals it).
 *   - quota          — the daily token cap was hit; building pauses until it resets.
 */
import { RefreshCw } from 'lucide-react'
import type { QuotaState } from '../../hooks/useBuildSession'
import { formatDailyLimitMessage } from '../../utils/buildSessionTypes'

export interface SessionBannersProps {
  feedDisconnected: boolean
  quota: QuotaState | null
  onReconnect: () => void
}

const BANNER_BASE = 'rounded-lg border px-3 py-2 text-xs'

export default function SessionBanners({
  feedDisconnected,
  quota,
  onReconnect,
}: SessionBannersProps) {
  return (
    <>
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
