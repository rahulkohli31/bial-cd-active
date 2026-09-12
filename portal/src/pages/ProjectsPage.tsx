/**
 * `/projects` — the landing screen: list/grid views, numbered pagination, a summary strip.
 *
 * Pagination is OFFSET, not keyset — `list_projects` needs a `total` for "Page 1 of 2",
 * which the keyset envelope doesn't compute — so `page` and `pageSize` are committed
 * state and an effect re-fetches, rather than a hook that appends forward-only.
 *
 * WHY THIS EXISTS
 * `page`, `pageSize` and `q` live in the URL, not local state — opening a project and
 * pressing Back, reloading, or pasting the address to a colleague all land on the same
 * view, which matters more here than on most lists: the canvas gives a citizen no recents
 * list, so this page IS how a project is found again. `view` and `density` stay in
 * `localStorage` instead, on purpose — they are a person's habit rather than a place in a
 * list, and a shared link should not reach into the reader's window and rearrange it.
 *
 * Two empty states differ: zero projects (first run) vs. zero results WITH a search,
 * which quotes `appliedQuery` — never the live `q`, which runs 300ms ahead and would
 * flash a false "no projects" mid-type. The skeleton matches the active view, and a
 * page-2 failure is shown below the rows already on screen, never clears them.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import {
  Plus,
  Search,
  LayoutGrid,
  List as ListIcon,
  AlertTriangle,
  AlertCircle,
  Info,
  X,
  ChevronsLeft,
  ChevronsRight,
} from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import {
  listProjects,
  listProjectCounts,
  deleteProject,
  type Project,
  type ProjectCounts,
} from '../utils/projectApi'
import { listSharedWithMe, type SharedProject, type SharedProjectsPage } from '../utils/sharingApi'
import { ApiError } from '../utils/apiError'
import ProjectCard from '../components/projects/ProjectCard'
import ProjectRow from '../components/projects/ProjectRow'
import SharedProjectCard from '../components/projects/SharedProjectCard'
import ProjectCreateModal from '../components/projects/ProjectCreateModal'
import ProjectDeleteDialog from '../components/projects/ProjectDeleteDialog'
import { useKeysetList } from '../hooks/useKeysetList'
import { Input } from '../components/ui/input'
import { Skeleton } from '../components/ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '../components/ui/toggle-group'
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from '../components/ui/pagination'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'

/**
 * THE ONE SENTENCE A DEAD ADDRESS SAYS ON THE WAY OUT.
 *
 * Exported because more than one surface has to say it BYTE FOR BYTE: `ProjectPage` sends it when
 * a project id 404s, `ChatRoute` sends it when a chat resolves to nothing, and it appears again in
 * place — as a card body, on a page that stays — for an id the server cannot even parse.
 * A second copy of these words somewhere else is how two of those three drift apart.
 *
 * IT IS NEUTRAL, AND IT IS NOT DIFFERENTIATED PER CAUSE. A project id belonging to another
 * citizen is a deliberately non-leaking 404, identical to one that never existed
 * (`owned_project_or_404` — "fail closed with a non-leaking 404"), so "you do not have access"
 * would confirm the existence of someone else's project. One line, whatever the reason — which is
 * also why the line is a CONSTANT rather than the server's own message piped through: the moment
 * it is derived from the response, two causes can print two sentences again.
 */
export const PROJECT_GONE_NOTICE = 'That project is no longer available.'

type View = 'list' | 'grid'
type Density = 'S' | 'M' | 'L'

/** Remembered per person so the choice survives a reload.
 *  Reads are wrapped because a private window or blocked site data throws on access. */
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

/** Grid columns per density. S is denser, L roomier — the mockup's S/M/L control. */
const DENSITY_COLS: Record<Density, string> = {
  S: 'grid-cols-1 sm:grid-cols-3 lg:grid-cols-4',
  M: 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3',
  L: 'grid-cols-1 sm:grid-cols-2',
}

// shadcn's toggle marks its ON state with `bg-accent`, and this theme maps `--accent` to
// the brand ORANGE (#F5A623) — a solid orange pill in a teal interface. The component is
// right; its default theme mapping is not for this design, and no unit test can see which
// colour a class resolves to. Overridden at the call site rather than in `ui/toggle.tsx`,
// so the vendored component stays upstream-shaped for whoever uses it next.
const ACTIVE =
  ' data-[state=on]:bg-primary/10 data-[state=on]:text-primary data-[state=on]:ring-1 data-[state=on]:ring-primary/30'

const PAGE_SIZES = [8, 16, 24, 48] as const
const DEFAULT_PAGE_SIZE = PAGE_SIZES[0]

/** The three values the URL carries. Everything else about this page is local. */
type Committed = { page: number; pageSize: number; q: string }

/**
 * READ DEFENSIVELY — a query string is user input, and this one is meant to be pasted around.
 *
 * `?page=0`, `?page=-3`, `?page=banana` and `?pageSize=9999` all arrive from a typo or a truncated
 * paste long before they arrive from an attack, and each of them, taken literally, asks the server
 * for something it will refuse and leaves the reader on an error where a list should be. Anything
 * that is not a whole page number at or past 1 falls back to page 1; a page size that is not one of
 * the four the control actually offers falls back to the default, because honouring `?pageSize=9999`
 * would let a link hand somebody else's browser a 9999-row request.
 */
function readCommitted(params: URLSearchParams): Committed {
  const asked = Number(params.get('page'))
  const size = Number(params.get('pageSize'))
  return {
    page: Number.isInteger(asked) && asked >= 1 ? asked : 1,
    pageSize: (PAGE_SIZES as readonly number[]).includes(size) ? size : DEFAULT_PAGE_SIZE,
    q: params.get('q') ?? '',
  }
}

/**
 * WRITE ONLY WHAT DIFFERS FROM THE DEFAULT, and leave every other parameter alone.
 *
 * A default that is spelled out is noise a citizen has to read past before they find the part of
 * the address that means something, and `?page=1&pageSize=8&q=` on a first paint is three
 * parameters saying "nothing has happened yet". Dropping them also keeps the plain `/projects`
 * address reachable: page 1 of an unfiltered list is written by DELETING the keys, not by setting
 * them to their defaults, so stepping back to the start returns the URL you started with.
 *
 * Foreign parameters are copied through rather than dropped. This function owns three keys, not the
 * query string, and a page that silently ate a parameter it did not recognise would be a trap for
 * whoever adds the fourth.
 */
function intoParams(prev: URLSearchParams, next: Committed): URLSearchParams {
  const params = new URLSearchParams(prev)
  const put = (key: string, value: string, isDefault: boolean): void => {
    if (isDefault) params.delete(key)
    else params.set(key, value)
  }
  put('page', String(next.page), next.page === 1)
  put('pageSize', String(next.pageSize), next.pageSize === DEFAULT_PAGE_SIZE)
  put('q', next.q, next.q === '')
  return params
}

export default function ProjectsPage(): React.JSX.Element {
  const navigate = useNavigate()
  const [showCreate, setShowCreate] = useState(false)
  const [deleting, setDeleting] = useState<Project | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  const [view, setView] = useState<View>(() => readStored(VIEW_KEY, ['list', 'grid'] as const, 'list'))
  const [density, setDensity] = useState<Density>(() => readStored(DENSITY_KEY, ['S', 'M', 'L'] as const, 'M'))
  // NOT PERSISTED, NOT IN THE URL — unlike `view`/`density` (a habit) and `page`/`q` (a place
  // in a list this page is the only way back to), which tab is open is neither: a shared link
  // to this page is about the citizen's OWN projects either way, and there is nothing here a
  // colleague would paste around expecting it to land on someone else's "shared with me".
  const [tab, setTab] = useState<'mine' | 'shared'>('mine')

  // COMMITTED query state — WHAT WAS ASKED FOR, and it lives in the address bar.
  //
  // ONE `commit` RATHER THAN THREE SETTERS, because `setSearchParams` reads the params of the
  // render it was created in: two calls in one handler would each start from that same snapshot,
  // and the second would silently drop the first's key. Every caller below that changes more than
  // one value — typing, which also resets the page; the rows-per-page control, which does the same
  // — therefore passes both in a single patch.
  const [searchParams, setSearchParams] = useSearchParams()
  const { page, pageSize, q } = readCommitted(searchParams)
  /**
   * `entry` IS THE WHOLE DEBOUNCE QUESTION, ANSWERED AT EVERY CALL SITE.
   *
   * The search box writes the URL on every keystroke, so a pushed entry per keystroke would put
   * `r`, `ra`, `ram`, `ramp` on the stack and make the Back button spell the word backwards
   * instead of leaving the page — the one control a reader reaches for when they want OUT. Typing
   * therefore replaces. So does the out-of-range correction below, which is the page fixing its
   * own address rather than the reader going anywhere: pushing it would put a page that bounces
   * one step behind the reader, so Back would land on it and immediately throw them forward again.
   *
   * Every deliberate click — a page number, a jump to first or last, a new rows-per-page, clearing
   * the search — pushes, because each of those IS a navigation and Back undoing exactly one of
   * them is what a reader expects.
   */
  const commit = useCallback(
    (patch: Partial<Committed>, entry: 'push' | 'replace'): void => {
      setSearchParams((prev) => intoParams(prev, { ...readCommitted(prev), ...patch }), {
        replace: entry === 'replace',
      })
    },
    [setSearchParams],
  )
  // WHAT THE ROWS ON SCREEN ANSWER, as opposed to what was last asked for. `appliedQuery`
  // already worked this way; `appliedPage`/`appliedPageSize` are its missing siblings, and
  // the footer needs them for the same reason the empty state needs the query.
  const [appliedQuery, setAppliedQuery] = useState<string | null>(null)
  const [appliedPage, setAppliedPage] = useState(1)
  const [appliedPageSize, setAppliedPageSize] = useState<number>(PAGE_SIZES[0])

  const [items, setItems] = useState<Project[]>([])
  const [total, setTotal] = useState(0)
  const [totalPages, setTotalPages] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const [counts, setCounts] = useState<ProjectCounts | null>(null)
  // TRUE ONLY WHEN THE FIRST LOAD FAILED WITH NOTHING TO FALL BACK ON.
  // A REFRESH failure (there IS a last-known-good `counts`) stays silent by design — the
  // comment below explains why — but that same silence, applied to a FIRST load, left the
  // three tiles skeleton-pulsing forever over a working list: no error, no retry, and
  // nothing but a delete (or the page list's own Retry) ever bumps `reloadNonce` again.
  const [countsFailedCold, setCountsFailedCold] = useState(false)
  const [reloadNonce, setReloadNonce] = useState(0)
  // DELETES IN FLIGHT, BY ID — not a boolean. Two overlapping deletes shared one flag, so the
  // faster one's `finally` cleared it while the slower was still running: precisely the
  // window the guard exists to cover. A set is the same treatment `requestId` already gets.
  const [deletingIds, setDeletingIds] = useState<ReadonlySet<string>>(() => new Set())
  const deleteInFlight = deletingIds.size > 0

  // Out-of-order guard: a slow page that lands after a newer one must not overwrite it.
  const requestId = useRef(0)
  // WHERE FOCUS GOES WHEN A DELETE CONFIRMATION CLOSES. Not onto its own trigger: the row's
  // Delete button outlives the close and is then unmounted by the refetch a beat later, so
  // Radix's default restore-to-trigger would put the keyboard on a control that disappears
  // under it and lands back on the body. `tabIndex={-1}` on the heading below makes it a
  // programmatic focus target without adding it to the tab order.
  const headingRef = useRef<HTMLHeadingElement>(null)

  // The search is debounced, but `page` resets IMMEDIATELY on a keystroke — a cursor into
  // page 3 of the previous query is meaningless against a new one.
  //
  // SEEDED FROM THE URL RATHER THAN FROM `''`. A cold load of `/projects?q=ramp` with an
  // empty seed asks the server for the UNFILTERED list first, paints all of it, and only 300ms
  // later asks the question the link actually carried — a flash of everybody's projects on an
  // address that named one, and a wasted round trip to produce it.
  const [debouncedQ, setDebouncedQ] = useState(q)
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 300)
    return () => clearTimeout(t)
  }, [q])

  // THE ARRIVAL NOTICE, READ ONCE AND THEN SCRUBBED.
  //
  // It rides ROUTER STATE, not the query string. A query survives a copy, a bookmark and a share,
  // and "that project is no longer available" pinned to a shareable `/projects?notice=…` is a
  // sentence about a bounce the next reader never made. Router state travels only on the one
  // navigation that set it.
  //
  // BUT IT SURVIVES MORE THAN THAT NAVIGATION UNLESS IT IS TAKEN AWAY. React Router keeps this
  // in `window.history.state`, which the browser restores on RELOAD and replays on BACK — so
  // without the replace below, refreshing the list re-announces a project the reader dealt with
  // ten minutes ago, and stepping back onto the list later does it again. Reading the sentence
  // into component state and then replacing the entry with a stateless one is what makes this a
  // one-shot. The replace cannot loop: the re-run reads a `notice` that is no longer there.
  //
  // WHICH IS ALSO WHY IT NEVER BECOMES A QUERY PARAMETER. The replace above carries
  // `location.search` through verbatim, so the page, size and query a reader arrived with survive
  // being told a project is gone — but the reverse must hold too: a `?notice=…` would be copied
  // forward by `intoParams`, which preserves the parameters it does not own, and would then
  // outlive the reload it is supposed to be cleared by. Router state is the only channel that
  // travels on exactly one navigation and nowhere else, so it stays the channel.
  //
  // THE TEXT ARRIVES AFTER ITS REGION, which is why this is an effect and not a `useState`
  // initialiser. A live region inserted together with its text is missed entirely by
  // several reader-and-browser combinations — `TurnBanner` and `LivePreview` both record it — so
  // the region below is mounted on every render, empty, and the sentence lands inside it a tick
  // later. It is its own region rather than a second tenant of `projects-wait`: that one narrates
  // a wait that is still running, and two unrelated sentences sharing one polite region read as
  // one announcement.
  const location = useLocation()
  const [notice, setNotice] = useState<string | null>(null)
  useEffect(() => {
    const carried = (location.state as { notice?: unknown } | null)?.notice
    if (typeof carried !== 'string' || carried.length === 0) return
    setNotice(carried)
    navigate(`${location.pathname}${location.search}`, { replace: true, state: null })
  }, [location.pathname, location.search, location.state, navigate])

  useEffect(() => {
    const id = ++requestId.current
    setLoading(true)
    listProjects({ page, limit: pageSize, q: debouncedQ || undefined })
      .then((res) => {
        if (requestId.current !== id) return
        setItems(res.items)
        setTotal(res.total)
        setTotalPages(res.totalPages)
        setAppliedQuery(debouncedQ)
        setAppliedPage(res.page)
        setAppliedPageSize(res.pageSize)
        setError(null)
      })
      .catch((caught: unknown) => {
        if (requestId.current !== id) return
        // The rows already on screen are LEFT INTACT. A later page failing must not blank
        // the list the reader is using; the message goes underneath them instead.
        setError(caught instanceof Error ? caught : new Error('Could not load your projects.'))
        setAppliedQuery(debouncedQ)
      })
      .finally(() => {
        if (requestId.current === id) setLoading(false)
      })
  }, [page, pageSize, debouncedQ, reloadNonce])

  // The three numbers. A separate route, because the page holds 8 of 12 rows and cannot
  // compute any of them, and because polling the list for three integers would pay for row
  // projection and joins it does not need.
  useEffect(() => {
    let alive = true
    // Captured BEFORE the request, not read inside `.catch()`: this is "did we have
    // something to show before THIS attempt started", which is exactly the refresh-vs-first-
    // load distinction the two branches below need. Reading `counts` inside the callback
    // would still answer that correctly here (nothing else sets `counts` between this line
    // and the request settling), but capturing it up front says so without relying on that.
    const hadValueAlready = counts !== null
    listProjectCounts()
      .then((c) => {
        if (!alive) return
        setCounts(c)
        setCountsFailedCold(false)
      })
      // A REFRESH FAILURE KEEPS THE LAST KNOWN-GOOD NUMBERS. Clearing to `null` sent the
      // tiles back to their skeleton, so a page showing real rows underneath grew three
      // empty boxes above them — which reads as the page breaking rather than as one
      // request failing. The skeleton means "not asked yet", and after a successful load
      // that is no longer true. Slightly stale beats visibly broken.
      //
      // A FIRST-LOAD FAILURE IS DIFFERENT: there is no last-known-good number to fall back
      // on, so silence here meant the skeleton pulsed forever with no error and no retry.
      .catch(() => {
        if (!alive) return
        if (!hadValueAlready) setCountsFailedCold(true)
      })
    return () => {
      alive = false
    }
    // `counts` is read once, at the top of the effect body, to characterise THIS attempt as
    // first-load-vs-refresh. Adding it as a dep would re-run the fetch every time the
    // response above changes it, which is not a real trigger for asking again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadNonce])

  // Paged past the end — a delete elsewhere can shrink the list under a reader. Step back
  // rather than stranding them on a blank page with no way out.
  useEffect(() => {
    if (!loading && totalPages > 0 && page > totalPages) commit({ page: totalPages }, 'replace')
  }, [loading, page, totalPages, commit])

  const chooseView = (next: View): void => {
    setView(next)
    store(VIEW_KEY, next)
  }
  const chooseDensity = (next: Density): void => {
    setDensity(next)
    store(DENSITY_KEY, next)
  }

  const openProject = (id: string): void => navigate(`/projects/${id}`)
  const openSharedProject = (id: string): void => navigate(`/shared/${id}`)

  // "Shared with me" — keyset-paginated, unlike the offset-paginated "mine" list above, because
  // `GET /v1/projects/shared` writes into a list every SHARER adds to rather than one this
  // reader's own total governs; `useKeysetList` never touches `q` here (no search over this
  // list exists yet), only `items`/`loading`/`error`/`hasMore`/`loadMore`.
  const shared = useKeysetList<SharedProject, SharedProjectsPage>({
    fetchPage: ({ cursor, limit }) => listSharedWithMe({ cursor, limit }),
  })
  // FETCHED ONCE, ON FIRST VISIT TO THE TAB — `lastPage === null` is "no page has landed yet",
  // which `loadMore` itself then flips, so this does not re-fire on every render the tab stays
  // open, and switching back from "mine" and forth does not repeat the request. `error === null`
  // is what stops a failed first load from retrying itself every render: `loading` flips back to
  // `false` on failure too, and without this guard that flip alone would re-satisfy the other two
  // conditions and fire `loadMore` again, forever, against a server that just refused it. A failed
  // load waits for the Retry button below instead.
  useEffect(() => {
    if (tab === 'shared' && shared.lastPage === null && shared.error === null && !shared.loading) {
      shared.loadMore()
    }
    // `shared` is a fresh object every render (`useKeysetList` returns a new literal each call);
    // depending on it whole would re-run this on every render the tab stays open. The four
    // properties actually read above are what should gate the effect, and are exactly what's listed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, shared.lastPage, shared.error, shared.loading, shared.loadMore])

  const handleCreated = (project: Project): void => {
    setShowCreate(false)
    navigate(`/projects/${project.id}`)
  }

  const handleDelete = async (project: Project, remark: string): Promise<void> => {
    setDeletingIds((ids) => new Set(ids).add(project.id))
    // THE ROW LEAVES WHEN THE CASCADE SAYS IT LEFT, never before. It used to be
    // filtered out of `items` here, one line above the request — so a citizen watched their
    // project vanish while the server was still dropping its database, and if the drop failed
    // the row reappeared under them. That is a completed delete the platform had not
    // performed, which is the one thing this dialog's sentence must not do.
    //
    // Nothing replaces it, because nothing has to: the refetch below removes the row on every
    // outcome that actually removed it, and the citizen's signal during the wait is the
    // dialog, which stays open and busy for exactly as long as the request runs. The backend
    // force-drops a database, sweeps blobs and tears down a container before it answers, so
    // that wait is real and worth showing honestly.
    try {
      await deleteProject(project.id, remark)
      setReloadNonce((n) => n + 1) // the row goes here, with the totals and the counts strip
    } catch (caught) {
      // 404 = already gone (another tab). That IS the desired end state, so no toast — and
      // the refetch is what takes the row, `total` and the counts strip with it. Returning
      // early here left "Showing 1–7 of 8" on screen.
      if (caught instanceof ApiError && caught.status === 404) {
        setReloadNonce((n) => n + 1)
        return
      }
      // The row never left, so this is not "put it back" — it is the totals and the counts
      // strip catching up with whatever the failed attempt did or did not change.
      setReloadNonce((n) => n + 1)
      setToast(caught instanceof Error ? caught.message : 'Could not delete the project.')
    } finally {
      setDeletingIds((ids) => {
        const next = new Set(ids)
        next.delete(project.id)
        return next
      })
      // CLOSING THE DIALOG IS DEFERRED TO HERE, not the top of this function. It used to
      // close synchronously before the request even started —
      // batched into the SAME commit as the optimistic row removal — so the dialog's own
      // `busy` state (the spinner, Cancel disabling) was set and then immediately unmounted
      // in the same render, never actually observable. The backend does real work before
      // answering (force-drop the database, sweep blobs, tear down the container), so this
      // is not decorative: a citizen genuinely waits, across every outcome here — success,
      // 404, or a real failure — which is why this sits in `finally` rather than in only
      // one branch.
      setDeleting(null)
      // FOCUS EXPLICITLY, rather than let Radix try, and it still has to be explicit now that
      // the row survives until the refetch. The Delete button Radix captured is about to be
      // unmounted by that refetch — a beat AFTER the dialog closes — so a restore onto it
      // would put the keyboard on a control that is removed a moment later, which lands right
      // back on the body. The heading is the nearest stable, always-mounted landmark, and it
      // is unaffected when the row goes.
      headingRef.current?.focus()
    }
  }

  const isEmpty = items.length === 0
  const settled = appliedQuery !== null
  const showSkeleton = isEmpty && (loading || !settled)
  // THE THREE NUMBERS ARE STILL BEING FETCHED. `null` is "not asked yet or the first ask is in
  // flight"; `countsFailedCold` is the arm that has stopped waiting and shows a retry instead, so
  // it is not a wait and must not claim to be one.
  const countsPending = counts === null && !countsFailedCold
  // WHAT THE WAIT SENTENCE ANSWERS TO. Deliberately WIDER than `showSkeleton`, which only fires on
  // an EMPTY list: turning to page 2 leaves the rows on screen, draws no skeleton, and until now
  // said nothing at all for the length of the round trip. One sentence covers all three of the
  // page's reads because a citizen is in one situation — waiting for their projects.
  const waiting = loading || showSkeleton || countsPending
  const showFirstPageError = error !== null && isEmpty
  // `deleting` covers the round trip. It was written for the optimistic removal — deleted
  // because a row that leaves before the cascade returns is a completed delete the platform
  // has not performed — and it is kept because the window it guards did not go with it:
  // between the request settling and the refetch landing, `items` can still be a stale
  // answer, and "Nothing here yet" is a claim about the ACCOUNT, not about this page.
  //
  // `total === 0` CLOSES THE WINDOW `deleteInFlight` DOES NOT. When the
  // delete settles, `setReloadNonce` and the `finally`'s `deletingIds` clear land in ONE
  // commit — and the refetch that `reloadNonce` triggers is an EFFECT, which runs after that
  // commit paints. `total` is the server's own last answer, so it still reads 40 in exactly
  // that frame and discriminates the case. Belt and braces on a screen that answers a
  // question about somebody's whole account.
  const showFirstRun =
    settled &&
    !loading &&
    !deleteInFlight &&
    error === null &&
    isEmpty &&
    total === 0 &&
    appliedQuery === ''
  // Gated on the same flag as the first run, and for the same reason: deleting the last
  // matching row must not claim the search found nothing for the length of the round trip.
  const showNoMatches =
    settled && !loading && !deleteInFlight && error === null && isEmpty && !!appliedQuery
  const showRows = !isEmpty
  // A SLIDING WINDOW, not the first five. `Math.min(totalPages, 5)` rendered pages 1-5
  // whatever page you were on, so from page 6 nothing was marked active and the only way
  // deeper was clicking Next repeatedly — with the page you were reading not shown at all.
  const pageWindow = useMemo(() => {
    const span = Math.min(5, Math.max(totalPages, 1))
    // Centre on the current page, then clamp so the window never runs past either end.
    const first = Math.min(Math.max(page - Math.floor(span / 2), 1), Math.max(totalPages - span + 1, 1))
    return Array.from({ length: span }, (_, i) => first + i)
  }, [page, totalPages])

  // DERIVED FROM WHAT THE ROWS ANSWER, never from what was requested. The footer used to
  // narrate the page that FAILED over the rows that succeeded: 12 projects, page 2 refused,
  // and the caption read `Showing 9–16 of 12`, a range past its own total, above rows 1-8.
  const firstOnPage = useMemo(
    () => (appliedPage - 1) * appliedPageSize + 1,
    [appliedPage, appliedPageSize],
  )
  const lastOnPage = useMemo(() => firstOnPage + items.length - 1, [firstOnPage, items.length])

  return (
    <div className="min-h-screen font-manrope flex flex-col bg-bial-bg">
      <Navbar />

      <main className="flex-1 max-w-6xl mx-auto w-full px-6 py-8">
        <h1 ref={headingRef} tabIndex={-1} className="text-2xl font-extrabold text-tertiary outline-none">Your apps</h1>
        <p className="text-sm text-neutral mt-1">
          Each project is one tool — its app, its description, and its chats.
        </p>

        {/* "Shared with me" (#198) is a second list, not a filter on this one — a colleague's
            project has no page/size/search state of its own to fold into `Committed`, and
            "mine" keeps every line below untouched by adding a sibling arm instead. */}
        <ToggleGroup
          type="single"
          value={tab}
          onValueChange={(v) => v && setTab(v as 'mine' | 'shared')}
          aria-label="Project list"
          className="mt-4"
        >
          <ToggleGroupItem value="mine" className={ACTIVE.trim()}>
            My projects
          </ToggleGroupItem>
          <ToggleGroupItem value="shared" className={ACTIVE.trim()}>
            Shared with me
          </ToggleGroupItem>
        </ToggleGroup>

        {tab === 'shared' ? (
          <div className="mt-6">
            {shared.error !== null && shared.items.length === 0 ? (
              <div
                data-testid="shared-error"
                className="bg-white border border-danger/30 rounded-2xl py-16 px-6 text-center"
              >
                <AlertTriangle size={22} className="mx-auto text-danger mb-3" />
                <p className="text-sm font-semibold text-tertiary">Couldn’t load projects shared with you</p>
                <p className="text-xs text-neutral mt-1 mb-3">The server did not answer. Nothing has been lost.</p>
                <button onClick={() => shared.refresh()} className="text-xs text-primary font-semibold hover:underline">
                  Retry
                </button>
              </div>
            ) : shared.lastPage === null ? (
              <div className={`grid gap-4 ${DENSITY_COLS[density]}`} aria-busy="true">
                {[0, 1, 2].map((i) => (
                  <div key={i} className="bg-white border border-bial-border rounded-2xl px-5 py-4">
                    <Skeleton className="h-4 w-1/2 mb-3" />
                    <Skeleton className="h-3 w-3/4 mb-2" />
                    <Skeleton className="h-3 w-1/4" />
                  </div>
                ))}
              </div>
            ) : shared.items.length === 0 ? (
              // The plain, message-only empty state `showNoMatches` uses — never `showFirstRun`'s,
              // which offers "New project": requirement 13 forbids inviting a recipient to create
              // one from a list that is entirely about what colleagues have shared with them.
              <div
                data-testid="shared-empty"
                className="bg-white border border-bial-border rounded-2xl py-16 px-6 text-center"
              >
                <p className="text-sm font-semibold text-tertiary">Nothing shared with you yet</p>
                <p className="text-xs text-neutral mt-1">
                  When a colleague shares a project with you, it will show up here.
                </p>
              </div>
            ) : (
              <>
                <div className={`grid gap-4 ${DENSITY_COLS[density]}`}>
                  {shared.items.map((project) => (
                    <SharedProjectCard
                      key={project.projectId}
                      project={project}
                      onOpen={() => openSharedProject(project.projectId)}
                    />
                  ))}
                </div>
                {shared.error !== null && (
                  <p role="alert" className="text-xs text-danger text-center mt-4">
                    Couldn’t load more.{' '}
                    <button
                      type="button"
                      onClick={() => shared.refresh()}
                      className="font-semibold text-primary hover:underline"
                    >
                      Retry
                    </button>
                  </p>
                )}
                {shared.hasMore && shared.error === null && (
                  <div className="flex justify-center mt-5">
                    <button
                      type="button"
                      onClick={() => shared.loadMore()}
                      aria-disabled={shared.loading}
                      className="text-xs font-semibold text-primary hover:underline disabled:opacity-50"
                    >
                      {shared.loading ? 'Loading…' : 'Load more'}
                    </button>
                  </div>
                )}
              </>
            )}
          </div>
        ) : (
        <>
        {/* THE PAGE'S ONE POLITE REGION — permanently mounted, empty when nothing is in flight.
            The skeletons below are the only thing this page used to say while it
            loaded, and `index.css` suppresses `.animate-pulse` for a citizen who asks for less
            motion: with that block extended, three grey boxes and five grey rows sit perfectly
            still and say nothing. This is the sentence they now sit under.

            IT IS MOUNTED HERE, NOT IN THE SKELETON, on purpose. A region inserted together with
            its text is missed entirely by several reader-and-browser combinations (`TurnBanner`,
            `LivePreview`), and every skeleton on this page is conditional — so the region has to
            live in the header, which is not. It WRAPS the visible sentence rather than adding an
            `sr-only` copy: two elements carrying one sentence is that sentence read twice, which
            `Announcer.tsx` records as having broken three tests. */}
        <div role="status" aria-live="polite" data-testid="projects-wait">
          {waiting ? <p className="text-sm font-medium text-neutral mt-3">Loading your projects…</p> : null}
        </div>

        {/* WHY THIS IS NOT THE TOAST AT THE BOTTOM OF THIS FILE. That channel is
            documented failure-only — red, `role="alert"`, an `AlertCircle`, and deliberately no
            auto-dismiss, because "something went wrong" waits for its reader. A dead bookmark is
            none of those things: nothing failed, nothing was lost, and nothing the citizen did
            was wrong. Dressing it in the failure styling would tell them, in colour, that it was.

            So the presentation differs in every way the failure toast's own docblock claims as
            meaningful: `role="status"` (polite) rather than `role="alert"` (assertive), a plain
            card in the page's own flow rather than a floating red bar, and an `Info` mark rather
            than the `AlertCircle` the other three sites use to say "this is a failure". */}
        <div role="status" aria-live="polite" data-testid="projects-notice">
          {notice !== null ? (
            <div className="flex items-center gap-2.5 bg-white border border-bial-border rounded-2xl px-4 py-3 mt-4">
              <Info size={15} className="flex-shrink-0 text-neutral" data-testid="projects-notice-marker" />
              <p className="text-sm text-tertiary">{notice}</p>
              <button
                type="button"
                onClick={() => setNotice(null)}
                aria-label="Dismiss notice"
                className="ml-auto text-neutral hover:text-tertiary"
              >
                <X size={15} />
              </button>
            </div>
          ) : null}
        </div>

        {/* Three numbers. Nothing else — no charts.

            AND THEY ANNOUNCE, because they CHANGE without saying so. A citizen who deletes a
            project, or publishes one, watches "In production" go from 3 to 4 with no sound at
            all — the numbers are the page's only report of what just happened to the estate.
            The region is `polite`, never `alert`: nothing here is a failure, and an assertive
            channel would interrupt whatever the person was reading to say "4".

            MOUNTED UNCONDITIONALLY, WRAPPING BOTH ARMS. A region inserted together with its text
            is missed entirely by several reader-and-browser combinations — the rule the wait
            region above already states and the reason it lives in the header. The counts have two
            arms (the cold-failure card and the tiles) and both swap in and out, so the region has
            to sit OUTSIDE the ternary or it is a region that arrives with its own content.

            IT WRAPS THE VISIBLE NUMBERS rather than adding an `sr-only` copy — two elements
            carrying one sentence is that sentence read twice (`Announcer.tsx` records that as
            having broken three tests). */}
        <div role="status" aria-live="polite" data-testid="projects-counts">
        {countsFailedCold ? (
          <div className="flex items-center justify-between gap-3 bg-white border border-danger/30 rounded-2xl px-5 py-4 mt-5 mb-6">
            <p className="text-xs text-danger">Couldn’t load your counts.</p>
            <button
              type="button"
              onClick={() => setReloadNonce((n) => n + 1)}
              className="text-xs font-semibold text-primary hover:underline"
            >
              Retry
            </button>
          </div>
        ) : (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mt-5 mb-6" aria-busy={countsPending}>
          {[
            { label: 'In production', value: counts?.inProduction, hint: 'apps live for BIAL staff right now' },
            { label: 'Total applications', value: counts?.totalApplications, hint: 'created since the platform opened' },
            { label: 'In review, in progress or deployed', value: counts?.inPipeline, hint: 'moving through the pipeline' },
          ].map((card) => (
            <div key={card.label} className="bg-white border border-bial-border rounded-2xl px-5 py-4">
              <p className="text-xs font-semibold text-neutral">{card.label}</p>
              <div className="flex items-baseline gap-2 mt-1.5">
                {card.value === undefined ? (
                  <Skeleton className="h-7 w-10" />
                ) : (
                  <span className="text-2xl font-extrabold text-tertiary tabular-nums">{card.value}</span>
                )}
                <span className="text-[11px] text-neutral/80">{card.hint}</span>
              </div>
            </div>
          ))}
        </div>
        )}
        </div>

        {/* ONE controls row: search, density (grid only), view, New project. The
            New project button lives HERE and nowhere else — adding it to the page
            header too would ship two of them. */}
        <div className="flex items-center gap-3 flex-wrap mb-4">
          <div className="relative flex-1 min-w-[220px] max-w-md">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral" />
            <Input
              value={q}
              onChange={(e) => commit({ q: e.target.value, page: 1 }, 'replace')}
              placeholder="Search projects…"
              aria-label="Search projects"
              className="pl-9"
            />
          </div>

          <div className="ml-auto flex items-center gap-2">
            {view === 'grid' && (
              <ToggleGroup
                type="single"
                value={density}
                onValueChange={(v) => v && chooseDensity(v as Density)}
                aria-label="Card size"
              >
                {(['S', 'M', 'L'] as const).map((d) => (
                  <ToggleGroupItem
                    key={d}
                    value={d}
                    aria-label={`${d} cards`}
                    className={`px-2.5${ACTIVE}`}
                  >
                    {d}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
            )}

            <ToggleGroup
              type="single"
              value={view}
              onValueChange={(v) => v && chooseView(v as View)}
              aria-label="View"
            >
              <ToggleGroupItem value="list" aria-label="List view" className={ACTIVE.trim()}>
                <ListIcon size={15} />
              </ToggleGroupItem>
              <ToggleGroupItem value="grid" aria-label="Grid view" className={ACTIVE.trim()}>
                <LayoutGrid size={15} />
              </ToggleGroupItem>
            </ToggleGroup>

            <button
              onClick={() => setShowCreate(true)}
              className="flex items-center gap-1.5 px-3.5 py-2 text-sm font-semibold bg-primary text-white rounded-lg hover:bg-primary/90 transition whitespace-nowrap"
            >
              <Plus size={15} /> New project
            </button>
          </div>
        </div>

        {showSkeleton ? (
          // Shaped like the view you are in — a card skeleton under a list flashes wrong.
          view === 'list' ? (
            <div className="bg-white border border-bial-border rounded-2xl overflow-hidden" aria-busy="true">
              {[0, 1, 2, 3, 4].map((i) => (
                <div key={i} className="px-4 py-3.5 border-b border-bial-border last:border-0">
                  <Skeleton className="h-4 w-48 mb-2" />
                  <Skeleton className="h-3 w-80" />
                </div>
              ))}
            </div>
          ) : (
            <div className={`grid gap-4 ${DENSITY_COLS[density]}`} aria-busy="true">
              {[0, 1, 2, 3, 4, 5].map((i) => (
                <div key={i} className="bg-white border border-bial-border rounded-2xl px-5 py-4">
                  <Skeleton className="h-4 w-1/2 mb-3" />
                  <Skeleton className="h-3 w-3/4 mb-2" />
                  <Skeleton className="h-3 w-1/4" />
                </div>
              ))}
            </div>
          )
        ) : showFirstPageError ? (
          <div
            data-testid="projects-error"
            className="bg-white border border-danger/30 rounded-2xl py-16 px-6 text-center"
          >
            <AlertTriangle size={22} className="mx-auto text-danger mb-3" />
            <p className="text-sm font-semibold text-tertiary">Couldn’t load your projects</p>
            <p className="text-xs text-neutral mt-1 mb-3">The server did not answer. Nothing has been lost.</p>
            <button
              onClick={() => setReloadNonce((n) => n + 1)}
              className="text-xs text-primary font-semibold hover:underline"
            >
              Retry
            </button>
          </div>
        ) : showFirstRun ? (
          <div
            data-testid="projects-empty"
            className="bg-white border border-bial-border rounded-2xl py-16 px-6 text-center"
          >
            <p className="text-sm font-semibold text-tertiary">Nothing here yet</p>
            <p className="text-xs text-neutral mt-1 mb-4">Create a project and describe what you need inside it.</p>
            {/* The SAME dialog the controls row opens — there is exactly one way to make a
                project. No composer, no chat-kind toggle, no second path. */}
            <button
              onClick={() => setShowCreate(true)}
              className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-semibold bg-primary text-white rounded-lg hover:bg-primary/90 transition"
            >
              <Plus size={15} /> New project
            </button>
          </div>
        ) : showNoMatches ? (
          <div
            data-testid="projects-no-matches"
            className="bg-white border border-bial-border rounded-2xl py-16 px-6 text-center"
          >
            <Search size={22} className="mx-auto text-neutral/50 mb-3" />
            <p className="text-sm font-semibold text-tertiary">No matches</p>
            {/* The query the ROWS answer, not the one still being typed. */}
            <p className="text-xs text-neutral mt-1">No project matches “{appliedQuery}”. Try a different search.</p>
            <button
              onClick={() => commit({ q: '', page: 1 }, 'push')}
              className="text-xs text-primary font-semibold hover:underline mt-2"
            >
              Clear the search
            </button>
          </div>
        ) : null}

        {showRows && (
          <>
            {view === 'list' ? (
              <div className="bg-white border border-bial-border rounded-2xl overflow-hidden">
                {/* The column header the default list was missing. */}
                <div className="flex items-center gap-4 px-4 py-2.5 bg-bial-bg/60 border-b border-bial-border text-[10px] font-bold uppercase tracking-wider text-neutral">
                  <span className="flex-1">Application</span>
                  {/* "Details updated", NOT "Last updated": `updatedAt` moves only when the
                      project ROW is written — a rename or a description edit — and never
                      when the app is built, previewed, published or deployed. Naming it for
                      what it tracks is what stops the column reading as "when the app
                      last changed". */}
                  <span className="hidden sm:block w-28 text-right">Details updated</span>
                  <span className="w-[104px] text-right">Status</span>
                  <span className="w-7" aria-hidden />
                </div>
                {items.map((project) => (
                  <ProjectRow
                    key={project.id}
                    project={project}
                    onOpen={() => openProject(project.id)}
                    onDelete={() => setDeleting(project)}
                  />
                ))}
              </div>
            ) : (
              <div className={`grid gap-4 ${DENSITY_COLS[density]}`}>
                {items.map((project) => (
                  <ProjectCard
                    key={project.id}
                    project={project}
                    onOpen={() => openProject(project.id)}
                    onDelete={() => setDeleting(project)}
                  />
                ))}
              </div>
            )}

            {/* A later page failing keeps the rows above. Say it underneath them — a control
                that quietly does nothing reads as a frozen button.

                This used to BE that frozen button — static text, no control at all. Clicking
                the same page number again is a React no-op (the state value is unchanged,
                so the fetch effect's deps do not change and nothing re-runs); `reloadNonce`
                is the one thing in this effect's deps that is guaranteed to change on every
                bump, regardless of which page failed, so it is what a real retry has to
                touch. */}
            {error !== null && (
              <p role="alert" className="text-xs text-danger text-center mt-4">
                Couldn’t load more projects.{' '}
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
              {/* THE CAPTION ANNOUNCES. Searching, turning a page or changing the
                  page size leaves the rows below silently different and this line the only thing
                  that says how many there now are; a reader was told nothing at all.

                  IT IS THE VISIBLE SENTENCE ITSELF, not an `sr-only` twin, and that is the
                  trade this makes deliberately. The wait region above states the rule this
                  breaks: a region mounted together with its own text is missed by several
                  reader-and-browser combinations, and this one lives inside the `showRows`
                  fence. The alternative was a second element carrying the same sentence — which
                  is that sentence read twice, the failure `Announcer.tsx` records as having
                  broken three tests, and which the caption cannot escape by being hoisted
                  because "Rows per page" would come with it onto an empty list.

                  So the case this does NOT announce is the FIRST appearance of rows: an empty
                  list becoming a full one. That transition is covered — `projects-wait` speaks
                  for the load, and the no-matches and empty states below are the page's answer
                  in between. Every subsequent change — every page turn, every search that still
                  matches, every page-size change — lands in a region that is already mounted,
                  which is the case a citizen actually repeats. */}
              <span className="tabular-nums" role="status" aria-live="polite" data-testid="projects-range">
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

                {/* WRAPS rather than overflowing. The number list reached `right: 534px` on a
                    390px screen with only two pages, which put a horizontal scrollbar on the
                    landing page and got worse with six. */}
                <Pagination className="mx-0 w-auto" aria-label="Projects pagination">
                  <PaginationContent className="flex-wrap justify-end">
                    {/* Jump-to-first/last were missing; at six pages the difference is four
                        clicks or one. */}
                    <PaginationItem>
                      <PaginationLink
                        aria-label="First page"
                        aria-disabled={page <= 1}
                        onClick={() => page > 1 && commit({ page: 1 }, 'push')}
                        className={page <= 1 ? 'pointer-events-none opacity-40' : undefined}
                      >
                        <ChevronsLeft size={15} />
                      </PaginationLink>
                    </PaginationItem>
                    <PaginationItem>
                      <PaginationPrevious
                        aria-disabled={page <= 1}
                        onClick={() => page > 1 && commit({ page: page - 1 }, 'push')}
                        className={page <= 1 ? 'pointer-events-none opacity-40' : undefined}
                      />
                    </PaginationItem>
                    {pageWindow.map((n) => (
                      <PaginationItem key={n}>
                        <PaginationLink isActive={n === appliedPage} onClick={() => commit({ page: n }, 'push')}>
                          {n}
                        </PaginationLink>
                      </PaginationItem>
                    ))}
                    <PaginationItem>
                      <PaginationNext
                        aria-disabled={page >= totalPages}
                        onClick={() => page < totalPages && commit({ page: page + 1 }, 'push')}
                        className={page >= totalPages ? 'pointer-events-none opacity-40' : undefined}
                      />
                    </PaginationItem>
                    <PaginationItem>
                      <PaginationLink
                        aria-label="Last page"
                        aria-disabled={page >= totalPages}
                        onClick={() => page < totalPages && commit({ page: totalPages }, 'push')}
                        className={
                          page >= totalPages ? 'pointer-events-none opacity-40' : undefined
                        }
                      >
                        <ChevronsRight size={15} />
                      </PaginationLink>
                    </PaginationItem>
                  </PaginationContent>
                </Pagination>
              </div>
            </div>
          </>
        )}
        </>
        )}
      </main>

      {showCreate && <ProjectCreateModal onClose={() => setShowCreate(false)} onCreated={handleCreated} />}
      {deleting !== null && (
        <ProjectDeleteDialog
          project={deleting}
          onClose={() => setDeleting(null)}
          onConfirm={(remark) => handleDelete(deleting, remark)}
        />
      )}

      {/* This channel only ever carries a failure (a successful delete is silent — the
          row is just gone), so it is deliberately NOT wired to a dismiss timer the way
          Navbar's and AdminPage's toasts once were. A confirmation may fade on its own;
          something that went wrong waits for the reader to dismiss it, and the reader is the
          only thing that clears this one. The AlertCircle marks it as a failure the same way
          the other two sites now mark theirs, so the appearance carries the fact even
          without reading the words. */}
      {toast !== null && (
        <div
          role="alert"
          data-testid="projects-toast"
          className="fixed bottom-6 left-1/2 -translate-x-1/2 flex items-center gap-3 bg-red-600 text-white text-sm font-medium px-4 py-2.5 rounded-xl shadow-lg"
        >
          <AlertCircle size={15} className="flex-shrink-0" data-testid="projects-toast-marker" />
          {toast}
          <button onClick={() => setToast(null)} aria-label="Dismiss" className="text-white/80 hover:text-white">
            <X size={15} />
          </button>
        </div>
      )}
    </div>
  )
}
