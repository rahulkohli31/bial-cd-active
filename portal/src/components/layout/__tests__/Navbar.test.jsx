/**
 * N4 — the daily-token meter has to be BOTH live and visible.
 *
 * Two halves of one regression, both introduced on this branch. F7 removed the in-rail meter on
 * the grounds that "the header already shows real usage" — but the header's badge was
 * `hidden md:flex`, so below 768px there was no usage feedback anywhere at all; and the header
 * only ever refetched on mount, because `notifyUsageChanged` had exactly one caller in the
 * retiring relay hook and the turn transport never signalled. Between them a citizen could spend
 * their entire daily budget watching a number that never moved — or that was not on screen.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({
  fetchUsageToday: vi.fn(),
  onUsageChanged: vi.fn(),
  isAuthenticated: vi.fn(() => true),
  getStoredUser: vi.fn(() => ({ email: 'asha@rvaiglobal.com', display_name: 'Asha' })),
  logout: vi.fn(),
  fetchAppStatusCounts: vi.fn(),
}))

vi.mock('../../../utils/usage', () => ({
  fetchUsageToday: h.fetchUsageToday,
  onUsageChanged: h.onUsageChanged,
}))
vi.mock('../../../utils/auth', () => ({
  isAuthenticated: h.isAuthenticated,
  getStoredUser: h.getStoredUser,
  logout: h.logout,
}))
vi.mock('../../../utils/attachmentApi', () => ({ revokeAllAttachmentUrls: vi.fn() }))
vi.mock('../../../utils/appRegistryApi', () => ({ fetchAppStatusCounts: h.fetchAppStatusCounts }))
vi.mock('../../FeedbackModal', () => ({ default: () => null }))

import Navbar from '../Navbar'

// Deliberately NOT named "Admin": the display name is rendered in the avatar block, and a
// `getByText('Admin')` on the nav entry would then match two nodes.
const ADMIN = { email: 'admin@bial.com', display_name: 'Priya', isAdmin: true }
const counts = (pending) => ({ draft: 0, pending, approved: 0, rejected: 0, disabled: 0 })

/** The subscriber the Navbar hands `onUsageChanged`, so a test can fire the signal itself. */
let subscriber = null

beforeEach(() => {
  vi.clearAllMocks()
  subscriber = null
  h.isAuthenticated.mockReturnValue(true)
  h.getStoredUser.mockReturnValue({ email: 'asha@rvaiglobal.com', display_name: 'Asha' })
  h.fetchUsageToday.mockResolvedValue({ used: 12_345, limit: 50_000, remaining: 37_655 })
  h.fetchAppStatusCounts.mockResolvedValue(counts(0))
  h.onUsageChanged.mockImplementation((fn) => {
    subscriber = fn
    return () => {
      subscriber = null
    }
  })
})
afterEach(() => cleanup())

const renderNavbar = () =>
  render(
    <MemoryRouter>
      <Navbar />
    </MemoryRouter>,
  )

describe('the usage meter is visible on a narrow screen (N4)', () => {
  it('THE BUG: the meter is never hidden behind a breakpoint', async () => {
    // jsdom has no viewport-driven CSS, so the honest assertion is on the MECHANISM: a
    // `hidden md:flex` container is unreachable below 768px no matter what the media query
    // would do. Mutation-check: restore `hidden md:flex` and this goes red.
    renderNavbar()
    const meter = await screen.findByTestId('usage-meter')
    expect(meter.className).not.toMatch(/(^|\s)hidden(\s|$)/)
    expect(meter.className).toMatch(/(^|\s)flex(\s|$)/)
  })

  it('states the same fact at both widths — compact on small, full on md and up', async () => {
    renderNavbar()
    const meter = await screen.findByTestId('usage-meter')
    // Both readings are rendered; CSS picks one. The compact one must still be a real reading
    // of the same numbers, not a bare bar with no figures.
    expect(meter.textContent).toMatch(/12\.3K\s*\/\s*50K/)
    expect(meter.textContent).toMatch(/12,345 \/ 50,000 tokens/)
  })

  it('turns danger when the budget is spent, at either width', async () => {
    h.fetchUsageToday.mockResolvedValue({ used: 50_000, limit: 50_000, remaining: 0 })
    renderNavbar()
    const meter = await screen.findByTestId('usage-meter')
    expect(meter.querySelector('.text-danger')).not.toBeNull()
  })
})

describe('the meter settles without a reload (N4)', () => {
  it('subscribes to the usage-changed signal and refetches when it fires', async () => {
    renderNavbar()
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalledTimes(1))
    expect(subscriber).toBeTypeOf('function')

    h.fetchUsageToday.mockResolvedValue({ used: 20_000, limit: 50_000, remaining: 30_000 })
    subscriber()

    await waitFor(() => expect(screen.getByTestId('usage-meter').textContent).toMatch(/20,000 \/ 50,000/))
    expect(h.fetchUsageToday).toHaveBeenCalledTimes(2)
  })

  it('unsubscribes on unmount — a fired signal must not touch a dead component', async () => {
    const { unmount } = renderNavbar()
    await waitFor(() => expect(h.onUsageChanged).toHaveBeenCalled())
    unmount()
    expect(subscriber).toBeNull()
  })

  it('hides the meter entirely when the session is gone, rather than showing a stale budget', async () => {
    h.isAuthenticated.mockReturnValue(false)
    renderNavbar()
    await waitFor(() => expect(h.onUsageChanged).toHaveBeenCalled())
    expect(screen.queryByTestId('usage-meter')).toBeNull()
    expect(h.fetchUsageToday).not.toHaveBeenCalled()
  })
})

/**
 * U13/P1 — the waiting count an administrator cannot miss.
 *
 * The badge sits on the admin nav entry so a superadmin sees the queue WITHOUT navigating
 * into it, carries a real accessible name rather than a bare numeral, and is not even
 * REQUESTED for anyone else (the route is superadmin-only; asking would spend a request
 * to earn a 403 in every citizen's console).
 */
describe('the waiting-count badge (P1)', () => {
  it('renders the pending count on the admin entry, with an accessible name', async () => {
    h.getStoredUser.mockReturnValue(ADMIN)
    h.fetchAppStatusCounts.mockResolvedValue(counts(7))
    renderNavbar()

    const badge = await screen.findByTestId('waiting-count-nav')
    expect(badge.textContent).toContain('7')
    // Not a bare number to a screen reader: the numeral is aria-hidden and the real name
    // is the sentence beside it.
    expect(screen.getByText('7 apps waiting for review')).toBeTruthy()
    expect(badge.querySelector('[aria-hidden="true"]').textContent).toBe('7')
    // …and it hangs off the ADMIN entry, not off Projects or Help.
    expect(badge.closest('a').getAttribute('href')).toBe('/admin')
  })

  it('re-reads the count when the tab comes back — a fetch-once badge goes stale', async () => {
    // The queue moves underneath this badge (an admin approves, a citizen withdraws, a
    // drifted version routes). Fetching once per mount left the nav badge contradicting
    // the registry panel's own badge two inches away, which its contract forbids.
    h.getStoredUser.mockReturnValue(ADMIN)
    h.fetchAppStatusCounts.mockResolvedValue(counts(7))
    renderNavbar()
    await screen.findByTestId('waiting-count-nav')

    h.fetchAppStatusCounts.mockResolvedValue(counts(2))
    fireEvent.focus(window)

    await waitFor(() => {
      expect(screen.getByTestId('waiting-count-nav').textContent).toContain('2')
    })
  })

  it('says "1 app", not "1 apps"', async () => {
    h.getStoredUser.mockReturnValue(ADMIN)
    h.fetchAppStatusCounts.mockResolvedValue(counts(1))
    renderNavbar()
    await screen.findByTestId('waiting-count-nav')
    expect(screen.getByText('1 app waiting for review')).toBeTruthy()
  })

  it('disappears entirely at zero — a "0" badge trains you to ignore the badge', async () => {
    h.getStoredUser.mockReturnValue(ADMIN)
    h.fetchAppStatusCounts.mockResolvedValue(counts(0))
    renderNavbar()
    await waitFor(() => expect(h.fetchAppStatusCounts).toHaveBeenCalled())
    expect(screen.queryByTestId('waiting-count-nav')).toBeNull()
    // Liveness: the entry itself IS on screen, so the absence above is about the badge
    // and not about a component that failed to render at all.
    expect(screen.getByText('Admin')).toBeTruthy()
  })

  it('shows no number when the count could not be fetched', async () => {
    h.getStoredUser.mockReturnValue(ADMIN)
    h.fetchAppStatusCounts.mockRejectedValue(new Error('nope'))
    renderNavbar()
    await waitFor(() => expect(h.fetchAppStatusCounts).toHaveBeenCalled())
    expect(screen.queryByTestId('waiting-count-nav')).toBeNull()
    expect(screen.getByText('Admin')).toBeTruthy()
  })

  it('a NON-ADMIN gets no admin entry, no badge, and never requests the count route', async () => {
    h.getStoredUser.mockReturnValue({ email: 'asha@rvaiglobal.com', display_name: 'Asha' })
    h.fetchAppStatusCounts.mockResolvedValue(counts(7))
    renderNavbar()
    // Liveness first: the navbar really rendered, so the three absences below mean
    // something (a crashed component would "pass" all three).
    await screen.findByText('Projects')
    expect(screen.queryByText('Admin')).toBeNull()
    expect(screen.queryByTestId('waiting-count-nav')).toBeNull()
    expect(h.fetchAppStatusCounts).not.toHaveBeenCalled()
  })

  it('does not ask for the count when the session is gone', async () => {
    h.getStoredUser.mockReturnValue(ADMIN)
    h.isAuthenticated.mockReturnValue(false)
    renderNavbar()
    await waitFor(() => expect(h.onUsageChanged).toHaveBeenCalled())
    expect(h.fetchAppStatusCounts).not.toHaveBeenCalled()
  })
})


/**
 * The avatar menu's state machine.
 *
 * #157 A removed three of the four header dropdowns, which left a `DropdownName` union with one
 * member and a `toggle(name)` comparing it against itself; a later commit collapsed both into a
 * boolean. Nothing in this file covered the open/close behaviour either before or after, so the
 * rewrite rested entirely on a manual browser pass. These are the three ways the menu closes.
 */
describe('the avatar menu opens and closes (#157 A)', () => {
  // Resolved ONCE, while the menu is closed, and the node is reused afterwards. Two traps
  // here, both of which bit during #157:
  //   - `getAllByRole('button').at(-1)` re-resolves, and once the menu is open the last
  //     button in the nav IS "Sign out" — the browser harness clicked it and logged itself
  //     out mid-run, then asserted the rest against a login page.
  //   - the display name is rendered TWICE while the menu is open (trigger + menu header),
  //     so even a name-based query is ambiguous after the first click.
  // React keeps the same DOM node for the trigger across these re-renders, so holding the
  // reference is both stable and unambiguous.
  const openMenu = () => {
    const trigger = screen.getByText('Asha', { selector: 'p' }).closest('button')
    fireEvent.click(trigger)
    return trigger
  }
  const menuIsOpen = () => screen.queryByRole('button', { name: /sign out/i }) !== null

  it('starts closed, opens on click, and the same click closes it again', async () => {
    renderNavbar()
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalled())

    expect(menuIsOpen()).toBe(false)
    const trigger = openMenu()
    expect(menuIsOpen()).toBe(true)
    // The toggle half. A handler that only ever SET the flag would pass the line above and
    // fail here — which is the mutation the collapse could plausibly have introduced.
    fireEvent.click(trigger)
    expect(menuIsOpen()).toBe(false)
  })

  it('closes on Escape', async () => {
    renderNavbar()
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalled())

    openMenu()
    expect(menuIsOpen()).toBe(true)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(menuIsOpen()).toBe(false)
  })

  it('closes on a click outside the nav, but not on a click inside it', async () => {
    renderNavbar()
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalled())

    const trigger = openMenu()
    // Inside first: useClickOutside listens on `mousedown` at the document, so a nav-local
    // mousedown must NOT close the menu the click that follows is trying to use.
    fireEvent.mouseDown(trigger)
    expect(menuIsOpen()).toBe(true)

    fireEvent.mouseDown(document.body)
    expect(menuIsOpen()).toBe(false)
  })

  it('closes when Feedback opens, instead of sitting behind the modal', async () => {
    // Pre-existing gap the dropdown union had too: the Feedback button lives OUTSIDE the
    // menu, so opening the modal left the menu rendered underneath it (#157 review).
    renderNavbar()
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalled())

    openMenu()
    expect(menuIsOpen()).toBe(true)
    fireEvent.click(screen.getByTitle('Send feedback'))
    expect(menuIsOpen()).toBe(false)
  })
})
