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
 * ADDRESSES IN THESE SENTENCES ARE CLICKABLE. One of the five ends in "ask <someone>" and names
 * a configured support address; printed as plain text that is a string the citizen has to select
 * and retype, which is not a next action — it is homework. `withMailtoLinks` turns it into a real
 * anchor and leaves every other sentence byte-identical, so the slot stays a plain-text slot for
 * the four that carry no address.
 *
 * THE LINKIFYING LIVES HERE rather than in the copy, deliberately: `AT_LIMIT_TEXT` is written
 * server-side and also reaches surfaces with no DOM at all, so a `mailto:` URI embedded in the
 * sentence would be jargon printed mid-paragraph exactly where `copy.py` exists to prevent it.
 *
 * ANNOUNCED POLITELY, never assertively. These are endings with an action attached, not alarms —
 * `assertive` is reserved on this page for the two things that genuinely interrupt (a failed
 * relaunch, a failed save), and spending it here would make those stop cutting through. The
 * region is permanent and only its text changes, for the reason the render comment gives.
 */
import type { ReactElement } from 'react'

/** An email address, stopping before a trailing full stop — or the `mailto:` would carry the
 *  sentence's punctuation into the mailbox name. */
const AN_EMAIL_ADDRESS = /[^\s<>@]+@[^\s<>@.]+(?:\.[^\s<>@.]+)+/g

/**
 * The sentence with its support address turned into a real `mailto:` link.
 *
 * RE-HOMED FROM `BuildProgress.tsx` (Plan D U17 deleted it) TO ITS ONE REMAINING READER. It was
 * exported from there because the at-limit row and this banner rendered the same server sentence;
 * the row is gone with the card, so this slot is the only DOM the sentence reaches and there is
 * nothing left to share it with.
 *
 * THE ADDRESS ARRIVES AS TEXT, and that is a division of labour rather than an oversight. The
 * server owns the words, and a `mailto:` URI spelled out mid-sentence is precisely the register
 * `services/turns/copy.py` exists to keep out. Making it clickable is a rendering concern, so it
 * happens where there is a DOM to click.
 *
 * Returns an array of React nodes, never a string of markup: the sentence is server copy today,
 * but a renderer that interprets its input as HTML is one configuration change away from being an
 * injection sink, and there is nothing here that needs the risk.
 */
export function withMailtoLinks(text: string): (string | ReactElement)[] {
  const out: (string | ReactElement)[] = []
  let cursor = 0
  // `matchAll` starts a fresh iteration each call — the regex is module-level and `g`-flagged, so
  // reusing `exec` across calls would carry `lastIndex` between renders and drop links at random.
  for (const match of text.matchAll(AN_EMAIL_ADDRESS)) {
    const at = match.index
    if (at > cursor) out.push(text.slice(cursor, at))
    out.push(
      <a
        key={`${at}-${match[0]}`}
        href={`mailto:${match[0]}`}
        className="font-semibold underline underline-offset-2"
      >
        {match[0]}
      </a>,
    )
    cursor = at + match[0].length
  }
  if (cursor < text.length) out.push(text.slice(cursor))
  return out
}

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
          {withMailtoLinks(text)}
        </div>
      ) : null}
    </div>
  )
}
