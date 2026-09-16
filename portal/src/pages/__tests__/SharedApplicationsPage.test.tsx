/**
 * SharedApplicationsPage (`/shared-applications`) — the applications colleagues shared with the
 * reader, drawn out of the home list's own parts.
 *
 * The data layer is mocked at the module boundary; the page's own paging, filter and sort state
 * run for real, because that is what is being exercised. A LocationProbe outside the Routes
 * reports the path and the query string, so the address bar is an assertable output rather than
 * scenery — the same shape `ProjectsPage.test.tsx` uses.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom'

const h = vi.hoisted(() => ({ listSharedWithMe: vi.fn() }))

vi.mock('../../utils/sharingApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  listSharedWithMe: h.listSharedWithMe,
}))

import SharedApplicationsPage from '../SharedApplicationsPage'
import type { SharedProject, SharedProjectSharer } from '../../utils/sharingApi'

function LocationProbe(): React.JSX.Element {
  const loc = useLocation()
  return (
    <>
      <div data-testid="location">{loc.pathname}</div>
      <div data-testid="location-search">{loc.search}</div>
    </>
  )
}

/** The browser's Back button, which RTL cannot press. Named so it collides with nothing. */
function BackButton(): React.JSX.Element {
  const navigate = useNavigate()
  return <button onClick={() => navigate(-1)}>browser back</button>
}

type Entry = string | { pathname: string; search?: string; state?: { notice: string } }

function renderPage(entry: Entry = '/shared-applications') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <LocationProbe />
      <BackButton />
      <Routes>
        <Route path="/shared-applications" element={<SharedApplicationsPage />} />
        <Route path="/shared/:id" element={<div data-testid="shared-workspace">workspace</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

const RAHUL = '0199aa00-0000-7000-8000-000000000001'
const VARUN = '0199aa00-0000-7000-8000-000000000002'

const mkShared = (
  id: string,
  name: string,
  over: Partial<SharedProject> = {},
): SharedProject => ({
  projectId: id,
  projectName: name,
  projectDescription: 'Fuel uplift per stand.',
  projectUpdatedAt: '2026-09-14T00:00:00Z',
  sharedByUserId: RAHUL,
  sharedByDisplayName: 'Rahul Kohli',
  sharedAt: '2026-09-15T00:00:00Z',
  ...over,
})

const sharer = (userId: string, displayName: string | null, shareCount: number): SharedProjectSharer => ({
  userId,
  displayName,
  shareCount,
})

const pageOf = (
  items: SharedProject[],
  over: Partial<{
    sharers: SharedProjectSharer[]
    page: number
    pageSize: number
    total: number
    totalPages: number
  }> = {},
) => ({
  items,
  sharers: [],
  page: 1,
  pageSize: 8,
  total: items.length,
  totalPages: items.length === 0 ? 0 : 1,
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  h.listSharedWithMe.mockResolvedValue(pageOf([]))
})
afterEach(() => cleanup())

// --- what the list actually shows --------------------------------------

describe('★ the shared list', () => {
  it('lists what colleagues shared, newest grant first by default', async () => {
    h.listSharedWithMe.mockResolvedValue(
      pageOf([mkShared('p1', 'Apron Fuel Truck Log'), mkShared('p2', 'Lost Property Register')]),
    )
    renderPage()

    const rows = await screen.findAllByTestId('shared-app-row')
    expect(rows.map((row) => within(row).getByRole('button', { name: /^Open /i }).getAttribute('aria-label'))).toEqual([
      'Open Apron Fuel Truck Log',
      'Open Lost Property Register',
    ])
    // The ORDER IS THE SERVER'S, and the default it is asked for is the newest grant.
    expect(h.listSharedWithMe).toHaveBeenLastCalledWith(
      expect.objectContaining({ sort: 'recentlyShared' }),
    )
  })

  it('shows both dates: when it was shared, and when the owner last changed it', async () => {
    h.listSharedWithMe.mockResolvedValue(
      pageOf([
        mkShared('p1', 'Renamed Since', {
          sharedAt: '2026-09-01T00:00:00Z',
          projectUpdatedAt: '2026-09-10T00:00:00Z',
        }),
      ]),
    )
    renderPage()

    const row = await screen.findByTestId('shared-app-row')
    // The name is the owner's current one, and the two dates are different questions.
    expect(within(row).getByText('Renamed Since')).toBeTruthy()
    expect(within(row).getByText('1 Sep 2026')).toBeTruthy()
    expect(within(row).getByText('10 Sep 2026')).toBeTruthy()
  })

  it('renders no status chip, in either view', async () => {
    h.listSharedWithMe.mockResolvedValue(pageOf([mkShared('p1', 'Apron Fuel Truck Log')]))
    renderPage()

    // LIVENESS FIRST, then the absence — a crash renders no chip either.
    const row = await screen.findByTestId('shared-app-row')
    expect(within(row).getByText('Apron Fuel Truck Log')).toBeTruthy()
    expect(within(row).queryByText(/live|draft|in review|taken offline|nothing built/i)).toBeNull()

    fireEvent.click(screen.getByLabelText('Grid view'))

    const tileNode = await screen.findByTestId('shared-app-tile')
    expect(within(tileNode).getByText('Apron Fuel Truck Log')).toBeTruthy()
    expect(within(tileNode).queryByText(/live|draft|in review|taken offline|nothing built/i)).toBeNull()
  })

  it('offers a recipient Open and no menu at all, in either view', async () => {
    h.listSharedWithMe.mockResolvedValue(pageOf([mkShared('p1', 'Apron Fuel Truck Log')]))
    renderPage()

    const row = await screen.findByTestId('shared-app-row')
    expect(within(row).getByRole('button', { name: 'Open Apron Fuel Truck Log' })).toBeTruthy()
    // THE PERMISSION-BOUNDARY LEAK THE EXTRACTION COULD HAVE INTRODUCED. Paired with the
    // liveness above, because a row that failed to render carries no menu either.
    expect(screen.queryByTestId('app-menu-row')).toBeNull()

    fireEvent.click(screen.getByLabelText('Grid view'))

    const tileNode = await screen.findByTestId('shared-app-tile')
    expect(within(tileNode).getByRole('button', { name: 'Open Apron Fuel Truck Log' })).toBeTruthy()
    expect(screen.queryByTestId('app-menu-tile')).toBeNull()
  })

  it('opens a shared application on its own restricted route', async () => {
    h.listSharedWithMe.mockResolvedValue(pageOf([mkShared('p1', 'Apron Fuel Truck Log')]))
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Open Apron Fuel Truck Log' }))

    expect(await screen.findByTestId('shared-workspace')).toBeTruthy()
    expect(screen.getByTestId('location').textContent).toBe('/shared/p1')
  })

  it('shows the empty state, not an empty table frame', async () => {
    renderPage()

    const empty = await screen.findByTestId('shared-empty')
    expect(empty.textContent).toContain('Nothing shared with you yet')
    expect(screen.queryByTestId('shared-app-row')).toBeNull()
    // …and it does NOT invite a recipient to create one from a list about other people's work.
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull()
  })

  it('does not say "nothing shared with you" when it is a filter that matched nothing', async () => {
    h.listSharedWithMe.mockResolvedValue(
      pageOf([], { sharers: [sharer(VARUN, 'Varun Menon', 0)], total: 0, totalPages: 0 }),
    )
    renderPage(`/shared-applications?sharedBy=${VARUN}`)

    // Liveness first: the page settled on its no-matches card rather than on nothing at all.
    const card = await screen.findByTestId('shared-no-matches')
    // "Nothing shared with you yet" is a claim about the WHOLE relationship; under a filter it
    // is simply false, and the reader would have no reason to look for the filter still on.
    expect(screen.queryByTestId('shared-empty')).toBeNull()
    expect(card.textContent).toContain('Nothing shared with you matches that filter.')

    fireEvent.click(within(card).getByRole('button', { name: 'Clear the search and the filter' }))

    await waitFor(() => expect(screen.getByTestId('location-search').textContent).toBe(''))
  })
})

// --- the "Shared by" filter --------------------------------------------

describe('★ the "Shared by" filter matches on an id and only labels with a name', () => {
  async function pickSharer(option: string | RegExp): Promise<void> {
    fireEvent.click(screen.getByRole('combobox', { name: 'Shared by' }))
    fireEvent.click(await screen.findByRole('option', { name: option }))
  }

  it('narrows the list to one colleague, and the caption states the filtered count', async () => {
    h.listSharedWithMe.mockImplementation((args: { sharedBy?: string }) =>
      Promise.resolve(
        args.sharedBy === VARUN
          ? pageOf([mkShared('p2', 'Security Lane Wait Times', { sharedByUserId: VARUN, sharedByDisplayName: 'Varun Menon' })], {
              sharers: [sharer(RAHUL, 'Rahul Kohli', 2), sharer(VARUN, 'Varun Menon', 1)],
              total: 1,
              totalPages: 1,
            })
          : pageOf(
              [
                mkShared('p1', 'Apron Fuel Truck Log'),
                mkShared('p3', 'Lost Property Register'),
                mkShared('p2', 'Security Lane Wait Times', { sharedByUserId: VARUN, sharedByDisplayName: 'Varun Menon' }),
              ],
              { sharers: [sharer(RAHUL, 'Rahul Kohli', 2), sharer(VARUN, 'Varun Menon', 1)], total: 3, totalPages: 1 },
            ),
      ),
    )
    renderPage()
    await screen.findByText('Apron Fuel Truck Log')
    expect(screen.getByTestId('shared-range').textContent).toContain('of 3')

    await pickSharer(/Varun Menon/)

    await waitFor(() => expect(screen.queryByText('Apron Fuel Truck Log')).toBeNull())
    expect(screen.getByText('Security Lane Wait Times')).toBeTruthy()
    expect(screen.getByTestId('shared-range').textContent).toContain('of 1')
    // THE ID, never the name: the filter travels as the colleague's id.
    expect(h.listSharedWithMe).toHaveBeenLastCalledWith(expect.objectContaining({ sharedBy: VARUN }))
    expect(screen.getByTestId('location-search').textContent).toBe(`?sharedBy=${VARUN}`)
  })

  it('keeps two colleagues who share a display name as two entries that filter independently', async () => {
    const TWIN = '0199aa00-0000-7000-8000-000000000003'
    h.listSharedWithMe.mockResolvedValue(
      pageOf([mkShared('p1', 'Apron Fuel Truck Log')], {
        sharers: [sharer(RAHUL, 'Rahul Kohli', 2), sharer(TWIN, 'Rahul Kohli', 1)],
      }),
    )
    renderPage()
    await screen.findByText('Apron Fuel Truck Log')

    fireEvent.click(screen.getByRole('combobox', { name: 'Shared by' }))
    const options = await screen.findAllByRole('option', { name: /Rahul Kohli/ })

    // TWO ENTRIES, not one: a filter keyed on the display name would have collapsed them and
    // handed this reader the other colleague's applications.
    expect(options.length).toBe(2)

    fireEvent.click(options[1])

    await waitFor(() =>
      expect(h.listSharedWithMe).toHaveBeenLastCalledWith(expect.objectContaining({ sharedBy: TWIN })),
    )
  })

  it('labels a colleague with no display name rather than showing an id', async () => {
    h.listSharedWithMe.mockResolvedValue(
      pageOf([mkShared('p1', 'Apron Fuel Truck Log', { sharedByDisplayName: null })], {
        sharers: [sharer(RAHUL, null, 1)],
      }),
    )
    renderPage()

    const row = await screen.findByTestId('shared-app-row')
    expect(within(row).getByText('A colleague')).toBeTruthy()
    expect(row.textContent).not.toContain(RAHUL)
  })
})

// --- search, sort, view and the page ------------------------------------

describe('★ what the reader asked for survives every other control', () => {
  it('searches without narrowing anything itself — the scope is the server\'s', async () => {
    // The term matches this row's DESCRIPTION and not its name, which is the whole point: the
    // server searches descriptions only. Turn red by adding a client-side name pass — this row
    // would vanish from a list the server had already answered with it.
    h.listSharedWithMe.mockImplementation((args: { q?: string }) =>
      Promise.resolve(
        pageOf(
          args.q === 'stand'
            ? [mkShared('p1', 'Apron Fuel Truck Log')]
            : [mkShared('p1', 'Apron Fuel Truck Log'), mkShared('p2', 'Lost Property Register')],
        ),
      ),
    )
    renderPage()
    await screen.findByText('Lost Property Register')

    fireEvent.change(screen.getByLabelText('Search applications shared with you'), {
      target: { value: 'stand' },
    })

    await waitFor(
      () => expect(h.listSharedWithMe).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'stand' })),
      { timeout: 3000 },
    )
    await waitFor(() => expect(screen.queryByText('Lost Property Register')).toBeNull())
    expect(screen.getByText('Apron Fuel Truck Log')).toBeTruthy()
    expect(screen.getByTestId('shared-range').textContent).toContain('of 1')
  })

  it('keeps the filter, the sort and the page when the view changes', async () => {
    h.listSharedWithMe.mockResolvedValue(
      pageOf([mkShared('p1', 'Apron Fuel Truck Log')], {
        sharers: [sharer(RAHUL, 'Rahul Kohli', 9)],
        page: 2,
        total: 9,
        totalPages: 2,
      }),
    )
    renderPage(`/shared-applications?page=2&sharedBy=${RAHUL}&sort=name`)
    await screen.findByTestId('shared-app-row')
    h.listSharedWithMe.mockClear()

    fireEvent.click(screen.getByLabelText('Grid view'))

    await screen.findByTestId('shared-app-tile')
    // The view is a habit, not a place in the list: changing it asks the server nothing new
    // and takes nothing away from the address.
    expect(h.listSharedWithMe).not.toHaveBeenCalled()
    expect(screen.getByTestId('location-search').textContent).toBe(
      `?page=2&sharedBy=${RAHUL}&sort=name`,
    )
  })

  it('remembers the view and the density the owner list already stores', async () => {
    localStorage.setItem('bial.projects.view', 'grid')
    localStorage.setItem('bial.projects.density', 'L')
    h.listSharedWithMe.mockResolvedValue(pageOf([mkShared('p1', 'Apron Fuel Truck Log')]))
    renderPage()

    // ONE REMEMBERED PREFERENCE FOR EVERY APPLICATION LIST — the shared tiles inherit the
    // owner list's S/M/L rather than carrying a density feature of their own.
    expect(await screen.findByTestId('shared-app-tile')).toBeTruthy()
    const density = screen.getByRole('radio', { name: 'L cards' })
    expect(density.getAttribute('data-state')).toBe('on')
  })

  it('steps a reader back when a revoke empties the page under them', async () => {
    h.listSharedWithMe.mockImplementation((args: { page: number }) =>
      // The share was revoked between loads: two pages became one, and page 2 is now empty.
      Promise.resolve(
        args.page === 2
          ? pageOf([], { page: 2, total: 1, totalPages: 1 })
          : pageOf([mkShared('p1', 'Apron Fuel Truck Log')], { page: 1, total: 1, totalPages: 1 }),
      ),
    )
    renderPage('/shared-applications?page=2')

    await screen.findByTestId('shared-app-row')
    expect(screen.getByTestId('location-search').textContent).toBe('')
  })

  it('reports the total the server last answered, never a cached one', async () => {
    h.listSharedWithMe
      .mockResolvedValueOnce(pageOf([mkShared('p1', 'Apron Fuel Truck Log')], { total: 3, totalPages: 1 }))
      // A colleague shares something while the reader is part-way through: the next read must
      // report the list as it now is. A repeat at a page boundary is the cost offset paging
      // accepts here; a stale total is not.
      .mockResolvedValue(pageOf([mkShared('p1', 'Apron Fuel Truck Log')], { total: 4, totalPages: 1 }))
    renderPage()
    await waitFor(() => expect(screen.getByTestId('shared-range').textContent).toContain('of 3'))

    fireEvent.click(screen.getByRole('combobox', { name: 'Sort' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Name' }))

    await waitFor(() => expect(screen.getByTestId('shared-range').textContent).toContain('of 4'))
    expect(h.listSharedWithMe).toHaveBeenLastCalledWith(expect.objectContaining({ sort: 'name' }))
  })
})

// --- when the read fails ------------------------------------------------

describe('★ a failing read', () => {
  it('keeps the rows already on screen and offers a retry that re-fetches', async () => {
    h.listSharedWithMe
      .mockResolvedValueOnce(pageOf([mkShared('p1', 'Apron Fuel Truck Log')], { total: 9, totalPages: 2 }))
      .mockRejectedValueOnce(new Error('nope'))
      .mockResolvedValue(pageOf([mkShared('p2', 'Lost Property Register')], { page: 2, total: 9, totalPages: 2 }))
    renderPage()
    await screen.findByText('Apron Fuel Truck Log')

    fireEvent.click(screen.getByRole('button', { name: '2' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Couldn’t load more')
    // The rows the reader was using are STILL THERE.
    expect(screen.getByText('Apron Fuel Truck Log')).toBeTruthy()

    fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }))

    expect(await screen.findByText('Lost Property Register')).toBeTruthy()
  })

  it('shows the failure card when the FIRST read fails, with a retry that really re-asks', async () => {
    h.listSharedWithMe
      .mockRejectedValueOnce(new Error('nope'))
      .mockResolvedValue(pageOf([mkShared('p1', 'Apron Fuel Truck Log')]))
    renderPage()

    const card = await screen.findByTestId('shared-error')
    expect(card.textContent).toContain('Couldn’t load the applications shared with you')

    fireEvent.click(within(card).getByRole('button', { name: 'Retry' }))

    expect(await screen.findByText('Apron Fuel Truck Log')).toBeTruthy()
  })
})

// --- the bounce off a share that is gone ---------------------------------

describe('★ a recipient bounced off a dead share', () => {
  it('is told once, in the page\'s own polite region, and the sentence does not survive a reload', async () => {
    renderPage({ pathname: '/shared-applications', state: { notice: 'That application is no longer available.' } })

    const region = await screen.findByTestId('shared-notice')
    expect(region.getAttribute('aria-live')).toBe('polite')
    await waitFor(() => expect(region.textContent).toContain('That application is no longer available.'))

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss notice' }))

    await waitFor(() => expect(screen.getByTestId('shared-notice').textContent).toBe(''))

    // AND IT DOES NOT COME BACK. React Router keeps the sentence in `window.history.state`, so
    // the entry the reader arrived on still carries it — stepping away and back replays a bounce
    // they already dealt with unless the entry itself was replaced with a stateless one.
    fireEvent.click(screen.getByRole('combobox', { name: 'Sort' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Name' }))
    await waitFor(() => expect(screen.getByTestId('location-search').textContent).toBe('?sort=name'))

    fireEvent.click(screen.getByText('browser back'))

    await waitFor(() => expect(screen.getByTestId('location-search').textContent).toBe(''))
    expect(screen.getByTestId('shared-notice').textContent).toBe('')
  })
})
