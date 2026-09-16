/**
 * `/projects` — the landing screen: list/grid views, numbered pagination, a summary strip.
 *
 * Pagination is OFFSET, not keyset — `list_projects` needs a `total` for "Page 1 of 2",
 * which the keyset envelope doesn't compute — so `page` and `pageSize` are committed
 * state and an effect re-fetches, rather than a hook that appends forward-only.
 *
 * The summary strip is also this page's ONE filter control: each tile counts a set and then
 * selects it, and the total tile is how a reader gets back out. See `TILES`.
 *
 * WHY THIS EXISTS
 * `page`, `pageSize`, `q` and `filter` live in the URL, not local state — opening a project and
 * pressing Back, reloading, or pasting the address to a colleague all land on the same
 * view, which matters more here than on most lists: the canvas gives a citizen no recents
 * list, so this page IS how a project is found again. `view` and `density` stay in
 * `localStorage` instead, on purpose — they are a person's habit rather than a place in a
 * list, and a shared link should not reach into the reader's window and rearrange it.
 *
 * Two empty states differ: zero projects (first run) vs. zero results with a search or a tile
 * filter, which quotes `appliedQuery` — never the live `q`, which runs 300ms ahead and would
 * flash a false "no projects" mid-type. The skeleton matches the active view, and a
 * page-2 failure is shown below the rows already on screen, never clears them.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Plus, Search, AlertTriangle, AlertCircle, Info, X } from 'lucide-react'
import {
  listProjects,
  listProjectCounts,
  deleteProject,
  type Project,
  type ProjectCounts,
  type ProjectFilter,
} from '../utils/projectApi'
import { ApiError } from '../utils/apiError'
import ProjectCard from '../components/projects/ProjectCard'
import ProjectRow from '../components/projects/ProjectRow'
import type { AppRowMenuProps } from '../components/projects/AppRowMenu'
import ProjectCreateModal from '../components/projects/ProjectCreateModal'
import ProjectDeleteDialog from '../components/projects/ProjectDeleteDialog'
import AppSettingsDialog from '../components/projects/AppSettingsDialog'
import { restartApp, takeAppDown } from '../utils/deployApi'
import { ListPager, ListSkeleton, ViewControls } from '../components/projects/listChrome'
import { COLUMN, DENSITY_COLS, DEFAULT_PAGE_SIZE, PAGE_SIZES } from '../utils/listView'
import { useListView } from '../hooks/useListView'
import { useArrivalNotice } from '../hooks/useArrivalNotice'
import { Input } from '../components/ui/input'
import { Skeleton } from '../components/ui/skeleton'
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
export const PROJECT_GONE_NOTICE = 'That application is no longer available.'

/**
 * The three summary tiles, and the filter each one applies.
 *
 * EACH TILE COUNTS A SET AND THEN SELECTS IT. `filter` is the value the server's own
 * `?filter=` takes, named after the count field it sits beside, so a tile cannot come to show a
 * number its own filter is unable to produce.
 *
 * `null` IS THE CLEAR-ALL, not a third filter: everything is the absence of one, which is why
 * the total tile has no state of its own to get stuck in.
 *
 * The strip renders ABOVE the list/grid branch, so the tiles behave identically in both views,
 * and the filter composes with the search rather than replacing it — there is exactly one
 * filter state on this page and one thing to clear.
 */
const TILES: readonly {
  label: string
  hint: string
  filter: ProjectFilter | null
  read: (counts: ProjectCounts | null) => number | undefined
}[] = [
  {
    label: 'In production',
    hint: 'apps live for BIAL staff right now',
    filter: 'inProduction',
    read: (counts) => counts?.inProduction,
  },
  {
    label: 'Total applications',
    hint: 'created since the platform opened',
    filter: null,
    read: (counts) => counts?.totalApplications,
  },
  {
    label: 'In review, in progress or deployed',
    hint: 'moving through the pipeline',
    filter: 'inPipeline',
    read: (counts) => counts?.inPipeline,
  },
]

/** The four values the URL carries. Everything else about this page is local. */
type Committed = { page: number; pageSize: number; q: string; filter: ProjectFilter | null }

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
  const tile = params.get('filter')
  return {
    page: Number.isInteger(asked) && asked >= 1 ? asked : 1,
    pageSize: (PAGE_SIZES as readonly number[]).includes(size) ? size : DEFAULT_PAGE_SIZE,
    q: params.get('q') ?? '',
    // Anything but the two tiles that filter is NO filter, which is the total tile — the one
    // state this page always has a control to leave. A `?filter=banana` the server would refuse
    // must not leave a reader on an error where their applications should be.
    filter: tile === 'inProduction' || tile === 'inPipeline' ? tile : null,
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
  put('filter', next.filter ?? '', next.filter === null)
  return params
}

export default function ProjectsPage(): React.JSX.Element {
  const navigate = useNavigate()
  const [showCreate, setShowCreate] = useState(false)
  const [deleting, setDeleting] = useState<Project | null>(null)
  // THE SETTINGS DIALOG IS AN OVERLAY OVER THIS LIST, not a route. Opening it changes no
  // address, so a citizen who came from a search and a page is still on that search and that
  // page when they close it.
  const [settingsFor, setSettingsFor] = useState<Project | null>(null)
  // WHICH ROWS HAVE AN OPERATION OF THEIR OWN IN FLIGHT. A set rather than a boolean, for the
  // reason the delete set already gives: two rows acting at once must settle independently.
  // The list does NOT become a polling surface — it refetches once when an operation returns,
  // which is the same refresh a delete already triggers.
  const [actingIds, setActingIds] = useState<ReadonlySet<string>>(() => new Set())

  const [toast, setToast] = useState<string | null>(null)

  const runProduction = useCallback(
    (project: Project, run: (id: string) => Promise<unknown>) => {
      setActingIds((ids) => new Set(ids).add(project.id))
      void (async () => {
        try {
          await run(project.id)
        } catch (caught) {
          // THE SERVER'S STATED REASON. Every refusal on these two routes names something the
          // owner can act on, and flattening them into "something went wrong" throws that away.
          setToast(caught instanceof Error ? caught.message : 'That did not work. Try again.')
        } finally {
          setActingIds((ids) => {
            const next = new Set(ids)
            next.delete(project.id)
            return next
          })
          setReloadNonce((n) => n + 1)
        }
      })()
    },
    [],
  )

  const { view, setView, density, setDensity } = useListView()

  // COMMITTED query state — WHAT WAS ASKED FOR, and it lives in the address bar.
  //
  // ONE `commit` RATHER THAN THREE SETTERS, because `setSearchParams` reads the params of the
  // render it was created in: two calls in one handler would each start from that same snapshot,
  // and the second would silently drop the first's key. Every caller below that changes more than
  // one value — typing, which also resets the page; the rows-per-page control, which does the same
  // — therefore passes both in a single patch.
  const [searchParams, setSearchParams] = useSearchParams()
  const { page, pageSize, q, filter } = readCommitted(searchParams)
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
  // WHICH TILE THE ROWS ANSWER TO. Its own state for the reason `appliedQuery` has one: the
  // empty states below make a claim about the ACCOUNT, and "Nothing here yet" under a filter
  // that simply matched nothing is a claim the page has no business making.
  const [appliedFilter, setAppliedFilter] = useState<ProjectFilter | null>(null)

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

  // ITS OWN REGION rather than a second tenant of `projects-wait`: that one narrates a wait that
  // is still running, and two unrelated sentences sharing one polite region read as one
  // announcement. See the hook for why the sentence rides router state and is scrubbed.
  const { notice, dismiss: dismissNotice } = useArrivalNotice()

  useEffect(() => {
    const id = ++requestId.current
    setLoading(true)
    listProjects({ page, limit: pageSize, q: debouncedQ || undefined, filter: filter ?? undefined })
      .then((res) => {
        if (requestId.current !== id) return
        setItems(res.items)
        setTotal(res.total)
        setTotalPages(res.totalPages)
        setAppliedQuery(debouncedQ)
        setAppliedFilter(filter)
        setAppliedPage(res.page)
        setAppliedPageSize(res.pageSize)
        setError(null)
      })
      .catch((caught: unknown) => {
        if (requestId.current !== id) return
        // The rows already on screen are LEFT INTACT. A later page failing must not blank
        // the list the reader is using; the message goes underneath them instead.
        setError(caught instanceof Error ? caught : new Error('Could not load your applications.'))
        setAppliedQuery(debouncedQ)
        setAppliedFilter(filter)
      })
      .finally(() => {
        if (requestId.current === id) setLoading(false)
      })
  }, [page, pageSize, debouncedQ, filter, reloadNonce])

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

  const openProject = (id: string): void => navigate(`/projects/${id}`)

  /** The row menu's two production entries, or `undefined` where there is nothing serving to act
   *  on. The list and the grid draw different components and must offer the SAME two actions. */
  const liveControls = (project: Project): AppRowMenuProps['live'] =>
    project.isServing
      ? {
          onRestart: () => runProduction(project, restartApp),
          onTakeDown: () => runProduction(project, takeAppDown),
          busy: actingIds.has(project.id),
        }
      : undefined

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
      setToast(caught instanceof Error ? caught.message : 'Could not delete the application.')
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
      // the row survives until the refetch. The control Radix captured is about to be unmounted
      // by that refetch — a beat AFTER the dialog closes — so a restore onto it would put the
      // keyboard on a control that is removed a moment later, which lands right back on the
      // body. The heading is the nearest stable, always-mounted landmark, and it is unaffected
      // when the row goes.
      //
      // NEXT FRAME, NOT THIS ONE, and that is not a nicety. `setDeleting(null)` above does not
      // unmount the dialog until React commits, so a focus() on this line moves focus OUT of a
      // focus trap that is still armed — and the trap pulls it straight back in, after which
      // the dialog unmounts and the keyboard lands on the body. Scheduling here also means
      // this runs BEFORE `ui/dialog.tsx`'s backstop, which schedules its own frame during
      // unmount: it then sees focus already placed and correctly does nothing.
      requestAnimationFrame(() => headingRef.current?.focus())
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
  //
  // AND IT IS GATED ON THE TILE FILTER TOO. "Nothing here yet. Create a project" is a claim
  // about the ACCOUNT; under `?filter=inProduction` the same empty page means only that nothing
  // is live, and `total` is the filtered total, so it reads 0 with forty applications behind it.
  const showFirstRun =
    settled &&
    !loading &&
    !deleteInFlight &&
    error === null &&
    isEmpty &&
    total === 0 &&
    appliedQuery === '' &&
    appliedFilter === null
  // Gated on the same flag as the first run, and for the same reason: deleting the last
  // matching row must not claim the search found nothing for the length of the round trip.
  const showNoMatches =
    settled &&
    !loading &&
    !deleteInFlight &&
    error === null &&
    isEmpty &&
    (!!appliedQuery || appliedFilter !== null)
  const showRows = !isEmpty

  // DERIVED FROM WHAT THE ROWS ANSWER, never from what was requested. The footer used to
  // narrate the page that FAILED over the rows that succeeded: 12 projects, page 2 refused,
  // and the caption read `Showing 9–16 of 12`, a range past its own total, above rows 1-8.
  const firstOnPage = useMemo(
    () => (appliedPage - 1) * appliedPageSize + 1,
    [appliedPage, appliedPageSize],
  )
  const lastOnPage = useMemo(() => firstOnPage + items.length - 1, [firstOnPage, items.length])

  return (
    <div className="min-h-full font-manrope flex flex-col bg-bial-bg">

      <main className="flex-1 max-w-6xl mx-auto w-full px-6 py-8">
        <h1 ref={headingRef} tabIndex={-1} className="text-2xl font-extrabold text-tertiary outline-none">
          Your apps
        </h1>
        <p className="text-sm text-neutral mt-1">
          Each application is one tool — its screens, its description, and its chats.
        </p>

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
          {waiting ? <p className="text-sm font-medium text-neutral mt-3">Loading your applications…</p> : null}
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
                onClick={dismissNotice}
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
          {TILES.map((tile) => {
            // THE TOTAL TILE IS THE CLEAR-ALL. "Total applications" selects everything, which is
            // the same as no filter — so it reads as pressed whenever nothing else is, and
            // pressing it clears the others rather than adding a fourth state nobody can leave.
            const pressed = tile.filter === null ? filter === null : filter === tile.filter
            const value = tile.read(counts)
            // A tile that can produce no rows is not a control. The one you are STANDING IN
            // stays live even at zero, because it is also the way back out.
            const unusable = tile.filter !== null && value === 0 && !pressed
            return (
              <button
                key={tile.label}
                type="button"
                aria-pressed={pressed}
                disabled={unusable}
                // Pressing the tile you are already in clears it. The total tile lands on the
                // same answer from either side, which is what makes it the clear-all rather
                // than a fourth state.
                onClick={() => commit({ filter: pressed ? null : tile.filter, page: 1 }, 'push')}
                className={`text-left bg-white border rounded-2xl px-5 py-4 transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 disabled:opacity-60 disabled:cursor-not-allowed ${
                  pressed
                    ? 'border-primary/40 ring-1 ring-primary/30'
                    : 'border-bial-border hover:border-primary/40'
                }`}
              >
                <p className="text-xs font-semibold text-neutral">{tile.label}</p>
                <div className="flex items-baseline gap-2 mt-1.5">
                  {value === undefined ? (
                    <Skeleton className="h-7 w-10" />
                  ) : (
                    // STILL A NUMBER IN ITS OWN ELEMENT, inside the live region above: the tile
                    // became a control, and the count still has to read as a count when it
                    // changes under a delete or a publish.
                    <span className="text-2xl font-extrabold text-tertiary tabular-nums">{value}</span>
                  )}
                  <span className="text-[11px] text-neutral/80">{tile.hint}</span>
                </div>
              </button>
            )
          })}
        </div>
        )}
        </div>

        {/* ONE controls row: search, density (grid only), view, Create App. The
            Create App button lives HERE and nowhere else — adding it to the page
            header too would ship two of them. */}
        <div className="flex items-center gap-3 flex-wrap mb-4">
          <div className="relative flex-1 min-w-[220px] max-w-md">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral" />
            <Input
              value={q}
              onChange={(e) => commit({ q: e.target.value, page: 1 }, 'replace')}
              placeholder="Search applications…"
              aria-label="Search applications"
              className="pl-9"
            />
          </div>

          <div className="ml-auto flex items-center gap-2">
            <ViewControls view={view} density={density} onView={setView} onDensity={setDensity} />

            <button
              onClick={() => setShowCreate(true)}
              className="flex items-center gap-1.5 px-3.5 py-2 text-sm font-semibold bg-primary text-white rounded-lg hover:bg-primary/90 transition whitespace-nowrap"
            >
              <Plus size={15} /> Create App
            </button>
          </div>
        </div>

        {showSkeleton ? (
          <ListSkeleton view={view} density={density} />
        ) : showFirstPageError ? (
          <div
            data-testid="projects-error"
            className="bg-white border border-danger/30 rounded-2xl py-16 px-6 text-center"
          >
            <AlertTriangle size={22} className="mx-auto text-danger mb-3" />
            <p className="text-sm font-semibold text-tertiary">Couldn’t load your applications</p>
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
            <p className="text-xs text-neutral mt-1 mb-4">Create an application and describe what you need inside it.</p>
            {/* The SAME dialog the controls row opens — there is exactly one way to make a
                project. No composer, no chat-kind toggle, no second path. */}
            <button
              onClick={() => setShowCreate(true)}
              className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-semibold bg-primary text-white rounded-lg hover:bg-primary/90 transition"
            >
              <Plus size={15} /> Create App
            </button>
          </div>
        ) : showNoMatches ? (
          <div
            data-testid="projects-no-matches"
            className="bg-white border border-bial-border rounded-2xl py-16 px-6 text-center"
          >
            <Search size={22} className="mx-auto text-neutral/50 mb-3" />
            <p className="text-sm font-semibold text-tertiary">No matches</p>
            {/* The query the ROWS answer, not the one still being typed. With a tile selected
                and nothing typed there is no phrase to quote, and quoting an empty one would
                print `No application matches “”`. */}
            <p className="text-xs text-neutral mt-1">
              {appliedQuery
                ? `No application matches “${appliedQuery}”. Try a different search.`
                : 'No application matches that filter.'}
            </p>
            {/* ONE BUTTON THAT CLEARS WHATEVER IS APPLIED, because clearing only half of a
                search-and-filter pair lands the reader on this same card again. The label says
                which half, or both, so the press is never a surprise. */}
            <button
              onClick={() => commit({ q: '', filter: null, page: 1 }, 'push')}
              className="text-xs text-primary font-semibold hover:underline mt-2"
            >
              {appliedQuery && appliedFilter !== null
                ? 'Clear the search and the filter'
                : appliedQuery
                  ? 'Clear the search'
                  : 'Clear the filter'}
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
                  {/* LEFT-ALIGNED, and the heading sits over its own column at the same fixed
                      width the cell uses — which is the whole of what makes the dates scan. */}
                  <span className={`hidden sm:block ${COLUMN.date}`}>Created</span>
                  {/* "Details updated", NOT "Last updated": `updatedAt` moves only when the
                      project ROW is written — a rename or a description edit — and never
                      when the app is built, previewed, published or deployed. Naming it for
                      what it tracks is what stops the column reading as "when the app
                      last changed". */}
                  <span className={`hidden sm:block ${COLUMN.date}`}>Details updated</span>
                  <span className={`${COLUMN.status} text-center`}>Status</span>
                  <span className={COLUMN.menu} aria-hidden />
                </div>
                {items.map((project) => (
                  <ProjectRow
                    key={project.id}
                    project={project}
                    onOpen={() => openProject(project.id)}
                    onSettings={() => setSettingsFor(project)}
                    onDelete={() => setDeleting(project)}
                    live={liveControls(project)}
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
                    onSettings={() => setSettingsFor(project)}
                    onDelete={() => setDeleting(project)}
                    live={liveControls(project)}
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
                Couldn’t load more applications.{' '}
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

                <ListPager
                  page={page}
                  activePage={appliedPage}
                  totalPages={totalPages}
                  onGo={(next) => commit({ page: next }, 'push')}
                  label="Applications pagination"
                />
              </div>
            </div>
          </>
        )}
      </main>

      {showCreate && <ProjectCreateModal onClose={() => setShowCreate(false)} onCreated={handleCreated} />}
      {settingsFor !== null && (
        <AppSettingsDialog
          project={settingsFor}
          onProjectUpdate={(updated) => {
            // The list is the source of truth for what a row says, so a rename saved in the
            // dialog has to reach it — and the dialog itself has to keep showing the stored
            // values rather than the ones it opened with.
            setSettingsFor(updated)
            setReloadNonce((n) => n + 1)
          }}
          onClose={() => setSettingsFor(null)}
          onProductionSettled={() => setReloadNonce((n) => n + 1)}
          // DELETE HANDS OFF TO THE SAME CONFIRMATION THE MENU OPENS, and the settings dialog
          // closes on the way: two dialogs stacked over one another is two focus traps, and the
          // one underneath is not the one being answered.
          onDelete={() => {
            setDeleting(settingsFor)
            setSettingsFor(null)
          }}
        />
      )}

      {deleting !== null && (
        <ProjectDeleteDialog
          project={deleting}
          onClose={() => setDeleting(null)}
          onConfirm={(remark) => handleDelete(deleting, remark)}
        />
      )}

      {/* This channel only ever carries a failure (a successful delete is silent — the
          row is just gone), so it is deliberately NOT wired to a dismiss timer the way
          AdminPage's toast is. A confirmation may fade on its own;
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
