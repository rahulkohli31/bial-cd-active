import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react'
import { ApiError } from '../../../utils/apiError'
import type { Colleague, ProjectShare } from '../../../utils/sharingApi'

const h = vi.hoisted(() => ({
  listProjectShares: vi.fn(),
  searchColleagues: vi.fn(),
  shareProject: vi.fn(),
  unshareProject: vi.fn(),
}))
vi.mock('../../../utils/sharingApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...h,
}))

import { SharePanelBody } from '../SharePanel'

const HINT = "They'll see it the first time they sign in."

const priya: Colleague = {
  kind: 'directory',
  directoryId: '5b1c7d2e-3f4a-4b5c-8d6e-7f8091a2b3c4',
  displayName: 'Priya Sharma',
  emailLocalPart: 'priya.sharma',
  signedIn: false,
}
const priyanka: Colleague = {
  kind: 'directory',
  directoryId: '9e8d7c6b-5a49-4382-9716-05f4e3d2c1b0',
  displayName: 'Priyanka Das',
  emailLocalPart: 'priyanka.das',
  signedIn: false,
}
const asha: Colleague = {
  kind: 'user',
  id: '0192f0a0-0000-7000-8000-000000000002',
  displayName: 'Asha Rao',
  emailLocalPart: 'asha.rao',
  signedIn: true,
}

const shareOf = (colleague: Colleague): ProjectShare => ({
  id: `share-${colleague.emailLocalPart}`,
  sharedWithUserId: colleague.kind === 'user' ? colleague.id : `user-${colleague.emailLocalPart}`,
  sharedWithDisplayName: colleague.displayName,
  sharedWithEmailLocalPart: colleague.emailLocalPart,
  signedIn: colleague.signedIn,
  createdAt: '2026-09-29T10:00:00Z',
})

function typeQuery(value: string): void {
  fireEvent.change(screen.getByPlaceholderText('Search by name or email'), { target: { value } })
}

async function findRow(name: string): Promise<HTMLElement> {
  const row = (await screen.findByText(name)).closest('li')
  if (!(row instanceof HTMLElement)) throw new Error(`no row for ${name}`)
  return row
}

beforeEach(() => {
  for (const fn of Object.values(h)) fn.mockReset()
  h.listProjectShares.mockResolvedValue([])
  h.searchColleagues.mockResolvedValue([])
  h.unshareProject.mockResolvedValue(undefined)
})
afterEach(cleanup)

describe('SharePanel — sharing with a colleague found only in the directory', () => {
  it('shows a directory hit with its local part and "Not signed in yet", and shares it by its directory id', async () => {
    h.searchColleagues.mockResolvedValue([priya])
    h.shareProject.mockResolvedValue(shareOf(priya))
    render(<SharePanelBody projectId="p1" />)

    typeQuery('pri')
    const row = await findRow('Priya Sharma')
    expect(within(row).getByText('priya.sharma')).toBeTruthy()
    expect(within(row).getByText('Not signed in yet')).toBeTruthy()

    fireEvent.click(within(row).getByRole('button', { name: 'Share' }))
    await waitFor(() => expect(h.shareProject).toHaveBeenCalledTimes(1))
    expect(h.shareProject).toHaveBeenCalledWith('p1', priya)
    expect(h.shareProject.mock.calls[0][1]).not.toHaveProperty('id')
  })

  it('shares a colleague who already has a user here by their user id, with no label', async () => {
    h.searchColleagues.mockResolvedValue([asha])
    h.shareProject.mockResolvedValue(shareOf(asha))
    render(<SharePanelBody projectId="p1" />)

    typeQuery('ash')
    const row = await findRow('Asha Rao')
    expect(within(row).getByText('asha.rao')).toBeTruthy()
    expect(within(row).queryByText('Not signed in yet')).toBeNull()

    fireEvent.click(within(row).getByRole('button', { name: 'Share' }))
    await waitFor(() => expect(h.shareProject).toHaveBeenCalledWith('p1', asha))
    expect(h.shareProject.mock.calls[0][1]).not.toHaveProperty('directoryId')
  })

  it('marks and removes only the directory hit being shared, leaving the other one clickable', async () => {
    // Directory hits have no user id, so any id-based bookkeeping would treat both as one row.
    let resolveFirst: (share: ProjectShare) => void = () => {}
    h.searchColleagues.mockResolvedValue([priya, priyanka])
    h.shareProject
      .mockImplementationOnce(
        () =>
          new Promise<ProjectShare>((resolve) => {
            resolveFirst = resolve
          }),
      )
      .mockResolvedValueOnce(shareOf(priyanka))
    render(<SharePanelBody projectId="p1" />)

    typeQuery('pri')
    const priyaRow = await findRow('Priya Sharma')
    const priyankaRow = await findRow('Priyanka Das')

    fireEvent.click(within(priyaRow).getByRole('button', { name: 'Share' }))
    await waitFor(() => expect(within(priyaRow).getByRole('button').getAttribute('aria-disabled')).toBe('true'))
    expect(within(priyaRow).queryByText('Share')).toBeNull()
    expect(within(priyankaRow).getByRole('button', { name: 'Share' }).getAttribute('aria-disabled')).toBe('false')

    resolveFirst(shareOf(priya))
    await waitFor(() => expect(screen.queryByText('priya.sharma')).toBeNull())
    const stillThere = await findRow('Priyanka Das')
    const priyankaShare = within(stillThere).getByRole('button', { name: 'Share' })
    expect(priyankaShare.getAttribute('aria-disabled')).toBe('false')

    fireEvent.click(priyankaShare)
    await waitFor(() => expect(h.shareProject).toHaveBeenCalledTimes(2))
    expect(h.shareProject).toHaveBeenLastCalledWith('p1', priyanka)
  })

  it('lists the new share with the label and announces the hint until the next search', async () => {
    h.searchColleagues.mockResolvedValueOnce([priya]).mockResolvedValueOnce([asha])
    h.shareProject.mockResolvedValue(shareOf(priya))
    h.listProjectShares.mockResolvedValueOnce([]).mockResolvedValue([shareOf(priya)])
    render(<SharePanelBody projectId="p1" />)
    await screen.findByText('Not shared with anyone yet.')

    typeQuery('pri')
    fireEvent.click(within(await findRow('Priya Sharma')).getByRole('button', { name: 'Share' }))

    const shareRow = (await screen.findByText('Can use')).closest('li')
    if (!(shareRow instanceof HTMLElement)) throw new Error('no share row')
    expect(within(shareRow).getByText('Priya Sharma')).toBeTruthy()
    expect(within(shareRow).getByText('Not signed in yet')).toBeTruthy()
    const status = screen.getByRole('status')
    await waitFor(() => expect(status.textContent).toBe(HINT))

    typeQuery('ash')
    await findRow('Asha Rao')
    expect(screen.getByRole('status').textContent).not.toContain(HINT)
    expect(screen.getByText('Can use')).toBeTruthy()
  })

  it('shows the server message when the directory is unreachable, and keeps the picker open and typeable', async () => {
    const message = "Couldn't look this person up right now. Try again in a moment."
    h.searchColleagues.mockResolvedValue([priya])
    h.shareProject.mockRejectedValue(new ApiError(message, 503))
    render(<SharePanelBody projectId="p1" />)

    typeQuery('pri')
    fireEvent.click(within(await findRow('Priya Sharma')).getByRole('button', { name: 'Share' }))

    expect((await screen.findByRole('alert')).textContent).toBe(message)
    const input = screen.getByPlaceholderText<HTMLInputElement>('Search by name or email')
    expect(input.value).toBe('pri')
    expect(within(await findRow('Priya Sharma')).getByRole('button', { name: 'Share' })).toBeTruthy()

    typeQuery('priy')
    expect(input.value).toBe('priy')
    await waitFor(() => expect(h.searchColleagues).toHaveBeenLastCalledWith('priy'))
  })
})

describe('SharePanel — a slow search for text the user has since changed', () => {
  function pendingSearch(): (found: Colleague[]) => void {
    let resolve: (found: Colleague[]) => void = () => {}
    h.searchColleagues.mockImplementationOnce(
      () =>
        new Promise<Colleague[]>((r) => {
          resolve = r
        }),
    )
    return (found) => resolve(found)
  }

  it('drops the late answer when newer text is still waiting on its own search', async () => {
    const answerPri = pendingSearch()
    h.searchColleagues.mockResolvedValueOnce([asha])
    render(<SharePanelBody projectId="p1" />)

    typeQuery('pri')
    await waitFor(() => expect(h.searchColleagues).toHaveBeenCalledWith('pri'))
    typeQuery('ash')
    await act(async () => answerPri([priya]))

    expect(screen.queryByText('Priya Sharma')).toBeNull()
    await findRow('Asha Rao')
    expect(screen.queryByText('Priya Sharma')).toBeNull()
  })

  it('drops the late answer once the box has been cleared', async () => {
    const answerPri = pendingSearch()
    render(<SharePanelBody projectId="p1" />)

    typeQuery('pri')
    await waitFor(() => expect(h.searchColleagues).toHaveBeenCalledWith('pri'))
    typeQuery('')
    await act(async () => answerPri([priya]))

    expect(screen.getByPlaceholderText<HTMLInputElement>('Search by name or email').value).toBe('')
    expect(screen.queryByText('Priya Sharma')).toBeNull()
    expect(h.searchColleagues).toHaveBeenCalledTimes(1)
  })
})
