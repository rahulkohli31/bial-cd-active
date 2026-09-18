import { MONTHS } from './monthNames'

/**
 * The two dates a list row and a tile show, formatted in ONE place so the two views cannot
 * describe one application differently.
 *
 * ABSOLUTE, NOT RELATIVE, AND THAT IS THE POINT OF THE COLUMNS. "2 days ago" is shorter to read
 * for one row and useless down a page: relative strings share no left edge and no width, so a
 * column of them cannot be scanned — which is the complaint the columns answer. The call sites
 * set them in tabular figures for the same reason.
 *
 * THE LIST CARRIES THE YEAR AND THE TILE DOES NOT. `Main.dc.html` draws `12 Aug 2026` in a 112px
 * column; `HomeTiles.dc.html` draws `28 Aug → 15 Sep` in a tile foot that also carries a status
 * chip. Two widths, two forms, one formatter each.
 *
 * The months come from `monthNames.ts` rather than from `Intl`, which spells September `Sept` in
 * exactly the locales BIAL's browsers are set to.
 */

/** An unparseable stamp renders as an em dash rather than `Invalid Date`. The wire has never
 *  sent one; a column that printed the words is worse than a column that admits it has none. */
function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? null : at
}

const NONE = '—'

/** `12 Aug 2026` — the list's column form. */
export function listDate(iso: string | null | undefined): string {
  const at = parse(iso)
  return at === null ? NONE : `${at.getDate()} ${MONTHS[at.getMonth()]} ${at.getFullYear()}`
}

/** `28 Aug` — one end of the tile's range, and the tile form of a single date. */
export function dayMonth(iso: string | null | undefined): string {
  const at = parse(iso)
  return at === null ? NONE : `${at.getDate()} ${MONTHS[at.getMonth()]}`
}

/**
 * `28 Aug → 15 Sep` — the tile's foot form, both dates in the space the list gives one.
 *
 * THE YEAR COMES BACK WHEN THE TWO DATES DO NOT SHARE ONE, and only then. The board's form drops
 * it because a tile has no room for it and both dates are usually the same year — but an
 * application made in November and touched the following September renders as `11 Nov → 14 Sep`,
 * which reads as an arrow pointing BACKWARDS in time. The short form is not merely terse there,
 * it is wrong, and it is wrong exactly on the oldest applications a person owns.
 */
export function tileDateRange(
  created: string | null | undefined,
  updated: string | null | undefined,
): string {
  const from = parse(created)
  const to = parse(updated)
  if (from !== null && to !== null && from.getFullYear() !== to.getFullYear()) {
    return `${listDate(created)} → ${listDate(updated)}`
  }
  return `${dayMonth(created)} → ${dayMonth(updated)}`
}

/** What the range MEANS, for the hover a tile has no room to print. The list says it in column
 *  headings; a tile has only the arrow, and an arrow does not say which end is which. */
export function tileDateTitle(
  created: string | null | undefined,
  updated: string | null | undefined,
): string {
  return `Created ${listDate(created)} · Details updated ${listDate(updated)}`
}

/**
 * `Today, 14:02` · `Yesterday, 09:41` · `1 Sep, 09:41` — the version list's form.
 *
 * RELATIVE FOR THE TWO DAYS THAT HAVE A NAME, ABSOLUTE AFTER THAT, which is the opposite of the
 * columns above and for the opposite reason. A version list is read top-down as a short stack of
 * moments in one working session, not scanned as a column: "Today, 14:02" answers *which of my
 * saves is this* in a way "17 Sep 2026, 14:02" does not, and there are never more than three
 * rows for the shared left edge to matter.
 *
 * THE TIME IS ALWAYS THERE, because two saves on one day is the ordinary case this list exists
 * for — a date alone could name both of them.
 *
 * Browser-local, like every other date the citizen reads here. The rollback dialog's title uses
 * this same function, so the row a person pressed and the question they are asked about it can
 * never describe one version differently.
 */
export function versionStamp(iso: string | null | undefined): string {
  const at = parse(iso)
  if (at === null) return NONE
  const clock = `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`
  const midnight = new Date()
  midnight.setHours(0, 0, 0, 0)
  const startOfYesterday = new Date(midnight)
  startOfYesterday.setDate(startOfYesterday.getDate() - 1)
  if (at >= midnight) return `Today, ${clock}`
  if (at >= startOfYesterday) return `Yesterday, ${clock}`
  return `${dayMonth(iso)}, ${clock}`
}
