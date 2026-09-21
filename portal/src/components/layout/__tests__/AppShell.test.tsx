/**
 * THE SHELL IS THE PRODUCT'S CHROME.
 *
 * Every destination navigates directly — nothing asks before an exit any more, so each is
 * asserted per destination rather than once, the way a per-route regression would otherwise slip
 * through four of five routes silently.
 *
 * THE OTHER HALF IS WHERE THE NAVIGATION IS. Docked on the list routes; not in the accessible
 * tree at all inside an application until it is summoned. Both are asserted per route, because
 * "the nav renders" would pass on a build that drew it in both places.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { MotionGlobalConfig } from 'motion/react'
import { NAV_RAIL_PIN_KEY } from '../useNavRail'

const h = vi.hoisted(() => ({
  fetchUsageToday: vi.fn(),
  onUsageChanged: vi.fn(),
  isAuthenticated: vi.fn(() => true),
  getStoredUser: vi.fn(),
  logout: vi.fn(),
  fetchAppStatusCounts: vi.fn(),
  projectsListHref: vi.fn(() => '/projects'),
  rememberProjectsSearch: vi.fn(),
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
vi.mock('../../../utils/projectsListMemory', () => ({
  projectsListHref: h.projectsListHref,
  rememberProjectsSearch: h.rememberProjectsSearch,
  recallProjectsSearch: vi.fn(() => ''),
}))

import AppShell from '../AppShell'
import { NavMenuButton } from '../NavReveal'

MotionGlobalConfig.skipAnimations = true

const CITIZEN = { email: 'asha@bial.aero', display_name: 'Asha Rao', isAdmin: false }
const ADMIN = { email: 'priya@bial.aero', display_name: 'Priya Nair', isAdmin: true }
const USAGE = { used: 537_102, limit: 1_000_000, remaining: 462_898, resetsAt: '' }
const counts = (pending: number) => ({ draft: 0, pending, approved: 0, rejected: 0, disabled: 0 })

/** The six destinations, in the order `NavStates.dc.html` draws them.
 *
 *  THE ORDER TEST FILTERS THE RENDERED BUTTONS *BY* THIS LIST, so a destination missing from here
 *  is not a failure — it is dropped, and the test goes on claiming it checks the whole rail. */
const BOARD_ORDER = [
  'My Applications',
  'BIAL Chat',
  'Shared Applications',
  'App Marketplace',
  'Integrations',
  'Admin',
]

beforeEach(() => {
  vi.clearAllMocks()
  h.isAuthenticated.mockReturnValue(true)
  h.getStoredUser.mockReturnValue(CITIZEN)
  h.fetchUsageToday.mockResolvedValue(USAGE)
  h.onUsageChanged.mockReturnValue(() => {})
  h.fetchAppStatusCounts.mockResolvedValue(counts(0))
  h.projectsListHref.mockReturnValue('/projects')
})
afterEach(() => cleanup())

/**
 * Stands in for the page the shell frames — and carries `NavMenuButton` exactly as the workspace
 * toolbar does, because that button is a CHILD of the shell rather than part of it. Rendering it
 * here is what makes the summon path under test the real one: the button reaches the reveal
 * through context, and a shell that forgot to provide that context would fail here rather than
 * in a browser.
 */
function Where() {
  return (
    <>
      <NavMenuButton />
      <span data-testid="where">{useLocation().pathname + useLocation().search}</span>
    </>
  )
}

/**
 * Radix triggers open on POINTERDOWN, not click, and `DismissableLayer` arms its document
 * listener on a macrotask after the menu mounts — so a press fired in the same tick as the open
 * lands before it is armed. `@testing-library/user-event` would smooth both over; it is in
 * neither `package.json`, the lockfile, nor `node_modules`, so this is the in-repo pattern.
 */
const openRadix = (trigger: HTMLElement) => fireEvent.pointerDown(trigger)
const settle = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })

/** Summon the navigation on a route where it is hidden. */
async function summonNav() {
  fireEvent.click(screen.getByTestId('nav-menu-button'))
  return screen.findByTestId('nav-panel')
}

/** The screen a sign-out lands on, reading the same router state `LoginPage` reads. */
function Landed() {
  const state = useLocation().state as { signoutWarning?: string } | null
  return <div data-testid="landed">{state?.signoutWarning ?? ''}</div>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/login" element={<Landed />} />
        <Route
          path="*"
          element={
            <AppShell>
              <Where />
            </AppShell>
          }
        />
      </Routes>
    </MemoryRouter>,
  )
}

describe('the navigation the boards draw', () => {
  it('renders all six entries in board order for an administrator', async () => {
    h.getStoredUser.mockReturnValue(ADMIN)
    renderAt('/projects')
    const labels = (await screen.findAllByRole('button'))
      .map((b) => b.textContent ?? '')
      .filter((text) => BOARD_ORDER.some((label) => text.startsWith(label)))
    expect(labels.map((t) => BOARD_ORDER.find((l) => t.startsWith(l)))).toEqual(BOARD_ORDER)
  })

  it('marks exactly one entry as the current page', async () => {
    h.getStoredUser.mockReturnValue(ADMIN)
    renderAt('/marketplace')
    await screen.findByTestId('nav-panel')
    const current = screen.getAllByRole('button').filter((b) => b.getAttribute('aria-current') === 'page')
    expect(current).toHaveLength(1)
    expect(current[0].textContent).toContain('App Marketplace')
  })

  it.each([
    ['/shared/p1', 'Shared Applications'],
    ['/chat/c1', 'My Applications'],
  ])(
    '★ still says where you are at %s, whose address shares no prefix with its list',
    async (path, label) => {
      // OPENING A ROW CAN LAND YOU SOMEWHERE THE PREFIX TEST CANNOT SEE. A shared application
      // lives at `/shared/{id}` while its list is `/shared-applications`; a chat lives at
      // `/chat/{id}` while its application's list is `/projects`. On both, nothing at all was
      // marked current — so the one thing the navigation is for, saying where you are, was
      // missing on exactly the screens a person reached by using it.
      h.getStoredUser.mockReturnValue(ADMIN)
      renderAt(path)
      // Inside an application the panel is summoned rather than docked; on the shared viewer it is
      // already there. Either way the question is what it says once it is on screen.
      if (screen.queryByTestId('nav-menu-button') !== null) await summonNav()
      await screen.findByTestId('nav-panel')
      const current = screen
        .getAllByRole('button')
        .filter((b) => b.getAttribute('aria-current') === 'page')
      expect(current).toHaveLength(1)
      expect(current[0].textContent).toContain(label)
    },
  )

  it('hides the Admin entry entirely from a citizen, and asks for no count on their behalf', async () => {
    renderAt('/projects')
    await screen.findByTestId('nav-panel')
    // Absence, PAIRED WITH LIVENESS: the other five rendered, so this is a gate rather than a
    // crash that happened to leave the page blank.
    expect(screen.queryByTestId('nav-admin')).toBeNull()
    expect(screen.getByTestId('nav-projects')).not.toBeNull()
    // The count route is superadmin-only server-side, so asking would spend a request to earn a
    // 403 in every citizen's console.
    expect(h.fetchAppStatusCounts).not.toHaveBeenCalled()
  })

  it('shows an administrator the entry with no badge when nothing is waiting', async () => {
    h.getStoredUser.mockReturnValue(ADMIN)
    renderAt('/projects')
    const admin = await screen.findByTestId('nav-admin')
    await waitFor(() => expect(h.fetchAppStatusCounts).toHaveBeenCalled())
    expect(admin.querySelector('[data-testid="waiting-count-nav"]')).toBeNull()
  })

  it('draws the badge once there is something waiting', async () => {
    h.getStoredUser.mockReturnValue(ADMIN)
    h.fetchAppStatusCounts.mockResolvedValue(counts(3))
    renderAt('/projects')
    const badge = await screen.findByTestId('waiting-count-nav')
    expect(badge.textContent).toContain('3')
  })

  it('never claims a number when the count cannot be read', async () => {
    h.getStoredUser.mockReturnValue(ADMIN)
    h.fetchAppStatusCounts.mockRejectedValue(new Error('403'))
    renderAt('/projects')
    await screen.findByTestId('nav-admin')
    await waitFor(() => expect(h.fetchAppStatusCounts).toHaveBeenCalled())
    expect(screen.queryByTestId('waiting-count-nav')).toBeNull()
  })
})

describe('every destination navigates directly — no exit routine in the way', () => {
  it.each([
    ['nav-projects', '/projects'],
    ['nav-assistant', '/assistant'],
    ['nav-shared-applications', '/shared-applications'],
    ['nav-marketplace', '/marketplace'],
    ['nav-integrations', '/integrations'],
    ['nav-admin', '/admin'],
  ])('%s navigates straight there, with nothing asked first', async (testId, expected) => {
    h.getStoredUser.mockReturnValue(ADMIN)
    renderAt('/chat/c1')
    await summonNav()
    fireEvent.click(await screen.findByTestId(testId))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe(expected))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('the logo navigates too, and lands on the remembered list rather than page one', async () => {
    h.projectsListHref.mockReturnValue('/projects?q=belt&page=3')
    renderAt('/chat/c1')
    await summonNav()
    fireEvent.click(await screen.findByRole('button', { name: /BIAL Citizen Developer/ }))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/projects?q=belt&page=3'))
  })

  it('remembers what the list address carried, so there is something to read back', async () => {
    // THE WRITER IS THE HALF THAT GOES MISSING. `projectsListHref()` degrades silently to a bare
    // `/projects` when nothing records the search — every read still works, every test that only
    // exercises the read still passes, and the whole remember-my-place mechanism becomes dead
    // code. The shell is the one component mounted on every route, so it is the one place that
    // can see the address bar read the list.
    renderAt('/projects?q=belt&page=3')
    await screen.findByTestId('nav-panel')
    expect(h.rememberProjectsSearch).toHaveBeenCalledWith('?q=belt&page=3')
  })

  it('remembers nothing from any other address', async () => {
    renderAt('/marketplace?q=belt')
    await screen.findByTestId('nav-panel')
    expect(h.rememberProjectsSearch).not.toHaveBeenCalled()
  })

  it('the list entry reads the remembered search at CLICK time, not at render time', async () => {
    // Memoising it at render is the defect: a search typed a moment ago on the list would be
    // dropped for whatever the href was when the shell first mounted.
    h.projectsListHref.mockReturnValue('/projects')
    renderAt('/marketplace')
    await screen.findByTestId('nav-panel')
    h.projectsListHref.mockReturnValue('/projects?q=typed-later')
    fireEvent.click(screen.getByTestId('nav-projects'))
    await waitFor(() =>
      expect(screen.getByTestId('where').textContent).toBe('/projects?q=typed-later'),
    )
  })

})

describe('where the navigation is, per route', () => {
  it('is docked and present on a list route', async () => {
    renderAt('/projects')
    expect(await screen.findByTestId('nav-docked')).not.toBeNull()
    expect(screen.getByTestId('nav-panel')).not.toBeNull()
  })

  it('is not in the accessible tree at all inside an application until it is summoned', async () => {
    renderAt('/projects/p1')
    // Liveness first: the page underneath really did render, so the absence below is the
    // navigation being hidden rather than the whole tree failing to mount.
    expect(screen.getByTestId('where').textContent).toBe('/projects/p1')
    expect(screen.queryByTestId('nav-panel')).toBeNull()
    expect(screen.queryByTestId('nav-docked')).toBeNull()
    expect(screen.queryByRole('navigation', { name: 'Primary' })).toBeNull()
  })

  it('is hidden on a chat address too, and the menu button is the visible way back to it', async () => {
    renderAt('/chat/c1')
    expect(screen.queryByTestId('nav-panel')).toBeNull()
    expect(await summonNav()).not.toBeNull()
  })
})

describe('the foot of the navigation', () => {
  it('shows the meter when usage reads, and the figures with it', async () => {
    renderAt('/projects')
    expect((await screen.findByTestId('usage-figures')).textContent).toBe('537,102 / 1,000,000')
  })

  it('hides the meter when the read fails, without collapsing the foot', async () => {
    h.fetchUsageToday.mockResolvedValue(null)
    renderAt('/projects')
    const profile = await screen.findByTestId('profile-cluster')
    // Absence PLUS the liveness the foot is supposed to keep: the profile cluster is still there.
    expect(screen.queryByTestId('usage-meter')).toBeNull()
    expect(profile.textContent).toContain('Asha Rao')
  })

  it('never asks for usage while signed out', async () => {
    h.isAuthenticated.mockReturnValue(false)
    renderAt('/projects')
    await screen.findByTestId('nav-panel')
    expect(h.fetchUsageToday).not.toHaveBeenCalled()
  })

  it('names the person by their email, not by a role the platform does not model', async () => {
    renderAt('/projects')
    const profile = await screen.findByTestId('profile-cluster')
    expect(profile.textContent).toContain('asha@bial.aero')
    expect(profile.textContent).not.toContain('Citizen developer')
  })

  it('opens Feedback from the profile menu', async () => {
    renderAt('/projects')
    openRadix(await screen.findByTestId('profile-cluster'))
    await settle()
    fireEvent.click(await screen.findByRole('menuitem', { name: /Feedback/ }))
    expect(await screen.findByRole('dialog')).not.toBeNull()
  })

  it('a failed sign-out leaves its warning readable on the screen the person LANDS on', async () => {
    // THE SHAPE OF THE ORIGINAL BUG: the warning was shown as a toast owned by the chrome, then
    // the very next line navigated — unmounting the thing that owned the message in the same tick
    // it was created. Nobody ever saw it. So this asserts the DESTINATION screen, not a message
    // that flashed on the way out, and the cluster being gone is the proof it is the destination.
    h.logout.mockResolvedValue(false)
    renderAt('/projects')
    openRadix(await screen.findByTestId('profile-cluster'))
    await settle()
    fireEvent.click(await screen.findByRole('menuitem', { name: /Sign out/ }))
    await waitFor(() =>
      expect(screen.getByTestId('landed').textContent).toBe(
        'Sign-out may be incomplete on this device.',
      ),
    )
    expect(screen.queryByTestId('profile-cluster')).toBeNull()
  })

  it('carries no warning across on a clean sign-out', async () => {
    h.logout.mockResolvedValue(true)
    renderAt('/projects')
    openRadix(await screen.findByTestId('profile-cluster'))
    await settle()
    fireEvent.click(await screen.findByRole('menuitem', { name: /Sign out/ }))
    await waitFor(() => expect(screen.getByTestId('landed').textContent).toBe(''))
  })

  it('signs out directly from inside an application, with nothing asked first', async () => {
    h.logout.mockResolvedValue(true)
    renderAt('/chat/c1')
    await summonNav()
    openRadix(await screen.findByTestId('profile-cluster'))
    await settle()
    fireEvent.click(await screen.findByRole('menuitem', { name: /Sign out/ }))
    await waitFor(() => expect(h.logout).toHaveBeenCalled())
  })
})

/**
 * ★ THE RAIL — the navigation's resting state, and the three things that change its width.
 *
 * Width itself is not assertable here: the panel animates it through Motion, and jsdom reports
 * no layout. `data-collapsed` is the state the width is computed FROM, which is the honest thing
 * to assert and the thing a regression would break first.
 */
describe('the navigation rests as a rail and grows when it is approached', () => {
  const panel = () => screen.getByTestId('nav-panel')
  const isCollapsed = () => panel().getAttribute('data-collapsed') === 'true'

  beforeEach(() => {
    h.getStoredUser.mockReturnValue(CITIZEN)
    window.localStorage.removeItem(NAV_RAIL_PIN_KEY)
  })

  afterEach(() => window.localStorage.removeItem(NAV_RAIL_PIN_KEY))

  it('★ rests collapsed, and grows when the pointer arrives', async () => {
    renderAt('/projects')
    await screen.findByTestId('nav-panel')
    expect(isCollapsed()).toBe(true)

    fireEvent.pointerEnter(screen.getByTestId('nav-docked'))
    await waitFor(() => expect(isCollapsed()).toBe(false))
  })

  it('★ waits before closing behind a pointer that has left', async () => {
    // FAKE TIMERS ARE INSTALLED AFTER THE RENDER, NOT BEFORE. `findBy*` polls on real timers, so
    // faking them first hangs the query until the test times out — and a timed-out test never
    // reaches its own restore, which leaves every later test in the file running on fake timers.
    renderAt('/projects')
    const docked = await screen.findByTestId('nav-docked')

    vi.useFakeTimers()
    try {
      fireEvent.pointerEnter(docked)
      expect(isCollapsed()).toBe(false)

      fireEvent.pointerLeave(docked)
      // Still open immediately after: the delay is what forgives a pointer merely crossing it.
      expect(isCollapsed()).toBe(false)
      act(() => { vi.advanceTimersByTime(200) })
      expect(isCollapsed()).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ stays open once pinned, with the pointer nowhere near it, and remembers across a mount', async () => {
    renderAt('/projects')
    const docked = await screen.findByTestId('nav-docked')
    fireEvent.pointerEnter(docked)
    await waitFor(() => expect(isCollapsed()).toBe(false))

    fireEvent.click(screen.getByTestId('nav-pin'))
    fireEvent.pointerLeave(docked)
    await settle()
    expect(isCollapsed()).toBe(false)
    expect(window.localStorage.getItem(NAV_RAIL_PIN_KEY)).toBe('1')

    // The preference is the one piece of nav state that outlives the page.
    cleanup()
    renderAt('/projects')
    await screen.findByTestId('nav-panel')
    expect(isCollapsed()).toBe(false)
  })

  it('★ reaching the profile is not what opens the navigation', async () => {
    // THE JOURNEY IS THE TEST. The avatar sits inside the rail, so a real pointer crosses the
    // rail to reach it — and the rail opened on the way, which is what the owner reported. The
    // enter is fired ON THE NAV with the profile as its target, which is what the browser does
    // when the pointer crosses into the aside at the profile row; dispatching straight at the
    // avatar never crosses the boundary and so proves nothing.
    renderAt('/projects')
    await screen.findByTestId('nav-docked')
    expect(isCollapsed()).toBe(true)

    // `pointerOver` on the inner element, not `pointerEnter` on the nav: React synthesises its
    // enter from `pointerover`, so this is the event the browser actually delivers, carrying the
    // element under the pointer as its target. `fireEvent`'s `target` option assigns to the NODE,
    // not to the event, so it cannot express "entered here" at all.
    fireEvent.pointerOver(screen.getByTestId('profile-cluster'))
    await settle()
    expect(isCollapsed()).toBe(true)

    // The paired positive, without which the assertion above is satisfied by a rail that never
    // opens at all: arriving over the destinations still opens it.
    fireEvent.pointerOver(screen.getByTestId('nav-projects'))
    await waitFor(() => expect(isCollapsed()).toBe(false))
  })

  it('★ moving from the profile UP to a destination opens it, without leaving the rail', async () => {
    // THE MOVE THE TEST ABOVE CANNOT EXPRESS. A `pointerOver` with no `relatedTarget` reads as
    // entering from outside the window, so React fires its synthetic ENTER for it and a handler
    // bound to `pointerEnter` passes. A real pointer travelling from the profile to a destination
    // never leaves the aside, so `enter` does not fire again — and the rail stayed shut for as
    // long as the pointer was inside it. `relatedTarget` is what says "came from in here".
    renderAt('/projects')
    await screen.findByTestId('nav-docked')

    const profile = screen.getByTestId('profile-cluster')
    const projects = screen.getByTestId('nav-projects')

    fireEvent.pointerOver(profile)
    await settle()
    expect(isCollapsed()).toBe(true)

    fireEvent.pointerOver(projects, { relatedTarget: profile })
    await waitFor(() => expect(isCollapsed()).toBe(false))
  })

  it('★ opens to the keyboard, which is the only way a keyboard reaches Pin', async () => {
    // THE PIN IS ONLY RENDERED WHILE EXPANDED, so a rail that expands on hover alone has no
    // keyboard path to it at all — the control cannot be tabbed to because it does not exist yet.
    renderAt('/projects')
    await screen.findByTestId('nav-docked')
    expect(isCollapsed()).toBe(true)
    expect(screen.queryByTestId('nav-pin')).toBeNull()

    const projects = screen.getByTestId('nav-projects')
    act(() => projects.focus())
    await waitFor(() => expect(isCollapsed()).toBe(false))
    expect(screen.getByTestId('nav-pin')).toBeTruthy()

    // And it lets go again when focus leaves, rather than latching open for the rest of the visit.
    act(() => projects.blur())
    await waitFor(() => expect(isCollapsed()).toBe(true))
  })

  it('★ pinning the rail does not dock a column beside a framed application', async () => {
    // TWO PREFERENCES ABOUT TWO SCREENS. The rail's pin and the floating panel's dock once shared
    // one storage key, so pinning the navigation on the list silently took 248px off every
    // application preview — a switch the owner never threw, on a screen they were not looking at.
    renderAt('/projects')
    const docked = await screen.findByTestId('nav-docked')
    fireEvent.pointerOver(docked)
    await waitFor(() => expect(isCollapsed()).toBe(false))
    fireEvent.click(screen.getByTestId('nav-pin'))
    await settle()
    expect(window.localStorage.getItem(NAV_RAIL_PIN_KEY)).toBe('1')

    cleanup()
    renderAt('/chat/c1')
    await screen.findByTestId('where')
    expect(screen.queryByTestId('nav-panel')).toBeNull()
    expect(screen.queryByTestId('nav-docked')).toBeNull()
  })

  it('★ opening the profile menu on a collapsed rail does NOT sweep the panel open', async () => {
    // The latch that keeps the rail from closing under its own menu freezes the width it found
    // rather than forcing the panel open: a click aimed at one control must not sweep the whole
    // navigation open behind it.
    renderAt('/projects')
    await screen.findByTestId('nav-panel')
    expect(isCollapsed()).toBe(true)

    openRadix(screen.getByTestId('profile-cluster'))
    await settle()
    // Liveness beside the absence: the menu really did open, so "still collapsed" is the latch
    // behaving rather than the press missing.
    expect(await screen.findByTestId('user-menu-identity')).toBeTruthy()
    expect(isCollapsed()).toBe(true)
  })

  it('★ …and does not let the rail close under the menu when it was already open', async () => {
    // The other half, and THE load-bearing one of the pair. The menu renders outside the nav, so
    // reaching for it reads as the pointer LEAVING — without the freeze the panel collapses out
    // from under the thing the person is reaching for.
    //
    // IT HAS TO OUTLAST THE CLOSE DELAY TO PROVE ANYTHING. Asserting straight after the leave
    // passes either way, because the rail has not had time to close yet — which is how the first
    // version of this test let the freeze be deleted without going red.
    renderAt('/projects')
    const docked = await screen.findByTestId('nav-docked')
    fireEvent.pointerEnter(docked)
    await waitFor(() => expect(isCollapsed()).toBe(false))

    openRadix(screen.getByTestId('profile-cluster'))
    await settle()
    expect(await screen.findByTestId('user-menu-identity')).toBeTruthy()

    fireEvent.pointerLeave(docked)
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 400)) })
    expect(isCollapsed()).toBe(false)
  })
})

/**
 * ★ WHAT THE COLLAPSE MAY NOT TAKE WITH IT. Two readings on this panel are not decoration: the
 * token budget is a client requirement, and the review queue is the one thing here that summons
 * an administrator rather than merely pointing somewhere. A rail that dropped either would be a
 * regression nobody would notice until it mattered.
 */
describe('the rail keeps the two readings that are not decoration', () => {
  const panel = () => screen.getByTestId('nav-panel')

  beforeEach(() => window.localStorage.removeItem(NAV_RAIL_PIN_KEY))

  it('★ still shows a token reading at rail width', async () => {
    h.getStoredUser.mockReturnValue(CITIZEN)
    h.fetchUsageToday.mockResolvedValue(USAGE)
    renderAt('/projects')
    await screen.findByTestId('nav-panel')
    expect(panel().getAttribute('data-collapsed')).toBe('true')

    const meter = await screen.findByTestId('usage-meter')
    // The percent written inside the arc is what survives the collapse; the full figures need a
    // width the rail does not have, so the title carries them instead.
    expect(meter.textContent).toContain('54%')
    expect(meter.getAttribute('title')).toContain('537,102 / 1,000,000')
  })

  it('★ still shows the review queue is waiting, for an administrator', async () => {
    h.getStoredUser.mockReturnValue(ADMIN)
    h.fetchAppStatusCounts.mockResolvedValue(counts(2))
    renderAt('/projects')
    await screen.findByTestId('nav-panel')
    expect(panel().getAttribute('data-collapsed')).toBe('true')

    const badge = await screen.findByTestId('waiting-count-nav')
    // The numeral has nowhere to sit at 56px, so the count is announced rather than drawn.
    expect(badge.textContent).toContain('2 apps waiting for review')
    // AND THE NUMERAL IS GENUINELY GONE. `textContent` alone cannot tell: with the numeral still
    // drawn it reads '22 apps waiting for review', which contains the sentence above and passes.
    // The drawn count is the `aria-hidden` node — its absence is the assertion that bites.
    expect(badge.querySelector('[aria-hidden="true"]')).toBeNull()
  })
})
