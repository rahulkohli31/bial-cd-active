/**
 * SharePanel — the four named colleague-search states, the mandatory disclosure copy, the
 * share/revoke round trip, and the a11y labels that distinguish one result's button from
 * another's. Shipped with zero tests before this file.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'

const h = vi.hoisted(() => ({
  listProjectShares: vi.fn(),
  searchColleagues: vi.fn(),
  shareProject: vi.fn(),
  unshareProject: vi.fn(),
}))
vi.mock('../../../utils/sharingApi', () => ({
  listProjectShares: h.listProjectShares,
  searchColleagues: h.searchColleagues,
  shareProject: h.shareProject,
  unshareProject: h.unshareProject,
}))

import SharePanel from '../SharePanel'
import { ApiError } from '../../../utils/apiError'
import type { Colleague, ProjectShare } from '../../../utils/sharingApi'

const ANA: Colleague = { id: 'colleague-1', displayName: 'Ana Reyes', emailLocalPart: 'ana' }

const EXISTING_SHARE: ProjectShare = {
  id: 'share-1',
  sharedWithUserId: 'colleague-9',
  sharedWithDisplayName: 'Priya K',
  sharedWithEmailLocalPart: 'priya.k',
  createdAt: '2026-09-01T00:00:00.000Z',
}

function renderPanel(onClose: () => void = vi.fn()) {
  return render(<SharePanel projectId="p1" projectName="Gate Pass Log" onClose={onClose} />)
}

function typeQuery(text: string) {
  fireEvent.change(screen.getByPlaceholderText('Search by name or email'), {
    target: { value: text },
  })
}

beforeEach(() => {
  h.listProjectShares.mockReset().mockResolvedValue([])
  h.searchColleagues.mockReset().mockResolvedValue([])
  h.shareProject.mockReset().mockResolvedValue({ ...EXISTING_SHARE, id: 'share-new' })
  h.unshareProject.mockReset().mockResolvedValue({ ok: true })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('mandatory disclosure copy', () => {
  it('states what a share grants before anyone is added', async () => {
    renderPanel()
    expect(await screen.findByText(/Anyone you add can open and use this app/)).toBeTruthy()
    expect(screen.getByText(/saved into the project.s real data/)).toBeTruthy()
  })

  it('labels an existing share "Can use", never "view only"', async () => {
    h.listProjectShares.mockResolvedValue([EXISTING_SHARE])
    renderPanel()
    expect(await screen.findByText('Can use')).toBeTruthy()
    expect(screen.queryByText(/view only/i)).toBeNull()
  })
})

describe('the empty "who can use this" state', () => {
  it('says nothing is shared yet, with no "New project" control anywhere in the panel', async () => {
    renderPanel()
    expect(await screen.findByText('Not shared with anyone yet.')).toBeTruthy()
    expect(screen.queryByText(/new project/i)).toBeNull()
  })
})

describe('the four named colleague-search states', () => {
  it('state 1: below the minimum says how many characters are needed, and runs no search', async () => {
    renderPanel()
    typeQuery('an')
    expect(await screen.findByText('Type at least 3 characters to search.')).toBeTruthy()
    expect(h.searchColleagues).not.toHaveBeenCalled()
  })

  it('an empty query shows no notice at all', async () => {
    renderPanel()
    typeQuery('an')
    typeQuery('')
    await waitFor(() => {
      expect(screen.getByTestId('colleague-search-status').textContent).toBe('')
    })
  })

  it('state 2: in flight shows Searching…', async () => {
    vi.useFakeTimers()
    let resolveSearch: (colleagues: Colleague[]) => void = () => {}
    h.searchColleagues.mockReturnValue(
      new Promise((resolve) => {
        resolveSearch = resolve
      }),
    )
    renderPanel()
    typeQuery('ana')
    await vi.advanceTimersByTimeAsync(300)
    expect(screen.getByText('Searching…')).toBeTruthy()
    resolveSearch([])
    await vi.advanceTimersByTimeAsync(0)
  })

  it('state 3: no matches', async () => {
    vi.useFakeTimers()
    h.searchColleagues.mockResolvedValue([])
    renderPanel()
    typeQuery('ana')
    await vi.advanceTimersByTimeAsync(300)
    await vi.advanceTimersByTimeAsync(0)
    expect(screen.getByText('No matching colleagues.')).toBeTruthy()
  })

  it('state 3: matches render, one row per colleague', async () => {
    vi.useFakeTimers()
    h.searchColleagues.mockResolvedValue([ANA])
    renderPanel()
    typeQuery('ana')
    await vi.advanceTimersByTimeAsync(300)
    await vi.advanceTimersByTimeAsync(0)
    expect(screen.getByText('Ana Reyes')).toBeTruthy()
  })

  it("state 4: rate-limited reads as a wait, never the server's own 429 text", async () => {
    vi.useFakeTimers()
    h.searchColleagues.mockRejectedValue(new ApiError('Too Many Requests', 429))
    renderPanel()
    typeQuery('ana')
    await vi.advanceTimersByTimeAsync(300)
    await vi.advanceTimersByTimeAsync(0)
    expect(screen.getByText('Too many searches. Please wait a moment and try again.')).toBeTruthy()
    expect(screen.queryByText('Too Many Requests')).toBeNull()
  })

  it('a non-429 search failure reads as a generic, non-technical notice', async () => {
    vi.useFakeTimers()
    h.searchColleagues.mockRejectedValue(new Error('ECONNRESET'))
    renderPanel()
    typeQuery('ana')
    await vi.advanceTimersByTimeAsync(300)
    await vi.advanceTimersByTimeAsync(0)
    expect(screen.getByText('Could not search colleagues right now.')).toBeTruthy()
  })
})

describe('the stale-results race', () => {
  it('a response landing after the query shrank below 3 characters never overwrites the notice', async () => {
    vi.useFakeTimers()
    let resolveSearch: (colleagues: Colleague[]) => void = () => {}
    h.searchColleagues.mockReturnValue(
      new Promise((resolve) => {
        resolveSearch = resolve
      }),
    )
    renderPanel()
    typeQuery('ana')
    await vi.advanceTimersByTimeAsync(300) // the debounced search fires
    typeQuery('an') // shrinks back below the minimum before it resolves
    resolveSearch([ANA]) // the stale request finally lands
    await vi.advanceTimersByTimeAsync(0)
    expect(screen.getByText('Type at least 3 characters to search.')).toBeTruthy()
    expect(screen.queryByText('Ana Reyes')).toBeNull()
  })
})

describe('sharing', () => {
  it('shares a colleague and appends the server\'s own row, without refetching the whole list', async () => {
    vi.useFakeTimers()
    h.searchColleagues.mockResolvedValue([ANA])
    h.shareProject.mockResolvedValue({
      id: 'share-new',
      sharedWithUserId: ANA.id,
      sharedWithDisplayName: ANA.displayName,
      sharedWithEmailLocalPart: ANA.emailLocalPart,
      createdAt: '2026-09-01T00:00:00.000Z',
    })
    renderPanel()
    typeQuery('ana')
    await vi.advanceTimersByTimeAsync(300)
    await vi.advanceTimersByTimeAsync(0)
    // `waitFor`/`findBy*` poll on REAL timers internally; fake timers past this point would
    // hang them forever rather than advance on their own.
    vi.useRealTimers()

    fireEvent.click(await screen.findByRole('button', { name: 'Share with Ana Reyes' }))
    await waitFor(() => expect(h.shareProject).toHaveBeenCalledWith('p1', ANA.id))
    // The new row lands without a second `listProjectShares` round trip — no "Loading…" flash
    // over the list on every single add.
    expect(h.listProjectShares).toHaveBeenCalledTimes(1)
    expect(await screen.findByRole('button', { name: 'Remove Ana Reyes' })).toBeTruthy()
    // The shared colleague drops out of the search results once they've been added.
    expect(screen.queryByRole('button', { name: 'Share with Ana Reyes' })).toBeNull()
  })

  it('a share failure shows an error banner', async () => {
    vi.useFakeTimers()
    h.searchColleagues.mockResolvedValue([ANA])
    // A non-`Error` rejection, deliberately: `errorMessage` reads `.message` off a real
    // `Error` and falls back to the given text only otherwise — this pins the fallback arm.
    h.shareProject.mockRejectedValue('boom')
    renderPanel()
    typeQuery('ana')
    await vi.advanceTimersByTimeAsync(300)
    await vi.advanceTimersByTimeAsync(0)
    vi.useRealTimers()

    fireEvent.click(await screen.findByRole('button', { name: 'Share with Ana Reyes' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Could not share this project.')
  })
})

describe('revoking', () => {
  it('removes a colleague, and the row disappears', async () => {
    h.listProjectShares.mockResolvedValue([EXISTING_SHARE])
    renderPanel()
    const removeButton = await screen.findByRole('button', { name: 'Remove Priya K' })
    fireEvent.click(removeButton)
    await waitFor(() =>
      expect(h.unshareProject).toHaveBeenCalledWith('p1', EXISTING_SHARE.sharedWithUserId),
    )
    await waitFor(() => expect(screen.queryByText('Priya K')).toBeNull())
  })
})

describe('a11y: distinguishable buttons per row', () => {
  it('two search results never both read as a bare "Share" button', async () => {
    vi.useFakeTimers()
    const priya: Colleague = { id: 'colleague-2', displayName: 'Priya K', emailLocalPart: 'priya' }
    h.searchColleagues.mockResolvedValue([ANA, priya])
    renderPanel()
    typeQuery('ana')
    await vi.advanceTimersByTimeAsync(300)
    await vi.advanceTimersByTimeAsync(0)

    expect(screen.getByRole('button', { name: 'Share with Ana Reyes' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Share with Priya K' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Share' })).toBeNull()
  })
})

describe('closing', () => {
  it('calls onClose from the close control', async () => {
    const onClose = vi.fn()
    renderPanel(onClose)
    fireEvent.click(await screen.findByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalled()
  })
})
