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
 * relaunch, a failed save), and spending it here would make those stop cutting through. The
 * region is permanent and only its text changes, for the reason the render comment gives.
 */
interface TurnBannerProps {
  /** The sentence to show, or `null` for nothing. One value: newest wins by construction. */
  text: string | null
}

export default function TurnBanner({ text }: TurnBannerProps) {
  // THE LIVE REGION WRAPS THE BOX AND IS ALWAYS MOUNTED; the box is what appears and disappears.
  // Inserting a region together with its text announces inconsistently — several reader and
  // browser combinations miss it entirely — so the element has to be in the accessibility tree
  // BEFORE the text arrives. The preview pane already learned this the hard way and keeps a
  // permanent region for the same reason.
  //
  // WRAPPING rather than a second `sr-only` copy, which is what this was first written as: two
  // elements carrying the same sentence is one sentence rendered twice as far as anything reading
  // the DOM is concerned, and it broke three existing tests that look the banner up by its text.
  // A duplicate is also a real hazard on its own — the next person to add a visual tweak has two
  // places to change and no reason to suspect the second.
  return (
    <div role="status" aria-live="polite">
      {text ? (
        <div
          data-testid="turn-banner"
          className="text-[11px] text-danger bg-danger/5 border border-danger/20 rounded-lg px-2.5 py-1.5"
        >
          {text}
        </div>
      ) : null}
    </div>
  )
}
