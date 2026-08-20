/**
 * The marketplace client's REQUEST and PARSE contract (#145).
 *
 * Two things are worth pinning here that a component test cannot reach. First, the query
 * string: `q`, `cursor` and `limit` have to arrive as the server's own param names or the
 * catalog silently ignores the search and returns page one of everything — a failure that
 * looks like "search found nothing" rather than "search never ran". Second, the tolerant
 * parse: a description or a builder display name may legitimately be absent (#145 does not
 * generate descriptions), so those must come back as `null` rather than throwing away the
 * whole page.
 */
import { describe, it, expect, vi } from 'vitest'
import { listMarketplace } from '../marketplaceApi'

const deps = (fetchImpl: unknown) =>
  ({ fetchImpl, getToken: () => null, refresh: vi.fn() }) as never

// authFetch peeks a 403 body through res.clone(), so a faked Response must be cloneable.
const res = (init: Record<string, unknown>): Record<string, unknown> => ({
  ...init,
  clone: () => res(init),
})
const ok = (json: unknown) => res({ ok: true, status: 200, json: async () => json })

const ENTRY = {
  name: 'Baggage Belt Faults',
  description: 'Report baggage belt faults.',
  builderDisplayName: 'Priya Builder',
  url: 'https://pub-abc.example/',
}
const PAGE = { items: [ENTRY], nextCursor: 'c1', hasMore: true }

describe('listMarketplace request shape', () => {
  it('hits the catalog with no query string when unfiltered', async () => {
    const fetchImpl = vi.fn(async (_url: string, _init?: unknown) => ok(PAGE))

    await listMarketplace({}, deps(fetchImpl))

    expect(fetchImpl.mock.calls[0][0]).toBe('/api/marketplace')
  })

  it('sends q, cursor and limit under the names the server reads', async () => {
    // Mutation receipt: rename any of these params in `listMarketplace` (e.g. `q` -> `search`)
    // and this goes red. The server would otherwise ignore the unknown param and answer with
    // an unfiltered first page, which reads as "no matches" rather than as a broken request.
    const fetchImpl = vi.fn(async (_url: string, _init?: unknown) => ok(PAGE))

    await listMarketplace({ q: 'baggage', cursor: 'c9', limit: 10 }, deps(fetchImpl))

    const url = new URL(String(fetchImpl.mock.calls[0][0]), 'http://x')
    expect(url.pathname).toBe('/api/marketplace')
    expect(url.searchParams.get('q')).toBe('baggage')
    expect(url.searchParams.get('cursor')).toBe('c9')
    expect(url.searchParams.get('limit')).toBe('10')
  })

  it('omits an empty q rather than sending a blank filter', async () => {
    const fetchImpl = vi.fn(async (_url: string, _init?: unknown) => ok(PAGE))

    await listMarketplace({ q: '' }, deps(fetchImpl))

    expect(String(fetchImpl.mock.calls[0][0])).toBe('/api/marketplace')
  })
})

describe('listMarketplace parse contract', () => {
  it('carries all four catalog fields through', async () => {
    const page = await listMarketplace({}, deps(vi.fn(async () => ok(PAGE))))

    expect(page.items[0]).toEqual(ENTRY)
    expect(page.nextCursor).toBe('c1')
    expect(page.hasMore).toBe(true)
  })

  it('reads a missing description and builder name as null, not as a broken page', async () => {
    // Both are legitimately absent: #145 does not generate descriptions, and a user row may
    // carry no display name. Dropping the whole page over either would hide live apps.
    const sparse = { ...ENTRY, description: undefined, builderDisplayName: undefined }
    const page = await listMarketplace(
      {},
      deps(vi.fn(async () => ok({ items: [sparse], nextCursor: null, hasMore: false }))),
    )

    expect(page.items[0].description).toBeNull()
    expect(page.items[0].builderDisplayName).toBeNull()
    expect(page.items[0].url).toBe(ENTRY.url)
  })

  it('treats a malformed envelope as an empty page rather than throwing', async () => {
    const page = await listMarketplace({}, deps(vi.fn(async () => ok({ nope: true }))))

    expect(page.items).toEqual([])
    expect(page.hasMore).toBe(false)
  })

  it('refuses an entry with no url — the one field a listing cannot be useful without', async () => {
    const broken = { items: [{ ...ENTRY, url: '' }], nextCursor: null, hasMore: false }

    await expect(listMarketplace({}, deps(vi.fn(async () => ok(broken))))).rejects.toThrow(
      /could not read/i,
    )
  })

  it('surfaces a non-ok response as an ApiError', async () => {
    const fail = res({ ok: false, status: 500, json: async () => ({ error: { message: 'boom' } }) })
    const err = await listMarketplace({}, deps(vi.fn(async () => fail))).catch((e: Error) => e)

    expect(err).toBeInstanceOf(Error)
  })
})
