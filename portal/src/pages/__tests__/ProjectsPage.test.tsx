/**
 * ProjectsPage (`/projects`) — the keyset-paginated projects index.
 *
 * The data layer (`listProjects` / `createProject` / `deleteProject`) and the
 * delete dialog's chat-counter (`listProjectConversations`) are mocked at the
 * module boundary; `useKeysetList` runs for real (it is what we are exercising
 * through the page). A LocationProbe outside the Routes reports the current path so
 * navigation is observable without a real project-home page. It uses the `vi.hoisted` +
 * `MemoryRouter` shape this directory's suites share.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'

const h = vi.hoisted(() => ({
  listProjects: vi.fn(),
  createProject: vi.fn(),
  deleteProject: vi.fn(),
  listProjectConversations: vi.fn(),
}))

vi.mock('../../utils/projectApi', () => ({
  listProjects: h.listProjects,
  createProject: h.createProject,
  deleteProject: h.deleteProject,
}))
vi.mock('../../utils/conversationApi', () => ({
  listProjectConversations: h.listProjectConversations,
  CONVERSATION_LIST_CAP: 200, // ProjectDeleteDialog compares the count against the server cap
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
  appStatus: null,
  hasRelaunchableSnapshot: null,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  ...over,
})

const emptyPage = { items: [] as Project[], nextCursor: null, hasMore: false }

beforeEach(() => {
  vi.clearAllMocks()
  h.listProjects.mockResolvedValue(emptyPage)
  h.listProjectConversations.mockResolvedValue([])
})
afterEach(() => cleanup())

describe('ProjectsPage — listing', () => {
  it('renders the first page; each card shows its name and a has-app / no-app badge', async () => {
    h.listProjects.mockResolvedValue({
      items: [
        mkProject('p1', 'Alpha', { description: 'first', appId: 'a1', appStatus: 'approved' }),
        mkProject('p2', 'Beta', { description: null, appId: null, appStatus: null }),
      ],
      nextCursor: null,
      hasMore: false,
    })
    renderPage()

    expect(await screen.findByText('Alpha')).toBeTruthy()
    expect(screen.getByText('Beta')).toBeTruthy()
    // Lifecycle status (approved/draft/…) is no longer surfaced on the card — a built project
    // just reads "App"; an unbuilt one reads "No app yet".
    expect(screen.getByText('App')).toBeTruthy() // p1 badge (has an app)
    expect(screen.getByText('No app yet')).toBeTruthy() // p2 badge (appStatus null)
  })

  it('"Load more" appends page 2 and disappears once hasMore is false', async () => {
    h.listProjects
      .mockResolvedValueOnce({ items: [mkProject('p1', 'Alpha')], nextCursor: 'c1', hasMore: true })
      .mockResolvedValueOnce({ items: [mkProject('p2', 'Beta')], nextCursor: null, hasMore: false })
    renderPage()

    expect(await screen.findByText('Alpha')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /load more/i }))

    expect(await screen.findByText('Beta')).toBeTruthy()
    expect(screen.getByText('Alpha')).toBeTruthy() // page 1 kept (appended, not replaced)
    expect(screen.queryByRole('button', { name: /load more/i })).toBeNull()
  })

  it('a description of null renders "No description yet", not a blank or the literal null', async () => {
    h.listProjects.mockResolvedValue({
      items: [mkProject('p1', 'Alpha', { description: null })],
      nextCursor: null,
      hasMore: false,
    })
    renderPage()

    expect(await screen.findByText('Alpha')).toBeTruthy()
    expect(screen.getByText(/no description yet/i)).toBeTruthy()
  })
})

describe('ProjectsPage — search', () => {
  it('debounces the search box, then refetches with q and without a cursor', async () => {
    h.listProjects.mockResolvedValue({ items: [mkProject('p1', 'Alpha')], nextCursor: 'c1', hasMore: true })
    renderPage()
    await screen.findByText('Alpha')

    fireEvent.change(screen.getByLabelText(/search projects/i), { target: { value: 'vip' } })

    await waitFor(() => expect(h.listProjects).toHaveBeenCalledWith(expect.objectContaining({ q: 'vip' })))
    const qCall = h.listProjects.mock.calls.find((c) => c[0]?.q === 'vip')
    expect(qCall?.[0].cursor).toBeNull() // a cursor from the old filter would be meaningless
  })

  it('zero results with a query shows "no matches"; clearing the query restores the list', async () => {
    h.listProjects
      .mockResolvedValueOnce({ items: [mkProject('p1', 'Alpha')], nextCursor: null, hasMore: false }) // mount
      .mockResolvedValueOnce({ items: [], nextCursor: null, hasMore: false }) // q = zzz
      .mockResolvedValueOnce({ items: [mkProject('p1', 'Alpha')], nextCursor: null, hasMore: false }) // q cleared
    renderPage()
    await screen.findByText('Alpha')

    const search = screen.getByLabelText(/search projects/i)
    fireEvent.change(search, { target: { value: 'zzz' } })
    expect(await screen.findByText(/no matches/i)).toBeTruthy()

    fireEvent.change(search, { target: { value: '' } })
    expect(await screen.findByText('Alpha')).toBeTruthy()
  })
})

describe('ProjectsPage — empty state (first run)', () => {
  it('zero projects and no query shows the "create your first project" CTA and no "Load more"', async () => {
    h.listProjects.mockResolvedValue(emptyPage)
    renderPage()

    expect(await screen.findByText(/no projects yet/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /create your first project/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /load more/i })).toBeNull()
  })
})

describe('ProjectsPage — create', () => {
  it('creates a project and navigates to its project home at /projects/{id}', async () => {
    h.listProjects.mockResolvedValue(emptyPage)
    h.createProject.mockResolvedValue(mkProject('p9', 'Created'))
    renderPage()
    await screen.findByText(/no projects yet/i)

    fireEvent.click(screen.getByRole('button', { name: /create your first project/i }))
    fireEvent.change(screen.getByPlaceholderText(/VIP Movement Tracker/i), { target: { value: 'Created' } })
    fireEvent.click(screen.getByRole('button', { name: /^create project$/i }))

    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/projects/p9'))
    expect(h.createProject).toHaveBeenCalledWith(expect.objectContaining({ name: 'Created' }))
  })

  it('blocks a 121-char name client-side (createProject is never called) but accepts exactly 120', async () => {
    h.listProjects.mockResolvedValue(emptyPage)
    h.createProject.mockResolvedValue(mkProject('p9', 'x'))
    renderPage()
    await screen.findByText(/no projects yet/i)

    fireEvent.click(screen.getByRole('button', { name: /new project/i }))
    const nameInput = screen.getByPlaceholderText(/VIP Movement Tracker/i)

    fireEvent.change(nameInput, { target: { value: 'x'.repeat(121) } })
    fireEvent.click(screen.getByRole('button', { name: /^create project$/i }))
    expect(h.createProject).not.toHaveBeenCalled() // blocked before any request fires

    fireEvent.change(nameInput, { target: { value: 'x'.repeat(120) } })
    fireEvent.click(screen.getByRole('button', { name: /^create project$/i }))
    await waitFor(() => expect(h.createProject).toHaveBeenCalledTimes(1))
    expect(h.createProject).toHaveBeenCalledWith(expect.objectContaining({ name: 'x'.repeat(120) }))
  })

  it('shows the backend 422 field message, not a synthetic "Failed to create project"', async () => {
    h.listProjects.mockResolvedValue(emptyPage)
    h.createProject.mockRejectedValue(new ApiError('String should have at most 120 characters', 422))
    renderPage()
    await screen.findByText(/no projects yet/i)

    fireEvent.click(screen.getByRole('button', { name: /new project/i }))
    fireEvent.change(screen.getByPlaceholderText(/VIP Movement Tracker/i), { target: { value: 'Valid name' } })
    fireEvent.click(screen.getByRole('button', { name: /^create project$/i }))

    expect(await screen.findByText(/at most 120 characters/i)).toBeTruthy()
    expect(screen.queryByText(/failed to create project/i)).toBeNull()
  })
})

describe('ProjectsPage — delete', () => {
  async function openDeleteDialogFor(name: string): Promise<void> {
    fireEvent.click(screen.getByRole('button', { name: new RegExp(`delete ${name}`, 'i') }))
    const input = await screen.findByLabelText(/type the project name/i)
    fireEvent.change(input, { target: { value: name } })
    fireEvent.click(screen.getByRole('button', { name: /delete project/i }))
  }

  it('a 404 (already deleted elsewhere) removes the row with no error toast', async () => {
    h.listProjects.mockResolvedValue({ items: [mkProject('p1', 'Alpha')], nextCursor: null, hasMore: false })
    h.listProjectConversations.mockResolvedValue([{}, {}])
    h.deleteProject.mockRejectedValue(new ApiError('Project not found.', 404))
    renderPage()
    await screen.findByText('Alpha')

    await openDeleteDialogFor('Alpha')

    await waitFor(() => expect(screen.queryByText('Alpha')).toBeNull()) // row gone
    expect(screen.queryByRole('alert')).toBeNull() // and no scary toast
  })

  it('a 500 restores the row and surfaces the error', async () => {
    h.listProjects.mockResolvedValue({ items: [mkProject('p1', 'Alpha')], nextCursor: null, hasMore: false })
    h.listProjectConversations.mockResolvedValue([{}])
    h.deleteProject.mockRejectedValue(new ApiError('Could not delete the project.', 500))
    renderPage()
    await screen.findByText('Alpha')

    await openDeleteDialogFor('Alpha')

    expect(await screen.findByRole('alert')).toBeTruthy() // error surfaced
    expect(await screen.findByText('Alpha')).toBeTruthy() // row restored by refetch
  })

  // U15 — this channel carries only failures (a successful delete is silent), and unlike
  // Navbar's and AdminPage's toasts it never had a 3-second dismiss timer wired to it in
  // the first place. Pin that explicitly: something that went wrong waits for the reader
  // to dismiss it, not for a clock. `getBy*` + manual `advanceTimersByTimeAsync` (not
  // `findBy*`) matches this file's own established fake-timer pattern above.
  it('a delete failure does not auto-dismiss, and carries a failure marker', async () => {
    h.listProjects.mockResolvedValue({ items: [mkProject('p1', 'Alpha')], nextCursor: null, hasMore: false })
    h.listProjectConversations.mockResolvedValue([{}])
    h.deleteProject.mockRejectedValue(new ApiError('Could not delete the project.', 500))
    vi.useFakeTimers()
    try {
      renderPage()
      await vi.advanceTimersByTimeAsync(0)
      expect(screen.getByText('Alpha')).toBeTruthy()

      fireEvent.click(screen.getByRole('button', { name: /delete alpha/i }))
      await vi.advanceTimersByTimeAsync(0) // the dialog's own chat-count fetch resolves
      fireEvent.change(screen.getByLabelText(/type the project name/i), { target: { value: 'Alpha' } })
      fireEvent.click(screen.getByRole('button', { name: /delete project/i }))
      await vi.advanceTimersByTimeAsync(0) // deleteProject's rejection resolves, toast is set

      // The marker, not the text: an icon distinguishes this as a failure the same way
      // Navbar's and AdminPage's toasts now do, so it reads at a glance without the words.
      // A specific testid (not just "some svg in the toast") — the dismiss button's own
      // X icon is also an svg, and would let a mutant that deletes the marker pass.
      expect(screen.getByTestId('projects-toast-marker')).toBeTruthy()

      // Advance well past the 3s window a confirmation elsewhere in this unit fades on —
      // nothing here schedules a dismiss at all, and this proves it stays that way.
      await vi.advanceTimersByTimeAsync(10_000)
      expect(screen.getByTestId('projects-toast')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('ProjectsPage — a failure with rows on screen is never silent', () => {
  it('surfaces a failed "Load more" inline instead of leaving a frozen button', async () => {
    h.listProjects
      .mockResolvedValueOnce({ items: [mkProject('p1', 'Alpha')], nextCursor: 'c1', hasMore: true })
      .mockRejectedValueOnce(new ApiError('Cursor is invalid.', 422))
    renderPage()
    await screen.findByText('Alpha')

    fireEvent.click(screen.getByRole('button', { name: /load more/i }))

    // The full-page error state cannot fire — we still have page 1's rows — so the
    // message has to appear next to the control the user just pressed.
    expect(await screen.findByText('Cursor is invalid.')).toBeTruthy()
    expect(screen.getByText('Alpha')).toBeTruthy()
  })

  // The empty state must describe the query the ROWS answer, not the one being typed.
  // Clearing a no-match search leaves `q === ''` for the whole 300ms debounce while
  // `items` still reflects the old query — deciding on `q` there tells a user with
  // dozens of projects that they have none, and offers to create their first.
  it('does not flash "create your first project" while a cleared search is still debouncing', async () => {
    vi.useFakeTimers()
    try {
      h.listProjects.mockResolvedValue(emptyPage) // the 'zzz' search finds nothing
      renderPage()
      await vi.advanceTimersByTimeAsync(0)

      fireEvent.change(screen.getByLabelText('Search projects'), { target: { value: 'zzz' } })
      await vi.advanceTimersByTimeAsync(400)
      expect(screen.getByText('No matches')).toBeTruthy()

      // Now clear it. This user has projects; the refetch has not landed yet.
      h.listProjects.mockResolvedValue({ items: [mkProject('p1', 'Alpha')], nextCursor: null, hasMore: false })
      fireEvent.change(screen.getByLabelText('Search projects'), { target: { value: '' } })
      await vi.advanceTimersByTimeAsync(100) // still inside the debounce window

      expect(screen.queryByText(/no projects yet/i)).toBeNull()
      expect(screen.queryByText(/create your first project/i)).toBeNull()

      await vi.advanceTimersByTimeAsync(400) // debounce fires, the rows land
      expect(screen.getByText('Alpha')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })
})
