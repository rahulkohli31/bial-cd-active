/**
 * The marketplace client's REQUEST and PARSE contract (#145).
 *
 * Two things are worth pinning here that a component test cannot reach. First, the query
 * string: `q`, `page`, `limit` and `sort` have to arrive as the server's own param names or
 * the catalog silently ignores the search and returns page one of everything — a failure
 * that looks like "search found nothing" rather than "search never ran". Second, the
 * tolerant parse: a description or a builder display name may legitimately be absent (#145
 * does not generate descriptions), so those must come back as `null` rather than throwing
 * away the whole page — and a row that is unusable outright drops itself rather than taking
 * the catalog down with it.
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
const PAGE = { items: [ENTRY], page: 1, pageSize: 10, total: 1, totalPages: 1 }

describe('listMarketplace request shape', () => {
  it('hits the catalog with no query string when unfiltered', async () => {
    const fetchImpl = vi.fn(async (_url: string, _init?: unknown) => ok(PAGE))

    await listMarketplace({}, deps(fetchImpl))

    expect(fetchImpl.mock.calls[0][0]).toBe('/api/marketplace')
  })

  it('sends q, page, limit and sort under the names the server reads', async () => {
    // Mutation receipt: rename any of these params in `listMarketplace` (e.g. `q` -> `search`)
    // and this goes red. The server would otherwise ignore the unknown param and answer with
    // an unfiltered first page, which reads as "no matches" rather than as a broken request.
    const fetchImpl = vi.fn(async (_url: string, _init?: unknown) => ok(PAGE))

    await listMarketplace({ q: 'baggage', page: 3, limit: 10, sort: 'name' }, deps(fetchImpl))

    const url = new URL(String(fetchImpl.mock.calls[0][0]), 'http://x')
    expect(url.pathname).toBe('/api/marketplace')
    expect(url.searchParams.get('q')).toBe('baggage')
    expect(url.searchParams.get('page')).toBe('3')
    expect(url.searchParams.get('limit')).toBe('10')
    expect(url.searchParams.get('sort')).toBe('name')
  })

  it('omits page 1 and the default sort, keeping the common URL clean', async () => {
    const fetchImpl = vi.fn(async (_url: string, _init?: unknown) => ok(PAGE))

    await listMarketplace({ page: 1, sort: 'newest' }, deps(fetchImpl))

    expect(String(fetchImpl.mock.calls[0][0])).toBe('/api/marketplace')
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
    expect(page.page).toBe(1)
    expect(page.pageSize).toBe(10)
    expect(page.total).toBe(1)
    expect(page.totalPages).toBe(1)
  })

  it('reads a missing description and builder name as null, not as a broken page', async () => {
    // Both are legitimately absent: #145 does not generate descriptions, and a user row may
    // carry no display name. Dropping the whole page over either would hide live apps.
    const sparse = { ...ENTRY, description: undefined, builderDisplayName: undefined }
    const page = await listMarketplace(
      {},
      deps(vi.fn(async () => ok({ ...PAGE, items: [sparse] }))),
    )

    expect(page.items[0].description).toBeNull()
    expect(page.items[0].builderDisplayName).toBeNull()
    expect(page.items[0].url).toBe(ENTRY.url)
  })

  it('treats a malformed envelope as an empty page rather than throwing', async () => {
    const page = await listMarketplace({}, deps(vi.fn(async () => ok({ nope: true }))))

    expect(page.items).toEqual([])
    // Never "Page 1 of 0" — an empty catalog is one empty page, not zero pages.
    expect(page.totalPages).toBe(1)
    expect(page.total).toBe(0)
  })

  it('drops an unparseable row rather than discarding the whole page', async () => {
    // The URL is the one field an entry cannot be useful without, so a row without one is
    // not renderable. But this is the ONE list every user on the platform shares, so an
    // all-or-nothing throw has an org-wide blast radius — one malformed entry would blank
    // the catalog for everyone instead of for the single app that is broken.
    const mixed = { ...PAGE, items: [{ ...ENTRY, url: '' }, ENTRY], total: 2 }
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    const page = await listMarketplace({}, deps(vi.fn(async () => ok(mixed))))

    expect(page.items).toEqual([ENTRY])

    // The drop must not be SILENT. Dropping the row is the right trade, but it leaves
    // `total` and the rendered count disagreeing, and without a signal that is
    // indistinguishable from a correct page. This was the one round-3 fix with no receipt
    // — deleting the `console.warn` left the whole suite green (#147 round 3 review).
    expect(warn).toHaveBeenCalledTimes(1)
    expect(warn.mock.calls[0][0]).toMatch(/dropped an unreadable catalog entry/)
    warn.mockRestore()
  })

  it('surfaces a non-ok response as an ApiError', async () => {
    const fail = res({ ok: false, status: 500, json: async () => ({ error: { message: 'boom' } }) })
    const err = await listMarketplace({}, deps(vi.fn(async () => fail))).catch((e: Error) => e)

    expect(err).toBeInstanceOf(Error)
  })
})
