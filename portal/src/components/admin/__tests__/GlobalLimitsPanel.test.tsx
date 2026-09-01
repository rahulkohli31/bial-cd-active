import { StrictMode } from 'react'
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

// `mode` defaults to 'all' (finding #6's actual fix — the roster never loads until the
// admin actively asks for it), so any test that needs the roster switches explicitly.
const enterSelectedMode = async () => {
  fireEvent.click(screen.getByTestId('mode-selected'))
  await screen.findByText('Alice')
}

afterEach(cleanup)
beforeEach(() => {
  for (const fn of Object.values(h)) fn.mockReset()
  h.fetchUsers.mockResolvedValue(pageOf([user(), user({ userId: 'u2', email: 'b@x.com', displayName: 'Bob' })]))
})

describe('GlobalLimitsPanel', () => {
  it('defaults to All users mode, showing the system-wide summary and loading no roster at all', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await screen.findByTestId('all-users-summary')
    expect(screen.getByTestId('mode-all').getAttribute('aria-checked')).toBe('true')
    expect(screen.queryByTestId('select-a@x.com')).toBeNull()
    // Finding #6: the background roster chain only ever starts once the admin actively
    // switches to "Selected users" — defaulting to 'all' means an admin who only uses
    // this mode never triggers a single roster request.
    expect(h.fetchUsers).not.toHaveBeenCalled()
  })

  it('switching to Selected users loads the roster with checkboxes', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await enterSelectedMode()
    expect(screen.getByTestId('mode-selected').getAttribute('aria-checked')).toBe('true')
    expect(screen.getByTestId('select-a@x.com')).toBeTruthy()
    expect(screen.getByTestId('select-b@x.com')).toBeTruthy()
  })

  it('switching back to All users hides the roster and shows the system-wide summary', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await enterSelectedMode()
    fireEvent.click(screen.getByTestId('mode-all'))
    expect(screen.queryByTestId('select-a@x.com')).toBeNull()
    expect(screen.getByTestId('all-users-summary').textContent).toContain('every current, active user')
    expect(screen.getByTestId('all-users-summary').textContent).toContain('Suspended users are excluded')
  })

  it('switching from Selected back to All users stops the background roster chain from continuing', async () => {
    // hasMore stays true forever — deliberately, to prove the mode-all switch is what
    // stops the chain rather than the fixture running out on its own. Both clicks fire
    // synchronously, BEFORE the first page's promise resolves: awaiting the roster to
    // actually render first (as `enterSelectedMode` does) would let the auto-chain run
    // uncontrolled toward the 2000-row cap in the gap before the switch lands, which is
    // real, expensive DOM work, not a hang — but enough of it to blow the test heap.
    h.fetchUsers.mockResolvedValue(pageOf([user()], { hasMore: true, nextCursor: 'c2' }))
    render(<GlobalLimitsPanel onToast={() => {}} />)
    fireEvent.click(screen.getByTestId('mode-selected'))
    fireEvent.click(screen.getByTestId('mode-all'))
    await waitFor(() => expect(screen.getByTestId('all-users-summary')).toBeTruthy())
    const callsAfterSettle = h.fetchUsers.mock.calls.length
    await new Promise((r) => setTimeout(r, 50))
    expect(h.fetchUsers.mock.calls.length).toBe(callsAfterSettle)
  })

  it('picking a preset fills the custom/exact-value input', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await screen.findByTestId('all-users-summary')
    const select = screen.getByTestId('preset-select') as HTMLSelectElement
    fireEvent.change(select, { target: { value: '5000000' } })
    expect((screen.getByTestId('custom-value') as HTMLInputElement).value).toBe('5000000')
  })

  it('Apply is disabled until at least one user is selected (Selected mode)', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await enterSelectedMode()
    const apply = screen.getByTestId('glp-apply') as HTMLButtonElement
    expect(apply.disabled).toBe(true)
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    expect(apply.disabled).toBe(false)
  })

  it('Apply is enabled immediately in All users mode (no selection needed)', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await screen.findByTestId('all-users-summary')
    expect((screen.getByTestId('glp-apply') as HTMLButtonElement).disabled).toBe(false)
  })

  it('Apply opens a confirm step and does not call the API until confirmed', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await enterSelectedMode()
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    fireEvent.click(screen.getByTestId('glp-apply'))
    expect(h.bulkUpdateUserLimits).not.toHaveBeenCalled()
    expect(screen.getByTestId('glp-confirm')).toBeTruthy()
  })

  it('confirming applies to exactly the selected user ids and toasts the updated count', async () => {
    h.bulkUpdateUserLimits.mockResolvedValue({ updatedCount: 2 })
    const onToast = vi.fn()
    render(<GlobalLimitsPanel onToast={onToast} />)
    await enterSelectedMode()
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
    await screen.findByTestId('all-users-summary')
    fireEvent.click(screen.getByTestId('glp-apply'))
    fireEvent.click(screen.getByTestId('glp-confirm'))
    await waitFor(() => expect(h.bulkUpdateUserLimits).toHaveBeenCalled())
    expect(h.bulkUpdateUserLimits).toHaveBeenCalledWith(1000000, undefined, {})
  })

  it('a failed apply surfaces the error inline and keeps the confirm step open for retry', async () => {
    h.bulkUpdateUserLimits.mockRejectedValue(new Error('Only super-admins can do this.'))
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await enterSelectedMode()
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    fireEvent.click(screen.getByTestId('glp-apply'))
    fireEvent.click(screen.getByTestId('glp-confirm'))
    expect((await screen.findByTestId('apply-error')).textContent).toContain('super-admins')
  })

  it('select-all-loaded toggles every currently-loaded row on and off', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await enterSelectedMode()
    fireEvent.click(screen.getByTestId('select-all-loaded'))
    expect((screen.getByTestId('select-a@x.com') as HTMLInputElement).checked).toBe(true)
    expect((screen.getByTestId('select-b@x.com') as HTMLInputElement).checked).toBe(true)
    fireEvent.click(screen.getByTestId('select-all-loaded'))
    expect((screen.getByTestId('select-a@x.com') as HTMLInputElement).checked).toBe(false)
  })

  it('select-all-loaded MERGES with a hand-picked selection made under the SAME query, never replaces it', async () => {
    // The distinguishing case for merge-vs-replace has to stay within one query: once
    // a query changes, `selected` is pruned regardless (see the prune test below), so
    // that scenario can't tell merge and replace apart — only this one can.
    h.fetchUsers.mockResolvedValue(
      pageOf([user(), user({ userId: 'u2', email: 'b@x.com', displayName: 'Bob' }), user({ userId: 'u3', email: 'c@x.com', displayName: 'Carl' })]),
    )
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await enterSelectedMode()
    fireEvent.click(screen.getByTestId('select-b@x.com'))
    fireEvent.click(screen.getByTestId('select-all-loaded'))
    expect((screen.getByTestId('select-a@x.com') as HTMLInputElement).checked).toBe(true)
    expect((screen.getByTestId('select-b@x.com') as HTMLInputElement).checked).toBe(true)
    expect((screen.getByTestId('select-c@x.com') as HTMLInputElement).checked).toBe(true)
  })

  it('`selected` is PRUNED on a query change — a stale pick can never silently outlive the search that made it visible', async () => {
    h.fetchUsers.mockImplementation(async ({ q }: { q?: string }) => {
      if (q === 'ops') return pageOf([user({ userId: 'u-ops', email: 'ops@x.com', displayName: 'Ops' })])
      if (q === 'eng') return pageOf([user({ userId: 'u-eng', email: 'eng@x.com', displayName: 'Eng' })])
      return pageOf([user(), user({ userId: 'u2', email: 'b@x.com', displayName: 'Bob' })])
    })
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await enterSelectedMode()
    fireEvent.change(screen.getByTestId('glp-search'), { target: { value: 'ops' } })
    await screen.findByText('Ops')
    fireEvent.click(screen.getByTestId('select-all-loaded'))
    expect((screen.getByTestId('select-ops@x.com') as HTMLInputElement).checked).toBe(true)

    fireEvent.change(screen.getByTestId('glp-search'), { target: { value: 'eng' } })
    await screen.findByText('Eng')
    // The prior "ops" pick is no longer visible under this search — `selected` is
    // pruned on a query change (finding 3's other half) so the confirmed count can
    // never silently refer to rows the admin can't currently see or review.
    // `waitFor`, not a bare read. `findByText('Eng')` resolves when the new page renders; the
    // prune of `selected` is a separate state update that can land a tick later under load. A
    // bare assertion here passes locally and fails in CI, which is a flake rather than a finding.
    await waitFor(() => expect(screen.getByText('0 selected')).toBeTruthy())
  })

  it('a failed background page shows a retry banner and disables Select-all/Apply — never renders nothing', async () => {
    let rejectPage2: (e: Error) => void = () => {}
    h.fetchUsers
      .mockResolvedValueOnce(pageOf([user()], { hasMore: true, nextCursor: 'c2' }))
      .mockImplementationOnce(() => new Promise((_, reject) => { rejectPage2 = reject }))
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await enterSelectedMode()
    rejectPage2(new Error('Network blip'))

    const banner = await screen.findByTestId('loadmore-error')
    expect(banner.textContent).toContain('Network blip')
    expect((screen.getByTestId('select-all-loaded') as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByTestId('glp-apply') as HTMLButtonElement).disabled).toBe(true)
  })

  it('the roster refreshes after a successful apply', async () => {
    h.bulkUpdateUserLimits.mockResolvedValue({ updatedCount: 1 })
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await enterSelectedMode()
    h.fetchUsers.mockClear()
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    fireEvent.click(screen.getByTestId('glp-apply'))
    fireEvent.click(screen.getByTestId('glp-confirm'))
    await waitFor(() => expect(h.fetchUsers).toHaveBeenCalled())
  })

  it('switching the preset while confirming closes the confirm step rather than showing a stale/invalid value', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await enterSelectedMode()
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    fireEvent.click(screen.getByTestId('glp-apply'))
    expect(screen.getByTestId('glp-confirm')).toBeTruthy()
    fireEvent.change(screen.getByTestId('preset-select'), { target: { value: 'custom' } })
    expect(screen.queryByTestId('glp-confirm')).toBeNull()
  })

  it('a rejected custom value shows an inline hint instead of silently greying out Apply', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await screen.findByTestId('all-users-summary')
    fireEvent.change(screen.getByTestId('preset-select'), { target: { value: 'custom' } })
    fireEvent.change(screen.getByTestId('custom-value'), { target: { value: '1e6' } })
    expect((await screen.findByTestId('glp-value-hint')).textContent).toMatch(/digits only/i)
  })

  it('a post-unmount apply resolution is silently dropped, not surfaced (dead-instance setState is guarded)', async () => {
    let resolveApply: (v: { updatedCount: number }) => void = () => {}
    h.bulkUpdateUserLimits.mockImplementation(() => new Promise((resolve) => { resolveApply = resolve }))
    const onToast = vi.fn()
    const { unmount } = render(<GlobalLimitsPanel onToast={onToast} />)
    await enterSelectedMode()
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    fireEvent.click(screen.getByTestId('glp-apply'))
    fireEvent.click(screen.getByTestId('glp-confirm'))
    unmount()
    // Not just "doesn't throw" (which can't fail regardless of the guard) — the guard's
    // actual job is to stop the dead instance from acting on a late resolution at all.
    resolveApply({ updatedCount: 1 })
    await new Promise((r) => setTimeout(r, 0))
    expect(onToast).not.toHaveBeenCalled()
  })

  it('apply completes normally under React StrictMode — isMountedRef resets on the simulated remount, not stuck false forever', async () => {
    // StrictMode (unconditional in main.tsx) mounts, cleans up, and mounts again. A
    // cleanup-only `isMountedRef.current = false` with no matching `= true` on mount
    // would read false for the panel's entire real lifetime from then on — every
    // apply would POST successfully but bail before onToast/setConfirming(false),
    // stranding the admin on a spinning confirm banner with no route back.
    h.bulkUpdateUserLimits.mockResolvedValue({ updatedCount: 1 })
    const onToast = vi.fn()
    render(
      <StrictMode>
        <GlobalLimitsPanel onToast={onToast} />
      </StrictMode>,
    )
    await enterSelectedMode()
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    fireEvent.click(screen.getByTestId('glp-apply'))
    fireEvent.click(screen.getByTestId('glp-confirm'))

    await waitFor(() => expect(onToast).toHaveBeenCalled())
    expect(screen.queryByTestId('glp-confirm')).toBeNull()
  })

  it('canApply stays false while `isPartial` — a background-page failure blocks Apply even with users selected', async () => {
    let rejectPage2: (e: Error) => void = () => {}
    h.fetchUsers
      .mockResolvedValueOnce(pageOf([user()], { hasMore: true, nextCursor: 'c2' }))
      .mockImplementationOnce(() => new Promise((_, reject) => { rejectPage2 = reject }))
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await enterSelectedMode()
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    expect((screen.getByTestId('glp-apply') as HTMLButtonElement).disabled).toBe(false)

    rejectPage2(new Error('Network blip'))
    await screen.findByTestId('loadmore-error')
    expect((screen.getByTestId('glp-apply') as HTMLButtonElement).disabled).toBe(true)
  })

  it('"Yes, apply" disables if the selection drops to zero while the confirm step is still open', async () => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await enterSelectedMode()
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    fireEvent.click(screen.getByTestId('glp-apply'))
    expect((screen.getByTestId('glp-confirm') as HTMLButtonElement).disabled).toBe(false)

    // toggleOne doesn't call setConfirming(false) — unlike the mode buttons and the
    // preset/custom-value editors, unticking a row leaves the confirm banner open.
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    expect(screen.getByTestId('glp-confirm')).toBeTruthy()
    expect((screen.getByTestId('glp-confirm') as HTMLButtonElement).disabled).toBe(true)
  })

  it('isCapped shows once the loaded roster reaches MAX_LOADED_USERS, with the background chain stopped', async () => {
    // 20 pages of 100 to reach the 2000-row cap, hasMore: true throughout (the server
    // always claims there's more — it's the CLIENT-side cap that has to stop it).
    // Deliberately NOT waited on via `waitFor`'s default polling: re-scanning a
    // 2000-row DOM every 50ms for up to a minute is expensive enough to blow the
    // worker's heap. A promise resolved by the mock itself on the 20th call needs
    // exactly one wait, not hundreds of re-checks.
    let call = 0
    let resolveDone: () => void = () => {}
    const allPagesRequested = new Promise<void>((res) => { resolveDone = res })
    h.fetchUsers.mockImplementation(async () => {
      call += 1
      const page = Array.from({ length: 100 }, (_, i) => {
        const n = (call - 1) * 100 + i
        return user({ userId: `cap-${n}`, email: `cap${n}@x.com`, displayName: `Cap ${n}` })
      })
      if (call >= 20) resolveDone()
      return pageOf(page, { hasMore: true, nextCursor: `c${call + 1}` })
    })
    render(<GlobalLimitsPanel onToast={() => {}} />)
    fireEvent.click(screen.getByTestId('mode-selected'))
    await allPagesRequested
    // One settle wait for the 20th page's state update + render to land, then a
    // single check — not a repeated scan.
    expect(await screen.findByText(/Showing the first 2,000 users/i, {}, { timeout: 5000 })).toBeTruthy()
    // The chain must actually stop at the cap, not keep issuing requests forever.
    const callsAtCap = call
    await new Promise((r) => setTimeout(r, 50))
    expect(call).toBe(callsAtCap)
    // 45s, not 20s. This spec renders 2,000 rows into jsdom — the cap is the thing under
    // test, so the row count cannot be reduced — and it runs alongside 78 other files
    // competing for the same cores. At 20s it sat right on its own budget and tipped over
    // intermittently, producing a red that named this test and went green on re-run. The
    // work is bounded and the assertions are unchanged; only the wall-clock allowance moved,
    // so a genuine regression (a chain that never stops) still fails, just later.
  }, 45000)
})

describe('isPlainPositiveInteger (via the Exact value input)', () => {
  const cases: [string, boolean][] = [
    ['1e3', false],
    [' 100', false],
    ['+100', false],
    ['1.5', false],
    ['0100', false],
    ['', false],
    ['0', false],
    ['-5', false],
    ['1', true],
    ['250000', true],
  ]

  it.each(cases)('%s is valid=%s', async (raw, valid) => {
    render(<GlobalLimitsPanel onToast={() => {}} />)
    await enterSelectedMode()
    fireEvent.change(screen.getByTestId('preset-select'), { target: { value: 'custom' } })
    fireEvent.change(screen.getByTestId('custom-value'), { target: { value: raw } })
    fireEvent.click(screen.getByTestId('select-a@x.com'))
    expect((screen.getByTestId('glp-apply') as HTMLButtonElement).disabled).toBe(!valid)
  })
})
