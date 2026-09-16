/**
 * `/shared-applications` — the applications colleagues have shared with the reader, as a list or
 * a grid, with search, a "Shared by" filter, a sort and numbered pages.
 *
 * IT IS THE HOME LIST'S OWN PARTS, not a second design: `AppListRow`/`AppTile` draw the rows, the
 * view and density controls read the one remembered preference every application list shares, and
 * the footer is the same numbered pager. What differs is what a recipient may see and do — four
 * columns instead of a status chip, and one Open control instead of the owner's `⋯` menu.
 *
 * `page`, `pageSize`, `q`, `sharedBy` and `sort` live in the URL for the reason the owner's list
 * gives: opening an application and pressing Back, reloading, or pasting the address all land on
 * the same view. Pasting it is safe in a way `?tab=shared` was not — every reader lands on their
 * OWN shared list, so there is nothing here that could promise somebody else's.
 *
 * THE FILTER MATCHES ON A COLLEAGUE'S ID AND ONLY LABELS WITH THEIR NAME. A display name is
 * nullable and not unique, so two colleagues who share one would collapse into a single filter
 * entry and hand the reader the other's applications.
 *
 * SEARCH IS DESCRIPTION-ONLY, server-side and by design, which is why the box does not promise to
 * find a name.
 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { AlertTriangle, Info, Search, X } from 'lucide-react'
import {
  listSharedWithMe,
  type SharedProject,
  type SharedProjectSharer,
  type SharedSort,
} from '../utils/sharingApi'
import { SharedAppRow, SharedAppTile, sharerName } from '../components/projects/SharedAppRow'
import { ListPager, ListSkeleton, ViewControls } from '../components/projects/listChrome'
import { DENSITY_COLS, DEFAULT_PAGE_SIZE, PAGE_SIZES } from '../utils/listView'
import { useListView } from '../hooks/useListView'
import { useArrivalNotice } from '../hooks/useArrivalNotice'
import { Input } from '../components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'

/** The Select's value for "no colleague chosen". A sentinel rather than `''`, because Radix
 *  treats an empty string as "no value" and would render the placeholder instead of the word. */
const ANYONE = 'anyone'

type Committed = { page: number; pageSize: number; q: string; sharedBy: string; sort: SharedSort }

/**
 * READ DEFENSIVELY — a query string is user input, and this one is meant to be pasted around.
 *
 * `?page=0`, `?pageSize=9999` and `?sort=banana` all arrive from a typo or a truncated paste long
 * before they arrive from an attack, and each of them, taken literally, asks the server for
 * something it will refuse and leaves the reader on an error where a list should be. `sharedBy`
 * is passed through as written: only the server can say whether an id is a colleague who has
 * shared anything, and it answers a malformed one with a 422 this page shows as a failure.
 */
function readCommitted(params: URLSearchParams): Committed {
  const asked = Number(params.get('page'))
  const size = Number(params.get('pageSize'))
  const sort = params.get('sort')
  return {
    page: Number.isInteger(asked) && asked >= 1 ? asked : 1,
    pageSize: (PAGE_SIZES as readonly number[]).includes(size) ? size : DEFAULT_PAGE_SIZE,
    q: params.get('q') ?? '',
    sharedBy: params.get('sharedBy') ?? '',
    sort: sort === 'name' ? 'name' : 'recentlyShared',
  }
}

/** WRITE ONLY WHAT DIFFERS FROM THE DEFAULT, and copy every other parameter through: a default
 *  spelled out is noise a reader has to look past, and a page that silently ate a parameter it
 *  did not recognise would be a trap for whoever adds the next one. */
function intoParams(prev: URLSearchParams, next: Committed): URLSearchParams {
  const params = new URLSearchParams(prev)
  const put = (key: string, value: string, isDefault: boolean): void => {
    if (isDefault) params.delete(key)
    else params.set(key, value)
  }
  put('page', String(next.page), next.page === 1)
  put('pageSize', String(next.pageSize), next.pageSize === DEFAULT_PAGE_SIZE)
  put('q', next.q, next.q === '')
  put('sharedBy', next.sharedBy, next.sharedBy === '')
  put('sort', next.sort, next.sort === 'recentlyShared')
  return params
}

export default function SharedApplicationsPage(): React.JSX.Element {
  const navigate = useNavigate()
  const { view, setView, density, setDensity } = useListView()

  const [searchParams, setSearchParams] = useSearchParams()
  const { page, pageSize, q, sharedBy, sort } = readCommitted(searchParams)

  /**
   * ONE `commit` RATHER THAN FIVE SETTERS, because `setSearchParams` reads the params of the
   * render it was created in: two calls in one handler would each start from that same snapshot,
   * and the second would silently drop the first's key. Every caller that changes more than one
   * value — typing, which also resets the page; the filter and the sort, which do the same —
   * passes them in a single patch.
   *
   * Typing REPLACES the history entry, because a pushed entry per keystroke makes the Back
   * button spell the word backwards instead of leaving the page. Every deliberate click pushes.
   */
  const commit = useCallback(
    (patch: Partial<Committed>, entry: 'push' | 'replace'): void => {
      setSearchParams((prev) => intoParams(prev, { ...readCommitted(prev), ...patch }), {
        replace: entry === 'replace',
      })
    },
    [setSearchParams],
  )

  // SEEDED FROM THE URL RATHER THAN FROM `''`: a cold load of `?q=fuel` with an empty seed asks
  // the server for the unfiltered list first, paints all of it, and only 300ms later asks the
  // question the link actually carried.
  const [debouncedQ, setDebouncedQ] = useState(q)
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 300)
    return () => clearTimeout(t)
  }, [q])

  const [items, setItems] = useState<SharedProject[]>([])
  const [sharers, setSharers] = useState<SharedProjectSharer[]>([])
  const [total, setTotal] = useState(0)
  const [totalPages, setTotalPages] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const [reloadNonce, setReloadNonce] = useState(0)
  // WHAT THE ROWS ON SCREEN ANSWER, as opposed to what was last asked for — the footer and the
  // empty states need it for the reason the owner's list documents: a settled `null` means no
  // read has landed yet, so a skeleton is not confused with an empty account.
  const [applied, setApplied] = useState<Committed | null>(null)

  useEffect(() => {
    let alive = true
    setLoading(true)
    listSharedWithMe({
      page,
      limit: pageSize,
      q: debouncedQ || undefined,
      sharedBy: sharedBy || undefined,
      sort,
    })
      .then((res) => {
        if (!alive) return
        setItems(res.items)
        setSharers(res.sharers)
        setTotal(res.total)
        setTotalPages(res.totalPages)
        setApplied({ page: res.page, pageSize: res.pageSize, q: debouncedQ, sharedBy, sort })
        setError(null)
      })
      .catch((caught: unknown) => {
        if (!alive) return
        // The rows already on screen are LEFT INTACT. A later page failing must not blank the
        // list the reader is using; the message goes underneath them instead.
        setError(
          caught instanceof Error
            ? caught
            : new Error('Could not load the applications shared with you.'),
        )
        setApplied({ page, pageSize, q: debouncedQ, sharedBy, sort })
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [page, pageSize, debouncedQ, sharedBy, sort, reloadNonce])

  // Paged past the end — a share revoked elsewhere can shrink this list under a reader, and this
  // one has many writers. Step back rather than stranding them on a blank page with no way out.
  useEffect(() => {
    if (!loading && totalPages > 0 && page > totalPages) commit({ page: totalPages }, 'replace')
  }, [loading, page, totalPages, commit])

  // A recipient bounced off a share that no longer exists lands here carrying one sentence.
  const { notice, dismiss: dismissNotice } = useArrivalNotice()

  const isEmpty = items.length === 0
  const settled = applied !== null
  const showSkeleton = isEmpty && (loading || !settled)
  const showFirstPageError = error !== null && isEmpty
  const narrowed = applied !== null && (applied.q !== '' || applied.sharedBy !== '')
  const showNothingShared = settled && !loading && error === null && isEmpty && !narrowed
  const showNoMatches = settled && !loading && error === null && isEmpty && narrowed
  const showRows = !isEmpty

  // DERIVED FROM WHAT THE ROWS ANSWER, never from what was requested — a footer narrating the
  // page that FAILED over the rows that succeeded prints a range past its own total.
  const appliedPage = applied?.page ?? 1
  const appliedPageSize = applied?.pageSize ?? DEFAULT_PAGE_SIZE
  const firstOnPage = (appliedPage - 1) * appliedPageSize + 1
  const lastOnPage = firstOnPage + items.length - 1

  const openShared = (id: string): void => navigate(`/shared/${id}`)

  return (
    <div className="min-h-full font-manrope flex flex-col bg-bial-bg">
      <main data-testid="shared-applications" className="flex-1 max-w-6xl mx-auto w-full px-6 py-8">
        <h1 className="text-2xl font-extrabold text-tertiary">Shared Applications</h1>
        <p className="text-sm text-neutral mt-1">
          Applications a colleague shared with you. You can open and use them; they stay theirs.
        </p>

        {/* THE PAGE'S ONE POLITE REGION for a wait — permanently mounted, empty when nothing is
            in flight. A region inserted together with its text is missed entirely by several
            reader-and-browser combinations, and every skeleton below is conditional. */}
        <div role="status" aria-live="polite" data-testid="shared-wait">
          {loading || !settled ? (
            <p className="text-sm font-medium text-neutral mt-3">Loading shared applications…</p>
          ) : null}
        </div>

        {/* Not the failure channel: nothing failed, nothing was lost, and nothing the reader did
            was wrong — so `role="status"`, a plain card in the page's flow, and an `Info` mark
            rather than the `AlertCircle` the failure sites use. */}
        <div role="status" aria-live="polite" data-testid="shared-notice">
          {notice !== null ? (
            <div className="flex items-center gap-2.5 bg-white border border-bial-border rounded-2xl px-4 py-3 mt-4">
              <Info size={15} className="flex-shrink-0 text-neutral" />
              <p className="text-sm text-tertiary">{notice}</p>
              <button
                type="button"
                onClick={dismissNotice}
                aria-label="Dismiss notice"
                className="ml-auto text-neutral hover:text-tertiary"
              >
                <X size={15} />
              </button>
            </div>
          ) : null}
        </div>

        <div className="flex items-center gap-3 flex-wrap mt-6 mb-4">
          <div className="relative flex-1 min-w-[220px] max-w-md">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral" />
            {/* NOT "Search applications…". The server searches DESCRIPTIONS only, so a
                name-search promise would be false — the same honest wording the marketplace's
                own description-only search carries. */}
            <Input
              value={q}
              onChange={(e) => commit({ q: e.target.value, page: 1 }, 'replace')}
              placeholder="Search what apps do…"
              aria-label="Search applications shared with you"
              className="pl-9"
            />
          </div>

          <label className="flex items-center gap-2 text-xs text-neutral">
            <span className="whitespace-nowrap">Shared by</span>
            <Select
              value={sharedBy === '' ? ANYONE : sharedBy}
              onValueChange={(v) => commit({ sharedBy: v === ANYONE ? '' : v, page: 1 }, 'push')}
            >
              <SelectTrigger className="h-9 w-[180px]" aria-label="Shared by">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ANYONE}>Anyone</SelectItem>
                {sharers.map((sharer) => (
                  <SelectItem key={sharer.userId} value={sharer.userId}>
                    {sharerName(sharer.displayName)} ({sharer.shareCount})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>

          <label className="flex items-center gap-2 text-xs text-neutral">
            <span className="whitespace-nowrap">Sort</span>
            <Select value={sort} onValueChange={(v) => commit({ sort: v as SharedSort, page: 1 }, 'push')}>
              <SelectTrigger className="h-9 w-[160px]" aria-label="Sort">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="recentlyShared">Recently shared</SelectItem>
                <SelectItem value="name">Name</SelectItem>
              </SelectContent>
            </Select>
          </label>

          <div className="ml-auto flex items-center gap-2">
            <ViewControls view={view} density={density} onView={setView} onDensity={setDensity} />
          </div>
        </div>

        {showSkeleton ? (
          <ListSkeleton view={view} density={density} />
        ) : showFirstPageError ? (
          <div
            data-testid="shared-error"
            className="bg-white border border-danger/30 rounded-2xl py-16 px-6 text-center"
          >
            <AlertTriangle size={22} className="mx-auto text-danger mb-3" />
            <p className="text-sm font-semibold text-tertiary">Couldn’t load the applications shared with you</p>
            <p className="text-xs text-neutral mt-1 mb-3">The server did not answer. Nothing has been lost.</p>
            <button
              onClick={() => setReloadNonce((n) => n + 1)}
              className="text-xs text-primary font-semibold hover:underline"
            >
              Retry
            </button>
          </div>
        ) : showNothingShared ? (
          // The plain, message-only empty state — never one offering "New application":
          // a list entirely about what colleagues have shared must not invite a reader to
          // create one of their own from it.
          <div
            data-testid="shared-empty"
            className="bg-white border border-bial-border rounded-2xl py-16 px-6 text-center"
          >
            <p className="text-sm font-semibold text-tertiary">Nothing shared with you yet</p>
            <p className="text-xs text-neutral mt-1">
              When a colleague shares an application with you, it will show up here.
            </p>
          </div>
        ) : showNoMatches ? (
          <div
            data-testid="shared-no-matches"
            className="bg-white border border-bial-border rounded-2xl py-16 px-6 text-center"
          >
            <Search size={22} className="mx-auto text-neutral/50 mb-3" />
            <p className="text-sm font-semibold text-tertiary">No matches</p>
            {/* The query the ROWS answer, not the one still being typed — the live value runs
                300ms ahead and would quote a search nothing was asked about. */}
            <p className="text-xs text-neutral mt-1">
              {applied !== null && applied.q !== ''
                ? `Nothing shared with you matches “${applied.q}”.`
                : 'Nothing shared with you matches that filter.'}
            </p>
            <button
              onClick={() => commit({ q: '', sharedBy: '', page: 1 }, 'push')}
              className="text-xs text-primary font-semibold hover:underline mt-2"
            >
              Clear the search and the filter
            </button>
          </div>
        ) : null}

        {showRows && (
          <>
            {view === 'list' ? (
              <div className="bg-white border border-bial-border rounded-2xl overflow-hidden">
                <div className="flex items-center gap-4 px-4 py-2.5 bg-bial-bg/60 border-b border-bial-border text-[10px] font-bold uppercase tracking-wider text-neutral">
                  <span className="flex-1">Application</span>
                  <span className="hidden sm:block w-[168px] flex-shrink-0">Shared by</span>
                  <span className="hidden sm:block w-28 flex-shrink-0">Shared on</span>
                  {/* "Details updated", NOT "Last updated": the owner's `updatedAt` moves on a
                      rename or a description edit and never on a build or a deploy. */}
                  <span className="hidden sm:block w-28 flex-shrink-0">Details updated</span>
                  <span className="w-[88px] flex-shrink-0" aria-hidden />
                </div>
                {items.map((project) => (
                  <SharedAppRow
                    key={project.projectId}
                    project={project}
                    onOpen={() => openShared(project.projectId)}
                  />
                ))}
              </div>
            ) : (
              <div className={`grid gap-4 ${DENSITY_COLS[density]}`}>
                {items.map((project) => (
                  <SharedAppTile
                    key={project.projectId}
                    project={project}
                    onOpen={() => openShared(project.projectId)}
                  />
                ))}
              </div>
            )}

            {/* A later page failing keeps the rows above. Say it underneath them, and make the
                retry touch `reloadNonce`: clicking the same page number again changes no state,
                so the fetch effect's deps do not change and nothing re-runs. */}
            {error !== null && (
              <p role="alert" className="text-xs text-danger text-center mt-4">
                Couldn’t load more.{' '}
                <button
                  type="button"
                  onClick={() => setReloadNonce((n) => n + 1)}
                  className="font-semibold text-primary hover:underline"
                >
                  Retry
                </button>
              </p>
            )}

            <div className="flex items-center justify-between gap-4 flex-wrap mt-4 text-xs text-neutral">
              {/* THE CAPTION ANNOUNCES: searching, filtering or turning a page leaves the rows
                  below silently different, and this line is the only thing that says how many
                  there now are. */}
              <span className="tabular-nums" role="status" aria-live="polite" data-testid="shared-range">
                Showing {firstOnPage}–{lastOnPage} of {total}
              </span>

              <div className="flex items-center gap-4">
                <label className="flex items-center gap-2">
                  <span className="whitespace-nowrap">Rows per page</span>
                  <Select
                    value={String(pageSize)}
                    onValueChange={(v) => commit({ pageSize: Number(v), page: 1 }, 'push')}
                  >
                    <SelectTrigger className="h-8 w-[72px]" aria-label="Rows per page">
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
                </label>

                <span className="whitespace-nowrap tabular-nums">
                  Page {appliedPage} of {Math.max(totalPages, 1)}
                </span>

                <ListPager
                  page={page}
                  activePage={appliedPage}
                  totalPages={totalPages}
                  onGo={(next) => commit({ page: next }, 'push')}
                  label="Shared applications pagination"
                />
              </div>
            </div>
          </>
        )}
      </main>
    </div>
  )
}
