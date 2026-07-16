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
  approveUser: vi.fn(),
}))
vi.mock('../../../utils/admin', () => h)

const DEFAULTS = { dailyTokenLimit: 100000, contextSoftLimit: 150000, contextHardLimit: 200000 }

// `status` mirrors the backend's own derivation (User.status(): disabled wins
// over pending) so a caller only needs to set suspendedAt/approvedAt like the
// real API does, not juggle a third field by hand — unless a test wants to
// force a specific (possibly server-inconsistent) status directly via `over`.
const user = (over = {}) => {
  const suspendedAt = over.suspendedAt ?? null
  const approvedAt = 'approvedAt' in over ? over.approvedAt : '2026-01-01T00:00:00Z' // default: already approved
  const status = over.status ?? (suspendedAt ? 'disabled' : approvedAt ? 'approved' : 'pending')
  return {
    userId: over.userId || 'u1',
    email: over.email || 'a@x.com',
    displayName: 'displayName' in over ? over.displayName : 'Alice',
    role: over.role || 'citizen',
    suspendedAt,
    approvedAt,
    status,
    usageToday: over.usageToday ?? 0,
    limits: over.limits || {},
    effectiveLimits: over.effectiveLimits || { ...DEFAULTS },
  }
}

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
})

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

  it('"Load more" appends the next page using nextCursor and then disappears', async () => {
    h.fetchUsers
      .mockResolvedValueOnce(pageOf([user({ userId: 'u1', email: 'a@x.com', displayName: 'Alice' })], { nextCursor: 'c1', hasMore: true }))
      .mockResolvedValueOnce(pageOf([user({ userId: 'u2', email: 'b@x.com', displayName: 'Bob' })], { hasMore: false }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('load-more-users'))
    await screen.findByText('Bob')
    expect(screen.getByText('Alice')).toBeTruthy() // page 1 retained, not replaced
    expect(h.fetchUsers).toHaveBeenNthCalledWith(2, expect.objectContaining({ cursor: 'c1' }))
    await waitFor(() => expect(screen.queryByTestId('load-more-users')).toBeNull())
  })

  it('a roster of 26 users shows "Load more" (regression guard for silent truncation)', async () => {
    const first25 = Array.from({ length: 25 }, (_, i) =>
      user({ userId: `u${i}`, email: `u${i}@x.com`, displayName: `U${i}` }),
    )
    h.fetchUsers.mockResolvedValue(pageOf(first25, { nextCursor: 'c1', hasMore: true }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('U0')
    expect(screen.getByTestId('load-more-users')).toBeTruthy()
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

describe('UsersLimitsPanel — pending approval', () => {
  it('renders Pending for a never-approved user, alongside Active/Suspended', async () => {
    h.fetchUsers.mockResolvedValue(
      pageOf([
        user({ userId: 'u1', email: 'a@x.com' }), // approved (default)
        user({ userId: 'u2', email: 'p@x.com', displayName: 'Pat', approvedAt: null }),
        user({ userId: 'u3', email: 'd@x.com', displayName: 'Dee', suspendedAt: '2026-07-01T00:00:00Z' }),
      ]),
    )
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    expect(within(screen.getByTestId('row-a@x.com')).getByText('Active')).toBeTruthy()
    expect(within(screen.getByTestId('row-p@x.com')).getByText('Pending')).toBeTruthy()
    expect(within(screen.getByTestId('row-d@x.com')).getByText('Suspended')).toBeTruthy()
  })

  it('disabled wins over pending — a suspended-and-never-approved row shows Suspended, not Pending, and offers no Approve', async () => {
    h.fetchUsers.mockResolvedValue(
      pageOf([user({ approvedAt: null, suspendedAt: '2026-07-01T00:00:00Z' })]),
    )
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    expect(within(screen.getByTestId('row-a@x.com')).getByText('Suspended')).toBeTruthy()
    expect(screen.queryByTestId('approve-a@x.com')).toBeNull()
    expect(screen.getByTestId('reactivate-a@x.com')).toBeTruthy()
  })

  it('the Approve action only appears for a pending row', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user()])) // approved (default)
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    expect(screen.queryByTestId('approve-a@x.com')).toBeNull()
  })

  it('approve flips the row to Active and calls approveUser', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ approvedAt: null })]))
    h.approveUser.mockResolvedValue({ userId: 'u1', approvedAt: '2026-07-14T09:00:00Z' })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    expect(within(screen.getByTestId('row-a@x.com')).getByText('Pending')).toBeTruthy()
    fireEvent.click(screen.getByTestId('approve-a@x.com'))
    await within(screen.getByTestId('row-a@x.com')).findByText('Active')
    expect(h.approveUser).toHaveBeenCalledWith('u1')
    expect(screen.queryByTestId('approve-a@x.com')).toBeNull() // Approve disappears once approved
  })

  it('approve → 403 reverts the optimistic flip and shows an error', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ approvedAt: null })]))
    h.approveUser.mockRejectedValue(new ApiError('Super-admin privileges required.', 403))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('approve-a@x.com'))
    await screen.findByTestId('action-error')
    const row = screen.getByTestId('row-a@x.com')
    await waitFor(() => expect(within(row).getByText('Pending')).toBeTruthy())
    expect(screen.getByTestId('approve-a@x.com')).toBeTruthy()
  })

  it('approve → 409 (already approved) reconciles to Active with no error toast', async () => {
    const onToast = vi.fn()
    h.fetchUsers.mockResolvedValue(pageOf([user({ approvedAt: null })]))
    h.approveUser.mockRejectedValue(new ApiError('User is already approved.', 409))
    render(<UsersLimitsPanel onToast={onToast} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('approve-a@x.com'))
    await within(screen.getByTestId('row-a@x.com')).findByText('Active')
    expect(screen.queryByTestId('action-error')).toBeNull()
    expect(onToast).not.toHaveBeenCalled()
  })

  it('reactivating a previously-pending-then-suspended row returns it to Pending, not Active', async () => {
    // approved_at was never set; suspended_at clears on reactivate — the underlying
    // state revealed must be "pending", not a false "approved".
    h.fetchUsers.mockResolvedValue(pageOf([user({ approvedAt: null, suspendedAt: '2026-07-01T00:00:00Z' })]))
    h.reactivateUser.mockResolvedValue({ userId: 'u1', suspendedAt: null })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('reactivate-a@x.com'))
    await within(screen.getByTestId('row-a@x.com')).findByText('Pending')
    expect(screen.getByTestId('approve-a@x.com')).toBeTruthy()
  })

  it('the status filter forwards `status` to fetchUsers and resets to page 1 while keeping the search query', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user()], { nextCursor: 'c1', hasMore: true }))
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')

    fireEvent.change(screen.getByTestId('users-search'), { target: { value: 'ana' } })
    await waitFor(() => expect(h.fetchUsers).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'ana' })))

    fireEvent.change(screen.getByTestId('users-status-filter'), { target: { value: 'pending' } })
    await waitFor(() =>
      expect(h.fetchUsers).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'pending', cursor: null, q: 'ana' })),
    )
  })

  it('"all" sends no status param at all', async () => {
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    h.fetchUsers.mockClear()
    fireEvent.change(screen.getByTestId('users-status-filter'), { target: { value: 'approved' } })
    await waitFor(() => expect(h.fetchUsers).toHaveBeenCalled())
    fireEvent.change(screen.getByTestId('users-status-filter'), { target: { value: 'all' } })
    await waitFor(() => {
      const lastCall = h.fetchUsers.mock.calls.at(-1)[0]
      expect(lastCall.status).toBeUndefined()
    })
  })

  it('approving a row while filtered to "Pending" removes it from view, not just re-badges it', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ approvedAt: null })]))
    h.approveUser.mockResolvedValue({ userId: 'u1', approvedAt: '2026-07-14T09:00:00Z' })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')

    fireEvent.change(screen.getByTestId('users-status-filter'), { target: { value: 'pending' } })
    await waitFor(() => expect(h.fetchUsers).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'pending' })))
    await screen.findByText('Alice') // still shown — the mock re-resolves the same row

    fireEvent.click(screen.getByTestId('approve-a@x.com'))
    // Gone from the filtered view immediately (optimistic), not left showing "Active".
    await waitFor(() => expect(screen.queryByTestId('row-a@x.com')).toBeNull())
  })

  it('deactivating a row while filtered to "Approved" removes it from view', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ suspendedAt: null })]))
    h.deactivateUser.mockResolvedValue({ userId: 'u1', suspendedAt: '2026-07-14T09:00:00Z' })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')

    fireEvent.change(screen.getByTestId('users-status-filter'), { target: { value: 'approved' } })
    await waitFor(() => expect(h.fetchUsers).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'approved' })))
    await screen.findByText('Alice')

    fireEvent.click(screen.getByTestId('deactivate-a@x.com'))
    await waitFor(() => expect(screen.queryByTestId('row-a@x.com')).toBeNull())
  })

  it('an unfiltered ("all") view still just re-badges the row in place, not removes it', async () => {
    h.fetchUsers.mockResolvedValue(pageOf([user({ approvedAt: null })]))
    h.approveUser.mockResolvedValue({ userId: 'u1', approvedAt: '2026-07-14T09:00:00Z' })
    render(<UsersLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')

    fireEvent.click(screen.getByTestId('approve-a@x.com'))
    await within(screen.getByTestId('row-a@x.com')).findByText('Active') // row stays, badge flips
  })

  it('under StrictMode, mounting fetches exactly once — no extra fetch from the statusFilter effect', async () => {
    // The bug this pins: a boolean "have we run yet" ref flips permanently on the
    // first invocation, so it doesn't survive StrictMode's dev-only double-invoke
    // of a fresh mount's effects — the second invocation sees the ref already
    // flipped and calls refresh() anyway, firing a real extra fetch on every mount.
    render(
      <StrictMode>
        <UsersLimitsPanel onToast={() => {}} />
      </StrictMode>,
    )
    await screen.findByText('Alice')
    await waitFor(() => expect(h.fetchUsers).toHaveBeenCalledTimes(1))
  })
})
