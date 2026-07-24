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
    await waitFor(() => expect(screen.getByText('Page 1 of 2')).toBeTruthy())
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
    h.fetchUsers.mockResolvedValue(pageOf([user()], { nextCursor: 'c1', hasMore: true }))
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
    expect(screen.getByText('Page 1 of 2')).toBeTruthy()
    expect(screen.getByTestId('users-prev-page').disabled).toBe(true)
    expect(screen.getByTestId('users-next-page').disabled).toBe(false)

    fireEvent.click(screen.getByTestId('users-next-page'))
    await waitFor(() => expect(screen.getAllByTestId(/^row-/)).toHaveLength(5))
    expect(screen.getByText('Page 2 of 2')).toBeTruthy()
    expect(screen.getByTestId('users-next-page').disabled).toBe(true)
  })

  it('applying a filter while on page 2 returns to page 1 instead of stranding an empty page', async () => {
    const thirty = Array.from({ length: 30 }, (_, i) => user({ userId: `u${i}`, email: `u${i}@x.com`, displayName: `U${i}`, role: 'citizen' }))
    h.fetchUsers.mockResolvedValue(pageOf(thirty, { hasMore: false }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('U0')

    fireEvent.click(screen.getByTestId('users-next-page'))
    await waitFor(() => expect(screen.getByText('Page 2 of 2')).toBeTruthy())

    await pickSelect('role-filter', 'Citizen')
    await waitFor(() => expect(screen.getByText(/^Page 1 of/)).toBeTruthy())
    expect(screen.getAllByTestId(/^row-/)).toHaveLength(25)
  })

  it('a new search resets the view back to page 1', async () => {
    const thirty = Array.from({ length: 30 }, (_, i) => user({ userId: `u${i}`, email: `u${i}@x.com`, displayName: `U${i}` }))
    h.fetchUsers.mockResolvedValueOnce(pageOf(thirty, { hasMore: false }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('U0')

    fireEvent.click(screen.getByTestId('users-next-page'))
    await waitFor(() => expect(screen.getByText('Page 2 of 2')).toBeTruthy())

    h.fetchUsers.mockResolvedValueOnce(pageOf([user({ userId: 'z1', email: 'z@x.com', displayName: 'Zara' })], { hasMore: false }))
    fireEvent.change(screen.getByTestId('users-search'), { target: { value: 'zara' } })
    await screen.findByText('Zara')
    expect(screen.getByText('Page 1 of 1')).toBeTruthy()
  })
})
