/**
 * The marketplace — every app anyone on the platform has published (#145).
 *
 * The point of this page is that it is NOT scoped to you. Someone with a real need can see
 * what already exists instead of describing it into the builder and rebuilding a tool that
 * is already running, so the copy leans on "built by" rather than hiding authorship.
 *
 * Pagination and the debounced search box come from `useKeysetList`, the same hook
 * `ProjectsPage` uses — this list differs from that one only in whose rows it returns.
 *
 * ONE ASYMMETRY worth knowing while reading the JSX: a SEARCH response is a single ranked
 * page (the server returns no cursor, because a relevance ordering cannot be continued by a
 * row-id cursor), so "Load more" is deliberately hidden while a search is active rather
 * than rendering a button that can never advance.
 */
import { ExternalLink, Search, Store } from 'lucide-react'
import { useCallback, useEffect } from 'react'
import type React from 'react'

import Navbar from '../components/layout/Navbar'
import { useKeysetList, type KeysetFetchArgs } from '../hooks/useKeysetList'
import { listMarketplace, type MarketplaceEntry } from '../utils/marketplaceApi'

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
  // `useCallback` because the hook keys its work on `fetchPage`'s identity — an inline
  // arrow would be a new function every render.
  const fetchPage = useCallback(
    (args: KeysetFetchArgs) =>
      listMarketplace({ cursor: args.cursor, limit: args.limit, q: args.q || undefined }),
    [],
  )
  const { items, q, appliedQuery, loading, hasMore, error, loadMore, setQuery } =
    useKeysetList<MarketplaceEntry>({ fetchPage })

  // The hook does NOT self-start: it fetches on `loadMore` and on a debounced query change,
  // so the first page needs an explicit kick. `loadMore` is memoized, so this fires once.
  useEffect(() => {
    loadMore()
  }, [loadMore])

  // Decided from `appliedQuery`, never `q`: `q` runs ahead of the data by the debounce
  // window, so using it would flash "nothing published yet" at someone who has simply
  // started typing.
  const searching = appliedQuery !== null && appliedQuery !== ''

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
          Every app published across BIAL. Search before you build — someone may have made it
          already.
        </p>
      </header>

      <div className="relative">
        <Search
          size={15}
          className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral pointer-events-none"
        />
        <input
          data-testid="marketplace-search"
          type="search"
          value={q}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search what apps do…"
          aria-label="Search published apps"
          className="w-full pl-9 pr-3 py-2.5 text-sm bg-white border border-bial-border rounded-xl focus:outline-none focus:ring-2 focus:ring-primary/30"
        />
      </div>

      {error && (
        <p role="alert" className="text-sm text-danger">
          {error.message}
        </p>
      )}

      {items.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {items.map((entry) => (
            <EntryCard key={entry.url} entry={entry} />
          ))}
        </div>
      )}

      {!loading && items.length === 0 && (
        <p data-testid="marketplace-empty" className="text-sm text-neutral py-10 text-center">
          {searching
            ? 'No published app matches that yet.'
            : 'Nothing has been published yet. The first app to go live shows up here.'}
        </p>
      )}

      {loading && <p className="text-sm text-neutral py-4 text-center">Loading…</p>}

      {/* Hidden while searching: a ranked search response carries no cursor, so this button
          would have nothing to ask for. See the module docstring. */}
      {hasMore && !searching && !loading && (
        <button
          type="button"
          data-testid="marketplace-load-more"
          onClick={loadMore}
          className="self-center text-sm font-semibold text-primary hover:underline"
        >
          Load more
        </button>
      )}
      </main>
    </div>
  )
}
