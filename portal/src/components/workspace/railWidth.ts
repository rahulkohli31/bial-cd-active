/**
 * HOW FAR THE LEFT COLUMN MOVES — the board's four numbers, in one place.
 *
 * WHY THIS EXISTS: `ResizeBounds`, the artboard these implement, says a free divider produces
 * two failures nobody asks for — dragged to 120px the composer is unusable, dragged to 1100px
 * the app is a sliver and the preview is pointless. The stops ARE the design; a citizen who
 * wants full width already has a control for that.
 *
 *   360px  narrowest   below this the composer starts wrapping
 *   520px  opening      for every rail — the application, its chat list and a chat — so the app
 *                       pane keeps its place as the rail changes between them
 *   640px  widest       past this the app is too narrow to judge on a desktop artboard
 *
 * Remembered per person in `localStorage`, never per project ("a width is a preference about a
 * screen, not a property of an app"); once the citizen drags, every project opens there. The handle exists in exactly one width class (below
 * the stacking threshold there is no handle at all), so one key already covers "per width class".
 */
export const RAIL_MIN = 360
export const RAIL_MAX = 640
export const RAIL_DEFAULT = 520

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
 * Throw-wrapped: `localStorage` genuinely throws (Safari private mode, blocked site data), and a
 * preference nobody can save is better silently defaulted than a workspace that fails to render.
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
