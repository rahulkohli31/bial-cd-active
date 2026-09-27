/**
 * "2 days ago" — a pure date formatter, with no module it drags along.
 *
 * WHY THIS EXISTS
 *
 * It lived in `chatHistory.ts`, which runs `createConversationStore('plan')` at module scope —
 * invisible until something outside chat (the projects list) wants a timestamp, and importing
 * it would spin up a chat store just to render a project row. The function moved here, briefly
 * re-exported from `chatHistory` until that module's chat list was retired along with it.
 *
 * TWO WORDINGS, ONE SET OF BUCKETS, and that is the whole reason `elapsed` is separate: the
 * projects list wants the abbreviated form in a dense column (`12d ago`), and the admin review
 * queue wants it spelled out, because "43 days ago" is the sentence that makes a backlog's depth
 * land. An admin surface once shipped its own copy of the bucketing, disagreeing at the edges
 * (`59m ago` here, `1 hour ago` there); one bucketer now.
 */

const MINUTE = 60
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

/**
 * How long ago `isoString` was, bucketed to the coarsest unit that still reads as a duration.
 * `null` means "less than a minute" — the one case both wordings spell as a phrase rather than
 * a count, so neither caller has to invent a zero.
 *
 * Never negative: a clock skew that puts a stored timestamp slightly in the future reads as
 * "just now" rather than as a count backwards.
 */
function elapsed(isoString: string): { count: number; unit: 'minute' | 'hour' | 'day' } | null {
  const secs = Math.max(0, Math.floor((Date.now() - new Date(isoString).getTime()) / 1000))
  if (secs < MINUTE) return null
  if (secs < HOUR) return { count: Math.floor(secs / MINUTE), unit: 'minute' }
  if (secs < DAY) return { count: Math.floor(secs / HOUR), unit: 'hour' }
  return { count: Math.floor(secs / DAY), unit: 'day' }
}

/** A short relative time for a dense column: `just now`, `5m ago`, `3h ago`, `12d ago`. */
export function relativeTime(isoString: string): string {
  const ago = elapsed(isoString)
  if (ago === null) return 'just now'
  return `${ago.count}${ago.unit[0]} ago`
}

/**
 * The same instant, spelled out and pluralised: `just now`, `1 minute ago`, `43 days ago`.
 *
 * NO NULL ARM, DELIBERATELY. Callers hand this a timestamp something else has already vouched
 * for. An age counted from a missing value would be fifty-odd years since the epoch — the same
 * "1/1/1970" lie in a different unit — so the question of whether an age exists at all stays
 * with the caller that can answer it.
 */
export function relativeTimeVerbose(isoString: string): string {
  const ago = elapsed(isoString)
  if (ago === null) return 'just now'
  return `${ago.count} ${ago.unit}${ago.count === 1 ? '' : 's'} ago`
}
