import { StrictMode } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent, within } from '@testing-library/react'
import UsersLimitsPanel from '../UsersLimitsPanel.jsx'
import { ApiError } from '../../../utils/apiError'

// Mock the data layer; the real useKeysetList hook drives pagination/search so the
// panel's cursor + reset behavior is exercised end to end (not stubbed away).
const h = vi.hoisted(() => ({
  fetchUsers: vi.fn(),
  updateUserLimits: vi.fn(),
  deactivateUser: vi.fn(),
  reactivateUser: vi.fn(),
  resetUserUsage: vi.fn(),
}))
vi.mock('../../../utils/admin', () => h)

const DEFAULTS = { dailyTokenLimit: 100000, contextSoftLimit: 150000, contextHardLimit: 200000 }

const user = (over = {}) => ({
  userId: over.userId || 'u1',
  email: over.email || 'a@x.com',
  displayName: 'displayName' in over ? over.displayName : 'Alice',
  role: over.role || 'citizen',
  suspendedAt: over.suspendedAt ?? null,
  usageToday: over.usageToday ?? 0,
  limits: over.limits || {},
  effectiveLimits: over.effectiveLimits || { ...DEFAULTS },
})

const pageOf = (users, over = {}) => ({
  defaults: DEFAULTS,
  users,
  nextCursor: over.nextCursor ?? null,
  hasMore: over.hasMore ?? false,
})

afterEach(cleanup)
beforeEach(() => {
  for (const fn of Object.values(h)) fn.mockReset()
  h.fetchUsers.mockResolvedValue(pageOf([user()]))
  // jsdom doesn't implement these; Radix's <Select> (role/status filters) calls them
  // on open/scroll (suite-wide convention, see BuilderPage/ChatPage test files).
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn().mockReturnValue(false)
  Element.prototype.releasePointerCapture = vi.fn()
  Element.prototype.setPointerCapture = vi.fn()
})

/** Opens a <Select> filter (role-filter / status-filter) and picks the option with this text. */
async function pickSelect(triggerTestId, optionText) {
  fireEvent.click(screen.getByTestId(triggerTestId))
  fireEvent.click(await screen.findByRole('option', { name: optionText }))
}

describe('UsersLimitsPanel — roster + suspension', () => {
  it('renders email, displayName, role, usageToday, effective limits, and a suspension badge', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ usageToday: 4200 })]))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    expect(screen.getByText('a@x.com')).toBeTruthy()
    expect(screen.getByText('Citizen')).toBeTruthy()
    expect(screen.getByText('4,200')).toBeTruthy() // usageToday
    expect(screen.getByText('100,000')).toBeTruthy() // effective daily limit
    expect(within(screen.getByTestId('row-a@x.com')).getByText('Active')).toBeTruthy()
  })

  it('renders Active for suspendedAt=null and Suspended for a timestamp', async () => {
    h.fetchUsers.mockResolvedValue(
      pageOf([
        user({ userId: 'u1', email: 'a@x.com', suspendedAt: null }),
        user({ userId: 'u2', email: 'b@x.com', displayName: 'Bob', suspendedAt: '2026-07-01T00:00:00Z' }),
      ]),
    )
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    expect(within(screen.getByTestId('row-a@x.com')).getByText('Active')).toBeTruthy()
    expect(within(screen.getByTestId('row-b@x.com')).getByText('Suspended')).toBeTruthy()
  })

  it('auto-loads the next keyset page in the background (no click) and keeps prior rows', async () => {
    h.fetchUsers
      .mockResolvedValueOnce(pageOf([user({ userId: 'u1', email: 'a@x.com', displayName: 'Alice' })], { nextCursor: 'c1', hasMore: true }))
      .mockResolvedValueOnce(pageOf([user({ userId: 'u2', email: 'b@x.com', displayName: 'Bob' })], { hasMore: false }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    await screen.findByText('Bob') // no click — the panel chains loadMore() itself
    expect(screen.getByText('Alice')).toBeTruthy() // page 1 retained, not replaced
    expect(h.fetchUsers).toHaveBeenNthCalledWith(2, expect.objectContaining({ cursor: 'c1' }))
    expect(screen.queryByTestId('load-more-users')).toBeNull() // no manual button in this UI anymore
  })

  it('a roster of 26 users auto-loads the second keyset page (regression guard for silent truncation)', async () => {
    const first25 = Array.from({ length: 25 }, (_, i) =>
      user({ userId: `u${i}`, email: `u${i}@x.com`, displayName: `U${i}` }),
    )
    h.fetchUsers
      .mockResolvedValueOnce(pageOf(first25, { nextCursor: 'c1', hasMore: true }))
      .mockResolvedValueOnce(pageOf([user({ userId: 'u25', email: 'u25@x.com', displayName: 'U25' })], { hasMore: false }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('U0')
    await waitFor(() => expect(h.fetchUsers).toHaveBeenNthCalledWith(2, expect.objectContaining({ cursor: 'c1' })))
    await waitFor(() => expect(screen.getByText(/Page 1 of 2/)).toBeTruthy())
    fireEvent.click(screen.getByTestId('users-next-page'))
    expect(screen.getByTestId('row-u25@x.com')).toBeTruthy() // the 26th user, silently truncated in the old bug
  })

  it('a failed background page shows an error with the rows already loaded intact, and Retry resumes it', async () => {
    h.fetchUsers
      .mockResolvedValueOnce(pageOf([user({ userId: 'u1', email: 'a@x.com', displayName: 'Alice' })], { nextCursor: 'c1', hasMore: true }))
      .mockRejectedValueOnce(new ApiError('Network hiccup', 500))
      .mockResolvedValueOnce(pageOf([user({ userId: 'u2', email: 'b@x.com', displayName: 'Bob' })], { hasMore: false }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    const banner = await screen.findByTestId('loadmore-error')
    expect(within(banner).getByText(/network hiccup/i)).toBeTruthy()
    expect(screen.getByText('Alice')).toBeTruthy() // rows kept on failure
    fireEvent.click(within(banner).getByText('Retry'))
    await screen.findByText('Bob')
    expect(screen.queryByTestId('loadmore-error')).toBeNull()
  })

  it('search sends q and resets the cursor to null', async () => {
    // hasMore: false — this test only cares about the search request shape, not
    // pagination continuation; a perpetual hasMore: true here would auto-chain
    // loadMore() forever against the panel's own background bulk-load effect.
    h.fetchUsers.mockResolvedValue(pageOf([user()], { hasMore: false }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    fireEvent.change(screen.getByTestId('users-search'), { target: { value: 'ana' } })
    await waitFor(() =>
      expect(h.fetchUsers).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'ana', cursor: null })),
    )
  })

  it('deactivate flips the row to Suspended', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ suspendedAt: null })]))
    h.deactivateUser.mockResolvedValue({ userId: 'u1', suspendedAt: '2026-07-10T09:00:00Z' })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('deactivate-a@x.com'))
    await within(screen.getByTestId('row-a@x.com')).findByText('Suspended')
    expect(h.deactivateUser).toHaveBeenCalledWith('u1')
  })

  it('reactivate clears the row back to Active', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ suspendedAt: '2026-07-01T00:00:00Z' })]))
    h.reactivateUser.mockResolvedValue({ userId: 'u1', suspendedAt: null })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    const row = screen.getByTestId('row-a@x.com')
    expect(within(row).getByText('Suspended')).toBeTruthy()
    fireEvent.click(screen.getByTestId('reactivate-a@x.com'))
    await within(row).findByText('Active')
    expect(h.reactivateUser).toHaveBeenCalledWith('u1')
  })

  it('a super-admin row offers no deactivate action (limits still editable)', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ role: 'super_admin', email: 'admin@x.com', displayName: 'Admin' })]))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Admin')
    expect(screen.queryByTestId('deactivate-admin@x.com')).toBeNull()
    expect(screen.queryByTestId('reactivate-admin@x.com')).toBeNull()
    expect(screen.getByTestId('edit-admin@x.com')).toBeTruthy()
  })

  it("the caller's own super-admin row cannot be self-suspended (AE6)", async () => {
    // Only a super-admin can load this panel, and they appear in their own roster;
    // the super-admin guard therefore also covers self-suspension.
    h.fetchUsers.mockResolvedValue(
      pageOf([
        user({ userId: 'me', role: 'super_admin', email: 'me@x.com', displayName: 'Me' }),
        user({ userId: 'u2', role: 'citizen', email: 'c@x.com', displayName: 'Cit' }),
      ]),
    )
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Me')
    expect(screen.queryByTestId('deactivate-me@x.com')).toBeNull()
    expect(screen.getByTestId('deactivate-c@x.com')).toBeTruthy() // a citizen is still actionable
  })

  it('deactivate → 403 reverts the optimistic flip and shows the super-admin guard message', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ suspendedAt: null })]))
    h.deactivateUser.mockRejectedValue(new ApiError('A super-admin cannot be suspended.', 403))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('deactivate-a@x.com'))
    const banner = await screen.findByTestId('action-error')
    expect(within(banner).getByText(/super-admin cannot be suspended/i)).toBeTruthy()
    // reverted: the row is Active again and the deactivate action is back
    const row = screen.getByTestId('row-a@x.com')
    await waitFor(() => expect(within(row).getByText('Active')).toBeTruthy())
    expect(screen.getByTestId('deactivate-a@x.com')).toBeTruthy()
  })

  it('deactivate → 409 (already suspended) reconciles to Suspended with no error toast', async () => {
    const onToast = vi.fn()
    h.fetchUsers.mockResolvedValue(pageOf([user({ suspendedAt: null })]))
    h.deactivateUser.mockRejectedValue(new ApiError('User is already suspended.', 409))
    render(<UsersLimitsPanel onToast={onToast} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('deactivate-a@x.com'))
    await within(screen.getByTestId('row-a@x.com')).findByText('Suspended')
    expect(screen.queryByTestId('action-error')).toBeNull()
    expect(onToast).not.toHaveBeenCalled()
  })

  it('reactivate → 409 (not suspended) reconciles to Active with no error toast', async () => {
    const onToast = vi.fn()
    h.fetchUsers.mockResolvedValue(pageOf([user({ suspendedAt: '2026-07-01T00:00:00Z' })]))
    h.reactivateUser.mockRejectedValue(new ApiError('User is not suspended.', 409))
    render(<UsersLimitsPanel onToast={onToast} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('reactivate-a@x.com'))
    await within(screen.getByTestId('row-a@x.com')).findByText('Active')
    expect(screen.queryByTestId('action-error')).toBeNull()
    expect(onToast).not.toHaveBeenCalled()
  })

  // Pins the e instanceof ApiError && e.status === 404 arm (PR #93 review finding 5):
  // the duck-typed e?.status === 404 this replaced was untested on either side, so the
  // narrowing to ApiError was "equivalent today" by inspection only, not by a test.
  it('deactivate → 404 (user gone) drops the row silently, with no error toast', async () => {
    const onToast = vi.fn()
    h.fetchUsers.mockResolvedValue(pageOf([user({ suspendedAt: null })]))
    h.deactivateUser.mockRejectedValue(new ApiError('User not found.', 404))
    render(<UsersLimitsPanel onToast={onToast} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('deactivate-a@x.com'))
    await waitFor(() => expect(screen.queryByTestId('row-a@x.com')).toBeNull())
    expect(screen.queryByTestId('action-error')).toBeNull()
    expect(onToast).not.toHaveBeenCalled()
  })

  it('reactivate → 404 (user gone) drops the row silently, with no error toast', async () => {
    const onToast = vi.fn()
    h.fetchUsers.mockResolvedValue(pageOf([user({ suspendedAt: '2026-07-01T00:00:00Z' })]))
    h.reactivateUser.mockRejectedValue(new ApiError('User not found.', 404))
    render(<UsersLimitsPanel onToast={onToast} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('reactivate-a@x.com'))
    await waitFor(() => expect(screen.queryByTestId('row-a@x.com')).toBeNull())
    expect(screen.queryByTestId('action-error')).toBeNull()
    expect(onToast).not.toHaveBeenCalled()
  })

  it('a citizen who reaches the panel sees the 403 gate message — not blank, not a suspension redirect', async () => {
    // The suspension interceptor (U3) lives in authFetch and never fires for this body;
    // fetchUsers simply throws the gate message, and the panel must show it.
    h.fetchUsers.mockRejectedValue(new ApiError('Super-admin privileges required.', 403))
    render(<UsersLimitsPanel onToast={() => {}} />)
    const msg = await screen.findByTestId('users-load-error')
    expect(msg.textContent).toContain('Super-admin privileges required.')
  })

  it('sets a daily token limit to 200000 and then clears it — PATCH null clears the override (AE4)', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ limits: {}, effectiveLimits: { ...DEFAULTS } })]))
    h.updateUserLimits
      .mockResolvedValueOnce({
        userId: 'u1',
        limits: { dailyTokenLimit: 200000, contextSoftLimit: null, contextHardLimit: null },
        effectiveLimits: { ...DEFAULTS, dailyTokenLimit: 200000 },
      })
      .mockResolvedValueOnce({
        userId: 'u1',
        limits: { dailyTokenLimit: null, contextSoftLimit: null, contextHardLimit: null },
        effectiveLimits: { ...DEFAULTS },
      })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')

    // set daily = 200000
    fireEvent.click(screen.getByTestId('edit-a@x.com'))
    fireEvent.click(screen.getByTestId('usedefault-daily')) // uncheck → enable the input
    fireEvent.change(screen.getByTestId('limit-daily'), { target: { value: '200000' } })
    fireEvent.click(screen.getByTestId('save-limits'))
    await waitFor(() =>
      expect(h.updateUserLimits).toHaveBeenNthCalledWith(1, 'u1', {
        dailyTokenLimit: 200000,
        contextSoftLimit: null,
        contextHardLimit: null,
      }),
    )
    await waitFor(() => expect(screen.queryByTestId('save-limits')).toBeNull()) // modal closed

    // reopen and clear daily back to default
    fireEvent.click(screen.getByTestId('edit-a@x.com'))
    fireEvent.click(screen.getByTestId('usedefault-daily')) // re-check → reset override to default
    fireEvent.click(screen.getByTestId('save-limits'))
    await waitFor(() =>
      expect(h.updateUserLimits).toHaveBeenNthCalledWith(2, 'u1', {
        dailyTokenLimit: null,
        contextSoftLimit: null,
        contextHardLimit: null,
      }),
    )
  })

  it('shows "0 (default)" in the placeholder when the server envelope omits a default (#106)', async () => {
    // dailyTokenLimit is missing from `defaults` entirely (not null) — the same
    // "genuinely absent" case Partial<LimitFields> exists to represent.
    const { dailyTokenLimit: _omit, ...defaultsMissingDaily } = DEFAULTS
    h.fetchUsers.mockResolvedValue({ ...pageOf([user({ limits: {} })]), defaults: defaultsMissingDaily })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')

    fireEvent.click(screen.getByTestId('edit-a@x.com'))
    // "Use default" starts checked (limits.dailyTokenLimit is unset), so the input is
    // disabled and its placeholder is what's under test.
    expect(screen.getByTestId('limit-daily').placeholder).toBe('0 (default)')
  })
})

describe('UsersLimitsPanel — sort, filter, pagination', () => {
  it('clicking a numeric column header sorts rows ascending, then descending', async () => {
    h.fetchUsers.mockResolvedValue(
      pageOf([
        user({ userId: 'u1', email: 'a@x.com', displayName: 'Alice', effectiveLimits: { ...DEFAULTS, dailyTokenLimit: 300000 } }),
        user({ userId: 'u2', email: 'b@x.com', displayName: 'Bob', effectiveLimits: { ...DEFAULTS, dailyTokenLimit: 100000 } }),
        user({ userId: 'u3', email: 'c@x.com', displayName: 'Cara', effectiveLimits: { ...DEFAULTS, dailyTokenLimit: 200000 } }),
      ]),
    )
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')

    const order = () => screen.getAllByTestId(/^row-/).map((el) => el.getAttribute('data-testid'))
    fireEvent.click(screen.getByText('Daily tokens'))
    await waitFor(() => expect(order()).toEqual(['row-b@x.com', 'row-c@x.com', 'row-a@x.com']))
    fireEvent.click(screen.getByText('Daily tokens'))
    await waitFor(() => expect(order()).toEqual(['row-a@x.com', 'row-c@x.com', 'row-b@x.com']))
  })

  it('the Role filter narrows the roster to the selected role', async () => {
    h.fetchUsers.mockResolvedValue(
      pageOf([
        user({ userId: 'u1', email: 'a@x.com', displayName: 'Alice', role: 'citizen' }),
        user({ userId: 'u2', email: 'admin@x.com', displayName: 'Admin', role: 'super_admin' }),
      ]),
    )
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')

    await pickSelect('role-filter', 'Super admin')
    await waitFor(() => expect(screen.queryByTestId('row-a@x.com')).toBeNull())
    expect(screen.getByTestId('row-admin@x.com')).toBeTruthy()
    expect(screen.getByTestId('noguard-admin@x.com')).toBeTruthy()
  })

  it('the Status filter narrows the roster to the selected status', async () => {
    h.fetchUsers.mockResolvedValue(
      pageOf([
        user({ userId: 'u1', email: 'a@x.com', displayName: 'Alice', suspendedAt: null }),
        user({ userId: 'u2', email: 'b@x.com', displayName: 'Bob', suspendedAt: '2026-07-01T00:00:00Z' }),
      ]),
    )
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')

    await pickSelect('status-filter', 'Suspended')
    await waitFor(() => expect(screen.queryByTestId('row-a@x.com')).toBeNull())
    expect(screen.getByTestId('row-b@x.com')).toBeTruthy()
  })

  it('a row deactivated while the Status filter is "Active" drops out of view immediately', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ suspendedAt: null })]))
    h.deactivateUser.mockResolvedValue({ userId: 'u1', suspendedAt: '2026-07-10T09:00:00Z' })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')

    await pickSelect('status-filter', 'Active')
    expect(screen.getByTestId('row-a@x.com')).toBeTruthy()
    fireEvent.click(screen.getByTestId('deactivate-a@x.com'))
    await waitFor(() => expect(screen.queryByTestId('row-a@x.com')).toBeNull())
  })

  it('paginates 30 loaded users at 25/page with working Prev/Next', async () => {
    const thirty = Array.from({ length: 30 }, (_, i) => user({ userId: `u${i}`, email: `u${i}@x.com`, displayName: `U${i}` }))
    h.fetchUsers.mockResolvedValue(pageOf(thirty, { hasMore: false }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('U0')

    expect(screen.getAllByTestId(/^row-/)).toHaveLength(25)
    expect(screen.getByText(/Page 1 of 2/)).toBeTruthy()
    expect(screen.getByTestId('users-prev-page').disabled).toBe(true)
    expect(screen.getByTestId('users-next-page').disabled).toBe(false)

    fireEvent.click(screen.getByTestId('users-next-page'))
    await waitFor(() => expect(screen.getAllByTestId(/^row-/)).toHaveLength(5))
    expect(screen.getByText(/Page 2 of 2/)).toBeTruthy()
    expect(screen.getByTestId('users-next-page').disabled).toBe(true)
  })

  it('applying a filter while on page 2 returns to page 1 instead of stranding an empty page', async () => {
    const thirty = Array.from({ length: 30 }, (_, i) => user({ userId: `u${i}`, email: `u${i}@x.com`, displayName: `U${i}`, role: 'citizen' }))
    h.fetchUsers.mockResolvedValue(pageOf(thirty, { hasMore: false }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('U0')

    fireEvent.click(screen.getByTestId('users-next-page'))
    await waitFor(() => expect(screen.getByText(/Page 2 of 2/)).toBeTruthy())

    await pickSelect('role-filter', 'Citizen')
    await waitFor(() => expect(screen.getByText(/Page 1 of/)).toBeTruthy())
    expect(screen.getAllByTestId(/^row-/)).toHaveLength(25)
  })

  it('a new search resets the view back to page 1', async () => {
    const thirty = Array.from({ length: 30 }, (_, i) => user({ userId: `u${i}`, email: `u${i}@x.com`, displayName: `U${i}` }))
    h.fetchUsers.mockResolvedValueOnce(pageOf(thirty, { hasMore: false }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('U0')

    fireEvent.click(screen.getByTestId('users-next-page'))
    await waitFor(() => expect(screen.getByText(/Page 2 of 2/)).toBeTruthy())

    h.fetchUsers.mockResolvedValueOnce(pageOf([user({ userId: 'z1', email: 'z@x.com', displayName: 'Zara' })], { hasMore: false }))
    fireEvent.change(screen.getByTestId('users-search'), { target: { value: 'zara' } })
    await screen.findByText('Zara')
    expect(screen.getByText(/Page 1 of 1/)).toBeTruthy()
  })
})

describe('UsersLimitsPanel — review-fix regressions', () => {
  it('deactivating a user on page 2 does not bounce the view back to page 1', async () => {
    // autoResetPageIndex defaults ON in TanStack Table; mergedUsers' identity changes
    // on every optimistic update too, not just a real sort/filter change, so the
    // default would silently return the admin to page 1 on a plain Deactivate click.
    const thirty = Array.from({ length: 30 }, (_, i) => user({ userId: `u${i}`, email: `u${i}@x.com`, displayName: `U${i}` }))
    h.fetchUsers.mockResolvedValue(pageOf(thirty, { hasMore: false }))
    h.deactivateUser.mockResolvedValue({ userId: 'u25', suspendedAt: '2026-07-10T09:00:00Z' })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('U0')

    fireEvent.click(screen.getByTestId('users-next-page'))
    await waitFor(() => expect(screen.getByText(/Page 2 of 2/)).toBeTruthy())

    fireEvent.click(screen.getByTestId('deactivate-u25@x.com')) // u25 lives on page 2
    await waitFor(() => expect(h.deactivateUser).toHaveBeenCalledWith('u25'))
    expect(screen.getByText(/Page 2 of 2/)).toBeTruthy() // still page 2, not bounced to page 1
  })

  it('does not append a stale-cursor page once the search query has moved on (debounce race guard)', async () => {
    // qRef updates synchronously on every keystroke, but appliedQuery (and the
    // hook's own cursor) only catches up once a fetch for that query actually lands.
    // A background page landing in between must not feed its stale cursor into a
    // loadMore() call carrying the NEW query text.
    let resolveSecondPage
    h.fetchUsers
      .mockResolvedValueOnce(pageOf([user({ userId: 'u1', email: 'a@x.com', displayName: 'Alice' })], { nextCursor: 'c1', hasMore: true }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveSecondPage = resolve }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')

    // The auto-chain's 2nd call (cursor: 'c1', q: '') is now in flight when the user types.
    fireEvent.change(screen.getByTestId('users-search'), { target: { value: 'ana' } })

    resolveSecondPage(pageOf([user({ userId: 'u2', email: 'b@x.com', displayName: 'Bob' })], { nextCursor: 'c2', hasMore: true }))
    await screen.findByText('Bob')

    // A 3rd call here would be the auto-chain firing loadMore() with the stale
    // cursor 'c2' under the NEW query text, ahead of the debounced search itself.
    await new Promise((r) => setTimeout(r, 50))
    expect(h.fetchUsers).toHaveBeenCalledTimes(2)

    h.fetchUsers.mockResolvedValueOnce(pageOf([], { hasMore: false }))
    await waitFor(() =>
      expect(h.fetchUsers).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'ana', cursor: null })),
    )
  })

  it('a row missing effectiveLimits renders 0, not the literal string "NaN"', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ effectiveLimits: {} })]))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    expect(screen.queryByText('NaN')).toBeNull()
    expect(screen.getAllByText('0').length).toBeGreaterThan(0)
  })

  it('a suspended super-admin shows Reactivate, not stranded behind "Protected"', async () => {
    // role is derived at read time from the env allowlist (ADR-0005) — a suspended
    // user who later lands on that allowlist is reachable with no 403 bypass, and
    // the server's reactivate_user has no super-admin guard.
    h.fetchUsers.mockResolvedValue(
      pageOf([user({ role: 'super_admin', email: 'admin@x.com', displayName: 'Admin', suspendedAt: '2026-07-01T00:00:00Z' })]),
    )
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Admin')
    expect(screen.queryByTestId('noguard-admin@x.com')).toBeNull()
    expect(screen.getByTestId('reactivate-admin@x.com')).toBeTruthy()
  })

  it('a stopped background chain shows a partial-data warning above the table, not just quiet text below it', async () => {
    h.fetchUsers
      .mockResolvedValueOnce(pageOf([user()], { nextCursor: 'c1', hasMore: true }))
      .mockRejectedValueOnce(new ApiError('Network hiccup', 500))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    const banner = await screen.findByTestId('loadmore-error')
    expect(within(banner).getByText(/Only 1 users loaded/)).toBeTruthy()
    expect(within(banner).getByText(/network hiccup/i)).toBeTruthy()
  })

  it('clicking Retry clears the partial-data banner immediately and the pager shows the in-flight signal instead', async () => {
    let resolveRetry
    h.fetchUsers
      .mockResolvedValueOnce(pageOf([user()], { nextCursor: 'c1', hasMore: true }))
      .mockRejectedValueOnce(new ApiError('Network hiccup', 500))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveRetry = resolve }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    const banner = await screen.findByTestId('loadmore-error')

    fireEvent.click(within(banner).getByText('Retry'))
    // runFetch clears `error` (and so isPartial) in the same render pass loading
    // flips true — the banner is gone immediately, not left dangling mid-retry.
    expect(screen.queryByTestId('loadmore-error')).toBeNull()
    expect(screen.getByText('Loading more users…')).toBeTruthy()

    resolveRetry(pageOf([user({ userId: 'u2', email: 'b@x.com', displayName: 'Bob' })], { hasMore: false }))
    await screen.findByText('Bob')
    expect(screen.queryByTestId('loadmore-error')).toBeNull()
  })

  it('exposes aria-sort and a stable per-column testid on sortable headers', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user()]))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    const dailyHeader = screen.getByTestId('sort-dailyTokenLimit')
    expect(dailyHeader.closest('th').getAttribute('aria-sort')).toBe('none')
    fireEvent.click(dailyHeader)
    expect(dailyHeader.closest('th').getAttribute('aria-sort')).toBe('ascending')
  })
})

describe('UsersLimitsPanel — reset usage', () => {
  it('resets a user\'s "Used today" to 0 optimistically and calls resetUserUsage', async () => {
    const onToast = vi.fn()
    h.fetchUsers.mockResolvedValue(pageOf([user({ usageToday: 4200 })]))
    h.resetUserUsage.mockResolvedValue({ userId: 'u1', usageToday: 0 })
    render(<UsersLimitsPanel onToast={onToast} />)
    await screen.findByText('4,200')

    fireEvent.click(screen.getByTestId('reset-usage-a@x.com'))
    await within(screen.getByTestId('row-a@x.com')).findByText('0')
    expect(h.resetUserUsage).toHaveBeenCalledWith('u1')
    await waitFor(() => expect(onToast).toHaveBeenCalledWith("Reset today's usage for Alice"))
  })

  it('the Reset usage button is disabled when usage is already 0', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ usageToday: 0 })]))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    expect(screen.getByTestId('reset-usage-a@x.com').disabled).toBe(true)
  })

  it('a 404 on reset removes the row (user is gone)', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ usageToday: 100 })]))
    h.resetUserUsage.mockRejectedValue(new ApiError('No such user.', 404))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')

    fireEvent.click(screen.getByTestId('reset-usage-a@x.com'))
    await waitFor(() => expect(screen.queryByTestId('row-a@x.com')).toBeNull())
  })

  it('a failed reset reverts the optimistic zero and shows an error', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ usageToday: 4200 })]))
    h.resetUserUsage.mockRejectedValue(new ApiError('Something went wrong.', 500))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('4,200')

    fireEvent.click(screen.getByTestId('reset-usage-a@x.com'))
    const banner = await screen.findByTestId('action-error')
    expect(within(banner).getByText('Something went wrong.')).toBeTruthy()
    await within(screen.getByTestId('row-a@x.com')).findByText('4,200') // reverted
  })
})

describe('UsersLimitsPanel — second-round review fixes', () => {
  it('disables the partial-data Retry button while a newer search has not landed yet (stale-cursor guard)', async () => {
    let resolveSecondPage
    h.fetchUsers
      .mockResolvedValueOnce(pageOf([user({ userId: 'u1', email: 'a@x.com', displayName: 'Alice' })], { nextCursor: 'c1', hasMore: true }))
      .mockRejectedValueOnce(new ApiError('Network hiccup', 500))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    const banner = await screen.findByTestId('loadmore-error')
    const retryBtn = within(banner).getByText('Retry').closest('button')
    expect(retryBtn.disabled).toBe(false) // appliedQuery ('') still matches q ('')

    // Type a new search — q now diverges from appliedQuery, which is still ''. The
    // debounced fetch for it is captured but held open, simulating "still in flight".
    h.fetchUsers.mockImplementationOnce(() => new Promise((resolve) => { resolveSecondPage = resolve }))
    fireEvent.change(screen.getByTestId('users-search'), { target: { value: 'ana' } })
    await waitFor(() => expect(retryBtn.disabled).toBe(true))

    // Clicking while disabled must not fire loadMore() with the stale ('') cursor context.
    fireEvent.click(retryBtn)
    await new Promise((r) => setTimeout(r, 50))
    expect(h.fetchUsers).toHaveBeenCalledTimes(2) // the 300ms debounce hasn't landed yet

    // Let the debounce actually fire, then resolve its fetch successfully.
    await waitFor(() => expect(resolveSecondPage).toBeDefined())
    expect(h.fetchUsers).toHaveBeenCalledTimes(3)
    resolveSecondPage(pageOf([], { hasMore: false }))
    await waitFor(() => expect(screen.queryByTestId('loadmore-error')).toBeNull())
  })

  it('stops the background chain at MAX_LOADED_USERS and shows the capped notice', async () => {
    let call = 0
    h.fetchUsers.mockImplementation(() => {
      call += 1
      const batch = Array.from({ length: 100 }, (_, i) =>
        user({ userId: `u${call}-${i}`, email: `u${call}-${i}@x.com`, displayName: `U${call}-${i}` }),
      )
      return Promise.resolve(pageOf(batch, { nextCursor: `c${call}`, hasMore: true }))
    })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('U1-0')

    // 2000 / 100-per-page = 20 pages before users.length (2000) stops being < MAX_LOADED_USERS.
    await waitFor(() => expect(h.fetchUsers).toHaveBeenCalledTimes(20), { timeout: 10000 })
    await new Promise((r) => setTimeout(r, 100)) // no 21st call sneaks in after the cap
    expect(h.fetchUsers).toHaveBeenCalledTimes(20)
    expect(screen.getByText(/Showing the first 2,000 users/)).toBeTruthy()
  }, 15000)

  it('aborts the in-flight background fetch on unmount', async () => {
    let capturedSignal
    h.fetchUsers
      .mockResolvedValueOnce(pageOf([user()], { nextCursor: 'c1', hasMore: true }))
      .mockImplementationOnce(({ signal }) => {
        capturedSignal = signal
        return new Promise(() => {}) // never resolves — simulates a still-in-flight request
      })
    const { unmount } = render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    await waitFor(() => expect(capturedSignal).toBeDefined())
    expect(capturedSignal.aborted).toBe(false)

    unmount()
    expect(capturedSignal.aborted).toBe(true)
  })

  it('self-heals from React StrictMode double-invoking the mount effect (dev-only mount→cleanup→remount)', async () => {
    // StrictMode intentionally mounts, cleans up, and remounts every component once
    // in development to catch effects that aren't safe to interrupt and restart. A
    // controller created lazily on the ref (`if (!abortRef.current) abortRef.current
    // = new AbortController()`) is permanently aborted by the simulated unmount and
    // never replaced, since the ref is already non-null on the simulated remount —
    // every real fetch from then on carries an already-aborted signal. This mock
    // honours the AbortSignal the way a real fetch() does (an abort-blind mock, like
    // a plain mockResolvedValue, would never exercise this at all).
    h.fetchUsers.mockImplementation(({ signal } = {}) => {
      // Reject with `signal.reason` — whatever DOMException the SAME AbortController
      // implementation the component uses actually produces — rather than a
      // hand-built `new DOMException(...)` of this test's own. jsdom/Node's
      // DOMException doesn't reliably satisfy `instanceof Error` the way a real
      // browser's does, and `error instanceof Error ? error : new Error(...)` in
      // useKeysetList's catch block re-wraps a non-Error into a plain Error, losing
      // `.name` — so a hand-built rejection here would test a DIFFERENT object
      // shape than production ever actually sees.
      if (signal?.aborted) return Promise.reject(signal.reason)
      return new Promise((resolve, reject) => {
        const onAbort = () => reject(signal.reason)
        signal?.addEventListener('abort', onAbort)
        // A macrotask tick, so StrictMode's synchronous mount→cleanup→remount cycle
        // has already run (and aborted the FIRST attempt's controller) before this
        // settles — reproducing the exact race, not just the steady state after it.
        setTimeout(() => {
          signal?.removeEventListener('abort', onAbort)
          if (!signal?.aborted) resolve(pageOf([user()], { hasMore: false }))
        }, 0)
      })
    })

    render(
      <StrictMode>
        <UsersLimitsPanel onToast={() => {}} />
      </StrictMode>,
    )

    await screen.findByText('Alice')
    expect(screen.queryByTestId('users-load-error')).toBeNull()
  })
})
