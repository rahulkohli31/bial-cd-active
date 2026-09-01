/**
 * THE POLITE ACTIVITY REGION (R65, R66).
 *
 * ══ THREE CHANNELS, EACH WITH A DISTINCT JOB, AND NO NEW DEPENDENCY ══
 *
 *   `Announcer`      — polite and INVISIBLE. Activity: the agent started working, a group sealed.
 *   `TurnBanner`     — polite and VISIBLE. One value, newest wins: the state the app is in now.
 *   `SessionBanners` — ASSERTIVE. The things that genuinely interrupt: refusals, blocks, failures.
 *
 * Route urgent to the assertive slot; route incidental and activity to the polite pair. The plan
 * called `TurnBanner` "the assertive slot" in several places and that was simply wrong —
 * `TurnBanner`'s own docblock records that `assertive` is deliberately reserved, and only
 * `SessionBanners` uses it.
 *
 * ══ WHY `sonner` IS NOT ADOPTED, THOUGH THE COMPONENT RESEARCH RECOMMENDED IT ══
 *
 * It renders exactly ONE live region for everything (`aria-live="polite"`, `aria-atomic="false"`;
 * `ToasterProps` exposes only `containerAriaLabel`) with no way to make any toast assertive — not
 * even `toast.error()`. So it could never have carried R65's urgent half. And the job turns out not
 * to need it: this surface's two toasts were the SAME VALUE — `usePendingAttachments`'s
 * `attachToast`, one hook with one timer — rendered twice in two corners with two different a11y
 * treatments. One composer rendering it once is the whole consolidation.
 *
 * ══ THE RULE THAT ACTUALLY BREAKS ══
 *
 * A live region must ALREADY EXIST IN THE DOM, EMPTY, before its text arrives. A region injected
 * together with its text is frequently not announced at all. This portal states that rule in two
 * of its own files (`TurnBanner`, `LivePreview`) and it is the reason this component is mounted
 * unconditionally and renders an empty span rather than being conditionally rendered.
 *
 * It also WRAPS rather than duplicates: a second `sr-only` copy of a sentence already on screen is
 * that sentence rendered twice to anything reading the DOM, and writing it that way broke three
 * existing tests the first time.
 *
 * ══ R66 IS TWO ANNOUNCEMENTS AND NO MORE ══
 *
 * The agent started working, and what a group amounted to when it sealed. NOT every step as it
 * happens. The old sr-only mirror throttled to one change per ten seconds with a flush branch —
 * that throttle was solving the wrong problem, and R66 removes the problem rather than tuning it.
 */
import { useEffect, useRef, useState, type FC } from 'react'

export interface AnnouncerProps {
  /** The sentence to announce. `null` leaves the region present and empty. */
  message: string | null
}

/**
 * The region itself. Permanently mounted; the TEXT is what changes.
 *
 * `role="status"` rather than `role="log"`: `status` announces the current state and replaces,
 * which is what a single-value region wants. `aria-atomic` makes the whole sentence read rather
 * than only the words that changed — the reasoning `BuildProgress` recorded before it was deleted,
 * carried forward.
 */
const Announcer: FC<AnnouncerProps> = ({ message }) => (
  <span
    role="status"
    aria-live="polite"
    aria-atomic="true"
    data-testid="activity-announcer"
    className="sr-only"
  >
    {message ?? ''}
  </span>
)

export default Announcer

/**
 * What the activity region should currently be saying (R66's two announcements).
 *
 * Kept as a hook beside the region so the "two announcements and no more" rule is one piece of
 * code rather than a discipline spread across call sites. It deliberately does NOT announce each
 * step: `sealedCount` changing from `null` to a number is one event, and a turn starting is one
 * event.
 */
export function useActivityAnnouncement({
  isRunning,
  sealedSummary,
}: {
  isRunning: boolean
  /** What the newest sealed group amounted to, or `null` while nothing has sealed. */
  sealedSummary: string | null
}): string | null {
  const [message, setMessage] = useState<string | null>(null)
  const wasRunning = useRef(false)
  const lastSealed = useRef<string | null>(null)

  useEffect(() => {
    if (isRunning && !wasRunning.current) setMessage('Working on your app.')
    wasRunning.current = isRunning
  }, [isRunning])

  useEffect(() => {
    if (sealedSummary && sealedSummary !== lastSealed.current) setMessage(sealedSummary)
    lastSealed.current = sealedSummary
  }, [sealedSummary])

  return message
}
