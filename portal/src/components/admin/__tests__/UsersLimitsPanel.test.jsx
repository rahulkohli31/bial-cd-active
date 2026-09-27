import { StrictMode } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent, within } from '@testing-library/react'
import UsersLimitsPanel from '../UsersLimitsPanel.jsx'
import { ApiError } from '../../../utils/apiError'

// Mocks the data layer only — the real useKeysetList hook still drives pagination/search.
const h = vi.hoisted(() => ({
  fetchUsers: vi.fn(),
  updateUserLimits: vi.fn(),
  deactivateUser: vi.fn(),
  reactivateUser: vi.fn(),
  resetUserUsage: vi.fn(),
}))
vi.mock('../../../utils/admin', () => h)

// The server's own defaults (`services/usage/limits.py`), so the modal is exercised against
// the numbers an administrator really opens it on. The two per-conversation figures moved
// with the corrected model window; a mock left at the old pair would have this file passing
// against a form nobody uses.
const DEFAULTS = { dailyTokenLimit: 100000, contextSoftLimit: 375000, contextHardLimit: 500000 }

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
    expect(within(screen.getByTestId('row-u1')).getByText('Active')).toBeTruthy()
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
    expect(within(screen.getByTestId('row-u1')).getByText('Active')).toBeTruthy()
    expect(within(screen.getByTestId('row-u2')).getByText('Suspended')).toBeTruthy()
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
    await waitFor(() => expect(screen.getByText('Showing 1–10 of 26')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: '3' }))
    expect(screen.getByTestId('row-u25')).toBeTruthy()
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
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search users' }), { target: { value: 'ana' } })
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
    await within(screen.getByTestId('row-u1')).findByText('Suspended')
    expect(h.deactivateUser).toHaveBeenCalledWith('u1')
  })

  it('reactivate clears the row back to Active', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ suspendedAt: '2026-07-01T00:00:00Z' })]))
    h.reactivateUser.mockResolvedValue({ userId: 'u1', suspendedAt: null })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    const row = screen.getByTestId('row-u1')
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

  it("the caller's own super-admin row cannot be self-suspended", async () => {
    // Only a super-admin loads this panel and appears in their own roster, so the guard below also covers self-suspension.
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
    const row = screen.getByTestId('row-u1')
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
    await within(screen.getByTestId('row-u1')).findByText('Suspended')
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
    await within(screen.getByTestId('row-u1')).findByText('Active')
    expect(screen.queryByTestId('action-error')).toBeNull()
    expect(onToast).not.toHaveBeenCalled()
  })

  // Pins the e instanceof ApiError && e.status === 404 arm:
  // the duck-typed e?.status === 404 this replaced was untested on either side, so the
  // narrowing to ApiError was "equivalent today" by inspection only, not by a test.
  it('deactivate → 404 (user gone) drops the row silently, with no error toast', async () => {
    const onToast = vi.fn()
    h.fetchUsers.mockResolvedValue(pageOf([user({ suspendedAt: null })]))
    h.deactivateUser.mockRejectedValue(new ApiError('User not found.', 404))
    render(<UsersLimitsPanel onToast={onToast} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('deactivate-a@x.com'))
    await waitFor(() => expect(screen.queryByTestId('row-u1')).toBeNull())
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
    await waitFor(() => expect(screen.queryByTestId('row-u1')).toBeNull())
    expect(screen.queryByTestId('action-error')).toBeNull()
    expect(onToast).not.toHaveBeenCalled()
  })

  it('a citizen who reaches the panel sees the 403 gate message — not blank, not a suspension redirect', async () => {
    // The suspension interceptor lives in authFetch and never fires for this body;
    // fetchUsers simply throws the gate message, and the panel must show it.
    h.fetchUsers.mockRejectedValue(new ApiError('Super-admin privileges required.', 403))
    render(<UsersLimitsPanel onToast={() => {}} />)
    const msg = await screen.findByTestId('users-load-error')
    expect(msg.textContent).toContain('Super-admin privileges required.')
  })

  it('sets a daily token limit to 200000 and then clears it — PATCH null clears the override', async () => {
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

  it('shows "0 (default)" in the placeholder when the server envelope omits a default', async () => {
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
    // The OTHER arm of the fold: contextSoftLimit was never omitted from `defaults`, so a
    // placeholder that ignored `defaultValue` and always rendered `fmt(0)` — a plausible
    // "silently reverted later" mistake this test exists to catch — would pass the assertion
    // above by coincidence but fail this one, which pins the real default actually rendering.
    expect(screen.getByTestId('limit-soft').placeholder).toBe('375,000 (default)')
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
    await waitFor(() => expect(order()).toEqual(['row-u2', 'row-u3', 'row-u1']))
    fireEvent.click(screen.getByText('Daily tokens'))
    await waitFor(() => expect(order()).toEqual(['row-u1', 'row-u3', 'row-u2']))
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
    await waitFor(() => expect(screen.queryByTestId('row-u1')).toBeNull())
    expect(screen.getByTestId('row-u2')).toBeTruthy()
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
    await waitFor(() => expect(screen.queryByTestId('row-u1')).toBeNull())
    expect(screen.getByTestId('row-u2')).toBeTruthy()
  })

  it('the Role filter still applies to the rows a new server search brings back', async () => {
    h.fetchUsers.mockResolvedValue(
      pageOf([
        user({ userId: 'u1', email: 'a@x.com', displayName: 'Alice', role: 'citizen' }),
        user({ userId: 'u2', email: 'admin@x.com', displayName: 'Admin', role: 'super_admin' }),
      ]),
    )
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    await pickSelect('role-filter', 'Super admin')
    await waitFor(() => expect(screen.queryByTestId('row-u1')).toBeNull())

    h.fetchUsers.mockResolvedValue(
      pageOf([
        user({ userId: 'u3', email: 'ana@x.com', displayName: 'Ana', role: 'citizen' }),
        user({ userId: 'u4', email: 'anand@x.com', displayName: 'Anand', role: 'super_admin' }),
      ]),
    )
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search users' }), { target: { value: 'an' } })
    await screen.findByTestId('row-u4')
    expect(screen.queryByTestId('row-u3')).toBeNull()
    expect(screen.getByRole('searchbox', { name: 'Search users' }).value).toBe('an')
  })

  it('a row deactivated while the Status filter is "Active" drops out of view immediately', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ suspendedAt: null })]))
    h.deactivateUser.mockResolvedValue({ userId: 'u1', suspendedAt: '2026-07-10T09:00:00Z' })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')

    await pickSelect('status-filter', 'Active')
    expect(screen.getByTestId('row-u1')).toBeTruthy()
    fireEvent.click(screen.getByTestId('deactivate-a@x.com'))
    await waitFor(() => expect(screen.queryByTestId('row-u1')).toBeNull())
  })

  it('pages 30 loaded users ten at a time with the shared pager', async () => {
    const thirty = Array.from({ length: 30 }, (_, i) => user({ userId: `u${i}`, email: `u${i}@x.com`, displayName: `U${i}` }))
    h.fetchUsers.mockResolvedValue(pageOf(thirty, { hasMore: false }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('U0')

    expect(screen.getAllByTestId(/^row-/)).toHaveLength(10)
    expect(screen.getByText('Showing 1–10 of 30')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Go to previous page' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Go to next page' }).disabled).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: '3' }))
    expect(screen.getByText('Showing 21–30 of 30')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Go to next page' }).disabled).toBe(true)
  })

  it('pages the roster it loaded from the server, keyset page by keyset page', async () => {
    const batch = (from, n) => Array.from({ length: n }, (_, i) => user({ userId: `u${from + i}`, email: `u${from + i}@x.com`, displayName: `U${from + i}` }))
    h.fetchUsers
      .mockResolvedValueOnce(pageOf(batch(0, 100), { nextCursor: 'c1', hasMore: true }))
      .mockResolvedValueOnce(pageOf(batch(100, 20), { hasMore: false }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('U0')
    await waitFor(() => expect(screen.getByText('Showing 1–10 of 120')).toBeTruthy())
    expect(h.fetchUsers).toHaveBeenNthCalledWith(2, expect.objectContaining({ cursor: 'c1', limit: 100 }))
  })

  it('applying a filter while on page 3 returns to page 1 instead of stranding an empty page', async () => {
    const thirty = Array.from({ length: 30 }, (_, i) => user({ userId: `u${i}`, email: `u${i}@x.com`, displayName: `U${i}`, role: 'citizen' }))
    h.fetchUsers.mockResolvedValue(pageOf(thirty, { hasMore: false }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('U0')

    fireEvent.click(screen.getByRole('button', { name: '3' }))
    await waitFor(() => expect(screen.getByText('Showing 21–30 of 30')).toBeTruthy())

    await pickSelect('role-filter', 'Citizen')
    await waitFor(() => expect(screen.getByText('Showing 1–10 of 30')).toBeTruthy())
    expect(screen.getAllByTestId(/^row-/)).toHaveLength(10)
  })

  it('a new search resets the view back to page 1', async () => {
    const thirty = Array.from({ length: 30 }, (_, i) => user({ userId: `u${i}`, email: `u${i}@x.com`, displayName: `U${i}` }))
    h.fetchUsers.mockResolvedValueOnce(pageOf(thirty, { hasMore: false }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('U0')

    fireEvent.click(screen.getByRole('button', { name: '3' }))
    await waitFor(() => expect(screen.getByText('Showing 21–30 of 30')).toBeTruthy())

    h.fetchUsers.mockResolvedValueOnce(pageOf([user({ userId: 'z1', email: 'z@x.com', displayName: 'Zara' })], { hasMore: false }))
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search users' }), { target: { value: 'zara' } })
    await screen.findByText('Zara')
    expect(screen.getByText('Showing 1–1 of 1')).toBeTruthy()
  })
})

describe('UsersLimitsPanel — review-fix regressions', () => {
  it('deactivating a user on page 3 does not bounce the view back to page 1', async () => {
    // autoResetPageIndex defaults ON in TanStack Table; mergedUsers' identity changes
    // on every optimistic update too, not just a real sort/filter change, so the
    // default would silently return the admin to page 1 on a plain Deactivate click.
    const thirty = Array.from({ length: 30 }, (_, i) => user({ userId: `u${i}`, email: `u${i}@x.com`, displayName: `U${i}` }))
    h.fetchUsers.mockResolvedValue(pageOf(thirty, { hasMore: false }))
    h.deactivateUser.mockResolvedValue({ userId: 'u25', suspendedAt: '2026-07-10T09:00:00Z' })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('U0')

    fireEvent.click(screen.getByRole('button', { name: '3' }))
    await waitFor(() => expect(screen.getByText('Showing 21–30 of 30')).toBeTruthy())

    fireEvent.click(screen.getByTestId('deactivate-u25@x.com')) // u25 lives on page 3
    await waitFor(() => expect(h.deactivateUser).toHaveBeenCalledWith('u25'))
    expect(screen.getByText('Showing 21–30 of 30')).toBeTruthy()
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
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search users' }), { target: { value: 'ana' } })

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
    // role is derived at read time from the env allowlist — a suspended
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
    await within(screen.getByTestId('row-u1')).findByText('0')
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
    await waitFor(() => expect(screen.queryByTestId('row-u1')).toBeNull())
  })

  it('a failed reset reverts the optimistic zero and shows an error', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ usageToday: 4200 })]))
    h.resetUserUsage.mockRejectedValue(new ApiError('Something went wrong.', 500))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('4,200')

    fireEvent.click(screen.getByTestId('reset-usage-a@x.com'))
    const banner = await screen.findByTestId('action-error')
    expect(within(banner).getByText('Something went wrong.')).toBeTruthy()
    await within(screen.getByTestId('row-u1')).findByText('4,200') // reverted
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
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search users' }), { target: { value: 'ana' } })
    await waitFor(() => expect(retryBtn.disabled).toBe(true))

    // Clicking while disabled must not fire loadMore() with the stale ('') cursor context.
    fireEvent.click(retryBtn)
    await new Promise((r) => setTimeout(r, 50))
    expect(h.fetchUsers).toHaveBeenCalledTimes(2) // the 300ms debounce hasn't landed yet

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
    // A controller created lazily on the ref is permanently aborted by the simulated unmount and
    // never replaced on remount, so every real fetch from then on would carry an already-aborted
    // signal — this mock honours the AbortSignal the way a real fetch() does, unlike an
    // abort-blind mockResolvedValue.
    h.fetchUsers.mockImplementation(({ signal } = {}) => {
      // Rejects with `signal.reason` rather than a hand-built DOMException: jsdom's DOMException
      // doesn't reliably satisfy `instanceof Error`, and useKeysetList's catch re-wraps a
      // non-Error into a plain Error, losing `.name`.
      if (signal?.aborted) return Promise.reject(signal.reason)
      return new Promise((resolve, reject) => {
        const onAbort = () => reject(signal.reason)
        signal?.addEventListener('abort', onAbort)
        // A macrotask tick lets StrictMode's mount→cleanup→remount cycle run first, reproducing
        // the exact race rather than just the steady state after it.
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

describe('UsersLimitsPanel — the per-conversation hints describe what actually happens', () => {
  // WHY THIS BLOCK EXISTS: both hints were FALSE — they described a guardrail deleted with
  // `ChatPage.tsx`, so the fields saved cleanly and changed nothing. Copy on its own cannot be
  // tested (a hint asserted against itself is a tautology, worse than none if satisfied by
  // deleting the words), so each hint is asserted in a PAIR: the sentence says what happens, and
  // the field it labels demonstrably writes the value the enforcement reads.
  //
  // The hard limit's enforcement itself lives on the server, pinned at
  // `backend/tests/api/v1/conversations/test_context_gate.py`.

  async function openEditor() {
    h.fetchUsers.mockResolvedValue(pageOf([user({ limits: {}, effectiveLimits: { ...DEFAULTS } })]))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('edit-a@x.com'))
  }

  it('the warn hint says it is a warning, and its field writes contextSoftLimit', async () => {
    h.updateUserLimits.mockResolvedValue({
      userId: 'u1',
      limits: { dailyTokenLimit: null, contextSoftLimit: 40000, contextHardLimit: null },
      effectiveLimits: { ...DEFAULTS, contextSoftLimit: 40000 },
    })
    await openEditor()

    const hint = screen.getByText(/Warn the user their chat is getting long/i)
    // The half that stops it being read as a stop: the browser warns and Send still works.
    expect(hint.textContent).toMatch(/does not stop them sending/i)

    fireEvent.click(screen.getByTestId('usedefault-soft'))
    fireEvent.change(screen.getByTestId('limit-soft'), { target: { value: '40000' } })
    fireEvent.click(screen.getByTestId('save-limits'))
    await waitFor(() =>
      expect(h.updateUserLimits).toHaveBeenCalledWith('u1', {
        dailyTokenLimit: null,
        contextSoftLimit: 40000,
        contextHardLimit: null,
      }),
    )
  })

  it('the max hint says the server refuses, and its field writes contextHardLimit', async () => {
    h.updateUserLimits.mockResolvedValue({
      userId: 'u1',
      limits: { dailyTokenLimit: null, contextSoftLimit: null, contextHardLimit: 480000 },
      effectiveLimits: { ...DEFAULTS, contextHardLimit: 480000 },
    })
    await openEditor()

    const hint = screen.getByText(/Hard stop for a single chat/i)
    // The three facts an administrator needs and did not have: that it is the SERVER, that it
    // acts on the NEXT MESSAGE, and what the user is told.
    expect(hint.textContent).toMatch(/server refuses/i)
    expect(hint.textContent).toMatch(/start a new chat/i)
    // BOTH ENDS OF THE RANGE, not just the ceiling: only the ceiling used to be enforced, so a
    // max below what a single run costs before anyone types was accepted, and that person then
    // could not get past the first message in any chat, with a message telling them to start a
    // new chat. The floor is now the first thing an administrator reads.
    expect(hint.textContent).toMatch(/Between 16,000 and 1,000,000 \(model window\)/)

    // 480,000 rather than something smaller: the modal refuses a max at or below the warn
    // threshold, and the default warn is 375,000. That refusal is its own existing behaviour;
    // tripping it here would prove nothing about the wiring this test is for.
    fireEvent.click(screen.getByTestId('usedefault-hard'))
    fireEvent.change(screen.getByTestId('limit-hard'), { target: { value: '480000' } })
    fireEvent.click(screen.getByTestId('save-limits'))
    await waitFor(() =>
      expect(h.updateUserLimits).toHaveBeenCalledWith('u1', {
        dailyTokenLimit: null,
        contextSoftLimit: null,
        contextHardLimit: 480000,
      }),
    )
  })

  it('a max below the floor is refused before it is submitted, and the floor is named', async () => {
    // THE FORM STOPS IT, and the server stops it too — this is the half that means an
    // administrator is told BEFORE they save rather than after. Below the floor that person
    // cannot get past the first message in any chat: a run's own system prompt and tool schemas
    // already exceed the ceiling, the provider counts them in the first turn it reports, and
    // the sentence they read tells them to start a new chat.
    //
    // Mutation check: delete the `hardNum < CONTEXT_HARD_FLOOR` arm from `submit` and this goes
    // red on the second assertion — the panel would call the API instead of refusing.
    await openEditor()
    fireEvent.click(screen.getByTestId('usedefault-hard'))
    fireEvent.change(screen.getByTestId('limit-hard'), { target: { value: '8000' } })
    fireEvent.click(screen.getByTestId('usedefault-soft'))
    fireEvent.change(screen.getByTestId('limit-soft'), { target: { value: '4000' } })
    fireEvent.click(screen.getByTestId('save-limits'))

    expect(await screen.findByText(/can't be below 16,000/i)).toBeTruthy()
    expect(h.updateUserLimits).not.toHaveBeenCalled()

    // …and the floor itself is a value an administrator may genuinely want, so the refusal is a
    // floor rather than a wall. Without this pairing the test would pass just as well against a
    // panel that refused every value.
    fireEvent.change(screen.getByTestId('limit-hard'), { target: { value: '16000' } })
    fireEvent.click(screen.getByTestId('save-limits'))
    await waitFor(() => expect(h.updateUserLimits).toHaveBeenCalled())
  })

  it('a max past the model window is refused, and the ceiling the server serves is not', async () => {
    // THE HAND-KEPT TWIN, PINNED FROM BOTH SIDES. `MODEL_CONTEXT_WINDOW` in this panel is a copy
    // of the server's, and the form VALIDATES against it — so a copy left behind at the old
    // 200,000 refuses a per-conversation max the server would happily have stored, and tells an
    // administrator the model cannot do something it demonstrably does.
    //
    // The refusal half alone would pass against a panel stuck at any number at all, which is why
    // the acceptance half is here: 500,000 is the ceiling the server now defaults to, and the
    // panel has to be able to reach it.
    h.updateUserLimits.mockResolvedValue({
      userId: 'u1',
      limits: { dailyTokenLimit: null, contextSoftLimit: null, contextHardLimit: 500000 },
      effectiveLimits: { ...DEFAULTS, contextHardLimit: 500000 },
    })
    await openEditor()

    fireEvent.click(screen.getByTestId('usedefault-hard'))
    fireEvent.change(screen.getByTestId('limit-hard'), { target: { value: '1200000' } })
    fireEvent.click(screen.getByTestId('save-limits'))

    expect(await screen.findByText(/can't exceed 1,000,000/i)).toBeTruthy()
    expect(h.updateUserLimits).not.toHaveBeenCalled()

    fireEvent.change(screen.getByTestId('limit-hard'), { target: { value: '500000' } })
    fireEvent.click(screen.getByTestId('save-limits'))
    await waitFor(() =>
      expect(h.updateUserLimits).toHaveBeenCalledWith('u1', {
        dailyTokenLimit: null,
        contextSoftLimit: null,
        contextHardLimit: 500000,
      }),
    )
  })

  it('the propagation note no longer lumps the two per-conversation limits together', async () => {
    // They propagate DIFFERENTLY now and an administrator acts on the difference: the hard stop
    // is read from the database on every send, the warn threshold rides the profile the browser
    // cached at sign-in. The old note said both waited for a reload, which is now wrong about
    // the one that matters most.
    await openEditor()
    const note = screen.getByText(/take effect on the user/i)
    expect(note.textContent).toMatch(/per-conversation max.*take effect on the user’s next message/is)
    expect(note.textContent).toMatch(/per-conversation warn.*after the user reloads/is)
  })
})
