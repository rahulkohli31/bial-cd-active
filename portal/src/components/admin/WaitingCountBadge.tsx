/**
 * The waiting-count badge on the admin nav entry — how many apps sit in the review queue.
 *
 * ACCESSIBILITY: the visible numeral is `aria-hidden`; the real accessible name is the
 * visually-hidden sentence beside it, so the count is announced once, with its meaning, not
 * twice without it.
 *
 * ZERO AND `null` BOTH RENDER NOTHING: an empty queue has nothing to say (a "0" badge would
 * train an administrator to ignore this pixel), and an unknown count must never claim a number.
 */

interface Props {
  /** The pending count, or `null` when it is unknown (not yet fetched, or the fetch failed). */
  count: number | null
  /** The testid suffix for this mount (`nav`). */
  where: string
  /**
   * A dot instead of a numeral, for the collapsed navigation rail.
   *
   * The number has no room there, and the ALTERNATIVE — drawing nothing — is what this guards
   * against: an administrator who leaves the pointer away from the navigation would lose every
   * trace that a queue is waiting. The announced sentence is unchanged, so the count is still
   * read out in full; only the pixels shrink.
   */
  compact?: boolean
}

/** The accessible sentence. Singular is not pedantry — "1 apps waiting" is the kind of
 *  thing that makes a person trust the rest of the screen slightly less. */
// Module-local: the badge's own sr-only label below is the only caller.
function waitingForReviewLabel(count: number): string {
  return `${count} ${count === 1 ? 'app' : 'apps'} waiting for review`
}

export default function WaitingCountBadge({ count, where, compact = false }: Props) {
  if (count === null || count <= 0) return null
  return (
    <span
      data-testid={`waiting-count-${where}`}
      // `relative` contains the sr-only sentence: sr-only is position:absolute, so
      // without a positioned ancestor it would anchor to the page and drag the badge's
      // layout with it (the same trap `ToolActivityLine` documents).
      className={
        compact
          ? 'relative block h-2 w-2 rounded-full bg-danger ring-2 ring-white'
          : 'relative inline-flex items-center justify-center min-w-[1.25rem] h-5 px-1.5 rounded-full bg-danger text-white text-[10px] font-bold leading-none'
      }
    >
      {!compact && <span aria-hidden="true">{count}</span>}
      <span className="sr-only">{waitingForReviewLabel(count)}</span>
    </span>
  )
}
