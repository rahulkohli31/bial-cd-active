/**
 * Forward-only cursor-list state for a keyset-paginated, searchable endpoint.
 *
 * Keyset gives no total and no offset, so numbered pages are not expressible:
 * the hook holds `items` and appends. The CALLER adapts the envelope's item key
 * inside its `fetchPage`, which is how the admin roster (U10) reuses this hook
 * even though its wire key is `users`, not `items` — it maps that into
 * `KeysetPage` before resolving, and reads its sibling `defaults` off `lastPage`.
 *
 * Invariants:
 * - Changing `q` resets the cursor AND clears `items` — a cursor from a different
 *   filter is meaningless. The immediate query value is exposed as `q`; only the
 *   fetch is debounced (300ms).
 * - Out-of-order guard: a monotonically-increasing request id; a response whose id
 *   is not the latest is dropped, so a slow `loadMore` that lands after a `setQuery`
 *   never appends stale rows.
 * - `hasMore: false` disables further `loadMore` calls.
 * - A failed page surfaces in `error` and stops `loading`; `items` is left intact
 *   (never silently degrade to an empty list).
 */
import { useCallback, useEffect, useRef, useState } from 'react'

/** The normalized page every `fetchPage` resolves to (the caller maps its own envelope into this). */
export interface KeysetPage<T> {
  items: T[]
  nextCursor: string | null
  hasMore: boolean
}

/** The args the hook hands `fetchPage`: the server cursor (null on a fresh load), the active query, and the page size. */
export interface KeysetFetchArgs {
  cursor: string | null
  q: string
  limit: number
}

export interface UseKeysetListOptions<T, P extends KeysetPage<T>> {
  fetchPage: (args: KeysetFetchArgs) => Promise<P>
  pageSize?: number
}

export interface UseKeysetListResult<T, P extends KeysetPage<T>> {
  items: T[]
  /** The immediate query value (updates synchronously on `setQuery`; the fetch is debounced). */
  q: string
  /**
   * The query that produced the CURRENT `items`, or `null` before the first fetch
   * has landed. Distinct from `q`, which runs ahead of the data by the debounce
   * window — so an empty-state decision made on `q` will misread "you searched for
   * something with no matches" as "you have nothing at all" for 300ms. Decide what
   * an empty list *means* from this, never from `q`.
   */
  appliedQuery: string | null
  loading: boolean
  hasMore: boolean
  error: Error | null
  /** The full object the most recent successful `fetchPage` resolved with — sibling keys survive. */
  lastPage: P | null
  loadMore: () => void
  setQuery: (next: string) => void
  /** Reload page 1 KEEPING the active query. Use to reconcile after a failed optimistic write. */
  refresh: () => void
  /** Forget everything, including the query. Use when leaving the list, not to reconcile it. */
  reset: () => void
  removeLocal: (predicate: (item: T) => boolean) => void
}

const DEFAULT_PAGE_SIZE = 25
const DEBOUNCE_MS = 300

export function useKeysetList<T, P extends KeysetPage<T> = KeysetPage<T>>(
  options: UseKeysetListOptions<T, P>,
): UseKeysetListResult<T, P> {
  const { fetchPage, pageSize = DEFAULT_PAGE_SIZE } = options

  const [items, setItems] = useState<T[]>([])
  const [q, setQ] = useState('')
  const [loading, setLoading] = useState(false)
  const [hasMore, setHasMore] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [lastPage, setLastPage] = useState<P | null>(null)
  const [appliedQuery, setAppliedQuery] = useState<string | null>(null)

  // Refs mirror the pieces that guards and callbacks must read synchronously,
  // without waiting for a re-render.
  const cursorRef = useRef<string | null>(null)
  const qRef = useRef('')
  const reqIdRef = useRef(0)
  const loadingRef = useRef(false)
  const hasMoreRef = useRef(true)
  // `ReturnType<typeof setTimeout>` (not `number`) so the timer id types the same
  // whether the ambient lib is DOM or a transitively-present `@types/node`.
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Latest-callback ref so `loadMore`/`setQuery` stay stable across renders even
  // when the caller passes a fresh inline `fetchPage` each time.
  const fetchPageRef = useRef(fetchPage)
  fetchPageRef.current = fetchPage

  const runFetch = useCallback(
    async (cursor: string | null, query: string): Promise<void> => {
      const myId = reqIdRef.current + 1
      reqIdRef.current = myId
      loadingRef.current = true
      setLoading(true)
      setError(null)
      try {
        const page = await fetchPageRef.current({ cursor, q: query, limit: pageSize })
        if (reqIdRef.current !== myId) return // a newer request superseded this one — drop it
        cursorRef.current = page.nextCursor
        hasMoreRef.current = page.hasMore
        setHasMore(page.hasMore)
        setLastPage(page)
        setAppliedQuery(query) // the rows below now answer THIS query, not the live input
        // cursor === null is a fresh load (first page or a query change) → replace;
        // a non-null cursor is a forward "load more" → append.
        setItems((prev) => (cursor === null ? page.items : [...prev, ...page.items]))
        loadingRef.current = false
        setLoading(false)
      } catch (caught) {
        if (reqIdRef.current !== myId) return // stale failure — the live request owns the state
        setError(caught instanceof Error ? caught : new Error(String(caught)))
        loadingRef.current = false
        setLoading(false)
      }
    },
    [pageSize],
  )

  const loadMore = useCallback((): void => {
    if (loadingRef.current || !hasMoreRef.current) return
    void runFetch(cursorRef.current, qRef.current)
  }, [runFetch])

  const setQuery = useCallback(
    (next: string): void => {
      qRef.current = next
      setQ(next) // immediate value for the input; only the fetch below is debounced
      if (debounceRef.current !== null) clearTimeout(debounceRef.current)
      debounceRef.current = setTimeout(() => {
        debounceRef.current = null
        // A new filter invalidates the accumulated cursor and rows outright.
        cursorRef.current = null
        hasMoreRef.current = true
        setHasMore(true)
        setItems([])
        void runFetch(null, qRef.current)
      }, DEBOUNCE_MS)
    },
    [runFetch],
  )

  const refresh = useCallback((): void => {
    // Refetch the first page under the CURRENT filter. Distinct from `reset`, which also
    // clears `q` — reconciling a failed delete must not throw away the search the user typed
    // and is still reading.
    //
    // Cancel any debounced fetch still armed from a recent setQuery call: it would otherwise
    // fire ~300ms later with the SAME qRef.current this call already uses, superseding this
    // fetch's result with a redundant duplicate — clearing `items` to empty first, then
    // repopulating with what should have been there all along (a visible flash for the caller).
    if (debounceRef.current !== null) {
      clearTimeout(debounceRef.current)
      debounceRef.current = null
    }
    // Do NOT pre-rewind cursorRef/hasMoreRef here. `runFetch(null, …)` is a page-1 load: on
    // success it REPLACES items and rewrites both refs from the fresh page. On FAILURE it
    // leaves items intact — and if we had already rewound the cursor to null, the next
    // `loadMore` would take the `cursor === null` REPLACE branch and collapse the list back to
    // page 1. Leaving the refs at their last-successful values keeps a following `loadMore`
    // an append. So the success path is their sole writer (the same rule `loadMore` relies on).
    void runFetch(null, qRef.current)
  }, [runFetch])

  const reset = useCallback((): void => {
    reqIdRef.current += 1 // invalidate any in-flight response
    cursorRef.current = null
    qRef.current = ''
    hasMoreRef.current = true
    loadingRef.current = false
    if (debounceRef.current !== null) {
      clearTimeout(debounceRef.current)
      debounceRef.current = null
    }
    setItems([])
    setQ('')
    setHasMore(true)
    setError(null)
    setLoading(false)
    setLastPage(null)
    setAppliedQuery(null)
  }, [])

  const removeLocal = useCallback((predicate: (item: T) => boolean): void => {
    setItems((prev) => prev.filter((item) => !predicate(item)))
  }, [])

  useEffect(
    () => () => {
      if (debounceRef.current !== null) clearTimeout(debounceRef.current)
    },
    [],
  )

  return { items, q, appliedQuery, loading, hasMore, error, lastPage, loadMore, setQuery, refresh, reset, removeLocal }
}
