/**
 * The daily-token meter has to be BOTH live and visible.
 *
 * Two halves of one regression, both introduced on this branch. An earlier version removed the
 * in-rail meter on the grounds that "the header already shows real usage" — but the header's
 * badge was `hidden md:flex`, so below 768px there was no usage feedback anywhere at all; and the header
 * only ever refetched on mount, because `notifyUsageChanged` had exactly one caller in the
 * retiring relay hook and the turn transport never signalled. Between them a citizen could spend
 * their entire daily budget watching a number that never moved — or that was not on screen.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup, fireEvent, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom'

const h = vi.hoisted(() => ({
  fetchUsageToday: vi.fn(),
  onUsageChanged: vi.fn(),
  isAuthenticated: vi.fn(() => true),
  getStoredUser: vi.fn(() => ({ email: 'asha@rvaiglobal.com', display_name: 'Asha' })),
  logout: vi.fn(),
  fetchAppStatusCounts: vi.fn(),
  listConnectors: vi.fn(),
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
// The Integrations dialog the profile menu opens is REAL here, not stubbed — the point of the
// suite below is that the menu entry opens the actual dialog on whatever screen the navbar is
// mounted on. Only its network boundary is mocked.
// SPREAD THE ORIGINAL. A bare factory silently drops every export it does not name, and the
// dialog's close path calls `notifyConnectorsChanged` — so listing only the three fetchers made
// closing throw, and the failure read as "the dialog would not close" rather than "the mock is
// incomplete". The signal itself is a real window event with no network behind it; there is
// nothing to stub.
vi.mock('../../../utils/connectorApi', async (importOriginal) => ({
  ...(await importOriginal()),
  listConnectors: h.listConnectors,
  requestConnectorAccess: vi.fn(),
  cancelConnectorRequest: vi.fn(),
}))
vi.mock('../../FeedbackModal', () => ({ default: () => null }))

import Navbar from '../Navbar'
import { WorkspaceExitProvider } from '../../workspace/UnsavedWorkGuard'

/** Where a navigation actually landed. */
function LocationProbe() {
  return <span data-testid="where">{useLocation().pathname}</span>
}

// Deliberately NOT named "Admin": the display name is rendered in the avatar block, and a
// `getByText('Admin')` on the nav entry would then match two nodes.
const ADMIN = { email: 'admin@bial.com', display_name: 'Priya', isAdmin: true }
const counts = (pending) => ({ draft: 0, pending, approved: 0, rejected: 0, disabled: 0 })

/** One connector as the wire sends it. Named by the suite, never by the component. */
const CONNECTOR = {
  key: 'orbit',
  displayName: 'ORBIT',
  subtitle: 'Airport operations',
  askSubtitle: 'ORBIT is what this suite calls its connector. An administrator answers once.',
  consentLinesRequester: [
    { lead: 'Read-only.', body: 'Nothing you build can change ORBIT data.' },
  ],
  state: 'neverAsked',
  askedAt: null,
  approvedAt: null,
  approvedByName: null,
  onProjectCount: null,
  decidedAt: null,
  decidedByName: null,
  decisionRemarks: null,
}

/** The subscriber the Navbar hands `onUsageChanged`, so a test can fire the signal itself. */
let subscriber = null

beforeEach(() => {
  vi.clearAllMocks()
  subscriber = null
  h.isAuthenticated.mockReturnValue(true)
  h.getStoredUser.mockReturnValue({ email: 'asha@rvaiglobal.com', display_name: 'Asha' })
  h.fetchUsageToday.mockResolvedValue({ used: 12_345, limit: 50_000, remaining: 37_655 })
  h.fetchAppStatusCounts.mockResolvedValue(counts(0))
  h.listConnectors.mockResolvedValue([CONNECTOR])
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

describe('the usage meter is visible on a narrow screen', () => {
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

describe('the navbar matches what the design board draws, without losing any existing feature', () => {
  it('the meter is amber well below any 80% threshold, because the board draws it amber at 54%', async () => {
    // The board's own worked example is `537,102 / 1,000,000 tokens` over a 54%-wide `--amb`
    // fill. The code turned the bar amber only past 80%, so at the board's own figures it
    // painted teal. Mutation-check: restore `pct >= 80 ? 'bg-accent' : 'bg-primary'` and the
    // 24.7% reading below goes teal and this goes red.
    h.fetchUsageToday.mockResolvedValue({ used: 12_345, limit: 50_000, remaining: 37_655 })
    renderNavbar()
    const meter = await screen.findByTestId('usage-meter')
    expect(meter.querySelector('.bg-accent')).not.toBeNull()
    expect(meter.querySelector('.bg-primary')).toBeNull()
  })

  it('keeps danger for a budget that is actually spent', async () => {
    h.fetchUsageToday.mockResolvedValue({ used: 50_000, limit: 50_000, remaining: 0 })
    renderNavbar()
    const meter = await screen.findByTestId('usage-meter')
    expect(meter.querySelector('.bg-danger')).not.toBeNull()
    expect(meter.querySelector('.bg-accent')).toBeNull()
  })

  it('draws the meter as an outlined pill with a visible unspent remainder', async () => {
    // The board: `border:1px solid var(--bd);border-radius:999px` and NO background, over a
    // 3px `--bd` track. It was a #F8F9FA chip with a `bg-white` track, which made the unspent
    // part of the budget invisible against the white header behind it.
    renderNavbar()
    const meter = await screen.findByTestId('usage-meter')
    expect(meter.className).toMatch(/border-bial-border/)
    expect(meter.className).not.toMatch(/bg-surface-muted/)
    expect(meter.querySelector('.bg-bial-border')).not.toBeNull()
  })

  it('renders the wordmark in the brand teal at the board\'s size and weight', async () => {
    renderNavbar()
    const wordmark = await screen.findByText('BIAL Citizen Developer')
    expect(wordmark.className).toMatch(/text-primary(?![-\w])/)
    expect(wordmark.className).toMatch(/text-\[15px\]/)
    expect(wordmark.className).toMatch(/font-extrabold/)
    // What it must NOT be: the 18px/700 #00818A it shipped as — a teal that is not the brand
    // teal and that no board draws.
    expect(wordmark.getAttribute('style')).toBeNull()
  })

  it('gives the active nav item weight and ink, and no rule and no brand colour', async () => {
    // The board draws the active item `color:#1A2B34;font-weight:600` at its siblings' size,
    // against `color:#6B7280;font-weight:500`. The code added teal, bold AND a 2px underline.
    render(
      <MemoryRouter initialEntries={['/projects']}>
        <Navbar />
      </MemoryRouter>,
    )
    const active = await screen.findByRole('link', { name: 'Projects' })
    expect(active.className).toMatch(/text-primary-900/)
    expect(active.className).not.toMatch(/border-b-2/)
    expect(active.className).not.toMatch(/text-primary(?![-\w])/)

    const inactive = screen.getByRole('link', { name: 'Help' })
    expect(inactive.className).toMatch(/text-neutral/)
    // Same size on both — the board changes weight, never scale.
    expect(active.className).toMatch(/text-sm/)
    expect(inactive.className).toMatch(/text-sm/)
  })

  it('keeps every navbar feature the boards predate', async () => {
    // The origin is explicit: do not delete a shipped feature because an older board omits it.
    // Only the styling the boards DO specify is corrected.
    h.getStoredUser.mockReturnValue(ADMIN)
    h.fetchAppStatusCounts.mockResolvedValue(counts(3))
    renderNavbar()
    expect(await screen.findByRole('link', { name: 'Marketplace' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Feedback' })).toBeTruthy()
    expect(await screen.findByText('3 apps waiting for review')).toBeTruthy()
    // The avatar menu is Radix now: it opens on POINTERDOWN, and `Sign out` is a
    // `role="menuitem"`, not a button.
    fireEvent.pointerDown(screen.getByText('Priya', { selector: 'p' }).closest('button'))
    expect(await screen.findByRole('menuitem', { name: /sign out/i })).toBeTruthy()
  })
})

describe('the meter settles without a reload', () => {
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
 * The waiting count an administrator cannot miss.
 *
 * The badge sits on the admin nav entry so a superadmin sees the queue WITHOUT navigating
 * into it, carries a real accessible name rather than a bare numeral, and is not even
 * REQUESTED for anyone else (the route is superadmin-only; asking would spend a request
 * to earn a 403 in every citizen's console).
 */
describe('the waiting-count badge is accurate, accessible, and admin-only', () => {
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
 * The avatar menu's state machine — these are the ways it closes.
 *
 * IT IS A RADIX `DropdownMenu` NOW, and that changes what a test has to do rather than what a
 * citizen sees. Three mechanical consequences, all of which turn a stale test red:
 *   - the trigger opens on POINTERDOWN, not click, so `fireEvent.click` no longer opens it
 *     (`fireEvent.pointerDown` is the in-repo pattern — see `RailResizeHandle.test.tsx`);
 *   - `Sign out` carries `role="menuitem"`, which wins over the underlying element, so
 *     `getByRole('button', { name: /sign out/i })` finds nothing;
 *   - `DismissableLayer` attaches its document `pointerdown` listener on a MACROTASK after the
 *     menu mounts, so a press fired in the same tick as the open lands before it is armed.
 * `@testing-library/user-event` would smooth over the first two — it is in neither package.json,
 * the lockfile, nor node_modules, so it is not an option here.
 */
describe('the avatar menu opens and closes', () => {
  // Resolved ONCE, while the menu is closed, and the node is reused afterwards. Two traps
  // here, both of which bit:
  //   - `getAllByRole('button').at(-1)` re-resolves, and once the menu is open the last
  //     button in the nav IS "Sign out" — the browser harness clicked it and logged itself
  //     out mid-run, then asserted the rest against a login page.
  //   - the display name is rendered TWICE while the menu is open (trigger + menu header),
  //     so even a name-based query is ambiguous after the first click.
  // React keeps the same DOM node for the trigger across these re-renders, so holding the
  // reference is both stable and unambiguous.
  const openMenu = () => {
    const trigger = screen.getByText('Asha', { selector: 'p' }).closest('button')
    fireEvent.pointerDown(trigger)
    return trigger
  }
  const menuIsOpen = () => screen.queryByRole('menuitem', { name: /sign out/i }) !== null
  /** Let Radix's dismissable layer finish arming — see the docblock. */
  const settle = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })

  it('starts closed, opens on the trigger, and the same trigger closes it again', async () => {
    renderNavbar()
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalled())

    expect(menuIsOpen()).toBe(false)
    const trigger = openMenu()
    await waitFor(() => expect(menuIsOpen()).toBe(true))
    await settle()

    // The toggle half. A handler that only ever SET the open flag would pass the line above
    // and fail here — the mutation an `open`/`onOpenChange` rewiring can plausibly introduce.
    fireEvent.pointerDown(trigger)
    await waitFor(() => expect(menuIsOpen()).toBe(false))
  })

  it('closes on Escape', async () => {
    renderNavbar()
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalled())

    openMenu()
    await waitFor(() => expect(menuIsOpen()).toBe(true))
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(menuIsOpen()).toBe(false))
  })

  it('closes on a pointer press outside it, but not on one inside the menu itself', async () => {
    renderNavbar()
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalled())

    openMenu()
    await waitFor(() => expect(menuIsOpen()).toBe(true))
    await settle()

    // Inside first. The menu content is PORTALLED — it is not inside `<nav>` any more — so
    // "inside" is now the menu's own subtree, which `DismissableLayer` marks on the capture
    // phase. A press on the menu's own header must not dismiss the menu it belongs to.
    // (The old scope was "inside the nav", enforced by a hand-rolled `useClickOutside`; with
    // the layer owning dismissal, a press elsewhere in the nav closes the menu too, which is
    // what every other menu in the product does.)
    fireEvent.pointerDown(screen.getByTestId('user-menu-identity'))
    await settle()
    expect(menuIsOpen()).toBe(true)

    fireEvent.pointerDown(document.body)
    await waitFor(() => expect(menuIsOpen()).toBe(false))
  })

  it('closes when Feedback opens, instead of sitting behind the modal, in ONE click', async () => {
    // Pre-existing gap the dropdown union had too: the Feedback button lives OUTSIDE the
    // menu, so opening the modal left the menu rendered underneath it.
    //
    // A BARE `click`, ON PURPOSE — no pointerdown. A real click carries one and Radix would
    // dismiss on that alone, which would make this test green with or without the button's own
    // `setUserMenuOpen(false)`. Firing only the click leaves the button's own close as the only
    // thing that can pass it.
    //
    // AND `getByRole`, NOT `getByTitle`: a modal Radix menu puts `aria-hidden` on everything
    // outside itself, so this query is also what catches a lost `modal={false}` — the prop that
    // keeps the first press on Feedback from being spent on dismissing the menu.
    renderNavbar()
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalled())

    openMenu()
    await waitFor(() => expect(menuIsOpen()).toBe(true))
    await settle()

    fireEvent.click(screen.getByRole('button', { name: 'Feedback' }))
    await waitFor(() => expect(menuIsOpen()).toBe(false))
  })

  it('moves focus between items with the arrow keys — what the swap is for', async () => {
    // Roving focus is the accessibility the hand-rolled menu had no way to get: opening with
    // ArrowDown must land focus ON an item, not leave it on the trigger — and ArrowDown again
    // must MOVE it, which is the half a one-item menu could never demonstrate.
    renderNavbar()
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalled())

    const trigger = screen.getByText('Asha', { selector: 'p' }).closest('button')
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })

    const first = await screen.findByRole('menuitem', { name: /integrations/i })
    await waitFor(() => expect(document.activeElement).toBe(first))

    fireEvent.keyDown(first, { key: 'ArrowDown' })
    const second = screen.getByRole('menuitem', { name: /sign out/i })
    await waitFor(() => expect(document.activeElement).toBe(second))
  })
})

/**
 * THE ONLY NEW DOOR (R5). Integrations is not a route and not a Settings link: it is an entry in
 * the profile menu that opens a dialog over whatever screen the citizen is standing on. The
 * `OpenIt` board's two annotations are requirements, and both are asserted here.
 */
describe('the profile menu opens Integrations', () => {
  const openMenu = () => {
    const trigger = screen.getByText('Asha', { selector: 'p' }).closest('button')
    fireEvent.pointerDown(trigger)
    return trigger
  }

  it('puts the entry between the identity header and Sign out, and adds no route and no Settings link', async () => {
    renderNavbar()
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalled())
    openMenu()

    const items = await screen.findAllByRole('menuitem')
    expect(items.map((item) => item.textContent)).toEqual(['Integrations', 'Sign out'])

    // The board's first annotation: no Settings link in the top bar.
    expect(screen.queryByRole('link', { name: /settings/i })).toBeNull()
    // Liveness for that absence — the top bar really rendered its own links.
    expect(screen.getByRole('link', { name: 'Marketplace' })).toBeTruthy()
  })

  it('opens the dialog over the current screen, with the connector list in it', async () => {
    renderNavbar()
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalled())
    openMenu()

    fireEvent.click(await screen.findByRole('menuitem', { name: /integrations/i }))

    expect(await screen.findByTestId('integrations-dialog')).toBeTruthy()
    expect(await screen.findByText('ORBIT')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Request access' })).toBeTruthy()
    // Still the same screen underneath — a dialog, not a navigation. Queried by TEXT, not by
    // role: a modal Radix dialog puts `aria-hidden` on everything outside itself, so the navbar
    // is deliberately out of the accessibility tree while this is open. It is still mounted, and
    // that is the fact this asserts.
    expect(screen.getByText('BIAL Citizen Developer')).toBeTruthy()
  })

  it('closes on its own X, and does not ask for the list again until it is reopened', async () => {
    renderNavbar()
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalled())
    openMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: /integrations/i }))
    await screen.findByTestId('integrations-dialog')
    expect(h.listConnectors).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    await waitFor(() => expect(screen.queryByTestId('integrations-dialog')).toBeNull())
    // Liveness: the navbar is still there, so the dialog closed rather than the tree dying.
    expect(screen.getByRole('link', { name: 'Projects' })).toBeTruthy()
    expect(h.listConnectors).toHaveBeenCalledTimes(1)
  })
})

/**
 * THE HEADLINE: a failed sign-out used to `showToast(...)` then `navigate('/login')` on the next
 * line — the navigate unmounts Navbar, which OWNS the toast state, destroying the message in the
 * same tick it was created. The fix carries the warning forward as router state instead.
 *
 * `LoginScreenProbe` reads the same `location.state.signoutWarning` LoginPage reads, so a pass
 * proves what reaches the destination screen, not what Navbar tried to render before leaving.
 */
function LoginScreenProbe() {
  const location = useLocation()
  return <div data-testid="login-screen">{location.state?.signoutWarning ?? ''}</div>
}

const renderNavbarWithLoginRoute = () =>
  render(
    <MemoryRouter initialEntries={['/dashboard']}>
      <Routes>
        <Route path="/dashboard" element={<Navbar />} />
        <Route path="/login" element={<LoginScreenProbe />} />
      </Routes>
    </MemoryRouter>,
  )

const signOut = async () => {
  await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalled())
  const trigger = screen.getByText('Asha', { selector: 'p' }).closest('button')
  fireEvent.pointerDown(trigger)
  fireEvent.click(await screen.findByRole('menuitem', { name: /sign out/i }))
}

describe('the sign-out warning outlives the navigation', () => {
  it('THE BUG: a failed sign-out leaves the warning readable on the screen the person lands on', async () => {
    h.logout.mockResolvedValue(false)
    renderNavbarWithLoginRoute()

    await signOut()

    // The navbar (and the toast state it used to own) is gone — this asserts the
    // destination screen, not a message that flashed before the redirect.
    const loginScreen = await screen.findByTestId('login-screen')
    expect(loginScreen.textContent).toBe('Sign-out may be incomplete on this device.')
    expect(screen.queryByText('Asha')).toBeNull()
  })

  it('carries no warning across on a clean sign-out', async () => {
    h.logout.mockResolvedValue(true)
    renderNavbarWithLoginRoute()

    await signOut()

    const loginScreen = await screen.findByTestId('login-screen')
    expect(loginScreen.textContent).toBe('')
  })
})

/**
 * THE WORKSPACE'S IN-PLACE EXITS ROUTE THROUGH ITS GUARD.
 *
 * `beforeunload` cannot cover a nav link: a single-page navigation is not an unload, so leaving the
 * workspace this way used to discard unsaved work in silence.
 *
 * Two halves, and the second is the one that keeps this from being a regression for every other
 * page: inside a workspace the link consults the guard, and OUTSIDE one it navigates exactly as it
 * always did. A guard that made the projects list ask before every click would be worse than the
 * bug it fixed.
 */
describe('Navbar — the workspace exit guard', () => {
  it('★ routes a nav link through the guard when one is provided', () => {
    const exits = []
    render(
      <MemoryRouter initialEntries={['/projects/p1']}>
        <WorkspaceExitProvider value={(go) => exits.push(go)}>
          <Routes>
            <Route path="*" element={<><Navbar /><LocationProbe /></>} />
          </Routes>
        </WorkspaceExitProvider>
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByRole('link', { name: /^projects$/i }))

    // Handed to the guard, and NOT performed: the guard decides whether it happens.
    expect(exits).toHaveLength(1)
    expect(screen.getByTestId('where').textContent).toBe('/projects/p1')

    // …and running it is what actually navigates, so nothing is lost when the guard says yes.
    act(() => exits[0]())
    expect(screen.getByTestId('where').textContent).toBe('/projects')
  })

  it('★ navigates straight through on a page with no workspace', () => {
    // Every other page in the portal renders this navbar. Without the pass-through default, this
    // unit would put a guard in front of navigation everywhere.
    render(
      <MemoryRouter initialEntries={['/dashboard']}>
        <Routes>
          <Route path="*" element={<><Navbar /><LocationProbe /></>} />
        </Routes>
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByRole('link', { name: /^projects$/i }))

    expect(screen.getByTestId('where').textContent).toBe('/projects')
  })
})

/**
 * THE BRAND LINK CARRIES THE PROJECTS LIST STATE BACK.
 *
 * `/projects`'s own address carries `page`, `pageSize` and `q` — reading that in is
 * `ProjectsPage.test.tsx`'s job. This link is mounted on a DIFFERENT address (a project, a chat,
 * admin, marketplace, help) and has always had to name a destination without ever having read
 * that query string itself; before this it hardcoded a bare `/projects`. `ProjectsPage` mounts
 * its OWN instance of this exact component, which is what lets THIS suite drive both halves —
 * render while the address bar reads `/projects?…`, navigate away, and check where the brand
 * link goes — without needing `ProjectsPage` in the tree at all.
 */
describe('Navbar — the brand link carries the projects list state back', () => {
  // Every test in this block sees a clean, "nothing remembered yet" tab — otherwise the FIRST
  // test's memory would silently stand in for the second's fresh session.
  afterEach(() => sessionStorage.removeItem('projectsListSearch'))

  function WhereFull() {
    const loc = useLocation()
    return <span data-testid="where-full">{loc.pathname + loc.search}</span>
  }

  function GoTo({ to }) {
    const navigate = useNavigate()
    return (
      <button type="button" data-testid="goto" onClick={() => navigate(to)}>
        go
      </button>
    )
  }

  const renderAcross = (from) =>
    render(
      <MemoryRouter initialEntries={[from]}>
        <Routes>
          <Route
            path="*"
            element={
              <>
                <Navbar />
                <WhereFull />
                <GoTo to="/chat/c1" />
              </>
            }
          />
        </Routes>
      </MemoryRouter>,
    )

  it('★ remembers the list address seen on `/projects` and returns to it, not to page one', async () => {
    renderAcross('/projects?page=2&pageSize=20&q=ramp')
    // LIVENESS FIRST: the navbar actually rendered on this address, not a crash a bare
    // destination check below would miss.
    expect(await screen.findByTestId('usage-meter')).toBeTruthy()
    expect(screen.getByTestId('where-full').textContent).toBe('/projects?page=2&pageSize=20&q=ramp')

    // Leave for a page that has nothing to do with the list — the memory has to outlive this.
    fireEvent.click(screen.getByTestId('goto'))
    expect(screen.getByTestId('where-full').textContent).toBe('/chat/c1')

    fireEvent.click(screen.getByRole('link', { name: /kempegowda/i }))

    expect(screen.getByTestId('where-full').textContent).toBe('/projects?page=2&pageSize=20&q=ramp')
  })

  it('falls back to the bare list when this tab never saw a search on `/projects`', async () => {
    renderAcross('/chat/c1')
    expect(await screen.findByTestId('usage-meter')).toBeTruthy()

    fireEvent.click(screen.getByRole('link', { name: /kempegowda/i }))

    expect(screen.getByTestId('where-full').textContent).toBe('/projects')
  })
})
