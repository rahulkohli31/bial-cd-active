/**
 * The Deletions tab (#176) — the reader `deleted_projects` did not have.
 *
 * What is worth pinning here is not the markup but the two things that would mislead an
 * administrator: an empty list that means the wrong thing, and a page-two failure that throws
 * away the rows already on screen.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

const h = vi.hoisted(() => ({
  fetchDeletedProjects: vi.fn(),
  fetchDeletionsAudit: vi.fn(),
}))
vi.mock('../../../utils/admin', () => ({
  fetchDeletedProjects: h.fetchDeletedProjects,
  fetchDeletionsAudit: h.fetchDeletionsAudit,
}))

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
  h.fetchDeletionsAudit.mockResolvedValue([])
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
  it('still says "nothing deleted yet" DURING the debounce, and only then "no matches"', async () => {
    // THE FAILURE THIS GUARDS. `q` runs 300ms ahead of the rows, so deciding the empty state
    // from it tells an admin mid-keystroke that no project has ever been deleted. The panel
    // reads `appliedQuery` — what the rows on screen actually answer.
    //
    // WHY THE FIRST ASSERTION IS SYNCHRONOUS. The previous version of this test read only
    // through `waitFor`, which polls until AFTER the debounce has applied the query — by which
    // point `q` and `appliedQuery` are equal and both spellings agree. Swapping `appliedQuery`
    // for `q` left it green, so the test named for this invariant could not fail on it. The
    // window where the two DISAGREE is the whole subject, so it has to be asserted inside that
    // window: immediately after the change event, before the 300ms debounce fires.
    //
    // Mutation receipt: change `appliedQuery` to `q` in the condition at the empty state and
    // the synchronous expectation below goes red, while the `waitFor` half still passes.
    h.fetchDeletedProjects.mockResolvedValue(page([]))
    render(<DeletedProjectsPanel />)

    expect(await screen.findByText(/No projects have been deleted yet/i)).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Search deletions'), { target: { value: 'gate pass' } })

    // Mid-flight: the rows on screen still answer the EMPTY query, so the message must too.
    expect(screen.getByText(/No projects have been deleted yet/i)).toBeTruthy()
    expect(screen.queryByText(/No deletions match/)).toBeNull()

    // And once the debounce lands and the rows really do answer the new query, it changes.
    await waitFor(() => expect(screen.getByText(/No deletions match “gate pass”/)).toBeTruthy())
  })
})

describe('a failed search does not trap the admin', () => {
  it('keeps the search box rendered and editable when a SEARCH fails', async () => {
    // THE TRAP. `useKeysetList` clears `items` inside the debounce before fetching, so a failing
    // search always lands on `items.length === 0`. The first-page error card does not render the
    // search input — so the admin was left with one "Try again" that re-issued the SAME failing
    // query for ever, with no rendered input to edit or clear it in. The only escape was leaving
    // the tab.
    //
    // Mutation receipt: drop `&& appliedQuery === null` from the early return and this fails —
    // the input disappears from the document.
    h.fetchDeletedProjects.mockResolvedValueOnce(page([row({ projectName: 'First' })]))
    render(<DeletedProjectsPanel />)
    await screen.findByText('First')

    h.fetchDeletedProjects.mockRejectedValue(new Error('Failed to load deletions'))
    fireEvent.change(screen.getByLabelText('Search deletions'), { target: { value: 'boom' } })

    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/Failed to load/))

    // The way out: the box is still there, still holds what was typed, and still accepts edits.
    const input = screen.getByLabelText('Search deletions') as HTMLInputElement
    expect(input.value).toBe('boom')
    fireEvent.change(input, { target: { value: '' } })
    expect((screen.getByLabelText('Search deletions') as HTMLInputElement).value).toBe('')
  })

  it('still takes the whole panel when the FIRST load fails, since there is nothing to keep', async () => {
    // The other side of the same gate: before anything has ever landed there are no rows to
    // preserve and no search to lose, so the full-panel card is right.
    h.fetchDeletedProjects.mockRejectedValueOnce(new Error('Failed to load deletions'))
    render(<DeletedProjectsPanel />)

    expect(await screen.findByRole('button', { name: /try again/i })).toBeTruthy()
    expect(screen.queryByLabelText('Search deletions')).toBeNull()
  })

  it('caps the search at the length the server accepts', async () => {
    // The most reachable route into the trap above was a long paste, which 422s. Capping the
    // input means it cannot be typed rather than being answered with an error to recover from.
    render(<DeletedProjectsPanel />)
    await screen.findByText('Visitor Gate Pass Tracker')

    expect((screen.getByLabelText('Search deletions') as HTMLInputElement).maxLength).toBe(200)
  })
})

describe('the pager cannot send a stale cursor', () => {
  it('disables "Load more" while a typed search has not landed yet', async () => {
    // `loadMore` reads cursorRef/qRef directly with no awareness of a pending debounce, so a
    // click inside the 300ms window sends the OLD filter's cursor under the NEW query text.
    // The screen self-heals when the debounce lands; the audit row it wrote does not.
    //
    // Mutation receipt: drop `|| appliedQuery !== q` from the button and this goes red.
    h.fetchDeletedProjects.mockResolvedValue(
      page([row({ projectName: 'First' })], { nextCursor: 'c1', hasMore: true }),
    )
    render(<DeletedProjectsPanel />)
    await screen.findByText('First')

    const more = screen.getByRole('button', { name: /load more/i }) as HTMLButtonElement
    expect(more.disabled).toBe(false)

    fireEvent.change(screen.getByLabelText('Search deletions'), { target: { value: 'gate' } })

    expect((screen.getByRole('button', { name: /load more/i }) as HTMLButtonElement).disabled).toBe(
      true,
    )
  })
})

describe('the reason survives being displayed', () => {
  it('wraps a long unbroken token and preserves the writer\'s line breaks', async () => {
    // The one field this whole feature exists to show. `count_words` splits on whitespace, so a
    // single ~1990-character token passes both the 50-word bound and the 2000-char backstop —
    // and the card it lands in is inside an `overflow-hidden` wrapper, so without these classes
    // it clipped away with no scrollbar, no title, and no signal anything was missing.
    h.fetchDeletedProjects.mockResolvedValue(page([row({ remark: 'a'.repeat(400) })]))
    render(<DeletedProjectsPanel />)

    const quote = await screen.findByText('a'.repeat(400))
    expect(quote.className).toContain('break-words')
    expect(quote.className).toContain('whitespace-pre-wrap')
  })
})

describe('who has read this log', () => {
  it('is collapsed until asked, then names the reader and what they did', async () => {
    // The audit row is the control this screen's cross-owner reading is justified by, and until
    // this strip existed nothing in the product could retrieve one.
    h.fetchDeletionsAudit.mockResolvedValue([
      {
        id: 'a1',
        action: 'admin:deletions:list',
        username: 'admin@bial.com',
        createdAt: '2026-09-02T10:00:00Z',
        detail: { filtered: true, count: 2 },
        count: 2,
      },
    ])
    render(<DeletedProjectsPanel />)
    await screen.findByText('Visitor Gate Pass Tracker')

    // Collapsed: not fetched, not shown.
    expect(h.fetchDeletionsAudit).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: /who has read this log/i }))

    expect(await screen.findByText('admin@bial.com')).toBeTruthy()
    expect(screen.getByText(/searched/)).toBeTruthy()
    expect(screen.getByText(/2 shown/)).toBeTruthy()
  })

  it('never shows the search term itself, only that a search happened', async () => {
    // `audit.py`'s contract is "never the record CONTENTS", so the term is stored as a digest.
    // This screen must not imply otherwise by rendering something that looks like the query.
    h.fetchDeletionsAudit.mockResolvedValue([
      {
        id: 'a1',
        action: 'admin:deletions:list',
        username: 'admin@bial.com',
        createdAt: '2026-09-02T10:00:00Z',
        detail: { filtered: false, count: 0 },
        count: 0,
      },
    ])
    render(<DeletedProjectsPanel />)
    await screen.findByText('Visitor Gate Pass Tracker')
    fireEvent.click(screen.getByRole('button', { name: /who has read this log/i }))

    expect(await screen.findByText(/read the whole log/)).toBeTruthy()
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
