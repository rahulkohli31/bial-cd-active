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

/** `28 Aug` — one end of the tile's range. */
function dayMonth(iso: string | null | undefined): string {
  const at = parse(iso)
  return at === null ? NONE : `${at.getDate()} ${MONTHS[at.getMonth()]}`
}

/** `28 Aug → 15 Sep` — the tile's foot form, both dates in the space the list gives one. */
export function tileDateRange(
  created: string | null | undefined,
  updated: string | null | undefined,
): string {
  return `${dayMonth(created)} → ${dayMonth(updated)}`
}
