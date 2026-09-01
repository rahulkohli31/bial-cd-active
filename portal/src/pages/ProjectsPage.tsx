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
import { Plus, Search, LayoutGrid, List as ListIcon, AlertTriangle, AlertCircle, X } from 'lucide-react'
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
  const [appliedQuery, setAppliedQuery] = useState<string | null>(null)

  const [items, setItems] = useState<Project[]>([])
  const [total, setTotal] = useState(0)
  const [totalPages, setTotalPages] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const [counts, setCounts] = useState<ProjectCounts | null>(null)
  const [reloadNonce, setReloadNonce] = useState(0)

  // Out-of-order guard: a slow page that lands after a newer one must not overwrite it.
  const requestId = useRef(0)

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
    listProjectCounts()
      .then((c) => alive && setCounts(c))
      .catch(() => alive && setCounts(null))
    return () => {
      alive = false
    }
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

  const handleDelete = async (project: Project): Promise<void> => {
    setDeleting(null)
    setItems((rows) => rows.filter((p) => p.id !== project.id))
    try {
      await deleteProject(project.id)
      setReloadNonce((n) => n + 1) // totals and the counts strip both move
    } catch (caught) {
      // 404 = already gone (another tab). That IS the desired end state.
      if (caught instanceof ApiError && caught.status === 404) return
      setReloadNonce((n) => n + 1) // put the row back
      setToast(caught instanceof Error ? caught.message : 'Could not delete the project.')
    }
  }

  const isEmpty = items.length === 0
  const settled = appliedQuery !== null
  const showSkeleton = isEmpty && (loading || !settled)
  const showFirstPageError = error !== null && isEmpty
  const showFirstRun = settled && !loading && error === null && isEmpty && appliedQuery === ''
  const showNoMatches = settled && !loading && error === null && isEmpty && !!appliedQuery
  const showRows = !isEmpty
  const firstOnPage = useMemo(() => (page - 1) * pageSize + 1, [page, pageSize])
  const lastOnPage = useMemo(() => firstOnPage + items.length - 1, [firstOnPage, items.length])

  return (
    <div className="min-h-screen font-manrope flex flex-col bg-bial-bg">
      <Navbar />

      <main className="flex-1 max-w-6xl mx-auto w-full px-6 py-8">
        <h1 className="text-2xl font-extrabold text-tertiary">Your apps</h1>
        <p className="text-sm text-neutral mt-1">
          Each project is one tool — its app, its description, and its chats.
        </p>

        {/* Three numbers. Nothing else — no charts (§1). */}
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
                  <ToggleGroupItem key={d} value={d} aria-label={`${d} cards`} className="px-2.5">
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
              <ToggleGroupItem value="list" aria-label="List view">
                <ListIcon size={15} />
              </ToggleGroupItem>
              <ToggleGroupItem value="grid" aria-label="Grid view">
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
                that quietly does nothing reads as a frozen button (§11). */}
            {error !== null && (
              <p role="alert" className="text-xs text-danger text-center mt-4">
                Couldn’t load more projects.
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
                  Page {page} of {Math.max(totalPages, 1)}
                </span>

                <Pagination className="mx-0 w-auto">
                  <PaginationContent>
                    <PaginationItem>
                      <PaginationPrevious
                        aria-disabled={page <= 1}
                        onClick={() => page > 1 && setPage(page - 1)}
                        className={page <= 1 ? 'pointer-events-none opacity-40' : undefined}
                      />
                    </PaginationItem>
                    {Array.from({ length: Math.min(totalPages, 5) }, (_, i) => i + 1).map((n) => (
                      <PaginationItem key={n}>
                        <PaginationLink isActive={n === page} onClick={() => setPage(n)}>
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
          onConfirm={() => handleDelete(deleting)}
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
