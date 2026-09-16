/**
 * What every connector surface draws a connector WITH — the teal tile, and the three date forms
 * the boards set a decision in. Shared by the Integrations page, the per-application settings
 * tab, the ask panel, the window chip and the administrator's queue.
 *
 * ONE FORMATTER PER SHAPE, so a single approval cannot be rendered two ways by the two screens
 * that both name it.
 */
import { Database } from 'lucide-react'
import { MONTHS } from '../../utils/monthNames'

/**
 * The connector's teal tile. Three sizes because the boards draw three: 44px on the Integrations
 * card, 26px in a list row, 24px beside the ask panel's title. Not a `size` number — Tailwind
 * cannot build a class from a runtime value, and the three the boards use are the three that
 * exist.
 */
const TILE = {
  card: { box: 'w-11 h-11 rounded-xl', icon: 22 },
  row: { box: 'w-[26px] h-[26px] rounded-lg', icon: 13 },
  title: { box: 'w-6 h-6 rounded-lg', icon: 12 },
} as const

export function ConnectorGlyph({
  size = 'row',
}: {
  size?: keyof typeof TILE
}): React.JSX.Element {
  const tile = TILE[size]
  return (
    <span
      aria-hidden
      className={`${tile.box} flex-shrink-0 bg-primary-50 border border-primary-100 inline-flex items-center justify-center`}
    >
      <Database size={tile.icon} strokeWidth={1.8} className="text-primary-dark" />
    </span>
  )
}

/**
 * RE-EXPORTED, defined in `utils/monthNames.ts` — see that module for why the months are spelled
 * out rather than left to `Intl`.
 *
 * `WindowChip.tsx` sets `1 – 30 Sep` on the date chip and reuses this LIST rather than `dayMonth`
 * below, and the difference matters: the functions here take an ISO INSTANT and read it in local
 * time, while a window's bounds are calendar DAYS (`2026-09-01`) that `new Date()` would parse as
 * UTC midnight — the day before, anywhere west of Greenwich. The chip splits its own strings on
 * the hyphen and comes back here only for the month's three letters.
 */
export { MONTHS }

/**
 * `2 Sep` — the board's form for a decision's date, which carries no year and no time.
 *
 * One formatter, so the Integrations card, the settings tab and the administrator's queue cannot
 * render one decision three ways.
 */
export function dayMonth(iso: string): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return iso
  return `${parsed.getDate()} ${MONTHS[parsed.getMonth()]}`
}

/**
 * `08:30` — the clock half on its own, local and zero-padded, or `null` for an instant that will
 * not parse.
 *
 * EXPORTED FOR THE ONE SENTENCE THAT SETS IT WITH A WORD RATHER THAN A COMMA: the decide
 * dialog's `Asked on 4 Sep at 09:12.` `dayMonthTime` composes it, so both forms move together.
 */
export function clockTime(iso: string): string | null {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return null
  const hh = String(parsed.getHours()).padStart(2, '0')
  const mm = String(parsed.getMinutes()).padStart(2, '0')
  return `${hh}:${mm}`
}

/**
 * `5 Sep, 08:30` — the form that DOES carry the time of day. The boards write it that way because
 * a request made twenty minutes ago and one made last Tuesday are a different kind of wait, and a
 * date alone flattens them. Twenty-four hour and zero-padded, as drawn.
 */
export function dayMonthTime(iso: string): string {
  const clock = clockTime(iso)
  return clock === null ? iso : `${dayMonth(iso)}, ${clock}`
}

/**
 * The board's ` · ` separator, with absent parts dropped rather than rendered as a gap.
 *
 * A NAME IS ALLOWED TO BE ABSENT, and this is the whole handling of it. The server already
 * substitutes the decider's email when their display name is unset, so `null` means there is no
 * decider to name — the administrator was deleted after deciding. `Approved for you 2 Sep` is the
 * true sentence in that case; `Approved for you 2 Sep · ` is a dangling separator, and looking up
 * an email here would invent a second answer to a question the server already answered.
 */
export function dotted(parts: readonly (string | null)[]): string {
  return parts.filter((part): part is string => part !== null && part !== '').join(' · ')
}
