/**
 * The waiting-count badge — how many things sit in a review queue. It exists as ONE component,
 * not a span per mount, because it now appears in three places (the admin nav entry, the app
 * registry's Pending tab, and the Integrations tab) which must never disagree about the number
 * or how it's announced.
 *
 * ACCESSIBILITY: the visible numeral is `aria-hidden`; the real accessible name is the
 * visually-hidden sentence beside it, so the count is announced once, with its meaning, not
 * twice without it.
 *
 * WHAT IS REUSED HERE IS THE ZERO HANDLING, NOT THE WORDS. The two app mounts announce "N apps
 * waiting for review"; the connector queue counts PEOPLE asking for data access, and announcing
 * three of them as three apps would be a sentence that is simply untrue on a governance screen.
 * So `subject` selects the sentence and DEFAULTS to the app wording — neither existing mount
 * changes, and neither call site had to be touched to keep working.
 *
 * ZERO AND `null` BOTH RENDER NOTHING: an empty queue has nothing to say (a "0" badge would
 * train an administrator to ignore this pixel), and an unknown count must never claim a number.
 */

/** Which queue is being counted, which is which sentence gets announced. */
export type WaitingSubject = 'apps' | 'people'

interface Props {
  /** The pending count, or `null` when it is unknown (not yet fetched, or the fetch failed). */
  count: number | null
  /** Distinguishes the mounts in the DOM (`nav`, `tab`, `integrations-tab`) — one testid each. */
  where: string
  /** The sentence to announce. Omitted means the app review queue's, so the two older mounts
   *  keep the exact words they shipped with. */
  subject?: WaitingSubject
}

/** The accessible sentences. Singular is not pedantry — "1 apps waiting" is the kind of
 *  thing that makes a person trust the rest of the screen slightly less. */
// Module-local now that the bell that called it is gone, its only outside caller. Still
// used by the badge's own sr-only label below, so it stays a function — it just stops
// advertising itself as part of this module's surface.
function waitingForReviewLabel(count: number): string {
  return `${count} ${count === 1 ? 'app' : 'apps'} waiting for review`
}

function waitingForAccessLabel(count: number): string {
  return `${count} ${count === 1 ? 'person' : 'people'} waiting for access`
}

export default function WaitingCountBadge({ count, where, subject = 'apps' }: Props) {
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
      <span className="sr-only">
        {subject === 'people' ? waitingForAccessLabel(count) : waitingForReviewLabel(count)}
      </span>
    </span>
  )
}
