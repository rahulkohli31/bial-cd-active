/**
 * `/projects` — the landing screen. Three numbers, then the citizen's tools.
 *
 * #158 replaced the card grid with TWO views, list default and grid second, numbered
 * pagination in both, and a summary strip above them.
 *
 * PAGINATION IS OFFSET NOW, and that is a deliberate exception the server documents at
 * `list_projects`: `Showing 1-8 of 12` and `Page 1 of 2` both need a `total`, which the
 * keyset envelope declines to compute. What changed here is that the page is COMMITTED
 * state — `page`, `pageSize`, `view` — and one effect fetches from it, rather than a hook
 * that appends forward-only.
 *
 * TWO EMPTY STATES THAT ARE NOT THE SAME THING, carried over because they were already
 * right: zero projects and no search is a first run; zero results WITH a search is a
 * no-match, and it quotes `appliedQuery` — the query the rows answer — never `q`, the live
 * input, which runs 300ms ahead of the data and would flash "you have no projects" at
 * someone who has plenty.
 *
 * THE SKELETON TAKES THE SHAPE OF THE VIEW YOU ARE IN (§11). A card skeleton under a list
 * view flashes the wrong layout for one frame, which reads as a bug.
 *
 * A PAGE-2 FAILURE MUST NOT CLEAR THE ROWS ALREADY ON SCREEN (§11). The error is said
 * underneath them instead.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Plus,
  Search,
  LayoutGrid,
  List as ListIcon,
  AlertTriangle,
  AlertCircle,
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
import { ApiError } from '../utils/apiError'
import ProjectCard from '../components/projects/ProjectCard'
import ProjectRow from '../components/projects/ProjectRow'
import ProjectCreateModal from '../components/projects/ProjectCreateModal'
import ProjectDeleteDialog from '../components/projects/ProjectDeleteDialog'
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

type View = 'list' | 'grid'
type Density = 'S' | 'M' | 'L'

/** Remembered per person so the choice survives a reload (§ "persists across reloads").
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

export default function ProjectsPage(): React.JSX.Element {
  const navigate = useNavigate()
  const [showCreate, setShowCreate] = useState(false)
  const [deleting, setDeleting] = useState<Project | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  const [view, setView] = useState<View>(() => readStored(VIEW_KEY, ['list', 'grid'] as const, 'list'))
  const [density, setDensity] = useState<Density>(() => readStored(DENSITY_KEY, ['S', 'M', 'L'] as const, 'M'))

  // COMMITTED query state — what the rows on screen answer.
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState<number>(PAGE_SIZES[0])
  const [q, setQ] = useState('')
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
  // TRUE ONLY WHEN THE FIRST LOAD FAILED WITH NOTHING TO FALL BACK ON (round-4 finding 12).
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
  // WHERE FOCUS GOES WHEN A DELETE CONFIRMATION CLOSES. Its own trigger — the row's Delete
  // button — is gone by then: the optimistic removal takes the row out immediately, well
  // before the request settles, so Radix's default restore-to-trigger finds a detached node
  // and silently no-ops (round-4 finding 2). `tabIndex={-1}` on the heading below makes it a
  // programmatic focus target without adding it to the tab order.
  const headingRef = useRef<HTMLHeadingElement>(null)

  // The search is debounced, but `page` resets IMMEDIATELY on a keystroke — a cursor into
  // page 3 of the previous query is meaningless against a new one.
  const [debouncedQ, setDebouncedQ] = useState('')
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 300)
    return () => clearTimeout(t)
  }, [q])

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
        // the list the reader is using; the message goes underneath them instead (§11).
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
    if (!loading && totalPages > 0 && page > totalPages) setPage(totalPages)
  }, [loading, page, totalPages])

  const chooseView = (next: View): void => {
    setView(next)
    store(VIEW_KEY, next)
  }
  const chooseDensity = (next: Density): void => {
    setDensity(next)
    store(DENSITY_KEY, next)
  }

  const openProject = (id: string): void => navigate(`/projects/${id}`)

  const handleCreated = (project: Project): void => {
    setShowCreate(false)
    navigate(`/projects/${project.id}`)
  }

  const handleDelete = async (project: Project, remark: string): Promise<void> => {
    setDeletingIds((ids) => new Set(ids).add(project.id))
    setItems((rows) => rows.filter((p) => p.id !== project.id))
    try {
      await deleteProject(project.id, remark)
      setReloadNonce((n) => n + 1) // totals and the counts strip both move
    } catch (caught) {
      // 404 = already gone (another tab). That IS the desired end state, so no toast — but
      // the row still left the list, so `total` and the counts strip have to move with it.
      // Returning early here left "Showing 1–7 of 8" on screen.
      if (caught instanceof ApiError && caught.status === 404) {
        setReloadNonce((n) => n + 1)
        return
      }
      setReloadNonce((n) => n + 1) // put the row back
      setToast(caught instanceof Error ? caught.message : 'Could not delete the project.')
    } finally {
      setDeletingIds((ids) => {
        const next = new Set(ids)
        next.delete(project.id)
        return next
      })
      // CLOSING THE DIALOG IS DEFERRED TO HERE, not the top of this function (round-4
      // finding 9). It used to close synchronously before the request even started —
      // batched into the SAME commit as the optimistic row removal — so the dialog's own
      // `busy` state (the spinner, Cancel disabling) was set and then immediately unmounted
      // in the same render, never actually observable. The backend does real work before
      // answering (force-drop the database, sweep blobs, tear down the container), so this
      // is not decorative: a citizen genuinely waits, across every outcome here — success,
      // 404, or a real failure — which is why this sits in `finally` rather than in only
      // one branch.
      setDeleting(null)
      // FOCUS EXPLICITLY, rather than let Radix try. The row (and its Delete button, the
      // trigger Radix captured at open time) left the DOM the moment the optimistic removal
      // ran, above — long before this `finally` runs — so `onCloseAutoFocus`'s default
      // restore would find a detached node and silently do nothing (round-4 finding 2). The
      // heading is the nearest stable, always-mounted landmark.
      headingRef.current?.focus()
    }
  }

  const isEmpty = items.length === 0
  const settled = appliedQuery !== null
  const showSkeleton = isEmpty && (loading || !settled)
  const showFirstPageError = error !== null && isEmpty
  // `deleting` covers the round trip: the optimistic removal can empty `items` while the
  // request is still in flight, and "Nothing here yet" is a claim about the ACCOUNT, not
  // about this page. Deleting your last row on page 2 must not tell you that you have no
  // projects for the length of a database drop.
  //
  // `total === 0` CLOSES THE WINDOW `deleteInFlight` DOES NOT (round-4 finding 13). When the
  // delete settles, `setReloadNonce` and the `finally`'s `deletingIds` clear land in ONE
  // commit — and the refetch that `reloadNonce` triggers is an EFFECT, which runs after
  // that commit paints. So there is a real rendered frame where `items` is empty (optimistic
  // removal), `deletingIds` is empty (just cleared), and `loading` is still false (the
  // refetch has not started): every guard above passes and an account with 40 projects is
  // told it has none. `total` is the server's own last answer, untouched by the local
  // filtering, so it still reads 40 in exactly that frame and discriminates the case.
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

  // DERIVED FROM WHAT THE ROWS ANSWER, never from what was requested. §11 requires a failed
  // page to leave the rows already on screen intact — which it does — but the footer then
  // narrated the page that FAILED over the rows that succeeded: 12 projects, page 2 refused,
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

        {/* Three numbers. Nothing else — no charts (§1). */}
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
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mt-5 mb-6">
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

        {/* ONE controls row: search, density (grid only), view, New project (§3). The
            New project button lives HERE and nowhere else — it used to sit in the page
            header, and leaving both would ship two of them. */}
        <div className="flex items-center gap-3 flex-wrap mb-4">
          <div className="relative flex-1 min-w-[220px] max-w-md">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral" />
            <Input
              value={q}
              onChange={(e) => {
                setQ(e.target.value)
                setPage(1)
              }}
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
            <div className="bg-white border border-bial-border rounded-2xl overflow-hidden">
              {[0, 1, 2, 3, 4].map((i) => (
                <div key={i} className="px-4 py-3.5 border-b border-bial-border last:border-0">
                  <Skeleton className="h-4 w-48 mb-2" />
                  <Skeleton className="h-3 w-80" />
                </div>
              ))}
            </div>
          ) : (
            <div className={`grid gap-4 ${DENSITY_COLS[density]}`}>
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
                project (§11). No composer, no chat-kind toggle, no second path. */}
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
              onClick={() => {
                setQ('')
                setPage(1)
              }}
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
                {/* The column header the default list was missing (§4). */}
                <div className="flex items-center gap-4 px-4 py-2.5 bg-bial-bg/60 border-b border-bial-border text-[10px] font-bold uppercase tracking-wider text-neutral">
                  <span className="flex-1">Application</span>
                  {/* "Details updated", NOT "Last updated": `updatedAt` moves only when the
                      project ROW is written — a rename or a description edit — and never
                      when the app is built, previewed, published or deployed. Naming it for
                      what it tracks is the honest half of §10's Trap 1. */}
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
                that quietly does nothing reads as a frozen button (§11).
                
                ROUND-4 FINDING 11: this used to BE that frozen button — static text, no
                control at all. Clicking the same page number again is a React no-op (the
                state value is unchanged, so the fetch effect's deps do not change and
                nothing re-runs); `reloadNonce` is the one thing in this effect's deps that
                is guaranteed to change on every bump, regardless of which page failed, so
                it is what a real retry has to touch. */}
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
              <span className="tabular-nums">
                Showing {firstOnPage}–{lastOnPage} of {total}
              </span>

              <div className="flex items-center gap-4">
                <label className="flex items-center gap-2">
                  <span className="whitespace-nowrap">Rows per page</span>
                  <Select
                    value={String(pageSize)}
                    onValueChange={(v) => {
                      setPageSize(Number(v))
                      setPage(1)
                    }}
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
                    {/* §2 spells the control set literally — « ‹ 1 2 › » — and the board draws
                        four icon buttons around the numbers. Jump-to-first/last were missing;
                        at six pages the difference is four clicks or one. */}
                    <PaginationItem>
                      <PaginationLink
                        aria-label="First page"
                        aria-disabled={page <= 1}
                        onClick={() => page > 1 && setPage(1)}
                        className={page <= 1 ? 'pointer-events-none opacity-40' : undefined}
                      >
                        <ChevronsLeft size={15} />
                      </PaginationLink>
                    </PaginationItem>
                    <PaginationItem>
                      <PaginationPrevious
                        aria-disabled={page <= 1}
                        onClick={() => page > 1 && setPage(page - 1)}
                        className={page <= 1 ? 'pointer-events-none opacity-40' : undefined}
                      />
                    </PaginationItem>
                    {pageWindow.map((n) => (
                      <PaginationItem key={n}>
                        <PaginationLink isActive={n === appliedPage} onClick={() => setPage(n)}>
                          {n}
                        </PaginationLink>
                      </PaginationItem>
                    ))}
                    <PaginationItem>
                      <PaginationNext
                        aria-disabled={page >= totalPages}
                        onClick={() => page < totalPages && setPage(page + 1)}
                        className={page >= totalPages ? 'pointer-events-none opacity-40' : undefined}
                      />
                    </PaginationItem>
                    <PaginationItem>
                      <PaginationLink
                        aria-label="Last page"
                        aria-disabled={page >= totalPages}
                        onClick={() => page < totalPages && setPage(totalPages)}
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
      </main>

      {showCreate && <ProjectCreateModal onClose={() => setShowCreate(false)} onCreated={handleCreated} />}
      {deleting !== null && (
        <ProjectDeleteDialog
          project={deleting}
          onClose={() => setDeleting(null)}
          onConfirm={(remark) => handleDelete(deleting, remark)}
        />
      )}

      {/* U15: this channel only ever carries a failure (a successful delete is silent — the
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
