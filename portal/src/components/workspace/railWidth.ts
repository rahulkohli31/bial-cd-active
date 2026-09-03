/**
 * HOW FAR THE LEFT COLUMN MOVES (plan 002, U7) — the board's four numbers, in one place.
 *
 * `ResizeBounds` is a whole artboard about this, and every number on it has a stated reason:
 *
 *   360px  narrowest   below this the composer and the status rows start wrapping
 *   400px  project     where it opens; fits the status rows without wrapping
 *   520px  a chat      where a chat opens — a conversation needs more width than a status panel
 *   640px  widest      past this the app is too narrow to judge on a desktop artboard
 *
 * WHY IT IS BOUNDED RATHER THAN FREE, in the board's own words: "a free divider produces two
 * failures nobody asks for — a column dragged to 120px where the composer is unusable, and a
 * column dragged to 1100px where the app is a sliver and the preview is pointless. The stops are
 * the design. Someone who wants the app at full width already has a control for it."
 *
 * WHAT IS REMEMBERED, AND WHAT IS NOT. Per person, in `localStorage`; NOT per project — "a width
 * is a preference about a screen, not a property of an app". The board says "per width class" as
 * well, and one stored value satisfies that here rather than dodging it: the handle exists in
 * exactly one width class, because below the stacking threshold there is no handle at all.
 *
 * The two OPENING widths are not the same number, and a remembered one replaces both: once the
 * citizen has dragged, "every project opens there", which is the board's own sentence.
 */
export const RAIL_MIN = 360
export const RAIL_MAX = 640
export const RAIL_DEFAULT_DETAILS = 400
export const RAIL_DEFAULT_CONVERSATION = 520

/** How far one arrow-key press moves the boundary. Ten CSS pixels: fine enough to land on a
 *  number the citizen means, coarse enough to cross the 280px range without wearing a key out. */
export const RAIL_KEY_STEP = 10

const KEY = 'bial:rail-width'

export function clampRailWidth(px: number): number {
  return Math.min(RAIL_MAX, Math.max(RAIL_MIN, Math.round(px)))
}

/**
 * The remembered width, or `null` when the citizen has never dragged one.
 *
 * `null` IS NOT A NUMBER TO SUBSTITUTE. It means "use the opening width for whichever rail this
 * is", and the two are different, so a caller that defaulted here would pick one of them for both.
 *
 * THROW-WRAPPED, because `localStorage` genuinely throws rather than degrading — Safari private
 * mode, and any browser with site data blocked. A preference nobody can save is a preference that
 * silently uses its default, which is a strictly better outcome than a workspace that will not
 * render.
 */
export function readRailWidth(): number | null {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (raw === null) return null
    const parsed = Number.parseInt(raw, 10)
    // A stored value from an older build, a hand-edited one, or a NaN all fall back to the
    // opening width rather than to a clamped guess — `null` says "we do not know", and inventing
    // 360 from a corrupt entry would silently narrow every workspace this person opens.
    return Number.isFinite(parsed) ? clampRailWidth(parsed) : null
  } catch {
    return null
  }
}

export function writeRailWidth(px: number): void {
  try {
    window.localStorage.setItem(KEY, String(clampRailWidth(px)))
  } catch {
    // Nothing to recover: the width is already applied in this session, and the only thing lost
    // is that it will not survive a reload.
  }
}

/** The width a rail opens at when nothing is remembered. */
export function openingWidth(mode: 'details' | 'conversation'): number {
  return mode === 'conversation' ? RAIL_DEFAULT_CONVERSATION : RAIL_DEFAULT_DETAILS
}
