/**
 * ProjectsPage (`/projects`) — the landing screen: three numbers, then list or grid.
 *
 * #158 replaced the card grid with two views, numbered pagination and a summary strip, so
 * this file was rewritten rather than patched. What it used to assert — a "Load more"
 * button, the first-run CTA that named creating a project, the card grid as the ONLY
 * layout — describes a page
 * that no longer exists, and §16.3 names that describe block as dead code to remove rather
 * than leave failing beside the new work.
 *
 * The data layer is mocked at the module boundary; the page's own paging state runs for
 * real, because that is what is being exercised. A LocationProbe outside the Routes reports
 * the current path so navigation is observable without a real project-home page, and it
 * uses the `vi.hoisted` + `MemoryRouter` shape this directory's suites share.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'

const h = vi.hoisted(() => ({
  listProjects: vi.fn(),
  listProjectCounts: vi.fn(),
  createProject: vi.fn(),
  deleteProject: vi.fn(),
  listProjectConversations: vi.fn(),
}))

vi.mock('../../utils/projectApi', () => ({
  listProjects: h.listProjects,
  listProjectCounts: h.listProjectCounts,
  createProject: h.createProject,
  deleteProject: h.deleteProject,
}))
vi.mock('../../utils/conversationApi', () => ({
  listProjectConversations: h.listProjectConversations,
  CONVERSATION_LIST_CAP: 200,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))

import ProjectsPage from '../ProjectsPage'
import { ApiError } from '../../utils/apiError'
import type { Project } from '../../utils/projectApi'

function LocationProbe(): React.JSX.Element {
  const loc = useLocation()
  return <div data-testid="location">{loc.pathname}</div>
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/projects']}>
      <LocationProbe />
      <Routes>
        <Route path="/projects" element={<ProjectsPage />} />
        <Route path="/projects/:id" element={<div data-testid="project-home">home</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

const mkProject = (id: string, name: string, over: Partial<Project> = {}): Project => ({
  id,
  name,
  description: 'A tool',
  appId: null,
  isServing: false,
  appStatus: null,
  hasRelaunchableSnapshot: null,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
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
  h.listProjects.mockResolvedValue(page([]))
  h.listProjectCounts.mockResolvedValue(COUNTS)
  h.listProjectConversations.mockResolvedValue([])
})
afterEach(() => cleanup())

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

// --- the two views -------------------------------------------------------------

describe('list and grid', () => {
  it('defaults to LIST, with the column header the grid does not have', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Visitor Log')]))
    renderPage()

    expect(await screen.findByText('Visitor Log')).toBeTruthy()
    expect(screen.getByText('Application')).toBeTruthy()
    // "Details updated", never "Last updated": `updatedAt` moves on a rename or a
    // description edit and never on a build, publish or deploy (§10 Trap 1).
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

// --- the row -------------------------------------------------------------------

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

  it('keeps Delete OUT of the open button (invariant F-10)', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Visitor Log')]))
    renderPage()
    await screen.findByText('Visitor Log')

    const del = screen.getByLabelText('Delete Visitor Log')
    const open = screen.getByRole('button', { name: 'Visitor Log' })
    // Neither contains the other. A row that nests them is a button inside a button.
    expect(open.contains(del)).toBe(false)
    expect(del.contains(open)).toBe(false)
    expect(del.closest('button')).toBe(del)
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

// --- pagination ----------------------------------------------------------------

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

    fireEvent.change(screen.getByLabelText('Search projects'), { target: { value: 'vip' } })

    await waitFor(
      () => expect(h.listProjects).toHaveBeenCalledWith(expect.objectContaining({ page: 1, q: 'vip' })),
      { timeout: 3000 },
    )
  })
})

// --- every state ---------------------------------------------------------------

describe('the states', () => {
  it('first run offers exactly ONE way to make a project', async () => {
    renderPage()

    const empty = await screen.findByTestId('projects-empty')
    expect(within(empty).getByText('Nothing here yet')).toBeTruthy()
    // No composer, no chat-kind toggle, no second "name it yourself" path (§11).
    expect(within(empty).getAllByRole('button')).toHaveLength(1)
  })

  it('no matches quotes the query the ROWS answer, not the one being typed', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')]))
    renderPage()
    await screen.findByText('Alpha')

    h.listProjects.mockResolvedValue(page([]))
    fireEvent.change(screen.getByLabelText('Search projects'), { target: { value: 'zzz' } })

    const noMatch = await screen.findByTestId('projects-no-matches', undefined, { timeout: 3000 })
    expect(noMatch.textContent).toContain('zzz')
  })

  it('a FIRST page failure is full-width and retryable', async () => {
    h.listProjects.mockRejectedValue(new Error('boom'))
    renderPage()

    const err = await screen.findByTestId('projects-error')
    expect(err.textContent).toMatch(/Couldn’t load your projects/)

    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')]))
    fireEvent.click(within(err).getByText('Retry'))

    expect(await screen.findByText('Alpha')).toBeTruthy()
  })

  it('a LATER page failure keeps the rows already on screen', async () => {
    // §11's rule: never blank the list the reader is using.
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
    // Round-4 finding 11: the message underneath the rows was static text with no control.
    // Clicking the same page number again is a React no-op — the state value is unchanged,
    // so the fetch effect's deps do not change and nothing re-runs — which left the failure
    // unrecoverable without a reload. `reloadNonce` is the dep that always changes.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')], { total: 12, totalPages: 2 }))
    renderPage()
    await screen.findByText('Alpha')

    h.listProjects.mockRejectedValue(new Error('boom'))
    fireEvent.click(screen.getByRole('button', { name: '2' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/Couldn’t load more/))

    // The recovery the old version had no way to reach.
    h.listProjects.mockResolvedValue(page([mkProject('p2', 'Beta')], { total: 12, totalPages: 2, page: 2 }))
    fireEvent.click(screen.getByRole('button', { name: /retry/i }))

    expect(await screen.findByText('Beta')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('a FIRST-LOAD counts failure offers a retry instead of pulsing forever', async () => {
    // Round-4 finding 12: the catch was made a total no-op, which is right for a REFRESH
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

// --- create and delete ---------------------------------------------------------

describe('create and delete', () => {
  it('has exactly ONE New project button', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')]))
    renderPage()
    await screen.findByText('Alpha')

    // The trap §16 names first: adding it to the controls row without deleting the page
    // header's one ships two.
    expect(screen.getAllByRole('button', { name: /New project/i })).toHaveLength(1)
  })

  it('a 404 on delete removes the row with no error toast', async () => {
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')]))
    h.deleteProject.mockRejectedValue(new ApiError('gone', 404))
    renderPage()
    await screen.findByText('Alpha')

    fireEvent.click(screen.getByLabelText('Delete Alpha'))
    // The dialog gates on a 5-50 word reason (#158 §13.1), which the page forwards to the
    // API. Its own bounds are asserted in ProjectDeleteDialog.test.tsx; here it just has to
    // be valid so the delete runs.
    fireEvent.change(await screen.findByLabelText(/why are you deleting/i), {
      target: { value: 'no longer needed by ground ops' },
    })
    fireEvent.click(screen.getByRole('button', { name: /delete project/i }))

    await waitFor(() => expect(h.deleteProject).toHaveBeenCalled())
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('a delete failure does not auto-dismiss, and carries a failure marker', async () => {
    // CARRIED FORWARD FROM #172, which landed this contract on the page this rewrite
    // replaced. Rewriting a file is the easiest way to drop a behaviour nobody restates,
    // so it is restated: the marker distinguishes a failure at a glance, and it has its own
    // testid because the dismiss button's X is an svg too — "some icon in the toast" would
    // let a mutant that deletes the marker pass.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')]))
    h.deleteProject.mockRejectedValue(new ApiError('Could not delete the project.', 500))
    renderPage()
    await screen.findByText('Alpha')

    fireEvent.click(screen.getByLabelText('Delete Alpha'))
    fireEvent.change(await screen.findByLabelText(/why are you deleting/i), {
      target: { value: 'no longer needed by ground ops' },
    })
    fireEvent.click(screen.getByRole('button', { name: /delete project/i }))

    expect(await screen.findByTestId('projects-toast-marker')).toBeTruthy()
    // This channel carries only failures and schedules no dismiss. Nothing here proves a
    // timer is absent by waiting — the point is that the toast is still there afterwards.
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.getByTestId('projects-toast')).toBeTruthy()
  })

  it('does not flash the first-run state while a cleared search is still debouncing', async () => {
    // ALSO FROM #172. `appliedQuery` is what decides what an empty list MEANS; branching on
    // the live input would read a cleared box as "this person has no projects" and flash
    // the first-run panel at someone who has plenty.
    h.listProjects.mockResolvedValue(page([]))
    renderPage()
    await screen.findByTestId('projects-empty')

    fireEvent.change(screen.getByLabelText('Search projects'), { target: { value: 'zzz' } })
    await screen.findByTestId('projects-no-matches', undefined, { timeout: 3000 })

    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')]))
    fireEvent.change(screen.getByLabelText('Search projects'), { target: { value: '' } })

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
    // requested too (round-4 finding 10). The original version of this test used one static
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
    // The response has to echo the page actually requested (round-4 finding 10) — a static
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
    // Exactly the last five, nothing past the end, and 10 — not any of the others — is
    // the one marked current.
    for (const n of ['6', '7', '8', '9']) {
      expect(screen.getByRole('button', { name: n }).getAttribute('aria-current')).toBeNull()
    }
    expect(screen.getByRole('button', { name: '10' }).getAttribute('aria-current')).toBe('page')
    expect(screen.queryByRole('button', { name: '5' })).toBeNull()
    expect(screen.queryByRole('button', { name: '11' })).toBeNull()
  })

  it('jumps to the first page and back, without walking', async () => {
    // §2 spells the control set literally — « ‹ 1 2 › ». Both jumps were missing.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')], { total: 80, totalPages: 10 }))
    renderPage()
    await screen.findByText('Alpha')

    fireEvent.click(screen.getByRole('button', { name: 'Last page' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '10' })).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: 'First page' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '1' })).toBeTruthy())
    expect(screen.queryByRole('button', { name: '10' })).toBeNull()
  })

  it('deleting the last row on a page does not flash "Nothing here yet"', async () => {
    // The optimistic removal empties `items` while the request is in flight, and that
    // request drops a database. "Nothing here yet" is a claim about the ACCOUNT, so
    // showing it to someone with 40 projects for the length of a round trip is a lie.
    h.listProjects.mockResolvedValue(page([mkProject('p1', 'Alpha')], { total: 40, totalPages: 5 }))
    let release: () => void = () => {}
    h.deleteProject.mockReturnValue(new Promise<void>((r) => (release = () => r())))
    renderPage()
    await screen.findByText('Alpha')

    fireEvent.click(screen.getByLabelText('Delete Alpha'))
    fireEvent.change(await screen.findByLabelText(/why are you deleting/i), {
      target: { value: 'no longer needed by ground ops' },
    })
    fireEvent.click(screen.getByRole('button', { name: /delete project/i }))

    await waitFor(() => expect(screen.queryByText('Alpha')).toBeNull()) // optimistic removal
    expect(screen.queryByTestId('projects-empty')).toBeNull() // ...but not the first-run screen

    // THE DIALOG ITSELF IS STILL OPEN, HERE, WHILE THE ROW IS ALREADY GONE (round-4 finding
    // 9). It used to close in the same commit as the optimistic removal above — batched
    // before the request had even been sent — so its own busy state (the spinner, Cancel
    // disabling) was set and unmounted in one render and could never be observed. The
    // backend does real work before answering, so this window is not theoretical.
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByRole('button', { name: /cancel/i }).hasAttribute('disabled')).toBe(true)

    release()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // FOCUS LANDS ON THE HEADING, not <body>. The row (and its Delete button, the trigger
    // Radix would otherwise try to restore focus to) left the DOM well before the dialog
    // closed, so a detached-node no-op is exactly the failure this proves did not happen
    // (round-4 finding 2).
    expect(document.activeElement?.textContent).toBe('Your apps')
  })

  it('an empty page with a non-zero total is NOT the first-run screen', async () => {
    // Round-4 finding 13, tested at the guard rather than at the frame. The race he
    // describes — the committed render between the delete settling and the refetch effect
    // running — is not observable from RTL, which flushes effects inside `act()`; asserting
    // around it produced a test that passed with the fix REMOVED, so this pins the condition
    // itself instead.
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

    fireEvent.click(screen.getByLabelText('Delete Alpha'))
    fireEvent.change(await screen.findByLabelText(/why are you deleting/i), {
      target: { value: 'no longer needed by ground ops' },
    })
    fireEvent.click(screen.getByRole('button', { name: /delete project/i }))

    await waitFor(() => expect(h.listProjects).toHaveBeenCalled()) // totals refetched
    expect(screen.queryByRole('alert')).toBeNull() // and still no scary toast
  })
})
