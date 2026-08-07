import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import GlobalLimitsPanel from '../GlobalLimitsPanel'

// Mock the data layer, same convention as UsersLimitsPanel.test.jsx: the real
// useKeysetList hook drives pagination/search, only the network calls are stubbed.
const h = vi.hoisted(() => ({
  fetchUsers: vi.fn(),
  bulkUpdateUserLimits: vi.fn(),
}))
vi.mock('../../../utils/admin', () => h)

const DEFAULTS = { dailyTokenLimit: 1000000, contextSoftLimit: 150000, contextHardLimit: 200000 }

const user = (over: Record<string, unknown> = {}) => ({
  userId: over.userId || 'u1',
  email: over.email || 'a@x.com',
  displayName: 'displayName' in over ? over.displayName : 'Alice',
  role: over.role || 'citizen',
  suspendedAt: over.suspendedAt ?? null,
  usageToday: over.usageToday ?? 0,
  limits: over.limits || {},
  effectiveLimits: over.effectiveLimits || { ...DEFAULTS },
})

const pageOf = (users: ReturnType<typeof user>[], over: Record<string, unknown> = {}) => ({
  defaults: DEFAULTS,
  users,
  nextCursor: over.nextCursor ?? null,
  hasMore: over.hasMore ?? false,
})

afterEach(cleanup)
beforeEach(() => {
  for (const fn of Object.values(h)) fn.mockReset()
  h.fetchUsers.mockResolvedValue(pageOf([user(), user({ userId: 'u2', email: 'b@x.com', displayName: 'Bob' })]))
})

describe('GlobalLimitsPanel', () => {
  it('defaults to Selected users mode and renders the roster with checkboxes', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    expect(screen.getByTestId('mode-selected').getAttribute('aria-checked')).toBe('true')
    expect(screen.getByTestId('select-a@x.com')).toBeTruthy()
    expect(screen.getByTestId('select-b@x.com')).toBeTruthy()
  })

  it('switching to All users hides the roster and shows the system-wide summary', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('mode-all'))
    expect(screen.queryByTestId('select-a@x.com')).toBeNull()
    expect(screen.getByTestId('all-users-summary').textContent).toContain('every user')
  })

  it('picking a preset fills the custom/exact-value input', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    const select = screen.getByTestId('preset-select') as HTMLSelectElement
    fireEvent.change(select, { target: { value: '5000000' } })
    expect((screen.getByTestId('custom-value') as HTMLInputElement).value).toBe('5000000')
  })

  it('Apply is disabled until at least one user is selected (Selected mode)', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    const apply = screen.getByTestId('glp-apply') as HTMLButtonElement
    expect(apply.disabled).toBe(true)
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    expect(apply.disabled).toBe(false)
  })

  it('Apply is enabled immediately in All users mode (no selection needed)', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('mode-all'))
    expect((screen.getByTestId('glp-apply') as HTMLButtonElement).disabled).toBe(false)
  })

  it('Apply opens a confirm step and does not call the API until confirmed', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    fireEvent.click(screen.getByTestId('glp-apply'))
    expect(h.bulkUpdateUserLimits).not.toHaveBeenCalled()
    expect(screen.getByTestId('glp-confirm')).toBeTruthy()
  })

  it('confirming applies to exactly the selected user ids and toasts the updated count', async () => {
    h.bulkUpdateUserLimits.mockResolvedValue({ updatedCount: 2 })
    const onToast = vi.fn()
    render(<GlobalLimitsPanel onToast={onToast} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    fireEvent.click(screen.getByTestId('select-b@x.com'))
    fireEvent.click(screen.getByTestId('glp-apply'))
    fireEvent.click(screen.getByTestId('glp-confirm'))

    await waitFor(() => expect(onToast).toHaveBeenCalled())
    expect(h.bulkUpdateUserLimits).toHaveBeenCalledWith(1000000, ['u1', 'u2'], {})
    expect(onToast).toHaveBeenCalledWith(expect.stringContaining('2'))
  })

  it('confirming in All users mode sends userIds=undefined (the backend resolves "all")', async () => {
    h.bulkUpdateUserLimits.mockResolvedValue({ updatedCount: 42 })
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('mode-all'))
    fireEvent.click(screen.getByTestId('glp-apply'))
    fireEvent.click(screen.getByTestId('glp-confirm'))
    await waitFor(() => expect(h.bulkUpdateUserLimits).toHaveBeenCalled())
    expect(h.bulkUpdateUserLimits).toHaveBeenCalledWith(1000000, undefined, {})
  })

  it('a failed apply surfaces the error inline and keeps the confirm step open for retry', async () => {
    h.bulkUpdateUserLimits.mockRejectedValue(new Error('Only super-admins can do this.'))
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    fireEvent.click(screen.getByTestId('glp-apply'))
    fireEvent.click(screen.getByTestId('glp-confirm'))
    expect((await screen.findByTestId('apply-error')).textContent).toContain('super-admins')
  })

  it('select-all-loaded toggles every currently-loaded row on and off', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await screen.findByText('Alice')
    fireEvent.click(screen.getByTestId('select-all-loaded'))
    expect((screen.getByTestId('select-a@x.com') as HTMLInputElement).checked).toBe(true)
    expect((screen.getByTestId('select-b@x.com') as HTMLInputElement).checked).toBe(true)
    fireEvent.click(screen.getByTestId('select-all-loaded'))
    expect((screen.getByTestId('select-a@x.com') as HTMLInputElement).checked).toBe(false)
  })
})
