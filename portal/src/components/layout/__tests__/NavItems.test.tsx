/**
 * The admin entry's waiting count follows the review queue in THIS tab. An administrator who
 * approves the last waiting app never leaves the tab, so focus and visibility never fire, and the
 * nav would go on saying one app is waiting.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type * as appRegistryApi from '../../../utils/appRegistryApi'

const h = vi.hoisted(() => ({ fetchAppStatusCounts: vi.fn() }))

vi.mock('../../../utils/auth', () => ({
  isAuthenticated: () => true,
  getStoredUser: () => ({ email: 'priya@bial.aero', display_name: 'Priya Nair', isAdmin: true }),
}))
vi.mock('../../../utils/appRegistryApi', async (importOriginal) => ({
  ...(await importOriginal<typeof appRegistryApi>()),
  fetchAppStatusCounts: h.fetchAppStatusCounts,
}))

import NavItems from '../NavItems'
import { announceReviewQueueChanged } from '../../../utils/appRegistryApi'

const counts = (pending: number) => ({ draft: 0, pending, approved: 0, rejected: 0, disabled: 0 })

beforeEach(() => {
  vi.clearAllMocks()
  h.fetchAppStatusCounts.mockResolvedValueOnce(counts(1)).mockResolvedValue(counts(0))
})
afterEach(cleanup)

const mount = () =>
  render(
    <MemoryRouter>
      <NavItems />
    </MemoryRouter>,
  )

describe('the waiting count follows the review queue in this tab', () => {
  it('reads the count again when the queue changes, and drops a badge that is no longer true', async () => {
    mount()
    expect((await screen.findByTestId('waiting-count-nav')).textContent).toContain('1')

    act(() => announceReviewQueueChanged())

    await waitFor(() => expect(h.fetchAppStatusCounts).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByTestId('waiting-count-nav')).toBeNull())
    // Liveness: the entry is still drawn, so the badge went because the count is zero.
    expect(screen.getByTestId('nav-admin')).toBeTruthy()
  })

  it('keeps the newest count when two reads overlap and the older one answers last', async () => {
    let answerOlder: (value: ReturnType<typeof counts>) => void = () => {}
    h.fetchAppStatusCounts.mockReset()
    h.fetchAppStatusCounts
      .mockResolvedValueOnce(counts(2))
      .mockReturnValueOnce(new Promise((resolve) => { answerOlder = resolve }))
      .mockResolvedValueOnce(counts(0))
    mount()
    expect((await screen.findByTestId('waiting-count-nav')).textContent).toContain('2')

    act(() => announceReviewQueueChanged())
    act(() => announceReviewQueueChanged())
    await waitFor(() => expect(screen.queryByTestId('waiting-count-nav')).toBeNull())
    await act(async () => answerOlder(counts(1)))

    expect(screen.getByTestId('nav-admin')).toBeTruthy()
    expect(screen.queryByTestId('waiting-count-nav')).toBeNull()
  })

  it('stops listening once it unmounts', async () => {
    const nav = mount()
    await screen.findByTestId('waiting-count-nav')
    nav.unmount()

    act(() => announceReviewQueueChanged())
    await act(async () => {})

    expect(h.fetchAppStatusCounts).toHaveBeenCalledTimes(1)
  })
})
