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
 */
import { ApiError, isRecord, readApiError } from './apiError'
import { authFetch } from './api'
import type { AuthFetchDeps } from './api'

/** One published app as the catalog shows it — the four fields the server will return. */
export interface MarketplaceEntry {
  name: string
  description: string | null
  builderDisplayName: string | null
  url: string
}

/** A keyset page of catalog entries, matching the projects list envelope. */
export interface MarketplacePage {
  items: MarketplaceEntry[]
  nextCursor: string | null
  hasMore: boolean
}

export interface ListMarketplaceArgs {
  cursor?: string | null
  limit?: number
  q?: string | null
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asStringOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function toEntry(value: unknown): MarketplaceEntry {
  if (!isRecord(value) || typeof value.url !== 'string' || value.url === '') {
    // The URL is the one field an entry cannot be useful without — it is the whole point of
    // the listing, and an entry only exists server-side because a deployment has one.
    throw new ApiError('The server returned a marketplace entry we could not read.', 500)
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
    items: Array.isArray(doc.items) ? doc.items.map(toEntry) : [],
    nextCursor: asStringOrNull(doc.nextCursor),
    hasMore: doc.hasMore === true,
  }
}

/**
 * A page of the catalog, or the ranked matches for `q`.
 *
 * A SEARCH RESPONSE IS ONE PAGE: the server returns `nextCursor: null` and `hasMore: false`
 * when `q` is set, because keyset pagination continues from a row id and a relevance-ranked
 * result is not ordered by id. Callers should therefore hide "load more" while searching
 * rather than treating the absent cursor as the end of a longer list.
 */
export async function listMarketplace(
  args: ListMarketplaceArgs = {},
  deps: AuthFetchDeps = {},
): Promise<MarketplacePage> {
  const params = new URLSearchParams()
  if (args.cursor) params.set('cursor', args.cursor)
  if (args.limit !== undefined) params.set('limit', String(args.limit))
  if (args.q) params.set('q', args.q)
  const qs = params.toString()
  const res = await authFetch(`/api/marketplace${qs ? `?${qs}` : ''}`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load the marketplace')
  return toPage(await res.json())
}
