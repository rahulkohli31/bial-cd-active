/**
 * How a citizen likes their application lists drawn — list or grid, and how dense the grid is.
 *
 * ONE SET OF KEYS FOR EVERY APPLICATION LIST, which is what makes the preference a habit rather
 * than a per-page setting: a person who puts their own applications in roomy tiles finds the
 * ones shared with them the same way. Two pages holding two copies of these strings is exactly
 * how the two would come to disagree.
 *
 * REMEMBERED IN `localStorage`, NOT THE URL, on purpose — these are a person's habit rather than
 * a place in a list, and a shared link should not reach into the reader's window and rearrange
 * it. Reads are wrapped because a private window or blocked site data throws on access.
 */

export type View = 'list' | 'grid'
export type Density = 'S' | 'M' | 'L'

/** The rows-per-page sizes the control offers, and the one a list opens on. Named once for every
 *  application list: a `?pageSize=` outside this set is what each list falls back from, so two
 *  copies would be two lists disagreeing about which links are honoured. */
export const PAGE_SIZES = [8, 16, 24, 48] as const
export const DEFAULT_PAGE_SIZE = PAGE_SIZES[0]

const VIEW_KEY = 'bial.projects.view'
const DENSITY_KEY = 'bial.projects.density'

function readStored<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const value = localStorage.getItem(key)
    return allowed.includes(value as T) ? (value as T) : fallback
  } catch {
    return fallback
  }
}

function store(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* a remembered preference is a convenience, never a requirement */
  }
}

export function readStoredView(): View {
  return readStored(VIEW_KEY, ['list', 'grid'] as const, 'list')
}

export function storeView(value: View): void {
  store(VIEW_KEY, value)
}

export function readStoredDensity(): Density {
  return readStored(DENSITY_KEY, ['S', 'M', 'L'] as const, 'M')
}

export function storeDensity(value: Density): void {
  store(DENSITY_KEY, value)
}

/** Grid columns per density. S is denser, L roomier — the mockup's S/M/L control. */
export const DENSITY_COLS: Record<Density, string> = {
  S: 'grid-cols-1 sm:grid-cols-3 lg:grid-cols-4',
  M: 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3',
  L: 'grid-cols-1 sm:grid-cols-2',
}

// shadcn's toggle marks its ON state with `bg-accent`, and this theme maps `--accent` to
// the brand ORANGE (#F5A623) — a solid orange pill in a teal interface. The component is
// right; its default theme mapping is not for this design, and no unit test can see which
// colour a class resolves to. Overridden at the call site rather than in `ui/toggle.tsx`,
// so the vendored component stays upstream-shaped for whoever uses it next.
export const TOGGLE_ACTIVE =
  ' data-[state=on]:bg-primary/10 data-[state=on]:text-primary data-[state=on]:ring-1 data-[state=on]:ring-primary/30'
