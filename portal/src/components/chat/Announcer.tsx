/**
 * THE POLITE ACTIVITY REGION.
 *
 * THREE CHANNELS, each with a distinct job, no new dependency: `Announcer` is polite and
 * INVISIBLE (agent started working, a group sealed); `TurnBanner` is polite and VISIBLE (one
 * value, newest wins — the app's current state); `SessionBanners` is ASSERTIVE (what genuinely
 * interrupts: refusals, blocks, failures). `assertive` is reserved to `SessionBanners` alone,
 * despite older plan text calling `TurnBanner` "the assertive slot".
 *
 * WHY THIS EXISTS: `sonner` (recommended by the component research) renders exactly ONE live
 * region for everything, with no way to make any toast assertive — it could never carry this
 * region's urgent half, and the job didn't need it anyway: this surface's two toasts were the
 * SAME VALUE (`usePendingAttachments`'s `attachToast`, one hook, one timer) rendered twice in
 * two corners with two different a11y treatments — one composer rendering it once is the
 * whole consolidation.
 *
 * THE RULE THAT ACTUALLY BREAKS: a live region must exist in the DOM, empty, before its text
 * arrives, or it is frequently never announced — why this mounts unconditionally with an empty
 * span rather than conditionally, and WRAPS rather than duplicates (a second `sr-only` copy of
 * an on-screen sentence reads twice to the DOM; broke three tests the first time it shipped).
 *
 * TWO ANNOUNCEMENTS, NO MORE: the agent started working, and what a group amounted to when it
 * sealed — not every step. The old mirror throttled to one change per ten seconds with a flush
 * branch; that solved the wrong problem, so this hook removes the problem instead of tuning it.
 *
 * NOT A DUPLICATE OF ANYTHING THE LIBRARY SHIPS: the installed `@assistant-ui/react` and
 * `@assistant-ui/core` source trees carry no `aria-live` region at all, in a primitive or in the
 * component registry — this region is filling a genuine gap, not shadowing one already there.
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
 * What the activity region should currently be saying.
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
