/**
 * The marketplace — every app anyone on the platform has published (#145).
 *
 * The point of this page is that it is NOT scoped to you. Someone with a real need can see
 * what already exists instead of describing it into the builder and rebuilding a tool that
 * is already running, so the copy leans on "built by" rather than hiding authorship.
 *
 * IT DOES NOT USE `useKeysetList`, unlike `ProjectsPage`. That hook is cursor-shaped, and
 * this catalog paginates by OFFSET so it can offer page numbers, a total, and sort-by-name
 * (see the server's `MarketplaceListResponse` docstring for why the deviation is contained
 * to this one surface). The debounce that hook provided is kept here by hand — typing must
 * not fire a request per keystroke.
 *
 * ONE DISPATCHER, and it is the effect below. Every fetch this page makes comes from that
 * single `useEffect`, keyed on the COMMITTED state (`page`/`pageSize`/`sort`/`applied`).
 * The debounce commits `applied` and nothing else; it never calls the loader itself. That
 * is what makes the controls safe to interleave: an earlier design had the debounce firing
 * its own request with `pageSize`/`sort` captured at KEYSTROKE time, so changing rows-per-page
 * inside the 300ms window lost to a stale request that happened to be issued later and
 * therefore won the `requestId` guard — rendering rows fetched at the old page size while
 * the control read the new one.
 *
 * THE RULE THAT TIES THE CONTROLS TOGETHER: anything that changes what the result SET is —
 * a new query, a new page size, a new sort — resets to page 1. Without that you can be on
 * page 4 of a three-page result and see nothing, with no clue why.
 */
import { ExternalLink, Search, Store } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type React from 'react'

import Navbar from '../components/layout/Navbar'
import {
  Pagination,
  PaginationContent,
  PaginationEllipsis,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from '../components/ui/pagination'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '../components/ui/select'
import {
  DEFAULT_PAGE_SIZE,
  PAGE_SIZES,
  listMarketplace,
  type MarketplaceEntry,
  type MarketplacePage,
  type MarketplaceSort,
} from '../utils/marketplaceApi'

const DEBOUNCE_MS = 300
/** How many numbered buttons to show before collapsing to an ellipsis. */
const WINDOW = 5

const EMPTY: MarketplacePage = {
  items: [],
  page: 1,
  pageSize: DEFAULT_PAGE_SIZE,
  total: 0,
  totalPages: 1,
}

/**
 * The page numbers to render: all of them when there are few, otherwise a window around the
 * current page with the first and last always reachable. Returning `'gap'` rather than a
 * number keeps the decision here instead of in the JSX.
 */
function pageWindow(current: number, totalPages: number): (number | 'gap')[] {
  if (totalPages <= WINDOW) return Array.from({ length: totalPages }, (_, i) => i + 1)
  const start = Math.max(2, Math.min(current - 1, totalPages - 3))
  const end = Math.min(totalPages - 1, Math.max(current + 1, 4))
  const middle = Array.from({ length: end - start + 1 }, (_, i) => start + i)
  return [
    1,
    ...(start > 2 ? (['gap'] as const) : []),
    ...middle,
    ...(end < totalPages - 1 ? (['gap'] as const) : []),
    totalPages,
  ]
}

function EntryCard({ entry }: { entry: MarketplaceEntry }): React.JSX.Element {
  return (
    <div
      data-testid="marketplace-entry"
      className="bg-white border border-bial-border rounded-2xl p-5 flex flex-col gap-3"
    >
      <div className="flex flex-col gap-1">
        <h3 className="text-sm font-bold text-tertiary">{entry.name}</h3>
        {/* Authorship is the reason to trust the entry, and the person to ask about it.
            Display name only — never the builder's email or directory id (#145). */}
        {entry.builderDisplayName && (
          <p className="text-[11px] text-neutral">Built by {entry.builderDisplayName}</p>
        )}
      </div>

      {entry.description ? (
        <p className="text-xs text-neutral leading-relaxed">{entry.description}</p>
      ) : (
        // Descriptions are not guaranteed (#145 does not generate them). Say so plainly
        // rather than rendering an empty gap that reads as a broken card.
        <p className="text-xs text-neutral/60 italic">No description yet.</p>
      )}

      <a
        data-testid="marketplace-open"
        href={entry.url}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center gap-1.5 text-xs font-semibold text-primary hover:underline mt-auto"
      >
        <ExternalLink size={12} />
        Open app
      </a>
    </div>
  )
}

export default function MarketplacePage(): React.JSX.Element {
  const [query, setQuery] = useState('')
  // The COMMITTED filter text — what actually produced the rows on screen. Distinct from
  // `query`, which runs ahead by the debounce window. Never read inside the dispatcher's
  // dependencies as `query`, or a page click mid-keystroke would send text the user has not
  // finished typing.
  const [applied, setApplied] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE)
  const [sort, setSort] = useState<MarketplaceSort>('newest')
  const [data, setData] = useState<MarketplacePage>(EMPTY)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)

  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Guards against an out-of-order response overwriting a newer one: type fast enough and a
  // slow early request can land after the request that superseded it.
  const requestId = useRef(0)
  // Bumped to re-dispatch the SAME page — the retry path. `setPage(sameValue)` is a React
  // bail-out, so without this a failed page has no way back short of a reload.
  //
  // This replaces an earlier design that rolled `page` back to the last good value inside
  // `catch`. That looked like a fix and was a bug: `page` is a dispatcher dependency, so the
  // rollback immediately refetched the previous page, and on success cleared `error` — a
  // transient failure on "Next" silently reverted the navigation and dismissed its own error
  // banner before anyone could read it.
  const [reloadNonce, setReloadNonce] = useState(0)

  const load = useCallback(
    async (args: { page: number; pageSize: number; sort: MarketplaceSort; q: string }) => {
      const id = ++requestId.current
      setLoading(true)
      try {
        const body = await listMarketplace({
          page: args.page,
          limit: args.pageSize,
          sort: args.sort,
          q: args.q || undefined,
        })
        if (id !== requestId.current) return // superseded
        setData(body)
        setError(null)
      } catch (err) {
        if (id !== requestId.current) return
        setError(err instanceof Error ? err : new Error('Failed to load the marketplace'))
        // Deliberately does NOT touch `page`. The pagination reads its state from `data`
        // (see `shownPage`), so the highlight already matches the cards on screen without
        // mutating a dispatcher dependency — and the error banner stays up until something
        // actually succeeds.
      } finally {
        if (id === requestId.current) setLoading(false)
      }
    },
    [],
  )

  // THE ONLY PLACE A FETCH IS DISPATCHED. Everything else commits state and lets this run.
  useEffect(() => {
    void load({ page, pageSize, sort, q: applied })
  }, [page, pageSize, sort, applied, reloadNonce, load])

  const onQueryChange = (next: string): void => {
    setQuery(next)
    if (debounce.current !== null) clearTimeout(debounce.current)
    debounce.current = setTimeout(() => {
      debounce.current = null
      // Commit only. The dispatcher above notices `applied` changed and does the fetch, so
      // no control handler has to clear anyone else's pending timer to stay correct.
      setApplied(next)
      setPage(1) // a new query is a new result set
    }, DEBOUNCE_MS)
  }

  useEffect(
    () => () => {
      if (debounce.current !== null) clearTimeout(debounce.current)
    },
    [],
  )

  // Decided from the query that produced the CURRENT items, never the input value — the
  // input runs ahead by the debounce window, so using it would flash "nothing published
  // yet" at someone who has merely started typing.
  const searching = applied !== ''
  const { items, totalPages, total } = data
  // THE PAGE ON SCREEN, which is not always the page requested: a failed fetch leaves `page`
  // at the value that failed while `data` still holds the last success. Driving the control
  // from `data` keeps the highlight and the Prev/Next boundaries honest about what the reader
  // is actually looking at, and removes any reason to mutate `page` on failure.
  const shownPage = data.page

  const goTo = (next: number): void => {
    const target = Math.min(Math.max(1, next), totalPages)
    // Same page re-requested (the retry after a failure): `setPage` would be a no-op, so
    // nudge the nonce instead and let the one dispatcher run again.
    if (target === page) setReloadNonce((n) => n + 1)
    else setPage(target)
  }

  // A page past the end is a NORMAL response, not an error — the server says so, and it
  // happens whenever the catalog shrinks under a reader who is deep in it (an admin
  // unpublishing a few apps is enough). Left alone it is a dead end: `items` is empty, so
  // there is nothing to page from, and if the catalog has shrunk below one page the whole
  // control unmounts, stranding `page` at a number nothing can reach. Snap back to the last
  // real page instead. Runs at most once — after it fires, `page <= totalPages` holds.
  useEffect(() => {
    if (!loading && !error && items.length === 0 && total > 0 && page > totalPages) {
      setPage(totalPages)
    }
  }, [loading, error, items.length, total, page, totalPages])

  // The two controls appear on DIFFERENT conditions, deliberately. Page numbers are
  // meaningless at one page. But gating rows-per-page on the same condition would be a trap:
  // 30 apps at 50 rows is a single page, so the control that would take you back to 10 would
  // be hidden exactly when you wanted it. It shows whenever the catalog is larger than the
  // smallest size on offer.
  //
  // Neither is gated on `!loading` any more: doing so unmounted the whole control on every
  // fetch, so the button under the pointer vanished mid-click, keyboard focus dropped to
  // <body>, and the layout shifted. They stay mounted and are marked `aria-busy` instead.
  const showSizer = total > PAGE_SIZES[0]
  const showPages = totalPages > 1

  return (
    // Same shell as ProjectsPage: each page renders its own `Navbar` (there is no layout
    // route), and the gradient ground is the platform's, not this page's.
    <div
      className="min-h-screen font-manrope flex flex-col"
      style={{ background: 'linear-gradient(160deg, #ffffff 0%, #f0f9f9 100%)' }}
    >
      <Navbar />

      <main className="flex-1 max-w-5xl mx-auto w-full px-6 py-10 flex flex-col gap-6">
        <header className="flex flex-col gap-2">
          <h1 className="text-2xl font-bold text-tertiary flex items-center gap-2">
            <Store size={22} />
            Marketplace
          </h1>
          <p className="text-sm text-neutral">
            Every app published across BIAL. Search before you build — someone may have made
            it already.
          </p>
        </header>

        <div className="flex flex-col sm:flex-row gap-3 sm:items-center">
          <div className="relative flex-1">
            <Search
              size={15}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral pointer-events-none"
            />
            <input
              data-testid="marketplace-search"
              type="search"
              value={query}
              onChange={(e) => onQueryChange(e.target.value)}
              placeholder="Search what apps do…"
              aria-label="Search published apps"
              className="w-full pl-9 pr-3 py-2.5 text-sm bg-white border border-bial-border rounded-xl focus:outline-none focus:ring-2 focus:ring-primary/30"
            />
          </div>

          {/* A <label> cannot wrap a non-labelable element, and Radix's trigger is a
              <button> — so the visible "Sort by" text no longer associates for free and the
              trigger carries its own accessible name. */}
          <div className="flex items-center gap-2 text-xs text-neutral whitespace-nowrap">
            Sort by
            <Select
              value={sort}
              onValueChange={(value: string) => {
                // Total over the two-member union rather than `as MarketplaceSort`: adding a
                // third sort server-side becomes a compile-time decision here instead of a
                // silent runtime fallback to 'newest'.
                setSort(value === 'name' ? 'name' : 'newest')
                setPage(1) // a new order is a new result set
              }}
            >
              {/* Width pinned: a Radix trigger is content-sized and would otherwise jitter
                  between "Newest first" and "Name (A–Z)". */}
              <SelectTrigger
                data-testid="marketplace-sort"
                aria-label="Sort by"
                className="w-[150px]"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="newest">Newest first</SelectItem>
                <SelectItem value="name">Name (A–Z)</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        {/* Said plainly while searching: the sort control stays usable, but relevance wins,
            so a user who picks A–Z mid-search is not left wondering why nothing moved. */}
        {searching && sort === 'name' && (
          <p className="text-xs text-neutral/70 -mt-3">
            Search results are ordered by relevance; sorting applies when you clear the
            search.
          </p>
        )}

        {/* The retry lives HERE, not on the pagination nav, and that is the whole point.
            `reloadNonce` was previously only reachable through `goTo` — but on a failed
            FIRST load `data` is still the EMPTY sentinel, so `showSizer`/`showPages` are
            both false, the nav never mounts, and the reader is left with a bare banner and
            no way forward short of reloading the browser (#147 round 3). Bound to `error`
            alone, it is present in exactly the states that need it. */}
        {error && (
          <div role="alert" className="flex flex-col items-start gap-2 text-sm text-danger">
            <p>{error.message}</p>
            <button
              type="button"
              data-testid="marketplace-retry"
              onClick={() => setReloadNonce((n) => n + 1)}
              className="rounded-xl border border-bial-border bg-white px-3 py-1.5 text-xs font-semibold text-tertiary transition hover:bg-bial-bg focus:outline-none focus:ring-2 focus:ring-primary/30"
            >
              Try again
            </button>
          </div>
        )}

        {items.length > 0 && (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {items.map((entry, i) => (
              // Index-qualified: the server now guarantees one row per app, but a future
              // server-side duplicate should degrade to a visible duplicate card rather than
              // a React reconciliation hazard.
              <EntryCard key={`${entry.url}-${i}`} entry={entry} />
            ))}
          </div>
        )}

        {/* `!error` matters: on a failed first load `loading` is false and `items` is empty,
            so without it this renders "Nothing has been published yet" directly beneath the
            error banner — telling the user the catalog is empty when we do not know. */}
        {!loading && !error && items.length === 0 && (
          <p data-testid="marketplace-empty" className="text-sm text-neutral py-10 text-center">
            {/* Branching on `page > totalPages` rather than `total !== 0`: `total` and the
                rows are two separate reads (the accepted READ COMMITTED risk), so an
                unpublish landing between them can return zero items on page 1 with a stale
                non-zero `total` — and "taking you back" on page 1 has nowhere to go. */}
            {searching
              ? 'No published app matches that yet.'
              : page > totalPages
                ? 'That page is past the end of the catalog — taking you back.'
                : 'Nothing has been published yet. The first app to go live shows up here.'}
          </p>
        )}

        {loading && <p className="text-sm text-neutral py-4 text-center">Loading…</p>}

        {(showSizer || showPages) && (
          <div className="flex flex-col sm:flex-row items-center justify-between gap-4 pt-2">
            {showSizer && (
              <div className="flex items-center gap-2 text-xs text-neutral whitespace-nowrap">
                Rows per page
                <Select
                  value={String(pageSize)}
                  onValueChange={(value: string) => {
                    // Radix values are strings only, so this coerces at both ends.
                    setPageSize(Number(value))
                    setPage(1) // a new page size renumbers every page
                  }}
                >
                  <SelectTrigger
                    data-testid="marketplace-page-size"
                    aria-label="Rows per page"
                    className="w-[72px] py-1.5"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {PAGE_SIZES.map((size) => (
                      <SelectItem key={size} value={String(size)}>
                        {size}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}

            {/* `sm:ml-auto` keeps the nav right-aligned when the sizer is absent. */}
            {showPages && (
              <Pagination className="mx-0 w-auto justify-end sm:ml-auto" aria-busy={loading}>
                <PaginationContent>
                  <PaginationItem>
                    <PaginationPrevious
                      data-testid="marketplace-prev"
                      disabled={shownPage <= 1}
                      onClick={() => goTo(shownPage - 1)}
                    />
                  </PaginationItem>

                  {pageWindow(shownPage, totalPages).map((entry, i) =>
                    entry === 'gap' ? (
                      <PaginationItem key={`gap-${i}`}>
                        <PaginationEllipsis />
                      </PaginationItem>
                    ) : (
                      <PaginationItem key={entry}>
                        <PaginationLink
                          data-testid={`marketplace-page-${entry}`}
                          isActive={entry === shownPage}
                          onClick={() => goTo(entry)}
                        >
                          {entry}
                        </PaginationLink>
                      </PaginationItem>
                    ),
                  )}

                  <PaginationItem>
                    <PaginationNext
                      data-testid="marketplace-next"
                      disabled={shownPage >= totalPages}
                      onClick={() => goTo(shownPage + 1)}
                    />
                  </PaginationItem>
                </PaginationContent>
              </Pagination>
            )}
          </div>
        )}

        {total > 0 && !loading && (
          <p className="text-xs text-neutral/70 text-center">
            {total} published {total === 1 ? 'app' : 'apps'}
          </p>
        )}
      </main>
    </div>
  )
}
