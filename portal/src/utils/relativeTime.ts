/**
 * "2 days ago" — a pure date formatter, with no module it drags along.
 *
 * It lived in `chatHistory.ts`, which is not a formatting module: importing anything from
 * there runs `createConversationStore('plan')` at module scope. That is invisible until
 * something outside chat wants a timestamp — the projects list (#158) — and then a project
 * row cannot render without a chat store existing, which is both wrong and the kind of
 * coupling that only shows up as a confusing test failure.
 *
 * So the function moved here. It was briefly re-exported from `chatHistory` for continuity,
 * but #175 retired that module's chat list along with the re-export — this docblock outlived
 * it by a round and claimed a link that no longer existed (round-4 review). The one caller
 * today is the projects list's "Details updated" column, importing it directly.
 */

/** A short relative time: `just now`, `5m ago`, `3h ago`, `12d ago`. */
export function relativeTime(isoString: string): string {
  const diff = Date.now() - new Date(isoString).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}
