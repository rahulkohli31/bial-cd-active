/**
 * The waiting-count badge (U13/P1) — how many apps sit in the review queue.
 *
 * It appears in TWO places, which is the reason it is a component rather than two spans:
 * on the admin navigation entry, so a superadmin signing in sees the queue without
 * navigating into it, and mirrored on the panel's Pending tab. Those two must never
 * disagree about the number OR about how it is announced, and a duplicated span is one
 * copy-edit away from doing exactly that.
 *
 * ACCESSIBILITY. The visible text is a bare numeral, which a screen reader would read as
 * "seven" with no subject. The real accessible name is the visually-hidden sentence
 * beside it — "7 apps waiting for review" — and the numeral is `aria-hidden`, so the
 * count is announced once, with its meaning, rather than twice without it.
 *
 * ZERO RENDERS NOTHING. An empty queue has nothing to say, and a "0" badge would train
 * an administrator to ignore the exact pixel that is supposed to catch their eye.
 * `null` (we haven't asked, or the request failed) renders nothing for the same reason:
 * a badge must never claim a number it does not have.
 */

interface Props {
  /** The pending count, or `null` when it is unknown (not yet fetched, or the fetch failed). */
  count: number | null
  /** Distinguishes the two mounts in the DOM (`nav`, `tab`) — one testid each. */
  where: string
}

/** The accessible sentence. Singular is not pedantry — "1 apps waiting" is the kind of
 *  thing that makes a person trust the rest of the screen slightly less. */
export function waitingForReviewLabel(count: number): string {
  return `${count} ${count === 1 ? 'app' : 'apps'} waiting for review`
}

export default function WaitingCountBadge({ count, where }: Props) {
  if (count === null || count <= 0) return null
  return (
    <span
      data-testid={`waiting-count-${where}`}
      // `relative` contains the sr-only sentence: sr-only is position:absolute, so
      // without a positioned ancestor it would anchor to the page and drag the badge's
      // layout with it (the same trap `ToolActivityLine` documents).
      className="relative inline-flex items-center justify-center min-w-[1.25rem] h-5 px-1.5 rounded-full bg-danger text-white text-[10px] font-bold leading-none"
    >
      <span aria-hidden="true">{count}</span>
      <span className="sr-only">{waitingForReviewLabel(count)}</span>
    </span>
  )
}
