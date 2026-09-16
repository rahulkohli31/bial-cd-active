/**
 * ProjectsPage (`/projects`) — the landing screen: three numbers, then list or grid.
 *
 * The data layer is mocked at the module boundary; the page's own paging state runs for
 * real, because that is what is being exercised. A LocationProbe outside the Routes reports
 * the current path so navigation is observable without a real project-home page, and it
 * uses the `vi.hoisted` + `MemoryRouter` shape this directory's suites share.
 */
import { useEffect, useRef } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within, act } from '@testing-library/react'
import {
  MemoryRouter,
  Routes,
  Route,
  useLocation,
  useNavigate,
  useNavigationType,
} from 'react-router-dom'

const h = vi.hoisted(() => ({
  listProjects: vi.fn(),
  listProjectCounts: vi.fn(),
  createProject: vi.fn(),
  deleteProject: vi.fn(),
  listProjectConversations: vi.fn(),
  restartApp: vi.fn(),
  takeAppDown: vi.fn(),
  patchProject: vi.fn(),
}))

vi.mock('../../utils/projectApi', () => ({
  listProjects: h.listProjects,
  listProjectCounts: h.listProjectCounts,
  createProject: h.createProject,
  deleteProject: h.deleteProject,
  patchProject: h.patchProject,
}))
vi.mock('../../utils/deployApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  restartApp: h.restartApp,
  takeAppDown: h.takeAppDown,
}))
vi.mock('../../utils/conversationApi', () => ({
  // The send path creates the chat before its first upload; stubbed so no network is reached.
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
  CONVERSATION_LIST_CAP: 200,
}))

import ProjectsPage, { PROJECT_GONE_NOTICE } from '../ProjectsPage'
import { ApiError } from '../../utils/apiError'
import type { Project } from '../../utils/projectApi'

function LocationProbe(): React.JSX.Element {
  const loc = useLocation()
  return (
    <>
      <div data-testid="location">{loc.pathname}</div>
      {/* Page, size and query live in the address bar, so the address bar is now an
          assertable output of this page rather than scenery. */}
      <div data-testid="location-search">{loc.search}</div>
    </>
  )
}

/**
 * THE HISTORY STACK, COUNTED — because "the debounce must not push an entry per keystroke" is a
 * claim about how DEEP the stack is, and no react-router hook reports that.
 *
 * `useNavigationType` reports how the CURRENT entry was reached, so a probe that observes every
 * location change can keep the tally itself: PUSH adds an entry, POP removes one, REPLACE swaps
 * the top and changes nothing. The first run is skipped — mounting is not a navigation, and
 * MemoryRouter reports its initial entry as a POP, which would otherwise count the page's own
 * arrival as a step backwards.
 */
const stack = { depth: 1, types: [] as string[] }
function HistoryProbe(): null {
  const type = useNavigationType()
  const { key } = useLocation()
  const mounting = useRef(true)
  useEffect(() => {
    if (mounting.current) {
      mounting.current = false
      return
    }
    stack.types.push(type)
    if (type === 'PUSH') stack.depth += 1
    else if (type === 'POP') stack.depth -= 1
  }, [key, type])
  return null
}

/** The browser's Back button, which RTL cannot press. Named so it collides with nothing. */
function BackButton(): React.JSX.Element {
  const navigate = useNavigate()
  return <button onClick={() => navigate(-1)}>browser back</button>
}

type Entry = string | { pathname: string; search?: string; state?: { notice: string } }

function renderPage(entry: Entry = '/projects') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <LocationProbe />
      <HistoryProbe />
      <BackButton />
      <Routes>
        <Route path="/projects" element={<ProjectsPage />} />
        <Route path="/projects/:id" element={<div data-testid="project-home">home</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

/**
 * A LIST THAT ANSWERS THE PAGE IT WAS ACTUALLY ASKED FOR.
 *
 * A static `mockResolvedValue` always answers `page: 1`, which pins `appliedPage` at 1 whatever
 * was clicked — and every assertion about restoring page 3 would then pass against a page 3 that
 * never arrived.
 */
function answersWithTheRequestedPage(
  rows: Project[],
  meta: { total: number; totalPages: number },
): void {
  h.listProjects.mockImplementation((args: { page: number; limit: number; q?: string }) =>
    Promise.resolve(page(rows, { ...meta, page: args.page, pageSize: args.limit })),
  )
}

/**
 * Delete an application the way a citizen now must: through the row's `⋯` menu. Two steps on
 * purpose — a list does not hand out a one-click route to an irreversible action. The trigger is
 * Radix, so it opens on POINTERDOWN rather than click.
 */
async function deleteFromRowMenu(): Promise<void> {
  fireEvent.pointerDown(screen.getByTestId('app-menu-row'))
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }))
}

/** Radix's Select is a button, not a `<select>`: `fireEvent.change` on it silently no-ops. */
async function pickRowsPerPage(option: string): Promise<void> {
  fireEvent.click(screen.getByRole('combobox', { name: 'Rows per page' }))
  fireEvent.click(await screen.findByRole('option', { name: option }))
}

const mkProject = (id: string, name: string, over: Partial<Project> = {}): Project => ({
  id,
  name,
  description: 'A tool',
  appId: null,
  isServing: false,
  appStatus: null,
  hasRelaunchableSnapshot: null,
  hasSavedSnapshot: null,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'owner',
  ...over,
})

const page = (
  items: Project[],
  over: Partial<{ page: number; pageSize: number; total: number; totalPages: number }> = {},
) => ({
  items,
  page: 1,
  pageSize: 8,
  total: items.length,
  totalPages: items.length === 0 ? 0 : 1,
  ...over,
})

const COUNTS = { inProduction: 2, totalApplications: 5, inPipeline: 1 }

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  stack.depth = 1
  stack.types.length = 0
  h.listProjects.mockResolvedValue(page([]))
  h.listProjectCounts.mockResolvedValue(COUNTS)
  h.listProjectConversations.mockResolvedValue([])
  h.restartApp.mockResolvedValue({ deploymentId: 'd1' })
  h.takeAppDown.mockResolvedValue({ message: 'done' })
})
afterEach(() => cleanup())

// --- what changes without saying so ----------------------------------

describe('★ the two things on this page that change silently now announce', () => {
  // The page already had two working regions — the wait sentence and the dead-bookmark notice.
  // What stayed genuinely uncovered are these two: the numbers, and the range caption. A citizen
  // who deletes a project watches "In production" go from 3 to 4 in silence, and a search
  // rewrites the rows underneath with nothing said about how many there now are.

  it('announces the three numbers, and the region is mounted before they arrive', async () => {
    let resolve: (c: typeof COUNTS) => void = () => {}
    h.listProjectCounts.mockReturnValue(new Promise((r) => (resolve = r)))

    renderPage()

    // MOUNTED FIRST. A region inserted together with its text is missed entirely by several
    // reader-and-browser combinations — the rule the wait region above it already states — so
    // the region has to exist while the numbers are still skeletons.
    const region = screen.getByTestId('projects-counts')
    expect(region.getAttribute('aria-live')).toBe('polite')
    expect(region.getAttribute('role')).toBe('status')
    expect(region.textContent).not.toContain('7')

    resolve({ inProduction: 7, totalApplications: 9, inPipeline: 2 })

    // …and the numbers land INSIDE it, so the change is what gets read.
    await waitFor(() => expect(screen.getByTestId('projects-counts').textContent).toContain('7'))
    expect(screen.getByTestId('projects-counts').textContent).toContain('In production')
  })

  it('keeps the region when the counts fail cold, rather than swapping it out', async () => {
    // The counts have two arms and both swap in and out. A region inside the ternary would
    // arrive with its own content on whichever arm won — which is the same defect as not
    // having one.
    h.listProjectCounts.mockRejectedValue(new Error('nope'))
    renderPage()

    await waitFor(() => expect(screen.getByText(/couldn’t load your counts/i)).toBeTruthy())
    const region = screen.getByTestId('projects-counts')
    expect(region.getAttribute('aria-live')).toBe('polite')
    expect(region.textContent).toContain('Couldn’t load your counts')
  })

  it('announces the range caption, and it really does change', async () => {
    h.listProjects.mockResolvedValue(
      page([mkProject('p1', 'One'), mkProject('p2', 'Two')], { total: 14, totalPages: 2, pageSize: 8 }),
    )
    renderPage()

    const range = await screen.findByTestId('projects-range')
    expect(range.getAttribute('aria-live')).toBe('polite')
    const before = range.textContent
    expect(before).toContain('of 14')

    // PAIRED WITH A REAL CHANGE, so a static page cannot pass this: the caption has to say
    // something different after the search, not merely carry the attribute.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'One')], { total: 1, totalPages: 1 }))
    fireEvent.change(screen.getByLabelText('Search applications'), { target: { value: 'One' } })
    await waitFor(() => expect(screen.getByTestId('projects-range').textContent).not.toBe(before))
    expect(screen.getByTestId('projects-range').textContent).toContain('of 1')
  })
})

// --- the three numbers ---------------------------------------------------------

describe('the dashboard strip', () => {
  it('shows the three numbers', async () => {
    renderPage()

    expect(await screen.findByText('2')).toBeTruthy()
    expect(screen.getByText('5')).toBeTruthy()
    expect(screen.getByText('1')).toBeTruthy()
    expect(screen.getByText('In production')).toBeTruthy()
    expect(screen.getByText('Total applications')).toBeTruthy()
  })

  it('does not render a 0 while the counts are still in flight', async () => {
    // A skeleton, not a confident zero: "0 in production" is a claim, and an unanswered
    // request has not earned it.
    let resolve: (c: typeof COUNTS) => void = () => {}
    h.listProjectCounts.mockReturnValue(new Promise((r) => (resolve = r)))

    renderPage()

    expect(screen.getByText('In production')).toBeTruthy()
    expect(screen.queryByText('0')).toBeNull()

    resolve({ inProduction: 0, totalApplications: 0, inPipeline: 0 })
    await waitFor(() => expect(screen.getAllByText('0').length).toBeGreaterThan(0))
  })
})

describe('list and grid', () => {
  it('defaults to LIST, with the column header the grid does not have', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Visitor Log')]))
    renderPage()

    expect(await screen.findByText('Visitor Log')).toBeTruthy()
    expect(screen.getByText('Application')).toBeTruthy()
    // "Details updated", never "Last updated": `updatedAt` moves on a rename or a
    // description edit and never on a build, publish or deploy.
    expect(screen.getByText('Details updated')).toBeTruthy()
    expect(screen.queryByText('Last updated')).toBeNull()
  })

  it('switches to grid and remembers the choice across a remount', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Visitor Log')]))
    const first = renderPage()
    await screen.findByText('Visitor Log')

    fireEvent.click(screen.getByLabelText('Grid view'))
    await waitFor(() => expect(screen.queryByText('Application')).toBeNull())

    first.unmount()
    renderPage()
    await screen.findByText('Visitor Log')
    expect(screen.queryByText('Application')).toBeNull() // still grid
  })

  it('offers the S/M/L density control in grid only', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Visitor Log')]))
    renderPage()
    await screen.findByText('Visitor Log')

    expect(screen.queryByLabelText('M cards')).toBeNull() // list view
    fireEvent.click(screen.getByLabelText('Grid view'))
    await waitFor(() => expect(screen.getByLabelText('M cards')).toBeTruthy())
  })
})

describe('a row', () => {
  it('shows the status the DEPLOYMENT supports, not the lifecycle', async () => {
    h.listProjects.mockResolvedValue(
      page([
        mkProject('p1', 'Serving', { isServing: true, appStatus: 'approved' }),
        mkProject('p2', 'Approved Only', { isServing: false, appStatus: 'approved' }),
        mkProject('p3', 'Nothing', { isServing: false, appStatus: null }),
      ]),
    )
    renderPage()

    expect(await screen.findByText('Live')).toBeTruthy()
    // The SAME `approved` status reads differently because only one of them is serving.
    expect(screen.getByText('Approved')).toBeTruthy()
    expect(screen.getByText('Nothing built yet')).toBeTruthy()
  })

  it('keeps the row menu OUT of the open button', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Visitor Log')]))
    renderPage()
    await screen.findByText('Visitor Log')

    const menu = screen.getByTestId('app-menu-row')
    const open = screen.getByRole('button', { name: 'Visitor Log' })
    // Neither contains the other. A row that nests them is a button inside a button.
    expect(open.contains(menu)).toBe(false)
    expect(menu.contains(open)).toBe(false)
    expect(menu.closest('button')).toBe(menu)
  })

  it('gives the list the two date columns it was missing', async () => {
    h.listProjects.mockResolvedValue(
      page([
        {
          ...mkProject('p1', 'Visitor Log'),
          createdAt: '2026-08-12T09:00:00Z',
          updatedAt: '2026-09-14T09:00:00Z',
        },
      ]),
    )
    renderPage()
    await screen.findByText('Visitor Log')

    // The headings and the cells, because a column is both — a heading over nothing, or a date
    // under no heading, is half a column.
    expect(screen.getByText('Created')).toBeTruthy()
    expect(screen.getByText('Details updated')).toBeTruthy()
    expect(screen.getByText('12 Aug 2026')).toBeTruthy()
    expect(screen.getByText('14 Sep 2026')).toBeTruthy()
  })

  it('offers no one-click delete anywhere in the list', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Visitor Log')]))
    renderPage()
    await screen.findByText('Visitor Log')

    // Absence PAIRED WITH LIVENESS: the row really rendered, so this is the control being gone
    // rather than the list failing to draw.
    expect(screen.queryByLabelText('Delete Visitor Log')).toBeNull()
    expect(screen.getByTestId('app-menu-row')).toBeTruthy()
  })

  it('opens the project from the name', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Visitor Log')]))
    renderPage()
    await screen.findByText('Visitor Log')

    fireEvent.click(screen.getByRole('button', { name: 'Visitor Log' }))

    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/projects/p1'))
  })

  it('renders a null description as words, not a blank or the literal null', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Visitor Log', { description: null })]))
    renderPage()

    expect(await screen.findByText('No description yet')).toBeTruthy()
  })
})

describe('numbered pagination', () => {
  it('reports the window and the total, and asks the server for page 2', async () => {
    h.listProjects.mockResolvedValue(
      page([mkProject('p1', 'Alpha'), mkProject('p2', 'Beta')], {
        total: 12,
        totalPages: 2,
        pageSize: 8,
      }),
    )
    renderPage()
    await screen.findByText('Alpha')

    expect(screen.getByText(/Showing 1–2 of 12/)).toBeTruthy()
    expect(screen.getByText(/Page 1 of 2/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: '2' }))

    await waitFor(() =>
      expect(h.listProjects).toHaveBeenCalledWith(expect.objectContaining({ page: 2 })),
    )
  })

  it('a search resets to page 1', async () => {
    // Page 3 of the previous query means nothing against a new one.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')], { total: 40, totalPages: 5 }))
    renderPage()
    await screen.findByText('Alpha')

    fireEvent.click(screen.getByRole('button', { name: '3' }))
    await waitFor(() =>
      expect(h.listProjects).toHaveBeenCalledWith(expect.objectContaining({ page: 3 })),
    )

    fireEvent.change(screen.getByLabelText('Search applications'), { target: { value: 'vip' } })

    await waitFor(
      () => expect(h.listProjects).toHaveBeenCalledWith(expect.objectContaining({ page: 1, q: 'vip' })),
      { timeout: 3000 },
    )
  })
})

describe('the states', () => {
  it('first run offers exactly ONE way to make a project', async () => {
    renderPage()

    const empty = await screen.findByTestId('projects-empty')
    expect(within(empty).getByText('Nothing here yet')).toBeTruthy()
    // No composer, no chat-kind toggle, no second "name it yourself" path.
    expect(within(empty).getAllByRole('button')).toHaveLength(1)
  })

  it('no matches quotes the query the ROWS answer, not the one being typed', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')]))
    renderPage()
    await screen.findByText('Alpha')

    h.listProjects.mockResolvedValue(page([]))
    fireEvent.change(screen.getByLabelText('Search applications'), { target: { value: 'zzz' } })

    const noMatch = await screen.findByTestId('projects-no-matches', undefined, { timeout: 3000 })
    expect(noMatch.textContent).toContain('zzz')
  })

  it('a FIRST page failure is full-width and retryable', async () => {
    h.listProjects.mockRejectedValue(new Error('boom'))
    renderPage()

    const err = await screen.findByTestId('projects-error')
    expect(err.textContent).toMatch(/Couldn’t load your applications/)

    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')]))
    fireEvent.click(within(err).getByText('Retry'))

    expect(await screen.findByText('Alpha')).toBeTruthy()
  })

  it('a LATER page failure keeps the rows already on screen', async () => {
    // The rule under test: never blank the list the reader is using.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')], { total: 12, totalPages: 2 }))
    renderPage()
    await screen.findByText('Alpha')

    h.listProjects.mockRejectedValue(new Error('boom'))
    fireEvent.click(screen.getByRole('button', { name: '2' }))

    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/Couldn’t load more/))
    expect(screen.getByText('Alpha')).toBeTruthy() // still there
    expect(screen.queryByTestId('projects-error')).toBeNull() // not the full-width state
  })

  it('a later page failure can actually be RETRIED', async () => {
    // The message underneath the rows was static text with no control.
    // Clicking the same page number again is a React no-op — the state value is unchanged,
    // so the fetch effect's deps do not change and nothing re-runs — which left the failure
    // unrecoverable without a reload. `reloadNonce` is the dep that always changes.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')], { total: 12, totalPages: 2 }))
    renderPage()
    await screen.findByText('Alpha')

    h.listProjects.mockRejectedValue(new Error('boom'))
    fireEvent.click(screen.getByRole('button', { name: '2' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/Couldn’t load more/))

    h.listProjects.mockResolvedValue(page([mkProject('p2', 'Beta')], { total: 12, totalPages: 2, page: 2 }))
    fireEvent.click(screen.getByRole('button', { name: /retry/i }))

    expect(await screen.findByText('Beta')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('a FIRST-LOAD counts failure offers a retry instead of pulsing forever', async () => {
    // The catch was made a total no-op, which is right for a REFRESH
    // (keep the last known-good numbers) and wrong for a first load — `counts` stayed null,
    // all three tiles skeleton-pulsed over a working list, and nothing but a delete could
    // ever bump `reloadNonce` to ask again.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')], { total: 8, totalPages: 1 }))
    h.listProjectCounts.mockRejectedValueOnce(new Error('counts boom'))
    renderPage()
    await screen.findByText('Alpha') // the list itself is fine

    const retry = await screen.findByRole('button', { name: /retry/i })
    expect(screen.getByText(/Couldn’t load your counts/)).toBeTruthy()

    h.listProjectCounts.mockResolvedValue(COUNTS)
    fireEvent.click(retry)

    expect(await screen.findByText(String(COUNTS.totalApplications))).toBeTruthy()
    expect(screen.queryByText(/Couldn’t load your counts/)).toBeNull()
  })

  it('a REFRESH counts failure stays silent and keeps the numbers', async () => {
    // The other half, and the reason the first-load case needed its own state rather than
    // just un-silencing the catch: once there ARE numbers, a failed refresh must not replace
    // them with an error — slightly stale beats visibly broken.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')], { total: 8, totalPages: 1 }))
    renderPage()
    await screen.findByText(String(COUNTS.totalApplications))

    h.listProjectCounts.mockRejectedValue(new Error('later boom'))
    fireEvent.click(screen.getByLabelText('Grid view')) // any re-render; counts refetch on nonce
    await waitFor(() => expect(screen.queryByLabelText('M cards')).toBeTruthy())

    expect(screen.getByText(String(COUNTS.totalApplications))).toBeTruthy()
    expect(screen.queryByText(/Couldn’t load your counts/)).toBeNull()
  })
})

describe('create and delete', () => {
  it('has exactly ONE Create App button', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')]))
    renderPage()
    await screen.findByText('Alpha')

    // The trap: adding it to the controls row without deleting the page
    // header's one ships two.
    expect(screen.getAllByRole('button', { name: /Create App/i })).toHaveLength(1)
  })

  it('a 404 on delete removes the row with no error toast', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')]))
    h.deleteProject.mockRejectedValue(new ApiError('gone', 404))
    renderPage()
    await screen.findByText('Alpha')

    await deleteFromRowMenu()
    // The dialog gates on a 5-50 word reason, which the page forwards to the
    // API. Its own bounds are asserted in ProjectDeleteDialog.test.tsx; here it just has to
    // be valid so the delete runs.
    fireEvent.change(await screen.findByLabelText(/why are you deleting/i), {
      target: { value: 'no longer needed by ground ops' },
    })
    fireEvent.click(screen.getByRole('button', { name: /delete application/i }))

    await waitFor(() => expect(h.deleteProject).toHaveBeenCalled())
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('a delete failure does not auto-dismiss, and carries a failure marker', async () => {
    // Rewriting a file is the easiest way to drop a behaviour nobody restates,
    // so it is restated: the marker distinguishes a failure at a glance, and it has its own
    // testid because the dismiss button's X is an svg too — "some icon in the toast" would
    // let a mutant that deletes the marker pass.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')]))
    h.deleteProject.mockRejectedValue(new ApiError('Could not delete the application.', 500))
    renderPage()
    await screen.findByText('Alpha')

    await deleteFromRowMenu()
    fireEvent.change(await screen.findByLabelText(/why are you deleting/i), {
      target: { value: 'no longer needed by ground ops' },
    })
    fireEvent.click(screen.getByRole('button', { name: /delete application/i }))

    expect(await screen.findByTestId('projects-toast-marker')).toBeTruthy()
    // This channel carries only failures and schedules no dismiss. Nothing here proves a
    // timer is absent by waiting — the point is that the toast is still there afterwards.
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.getByTestId('projects-toast')).toBeTruthy()
  })

  it('does not flash the first-run state while a cleared search is still debouncing', async () => {
    // `appliedQuery` is what decides what an empty list MEANS; branching on
    // the live input would read a cleared box as "this person has no projects" and flash
    // the first-run panel at someone who has plenty.
    h.listProjects.mockResolvedValue(page([]))
    renderPage()
    await screen.findByTestId('projects-empty')

    fireEvent.change(screen.getByLabelText('Search applications'), { target: { value: 'zzz' } })
    await screen.findByTestId('projects-no-matches', undefined, { timeout: 3000 })

    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')]))
    fireEvent.change(screen.getByLabelText('Search applications'), { target: { value: '' } })

    // Inside the debounce window the rows have not landed, and the first-run panel must not
    // appear in the gap.
    expect(screen.queryByTestId('projects-empty')).toBeNull()
    expect(await screen.findByText('Alpha', undefined, { timeout: 3000 })).toBeTruthy()
  })

  it('the page window SLIDES, so a deep page is reachable and marked active', async () => {
    // Was `Math.min(totalPages, 5)` — pages 1-5 whatever page you were on, so from page 6
    // nothing read as active and the only way deeper was clicking Next repeatedly.
    //
    // Driven through real navigation, because the window is computed from the component's
    // OWN page state — but the mocked RESPONSE has to answer with the page that was actually
    // requested too. The original version of this test used one static
    // `mockResolvedValue` that always said `page: 1` regardless of what was asked for, so
    // `appliedPage` never moved past 1 no matter which button was clicked — the window slid
    // (computed from local `page` state) but NO button was ever `aria-current`, and mutating
    // `isActive={n === appliedPage}` to `isActive={false}` passed the whole suite.
    h.listProjects
      .mockResolvedValueOnce(page([mkProject('p1', 'Alpha')], { total: 80, totalPages: 10, page: 1 }))
      .mockResolvedValueOnce(page([mkProject('p1', 'Alpha')], { total: 80, totalPages: 10, page: 5 }))
    renderPage()
    await screen.findByText('Alpha')

    // Page 1: the window is clamped to the start, so 6 is not offered yet, and 1 IS current.
    expect(screen.getByRole('button', { name: '1' }).getAttribute('aria-current')).toBe('page')
    expect(screen.queryByRole('button', { name: '6' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '5' }))

    // Centred on 5 now: 3..7. The page you are reading is offered, marked current, and 1
    // has slid off.
    await waitFor(() => expect(screen.getByRole('button', { name: '7' })).toBeTruthy())
    expect(screen.getByRole('button', { name: '5' }).getAttribute('aria-current')).toBe('page')
    expect(screen.queryByRole('button', { name: '1' })).toBeNull()
  })

  it.each([
    [3, ['1', '2', '3'], '4'],
    [5, ['1', '2', '3', '4', '5'], '6'],
  ])(
    'offers every page and no more when there are %i of them',
    async (totalPages, expected, absent) => {
      // The window is `min(5, totalPages)` wide, so at or below five pages it is the WHOLE
      // set and cannot slide. Only the deep case was pinned, which left the two shapes most
      // users actually see — a handful of pages — asserted by nothing.
      h.listProjects.mockResolvedValue(
        page([mkProject('p1', 'Alpha')], { total: totalPages * 8, totalPages }),
      )
      renderPage()
      await screen.findByText('Alpha')

      for (const n of expected) expect(screen.getByRole('button', { name: n })).toBeTruthy()
      expect(screen.queryByRole('button', { name: absent })).toBeNull()
    },
  )

  it('clamps at the END, so the last page is reachable and marked active', async () => {
    // The other half of the clamp. A window that always centred would ask for pages 9-13 of
    // 10 here; a window that never slid would strand you as it did before the fix. Neither
    // is caught by the mid-list case above.
    //
    // The response has to echo the page actually requested — a static
    // mock always answering `page: 1` left `appliedPage` at 1 while the window rendered
    // 6-10, so nothing was ever `aria-current` and this test's own title ("marked active")
    // was not being checked at all.
    h.listProjects
      .mockResolvedValueOnce(page([mkProject('p1', 'Alpha')], { total: 80, totalPages: 10, page: 1 }))
      .mockResolvedValueOnce(page([mkProject('p1', 'Alpha')], { total: 80, totalPages: 10, page: 10 }))
    renderPage()
    await screen.findByText('Alpha')

    fireEvent.click(screen.getByRole('button', { name: 'Last page' }))

    await waitFor(() => expect(screen.getByRole('button', { name: '10' })).toBeTruthy())
    for (const n of ['6', '7', '8', '9']) {
      expect(screen.getByRole('button', { name: n }).getAttribute('aria-current')).toBeNull()
    }
    expect(screen.getByRole('button', { name: '10' }).getAttribute('aria-current')).toBe('page')
    expect(screen.queryByRole('button', { name: '5' })).toBeNull()
    expect(screen.queryByRole('button', { name: '11' })).toBeNull()
  })

  it('jumps to the first page and back, without walking', async () => {
    // The control set is spelled out literally — « ‹ 1 2 › ». Both jumps were missing.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')], { total: 80, totalPages: 10 }))
    renderPage()
    await screen.findByText('Alpha')

    fireEvent.click(screen.getByRole('button', { name: 'Last page' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '10' })).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: 'First page' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '1' })).toBeTruthy())
    expect(screen.queryByRole('button', { name: '10' })).toBeNull()
  })

  it('★ the row leaves when the cascade returns, not when the button is pressed', async () => {
    // The row used to be filtered out of `items` one line ABOVE the request, so a citizen
    // watched their project vanish while the server was still dropping its database — and if
    // the drop failed the row came back under them. A completed delete the platform has not
    // performed is the one thing the sentence they agreed to must not show them.
    //
    // The dialog is what says "this is happening": it stays open and busy for the whole round
    // trip, which is real work — a force-dropped database, a blob sweep, a container teardown.
    //
    // And "Nothing here yet" is a claim about the ACCOUNT, so it must not appear at any point
    // in this sequence for someone holding 40 projects.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')], { total: 40, totalPages: 5 }))
    let release: () => void = () => {}
    h.deleteProject.mockReturnValue(new Promise<void>((r) => (release = () => r())))
    renderPage()
    await screen.findByText('Alpha')

    await deleteFromRowMenu()
    fireEvent.change(await screen.findByLabelText(/why are you deleting/i), {
      target: { value: 'no longer needed by ground ops' },
    })
    fireEvent.click(screen.getByRole('button', { name: /delete application/i }))

    // ★ STILL THERE. The request has not answered, so nothing has been deleted yet, so the
    // row is exactly where the citizen left it.
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    expect(screen.getByText('Alpha')).toBeTruthy()
    expect(screen.queryByTestId('projects-empty')).toBeNull() // and not the first-run screen

    // THE DIALOG HOLDS ITS BUSY STATE for the whole round trip. It used to close in the same
    // commit as the optimistic removal — batched before the request had even been sent — so
    // the spinner and the disabled Cancel were set and unmounted in one render and could never
    // be observed.
    expect(screen.getByRole('button', { name: /cancel/i }).hasAttribute('disabled')).toBe(true)

    // The server answers; the refetch is what takes the row.
    h.listProjects.mockResolvedValue(page([], { total: 39, totalPages: 5 }))
    release()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(screen.queryByText('Alpha')).toBeNull())
    expect(screen.queryByTestId('projects-empty')).toBeNull() // 39 projects is not "no projects"
    // FOCUS LANDS ON THE HEADING, not <body>, and it still has to be sent there explicitly:
    // the Delete button Radix captured is unmounted by the refetch a beat after the dialog
    // closes, so restoring onto it would put the keyboard on a control that is removed a
    // moment later.
    // AWAITED, because the page deliberately places focus on the NEXT FRAME: doing it inside the
    // close handler moves focus out of a trap that is still armed, and the trap takes it back.
    await waitFor(() => expect(document.activeElement?.textContent).toBe('My Applications'))
  })

  it('an empty page with a non-zero total is NOT the first-run screen', async () => {
    // TESTED AT THE GUARD RATHER THAN AT THE FRAME. The race — the committed render between
    // the delete settling and the refetch effect running — is not observable from RTL, which
    // flushes effects inside `act()`; asserting around it produced a test that passed with the
    // fix REMOVED, so this pins the condition itself instead.
    //
    // `items: []` with `total: 40` is the same state that frame has, and it is reachable for
    // real: delete the last row on page 5 and the server answers an empty page while the
    // account still has 40 projects. "Nothing here yet" is a claim about the ACCOUNT, so it
    // must key off `total`, never off the rows this page happens to be holding.
    //
    // Mutation receipt: drop `total === 0` from `showFirstRun` and this goes red.
    h.listProjects.mockResolvedValue(page([], { total: 40, totalPages: 5, page: 5 }))
    renderPage()

    // Liveness FIRST, so the absence below means something rather than the assertion
    // running before anything had rendered at all: the counts strip only fills in once a
    // response has landed.
    expect(await screen.findByText(String(COUNTS.totalApplications))).toBeTruthy()
    await waitFor(() => expect(h.listProjects).toHaveBeenCalled())

    expect(screen.queryByTestId('projects-empty')).toBeNull()
  })

  it('a 404 delete still refreshes the total, which the row left stale', async () => {
    // Already gone elsewhere is the desired end state, so no toast — but the row did leave
    // the list, and returning early left "Showing 1–7 of 8" on screen.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')], { total: 8, totalPages: 1 }))
    h.deleteProject.mockRejectedValue(new ApiError('gone', 404))
    renderPage()
    await screen.findByText('Alpha')
    h.listProjects.mockClear()

    await deleteFromRowMenu()
    fireEvent.change(await screen.findByLabelText(/why are you deleting/i), {
      target: { value: 'no longer needed by ground ops' },
    })
    fireEvent.click(screen.getByRole('button', { name: /delete application/i }))

    await waitFor(() => expect(h.listProjects).toHaveBeenCalled())
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('the projects list and the count tiles keep WORDS and a busy state', () => {
  /** Every live region currently SAYING the given thing — see the twin helper in `App.test.jsx`. */
  const regionsSaying = (re: RegExp): Element[] =>
    Array.from(document.querySelectorAll('[aria-live], [role="status"], [role="alert"]')).filter(
      (el) => re.test(el.textContent ?? ''),
    )

  it('★ says what it is doing, marks BOTH busy containers, and does it in exactly ONE region', async () => {
    // `index.css` suppresses `.animate-pulse`, so with motion off the three count tiles and the
    // five row skeletons sit perfectly still — eight grey rectangles and not one word.
    h.listProjects.mockReturnValue(new Promise(() => {}))
    h.listProjectCounts.mockReturnValue(new Promise(() => {}))
    renderPage()

    expect(await screen.findByText('Loading your applications…')).toBeTruthy()
    // Said ONCE, for both waits — no `sr-only` duplicate, and not one sentence per skeleton.
    expect(screen.getAllByText('Loading your applications…')).toHaveLength(1)
    const regions = regionsSaying(/Loading your applications/)
    expect(regions).toHaveLength(1)
    expect(regions[0]).toBe(screen.getByTestId('projects-wait'))
    // TWO busy containers, one sentence: the tiles grid and the row skeletons. `aria-busy` is a
    // property and announces nothing, which is why it may sit on both without saying anything
    // twice.
    expect(document.querySelectorAll('[aria-busy="true"]')).toHaveLength(2)
  })

  it('★ the region is already in the tree, EMPTY, before the wait starts — and it is the SAME node', async () => {
    // THE REASON THE REGION IS MOUNTED PERMANENTLY. Every skeleton on this page is
    // conditional, so a region rendered beside one is born holding its own text — which
    // several reader-and-browser combinations miss entirely. Move `<div role="status">` inside
    // the `waiting` branch and the empty-region assertion below goes red.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')], { total: 12, totalPages: 2 }))
    renderPage()
    await screen.findByText('Alpha')
    // Both reads have settled: nothing is in flight.
    await waitFor(() => expect(screen.getByTestId('projects-wait').textContent).toBe(''))

    const before = screen.getByTestId('projects-wait')
    expect(regionsSaying(/Loading your applications/)).toHaveLength(0)
    // Paired with a liveness assertion: an empty region also describes a crashed render.
    expect(screen.getByText('Alpha')).toBeTruthy()

    // Turning the page re-enters the wait against a page that has been mounted the whole time.
    h.listProjects.mockReturnValue(new Promise(() => {}))
    fireEvent.click(screen.getByRole('button', { name: '2' }))

    await waitFor(() => expect(before.textContent).toContain('Loading your applications…'))
    expect(screen.getByTestId('projects-wait')).toBe(before)
    expect(regionsSaying(/Loading your applications/)).toHaveLength(1)
  })

  it('★ a page whose reads have all landed says NOTHING — the region is present and silent', async () => {
    // The other half of "empty when idle": a region that keeps its sentence after the wait ends
    // is a screen reader told the page is still loading forever.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')]))
    renderPage()
    await screen.findByText('Alpha')

    await waitFor(() => expect(screen.getByTestId('projects-wait').textContent).toBe(''))
    expect(screen.queryByText('Loading your applications…')).toBeNull()
    expect(document.querySelectorAll('[aria-busy="true"]')).toHaveLength(0)
  })
})

// --- the list remembers where you were -----------------------------------

describe('page, search and rows-per-page live in the URL', () => {
  /**
   * The three round trips reproduced on a real account with 23 projects across 3 pages:
   * page 3 → open a project → Back landed on page 1; a search survived neither the trip nor a
   * reload; and rows-per-page reset to 8. All three were component state that no navigation could
   * see. What makes it a bug rather than a stated policy is the neighbour: the SAME page already
   * remembers list-vs-grid and card density, in `localStorage`, and still does — those are a
   * person's habit, not a place in a list.
   */

  it('page 3 survives opening a project and pressing Back', async () => {
    answersWithTheRequestedPage([mkProject('p1', 'Ramp Ops')], { total: 40, totalPages: 5 })
    renderPage()
    await screen.findByText('Ramp Ops')

    fireEvent.click(screen.getByRole('button', { name: '3' }))
    await waitFor(() => expect(screen.getByText(/Page 3 of 5/)).toBeTruthy())
    expect(screen.getByTestId('location-search').textContent).toBe('?page=3')

    fireEvent.click(screen.getByRole('button', { name: 'Ramp Ops' }))
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/projects/p1'))

    h.listProjects.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'browser back' }))

    // Back onto the list, and the list is where it was — asked for AND narrated.
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/projects'))
    expect(screen.getByTestId('location-search').textContent).toBe('?page=3')
    await waitFor(() =>
      expect(h.listProjects).toHaveBeenCalledWith(expect.objectContaining({ page: 3 })),
    )
    expect(await screen.findByText(/Page 3 of 5/)).toBeTruthy()
  })

  it('a search term survives the same round trip, and the caption reflects it', async () => {
    h.listProjects.mockImplementation((args: { page: number; limit: number; q?: string }) =>
      Promise.resolve(
        args.q === 'ramp'
          ? page([mkProject('p1', 'Ramp Ops')], { total: 1, totalPages: 1 })
          : page([mkProject('p1', 'Ramp Ops'), mkProject('p2', 'Visitor Log')], {
              total: 2,
              totalPages: 1,
            }),
      ),
    )
    renderPage()
    await screen.findByText('Visitor Log')

    fireEvent.change(screen.getByLabelText('Search applications'), { target: { value: 'ramp' } })
    await waitFor(() => expect(screen.queryByText('Visitor Log')).toBeNull(), { timeout: 3000 })
    expect(screen.getByTestId('location-search').textContent).toBe('?q=ramp')

    fireEvent.click(screen.getByRole('button', { name: 'Ramp Ops' }))
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/projects/p1'))

    h.listProjects.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'browser back' }))

    await screen.findByText('Ramp Ops')
    // The box holds the term, the request carried it, and the caption counts the FILTERED total.
    expect((screen.getByLabelText('Search applications') as HTMLInputElement).value).toBe('ramp')
    expect(h.listProjects.mock.calls[0][0]).toEqual({ page: 1, limit: 8, q: 'ramp' })
    expect(screen.getByText(/Showing 1–1 of 1/)).toBeTruthy()
    expect(screen.queryByText('Visitor Log')).toBeNull()
  })

  it('rows-per-page survives a reload', async () => {
    answersWithTheRequestedPage([mkProject('p1', 'Ramp Ops')], { total: 40, totalPages: 5 })
    renderPage()
    await screen.findByText('Ramp Ops')

    await pickRowsPerPage('24')
    await waitFor(() =>
      expect(h.listProjects).toHaveBeenCalledWith(expect.objectContaining({ limit: 24 })),
    )
    expect(screen.getByTestId('location-search').textContent).toBe('?pageSize=24')

    // THE RELOAD: the whole tree is thrown away and rebuilt at the address that was on screen.
    // Nothing but the URL crosses this line — component state does not, which is the entire
    // point, and `localStorage` is left alone so the two mechanisms stay distinguishable.
    cleanup()
    h.listProjects.mockClear()
    renderPage({ pathname: '/projects', search: '?pageSize=24' })

    await screen.findByText('Ramp Ops')
    expect(h.listProjects.mock.calls[0][0]).toEqual({ page: 1, limit: 24, q: undefined })
    expect(screen.getByRole('combobox', { name: 'Rows per page' }).textContent).toContain('24')
  })

  it('a shared URL carrying all three renders that exact view on a cold load', async () => {
    answersWithTheRequestedPage([mkProject('p1', 'Ramp Ops')], { total: 40, totalPages: 2 })
    renderPage({ pathname: '/projects', search: '?page=2&pageSize=24&q=ramp' })

    expect(await screen.findByText('Ramp Ops')).toBeTruthy()

    // ONE request, and it already carries all three. `debouncedQ` seeded from `''` would send
    // the UNFILTERED list first and paint it — a flash of everybody's projects on a link that
    // named one — so the wait below is longer than the 300ms debounce on purpose.
    await new Promise((r) => setTimeout(r, 400))
    expect(h.listProjects).toHaveBeenCalledTimes(1)
    expect(h.listProjects.mock.calls[0][0]).toEqual({ page: 2, limit: 24, q: 'ramp' })

    expect((screen.getByLabelText('Search applications') as HTMLInputElement).value).toBe('ramp')
    expect(screen.getByRole('combobox', { name: 'Rows per page' }).textContent).toContain('24')
    expect(screen.getByText(/Page 2 of 2/)).toBeTruthy()
  })

  it('a nonsense query string falls back to the default view instead of forwarding it', async () => {
    // A query string is user input and this one is meant to be pasted around, so it arrives
    // truncated, hand-edited and occasionally hostile. `?pageSize=9999` honoured literally is a
    // link that hands somebody else's browser a 9999-row request.
    answersWithTheRequestedPage([mkProject('p1', 'Ramp Ops')], { total: 40, totalPages: 5 })
    renderPage({ pathname: '/projects', search: '?page=banana&pageSize=9999' })

    await screen.findByText('Ramp Ops')
    expect(h.listProjects.mock.calls[0][0]).toEqual({ page: 1, limit: 8, q: undefined })
  })

  it('the debounce does not push a history entry per keystroke', async () => {
    // One entry per typed character makes Back spell the word backwards instead of leaving the
    // page — the one control a reader reaches for when they want OUT.
    answersWithTheRequestedPage([mkProject('p1', 'Ramp Ops')], { total: 40, totalPages: 5 })
    renderPage()
    await screen.findByText('Ramp Ops')
    expect(stack.depth).toBe(1)

    const box = screen.getByLabelText('Search applications')
    for (const value of ['r', 'ra', 'ram', 'ramp']) fireEvent.change(box, { target: { value } })

    await waitFor(() => expect(screen.getByTestId('location-search').textContent).toBe('?q=ramp'))
    await waitFor(
      () => expect(h.listProjects).toHaveBeenCalledWith(expect.objectContaining({ q: 'ramp' })),
      { timeout: 3000 },
    )

    // FOUR keystrokes, and the stack is exactly as deep as it was.
    expect(stack.depth).toBe(1)
    expect(stack.types).not.toContain('PUSH')
    // Paired with liveness, because "depth unchanged" also describes a URL that never moved:
    // the address really was rewritten once per character, by REPLACE every time.
    expect(stack.types.filter((t) => t === 'REPLACE').length).toBeGreaterThanOrEqual(4)

    // And the contrast that makes the rule a rule rather than an accident: a deliberate click
    // DOES push, so Back undoes exactly one page turn.
    fireEvent.click(screen.getByRole('button', { name: '2' }))
    await waitFor(() => expect(stack.depth).toBe(2))
  })

  it('the footer narrates the page the ROWS answer, not the page that was asked for', async () => {
    // `appliedPage` / `appliedPageSize` are NOT redundant copies of the URL. The URL is
    // what was asked for and moves the instant a number is clicked; the mirrors are what the rows
    // on screen answer and move only when a response lands. A failed request leaves the previous
    // rows on screen, so the gap between the two is a real rendered state, not a theoretical one.
    //
    // MUTATION RECEIPT: collapse the mirrors into the URL state — render `page` / `pageSize`
    // where `appliedPage` / `appliedPageSize` are read — and both halves below go red.
    h.listProjects.mockResolvedValueOnce(
      page([mkProject('p1', 'Ramp Ops')], { total: 12, totalPages: 2, page: 1, pageSize: 8 }),
    )
    renderPage()
    await screen.findByText('Ramp Ops')
    expect(screen.getByText(/Showing 1–1 of 12/)).toBeTruthy()
    expect(screen.getByText(/Page 1 of 2/)).toBeTruthy()

    h.listProjects.mockReturnValue(new Promise(() => {})) // page 2 never lands
    fireEvent.click(screen.getByRole('button', { name: '2' }))

    // The ASK is already in the address bar...
    await waitFor(() => expect(screen.getByTestId('location-search').textContent).toBe('?page=2'))
    // ...and the rows on screen are still page 1's, so the footer still says page 1.
    expect(screen.getByText('Ramp Ops')).toBeTruthy()
    expect(screen.getByText(/Page 1 of 2/)).toBeTruthy()
    expect(screen.getByText(/Showing 1–1 of 12/)).toBeTruthy()
    expect(screen.queryByText(/Page 2 of 2/)).toBeNull()
    expect(screen.queryByText(/Showing 9–9 of 12/)).toBeNull()
  })

  it('the arrival notice scrubs itself WITHOUT taking the view with it', async () => {
    // The two mechanisms meet here. The notice rides router state and is replaced away the moment
    // it is read; that replace carries `location.search` through verbatim, so the page and query
    // the reader arrived with survive being told a project is gone. The reverse matters as much:
    // the notice is NOT a query parameter, so it cannot be copied forward by the parameter writer
    // and cannot outlive the reload it is meant to be cleared by.
    answersWithTheRequestedPage([mkProject('p1', 'Ramp Ops')], { total: 40, totalPages: 5 })
    renderPage({
      pathname: '/projects',
      search: '?page=3&q=ramp',
      state: { notice: PROJECT_GONE_NOTICE },
    })

    expect(await screen.findByText(PROJECT_GONE_NOTICE)).toBeTruthy()
    expect(screen.getByTestId('location-search').textContent).toBe('?page=3&q=ramp')
    expect(screen.getByTestId('location-search').textContent).not.toContain('notice')
    expect(h.listProjects.mock.calls[0][0]).toEqual({ page: 3, limit: 8, q: 'ramp' })
    // The scrub REPLACED the entry it read from — it did not add one, so Back is unchanged.
    expect(stack.depth).toBe(1)
    expect(stack.types).toEqual(['REPLACE'])

    // A RELOAD at the address the scrub left says nothing: the view is restored, the sentence
    // is not re-announced.
    cleanup()
    renderPage({ pathname: '/projects', search: '?page=3&q=ramp' })
    await screen.findByText('Ramp Ops')
    expect(screen.queryByText(PROJECT_GONE_NOTICE)).toBeNull()
    expect(screen.getByTestId('projects-notice').textContent).toBe('')
  })
})

/**
 * RESTART AND TAKE DOWN, REACHED FROM THE LIST.
 *
 * The list is not a polling surface and does not become one: it refetches ONCE when an
 * operation returns, the same refresh a delete already triggers. What it must get right is that
 * two rows acting at once settle independently — a shared boolean would freeze a whole page of
 * applications because one of them is restarting.
 */
describe('ProjectsPage — the production actions', () => {
  const serving = (id: string, name: string) => mkProject(id, name, { isServing: true })

  async function openRowMenu(index = 0): Promise<void> {
    fireEvent.pointerDown(screen.getAllByTestId('app-menu-row')[index])
    await screen.findByRole('menuitem', { name: 'Open' })
  }

  /** Take-down is asked about before it runs; restart is not. */
  async function pressTakeDown(): Promise<void> {
    fireEvent.click(await screen.findByTestId('menu-takedown'))
    fireEvent.click(await screen.findByTestId('take-down-confirm'))
  }

  it('runs menu-restart against the row it was opened on', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Draft App'), serving('p2', 'Ramp Ops')]))
    renderPage()
    await screen.findByText('Ramp Ops')
    // The first row is not serving, so its menu carries neither entry — the SECOND row's does.
    await openRowMenu(1)
    fireEvent.click(await screen.findByTestId('menu-restart'))
    await waitFor(() => expect(h.restartApp).toHaveBeenCalledWith('p2'))
  })

  it('runs menu-takedown against the row it was opened on, once confirmed', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Draft App'), serving('p2', 'Ramp Ops')]))
    renderPage()
    await screen.findByText('Ramp Ops')
    await openRowMenu(1)
    await pressTakeDown()
    await waitFor(() => expect(h.takeAppDown).toHaveBeenCalledWith('p2'))
  })

  it('★ takes nothing down until the owner says so, and names which one it is asking about', async () => {
    // THE CALIBRATION THIS FIXES. One click in a menu ended an application for everyone at BIAL,
    // with no question and nothing to undo — beside a Delete that demands a written reason and a
    // Send for review, which changes nothing, that opens a questionnaire.
    h.listProjects.mockResolvedValue(page([serving('p1', 'Ramp Ops')]))
    renderPage()
    await screen.findByText('Ramp Ops')
    await openRowMenu()
    fireEvent.click(await screen.findByTestId('menu-takedown'))

    expect(screen.getByText(/Take “Ramp Ops” out of production\?/)).toBeTruthy()
    expect(h.takeAppDown).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('take-down-cancel'))
    await waitFor(() => expect(screen.queryByTestId('take-down-confirm')).toBeNull())
    // The row is still there and still serving — a cancel that quietly ran it anyway is exactly
    // the failure an absence assertion on its own would miss.
    expect(h.takeAppDown).not.toHaveBeenCalled()
    expect(screen.getByText('Ramp Ops')).toBeTruthy()
  })

  it('★ says what a take-down kept, in the server\'s own words', async () => {
    // The server composes the sentence — it names what survives, and appends the review-queue
    // fact when a version is waiting — and both callers dropped it, so a take-down that worked
    // produced no message at all: the only signal was a chip changing colour in a row a reader
    // may have scrolled past.
    h.takeAppDown.mockResolvedValue({
      message: 'Your app is no longer running in production. Everything it holds is kept.',
    })
    h.listProjects.mockResolvedValue(page([serving('p1', 'Ramp Ops')]))
    renderPage()
    await screen.findByText('Ramp Ops')
    await openRowMenu()
    await pressTakeDown()

    const notice = await screen.findByTestId('projects-toast')
    expect(notice.getAttribute('data-tone')).toBe('confirmation')
    expect(notice.textContent).toContain('Everything it holds is kept')
    // And it names its subject, because this list can be searched and paged out from under it.
    expect(notice.textContent).toContain('Ramp Ops')
  })

  it('re-reads the list once the operation returns, so the chip stops being stale', async () => {
    h.listProjects.mockResolvedValue(page([serving('p1', 'Ramp Ops')]))
    renderPage()
    await screen.findByText('Ramp Ops')
    const before = h.listProjects.mock.calls.length
    await openRowMenu()
    await pressTakeDown()
    await waitFor(() => expect(h.listProjects.mock.calls.length).toBeGreaterThan(before))
  })

  it("★ says the server's own reason when it refuses, not a generic failure", async () => {
    // Every refusal on these two routes names something the owner can act on — publish it
    // again, wait for the deploy to finish, ask an administrator. Flattening them into
    // "something went wrong" throws away the only part that helps.
    h.restartApp.mockRejectedValue(
      new ApiError('This app has been taken offline. Publish again to put it back.', 409, 'taken_offline'),
    )
    h.listProjects.mockResolvedValue(page([serving('p1', 'Ramp Ops')]))
    renderPage()
    await screen.findByText('Ramp Ops')
    await openRowMenu()
    fireEvent.click(await screen.findByTestId('menu-restart'))
    expect((await screen.findByTestId('projects-toast')).textContent).toContain(
      'This app has been taken offline. Publish again to put it back.',
    )
  })

  it('★ one row acting does not freeze the other', async () => {
    // Mutation receipt: make `actingIds` a single `actingId` string and this goes red — the
    // second application would be announced inert because an unrelated one is restarting.
    h.restartApp.mockReturnValue(new Promise(() => {}))
    h.listProjects.mockResolvedValue(page([serving('p1', 'Ramp Ops'), serving('p2', 'Gate Board')]))
    renderPage()
    await screen.findByText('Gate Board')

    await openRowMenu(0)
    fireEvent.click(await screen.findByTestId('menu-restart'))

    await openRowMenu(1)
    const restart = await screen.findByTestId('menu-restart')
    expect(restart.getAttribute('aria-disabled')).toBe('false')
    fireEvent.click(restart)
    await waitFor(() => expect(h.restartApp).toHaveBeenCalledTimes(2))
    expect(h.restartApp.mock.calls.map((c: unknown[]) => c[0])).toEqual(['p1', 'p2'])
  })

  it('★ the acting row itself IS announced inert while its own operation runs', async () => {
    // The paired positive for the test above: scoping per row must not mean nothing is scoped.
    h.restartApp.mockReturnValue(new Promise(() => {}))
    h.listProjects.mockResolvedValue(page([serving('p1', 'Ramp Ops')]))
    renderPage()
    await screen.findByText('Ramp Ops')
    await openRowMenu()
    fireEvent.click(await screen.findByTestId('menu-restart'))
    await openRowMenu()
    await waitFor(() =>
      expect(screen.getByTestId('menu-restart').getAttribute('aria-disabled')).toBe('true'),
    )
  })

  it('★ a rename that lands after the dialog closed does not bring it back', async () => {
    // THE NAME COMMITS ON BLUR, so its answer can arrive at any moment afterwards — including
    // after the X, or after Delete handed off to its confirmation. Writing the returned project
    // straight back into the page's dialog state re-opened a dialog nobody asked for, and over
    // the confirmation it stacked a second focus trap in front of the one being answered.
    let settle: ((value: Project) => void) | null = null
    h.patchProject.mockReturnValue(new Promise<Project>((resolve) => { settle = resolve }))
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Ramp Ops')]))
    renderPage()
    await screen.findByText('Ramp Ops')

    await openRowMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Settings…' }))
    const dialog = await screen.findByTestId('app-settings-dialog')

    const name = within(dialog).getByLabelText('Application name')
    fireEvent.change(name, { target: { value: 'Ramp Operations Board' } })
    fireEvent.blur(name)
    await waitFor(() => expect(h.patchProject).toHaveBeenCalled())

    fireEvent.click(within(dialog).getByLabelText('Close'))
    await waitFor(() => expect(screen.queryByTestId('app-settings-dialog')).toBeNull())

    await act(async () => {
      settle?.(mkProject('p1', 'Ramp Operations Board'))
      await Promise.resolve()
    })

    // Still gone. Paired with liveness, because "no dialog" is also what a crashed page looks like.
    expect(screen.queryByTestId('app-settings-dialog')).toBeNull()
    expect(screen.getByTestId('project-row')).toBeTruthy()
  })

  it('offers neither entry on an application that is not serving', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Draft App')]))
    renderPage()
    await screen.findByText('Draft App')
    await openRowMenu()
    // Liveness beside the absence: the menu opened and carries its ordinary entries.
    expect(screen.getByRole('menuitem', { name: 'Settings…' })).toBeTruthy()
    expect(screen.queryByTestId('menu-restart')).toBeNull()
    expect(screen.queryByTestId('menu-takedown')).toBeNull()
  })
})

// --- the summary strip is the filter ---------------------------------

/**
 * THE THREE NUMBERS BECAME THE PAGE'S ONE FILTER CONTROL.
 *
 * Each tile counts a set and then selects it, which is only honest while the count and the rows
 * come from one definition — pinned server-side, where the decision is actually made. What these
 * cover is the half a browser owns: that a tile is a real control, that there is exactly one
 * filter state, and that the total tile is always the way out of it.
 */
describe('★ the three summary tiles filter the list beneath them', () => {
  const LIVE = mkProject('p1', 'Live One', { isServing: true })
  const PIPELINE = mkProject('p2', 'Waiting')
  const NEITHER = mkProject('p3', 'Nothing Built')

  /** A server that honours `filter` and `q` the way the real one does. A list that ignored
   *  either would let every assertion below pass against rows nobody asked for. */
  function answersPerFilter(): void {
    h.listProjects.mockImplementation(
      (args: { page: number; limit: number; q?: string; filter?: string }) => {
        const byTile =
          args.filter === 'inProduction'
            ? [LIVE]
            : args.filter === 'inPipeline'
              ? [PIPELINE]
              : [LIVE, PIPELINE, NEITHER]
        const term = args.q
        const rows = term ? byTile.filter((p) => p.name.includes(term)) : byTile
        return Promise.resolve(
          page(rows, {
            page: args.page,
            pageSize: args.limit,
            total: rows.length,
            totalPages: rows.length === 0 ? 0 : 1,
          }),
        )
      },
    )
  }

  const tile = (label: RegExp): HTMLElement => screen.getByRole('button', { name: label })

  it('clicking “In production” narrows the list, and the tile reads as selected', async () => {
    answersPerFilter()
    renderPage()
    await screen.findByText('Nothing Built')

    fireEvent.click(tile(/In production/))

    await waitFor(() => expect(screen.queryByText('Nothing Built')).toBeNull())
    // Liveness beside the absence: the list narrowed rather than failing to render.
    expect(screen.getByText('Live One')).toBeTruthy()
    expect(tile(/In production/).getAttribute('aria-pressed')).toBe('true')
    expect(h.listProjects).toHaveBeenLastCalledWith(
      expect.objectContaining({ filter: 'inProduction' }),
    )
    // Committed to the address, like every other thing that decides which rows are on screen.
    expect(screen.getByTestId('location-search').textContent).toBe('?filter=inProduction')
  })

  it('clicking the selected tile again clears it and restores the full list', async () => {
    answersPerFilter()
    renderPage('/projects?filter=inProduction')
    await screen.findByText('Live One')

    fireEvent.click(tile(/In production/))

    await screen.findByText('Nothing Built')
    expect(tile(/In production/).getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByTestId('location-search').textContent).toBe('')
  })

  it('“Total applications” is selected whenever nothing else is, and clears the rest', async () => {
    answersPerFilter()
    renderPage('/projects?filter=inProduction')
    await screen.findByText('Live One')
    expect(tile(/Total applications/).getAttribute('aria-pressed')).toBe('false')

    fireEvent.click(tile(/Total applications/))

    await screen.findByText('Nothing Built')
    expect(tile(/Total applications/).getAttribute('aria-pressed')).toBe('true')
    expect(tile(/In production/).getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByTestId('location-search').textContent).toBe('')
  })

  it('filters identically in the grid, since the strip sits above that branch', async () => {
    answersPerFilter()
    renderPage('/projects?filter=inProduction')
    await screen.findByText('Live One')

    fireEvent.click(screen.getByLabelText('Grid view'))

    await waitFor(() => expect(screen.getAllByTestId('project-card').length).toBe(1))
    expect(screen.getByText('Live One')).toBeTruthy()
    expect(tile(/In production/).getAttribute('aria-pressed')).toBe('true')
  })

  it('composes with the search rather than replacing it', async () => {
    answersPerFilter()
    renderPage('/projects?filter=inPipeline')
    await screen.findByText('Waiting')

    fireEvent.change(screen.getByLabelText('Search applications'), { target: { value: 'Waiting' } })

    await waitFor(
      () =>
        expect(h.listProjects).toHaveBeenLastCalledWith(
          expect.objectContaining({ filter: 'inPipeline', q: 'Waiting' }),
        ),
      { timeout: 3000 },
    )
    expect(tile(/In review, in progress or deployed/).getAttribute('aria-pressed')).toBe('true')
  })

  it('each tile is a real button whose count still reads as a count', async () => {
    answersPerFilter()
    renderPage()
    const production = await screen.findByRole('button', { name: /In production/ })

    // A NATIVE BUTTON, not a div with an onClick: Enter and Space come for free, and so does
    // the tab order. `getByRole` above already refuses anything that is not one.
    expect(production.tagName).toBe('BUTTON')
    expect(production.getAttribute('tabindex')).toBeNull()
    production.focus()
    expect(document.activeElement).toBe(production)
    // The number is still the tile's own text, inside the region that announces a change to it.
    expect(production.textContent).toContain('2')
    expect(screen.getByTestId('projects-counts').contains(production)).toBe(true)
  })

  it('a tile counting nothing is not a control, and the clear-all never is', async () => {
    h.listProjectCounts.mockResolvedValue({ inProduction: 0, totalApplications: 5, inPipeline: 1 })
    answersPerFilter()
    renderPage()
    await screen.findByText('Nothing Built')

    expect(tile(/In production/).hasAttribute('disabled')).toBe(true)
    // The way OUT of a filter must never go dead, whatever the numbers say.
    expect(tile(/Total applications/).hasAttribute('disabled')).toBe(false)
  })

  it('an empty filtered list does not claim the account is empty', async () => {
    h.listProjects.mockResolvedValue(page([], { total: 0, totalPages: 0 }))
    renderPage('/projects?filter=inProduction')

    // Liveness first: the page settled on its no-matches card rather than on nothing at all.
    await screen.findByTestId('projects-no-matches')
    expect(screen.queryByTestId('projects-empty')).toBeNull()
    expect(screen.getByText('No application matches that filter.')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Clear the filter' }))

    await waitFor(() => expect(screen.getByTestId('location-search').textContent).toBe(''))
  })

  it('reads a filter the server would refuse as no filter at all', async () => {
    answersPerFilter()
    renderPage('/projects?filter=banana')

    await screen.findByText('Nothing Built')
    expect(h.listProjects).toHaveBeenLastCalledWith(expect.objectContaining({ filter: undefined }))
    expect(tile(/Total applications/).getAttribute('aria-pressed')).toBe('true')
  })
})
