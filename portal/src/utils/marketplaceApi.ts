/**
 * The marketplace catalog client (#145).
 *
 * Mirrors `projectApi.ts` deliberately — same `authFetch` + `readApiError` + tolerant
 * `to*` parse shape — because the only thing that differs about this surface is WHOSE apps
 * come back, and that difference belongs on the server, not in a bespoke client.
 *
 * The parsers coerce rather than throw on a missing optional field: a catalog entry whose
 * builder has no display name, or whose app has no description, is a normal row here (#145
 * accepts that descriptions are not guaranteed), not a response we should refuse to render.
 * That tolerance extends to a whole row: this is the ONE list on the platform every user
 * shares, so one unparseable entry drops itself rather than blanking the catalog for the
 * entire org.
 */
import { isRecord, readApiError } from './apiError'
import { authFetch } from './api'
import type { AuthFetchDeps } from './api'

/** One published app as the catalog shows it — the four fields the server will return. */
export interface MarketplaceEntry {
  name: string
  description: string | null
  builderDisplayName: string | null
  url: string
}

/** How the catalog may be ordered while BROWSING. Ignored while searching, where relevance
 *  wins — see `listMarketplace`. */
export type MarketplaceSort = 'newest' | 'name'

/** The page sizes the rows-per-page control offers, and the default it starts on.
 *
 *  Exported from HERE rather than declared in the page so the component's initial state and
 *  `toPage`'s fallback are the same number by construction. They previously disagreed (the
 *  page defaulted to 10, the parser fell back to 25), which is only invisible because the
 *  fallback is unreachable while the server always sends `pageSize`. */
export const PAGE_SIZES = [10, 25, 50] as const
export const DEFAULT_PAGE_SIZE = PAGE_SIZES[0]

/**
 * An OFFSET page of catalog entries.
 *
 * Unlike every other list in this client, which is keyset (`nextCursor`/`hasMore`). The
 * marketplace is a read-only catalog of ~10-200 rows, and page NUMBERS, a total, and
 * sort-by-name are all impossible without offset — see the server's
 * `MarketplaceListResponse` docstring for the full argument.
 */
export interface MarketplacePage {
  items: MarketplaceEntry[]
  page: number
  pageSize: number
  total: number
  totalPages: number
}

export interface ListMarketplaceArgs {
  page?: number
  limit?: number
  q?: string | null
  sort?: MarketplaceSort
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asStringOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

/** Coerce a wire number, falling back rather than throwing: a malformed count should not
 *  blank a page of results the caller can otherwise render. */
function asNumber(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

/** One row, or `null` if it is unusable. NOT a throw: see `toPage`. */
function toEntry(value: unknown): MarketplaceEntry | null {
  if (!isRecord(value) || typeof value.url !== 'string' || value.url === '') {
    // The URL is the one field an entry cannot be useful without — it is the whole point of
    // the listing, and an entry only exists server-side because a deployment has one. A row
    // without one is dropped rather than rendered as a dead card.
    return null
  }
  return {
    name: asString(value.name),
    description: asStringOrNull(value.description),
    builderDisplayName: asStringOrNull(value.builderDisplayName),
    url: value.url,
  }
}

function toPage(value: unknown): MarketplacePage {
  const doc = isRecord(value) ? value : {}
  return {
    // DROP unparseable rows, never discard the page. This is the one list on the platform
    // shared by every user, so an all-or-nothing throw here has an org-wide blast radius:
    // a single malformed entry would blank the catalog for everyone rather than for the one
    // app that is broken. Matches this module's stated "coerce, don't throw" posture for
    // every other field.
    items: Array.isArray(doc.items)
      ? doc.items.flatMap((row) => {
          const entry = toEntry(row)
          if (entry === null) {
            // Dropping the row is the right trade — one bad entry must not blank an
            // org-wide catalog — but doing it SILENTLY makes `total` and the rendered
            // count disagree with no signal to anyone: "10 results / Page 1 of 3"
            // rendering 9 cards indefinitely, indistinguishable from a correct page
            // (#147 round 3). This does not turn it into a REPORTED failure — the portal
            // captures no console output, so in practice it helps whoever already has
            // devtools open. That is still the difference between a diagnosable page and a
            // mystery, but it is not telemetry, and the empty-state copy in
            // `MarketplacePage` is what covers the reader who never opens devtools.
            console.warn('marketplace: dropped an unreadable catalog entry', row)
            return []
          }
          return [entry]
        })
      : [],
    page: asNumber(doc.page, 1),
    pageSize: asNumber(doc.pageSize, DEFAULT_PAGE_SIZE),
    total: asNumber(doc.total, 0),
    // Never below 1: a control that renders "Page 1 of 0" for an empty catalog reads as
    // broken rather than empty.
    totalPages: Math.max(1, asNumber(doc.totalPages, 1)),
  }
}

/**
 * A page of the catalog, or the ranked matches for `q`.
 *
 * `sort` orders BROWSING only. With `q` set the server ranks by relevance regardless — a
 * search box that returned alphabetical matches instead of good ones is not a search box —
 * so a caller may leave the sort control visible while searching, but should not expect it
 * to change the order of results.
 */
export async function listMarketplace(
  args: ListMarketplaceArgs = {},
  deps: AuthFetchDeps = {},
): Promise<MarketplacePage> {
  const params = new URLSearchParams()
  // Page 1 is the server's default, so omitting it keeps the common URL clean.
  if (args.page !== undefined && args.page > 1) params.set('page', String(args.page))
  if (args.limit !== undefined) params.set('limit', String(args.limit))
  if (args.q) params.set('q', args.q)
  if (args.sort && args.sort !== 'newest') params.set('sort', args.sort)
  const qs = params.toString()
  const res = await authFetch(`/api/marketplace${qs ? `?${qs}` : ''}`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load the marketplace')
  return toPage(await res.json())
}
