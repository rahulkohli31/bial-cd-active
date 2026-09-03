/**
 * The Deletions tab (#176) — the reader `deleted_projects` did not have.
 *
 * What is worth pinning here is not the markup but the two things that would mislead an
 * administrator: an empty list that means the wrong thing, and a page-two failure that throws
 * away the rows already on screen.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

const h = vi.hoisted(() => ({ fetchDeletedProjects: vi.fn() }))
vi.mock('../../../utils/admin', () => ({ fetchDeletedProjects: h.fetchDeletedProjects }))

import DeletedProjectsPanel from '../DeletedProjectsPanel'

const row = (over = {}) => ({
  id: 'd1',
  projectId: 'p1',
  projectName: 'Visitor Gate Pass Tracker',
  ownerId: 'u1',
  ownerEmail: 'asha@bial.example',
  deletedBy: 'u1',
  deletedByName: 'Asha Rao',
  deletedAt: '2026-09-02T10:00:00Z',
  remark: 'Superseded by the new gate pass tool',
  chatsDeleted: 3,
  hadApp: true,
  hadDatabase: true,
  ...over,
})

const page = (deletions: unknown[], over = {}) => ({
  deletions,
  nextCursor: null,
  hasMore: false,
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  h.fetchDeletedProjects.mockResolvedValue(page([row()]))
})
afterEach(() => cleanup())

describe('what a deletion says', () => {
  it('shows the reason, who deleted it, and what went with it', async () => {
    render(<DeletedProjectsPanel />)

    expect(await screen.findByText('Visitor Gate Pass Tracker')).toBeTruthy()
    // The reason is the only part of the row a person actually wrote.
    expect(screen.getByText('Superseded by the new gate pass tool')).toBeTruthy()
    expect(screen.getByText('Asha Rao')).toBeTruthy()
    expect(screen.getByText(/3 chats · an app · a database/)).toBeTruthy()
  })

  it('reads as a sentence when the project had no children', async () => {
    // "0 chats, no app, no database" is technically complete and unreadable. The counts are
    // assembled, so a bare project says "nothing else".
    h.fetchDeletedProjects.mockResolvedValue(
      page([row({ chatsDeleted: 0, hadApp: false, hadDatabase: false })]),
    )
    render(<DeletedProjectsPanel />)

    expect(await screen.findByText(/Went with it: nothing else/)).toBeTruthy()
  })

  it('says "1 chat", not "1 chats"', async () => {
    h.fetchDeletedProjects.mockResolvedValue(page([row({ chatsDeleted: 1, hadApp: false, hadDatabase: false })]))
    render(<DeletedProjectsPanel />)

    expect(await screen.findByText(/Went with it: 1 chat$/)).toBeTruthy()
  })
})

describe('the empty state means the right thing', () => {
  it('distinguishes "nothing deleted yet" from "no matches"', async () => {
    // THE FAILURE THIS GUARDS. `q` runs 300ms ahead of the rows, so deciding the empty state
    // from it tells an admin mid-keystroke that no project has ever been deleted. The panel
    // reads `appliedQuery` — what the rows on screen actually answer.
    h.fetchDeletedProjects.mockResolvedValue(page([]))
    render(<DeletedProjectsPanel />)

    expect(await screen.findByText(/No projects have been deleted yet/i)).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Search deletions'), { target: { value: 'gate pass' } })

    await waitFor(() => expect(screen.getByText(/No deletions match “gate pass”/)).toBeTruthy())
  })
})

describe('paging and failure', () => {
  it('loads the next page and keeps what is already on screen', async () => {
    h.fetchDeletedProjects
      .mockResolvedValueOnce(page([row({ id: 'd1', projectName: 'First' })], { nextCursor: 'c1', hasMore: true }))
      .mockResolvedValueOnce(page([row({ id: 'd2', projectName: 'Second' })]))
    render(<DeletedProjectsPanel />)

    await screen.findByText('First')
    fireEvent.click(screen.getByRole('button', { name: /load more/i }))

    await screen.findByText('Second')
    expect(screen.getByText('First')).toBeTruthy() // appended, not replaced
  })

  it('a LATER page failing does not clear the rows already read', async () => {
    // The same rule the projects list follows: an administrator reading a list must not lose
    // it because the next page failed. The message goes underneath instead.
    h.fetchDeletedProjects
      .mockResolvedValueOnce(page([row({ projectName: 'First' })], { nextCursor: 'c1', hasMore: true }))
      .mockRejectedValueOnce(new Error('Failed to load deletions'))
    render(<DeletedProjectsPanel />)

    await screen.findByText('First')
    fireEvent.click(screen.getByRole('button', { name: /load more/i }))

    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/Failed to load/))
    expect(screen.getByText('First')).toBeTruthy()
  })

  it('a FIRST page failing offers a retry, since there is nothing to preserve', async () => {
    h.fetchDeletedProjects.mockRejectedValueOnce(new Error('Failed to load deletions'))
    render(<DeletedProjectsPanel />)

    const retry = await screen.findByRole('button', { name: /try again/i })
    h.fetchDeletedProjects.mockResolvedValue(page([row({ projectName: 'Recovered' })]))
    fireEvent.click(retry)

    expect(await screen.findByText('Recovered')).toBeTruthy()
  })
})
