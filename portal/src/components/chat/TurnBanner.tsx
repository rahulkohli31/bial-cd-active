/**
 * The one banner slot above the composer (U7 / R13).
 *
 * WHY A SLOT AND NOT A LIST. This plan gives the platform five things it may need to say to a
 * citizen — their app was recovered, it could not be, the workspace could not be checked, the
 * change did not come together, today's allowance is used up — and every one of them arrives at
 * the same moment in the same place. Rendered as a list they would stack, and a stack of platform
 * sentences above the composer is a wall of text nobody reads, in which the one that still
 * matters is indistinguishable from the three that have been overtaken.
 *
 * At most one is on screen, and the newest wins, because each of these describes the state the
 * app is in NOW: an older sentence about the same app is not additional information, it is a
 * contradiction. The component takes ONE value, so "newest wins" is a property of the type rather
 * than a rule a call site has to remember.
 *
 * ABOVE THE COMPOSER, NOT IN THE TRANSCRIPT. Every one of these sentences ends in something the
 * citizen is being asked to do, and a message that scrolls away takes its own next action with
 * it. The transcript is the record of the conversation; this is the state of the app.
 *
 * ANNOUNCED POLITELY, never assertively. These are endings with an action attached, not alarms —
 * `assertive` is reserved on this page for the two things that genuinely interrupt (a failed
 * relaunch, a failed save), and spending it here would make those stop cutting through.
 */
interface TurnBannerProps {
  /** The sentence to show, or `null` for nothing. One value: newest wins by construction. */
  text: string | null
}

export default function TurnBanner({ text }: TurnBannerProps) {
  if (!text) return null
  return (
    <div
      data-testid="turn-banner"
      role="status"
      aria-live="polite"
      className="text-[11px] text-danger bg-danger/5 border border-danger/20 rounded-lg px-2.5 py-1.5"
    >
      {text}
    </div>
  )
}
