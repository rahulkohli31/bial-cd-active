/**
 * THE SHELL IS THE PRODUCT'S CHROME, AND THE ONE THING IT MUST NOT LOSE IS THE EXIT PATH.
 *
 * The header this replaces wired the unsaved-work guard onto the logo AND onto every one of its
 * links, separately. A shell can very easily wire it once and miss four — and the failure is
 * silent: the dialog, the save offer and the failed-save refusal simply stop happening on four of
 * five routes. So the guard is asserted PER DESTINATION rather than once, and on ordering rather
 * than on arrival: "it navigated" is true whether the guard ran first, last, or not at all.
 *
 * THE OTHER HALF IS WHERE THE NAVIGATION IS. Docked on the list routes; not in the accessible
 * tree at all inside an application until it is summoned. Both are asserted per route, because
 * "the nav renders" would pass on a build that drew it in both places.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { MotionGlobalConfig } from 'motion/react'

const h = vi.hoisted(() => ({
  fetchUsageToday: vi.fn(),
  onUsageChanged: vi.fn(),
  isAuthenticated: vi.fn(() => true),
  getStoredUser: vi.fn(),
  logout: vi.fn(),
  fetchAppStatusCounts: vi.fn(),
  listConnectors: vi.fn(),
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
// SPREAD THE ORIGINAL. A bare factory silently drops every export it does not name, and the
// connector dialog's close path calls `notifyConnectorsChanged` — listing only the fetchers makes
// closing throw, and that reads as "the dialog would not close" rather than "the mock is thin".
vi.mock('../../../utils/connectorApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  listConnectors: h.listConnectors,
}))

import AppShell from '../AppShell'
import { NavMenuButton } from '../NavReveal'
import { WorkspaceExitHost, useRegisterWorkspaceExit } from '../../workspace/UnsavedWorkGuard'

MotionGlobalConfig.skipAnimations = true

const CITIZEN = { email: 'asha@bial.aero', display_name: 'Asha Rao', isAdmin: false }
const ADMIN = { email: 'priya@bial.aero', display_name: 'Priya Nair', isAdmin: true }
const USAGE = { used: 537_102, limit: 1_000_000, remaining: 462_898, resetsAt: '' }
const counts = (pending: number) => ({ draft: 0, pending, approved: 0, rejected: 0, disabled: 0 })

/** The five destinations, in the order `NavStates.dc.html` draws them. */
const BOARD_ORDER = [
  'My Applications',
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
  h.listConnectors.mockResolvedValue([])
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

/**
 * Stands in for the workspace, which is the only thing that ever registers a guard — and it does
 * so from INSIDE the shell, which is the whole point. A fixture that provided the guard from
 * outside would prove the navigation can reach a context somebody handed it, not that it can
 * reach the one the workspace actually publishes.
 */
function RegistersAGuard({ guard }: { guard: (go: () => void) => void }) {
  useRegisterWorkspaceExit(guard)
  return null
}

/** The screen a sign-out lands on, reading the same router state `LoginPage` reads. */
function Landed() {
  const state = useLocation().state as { signoutWarning?: string } | null
  return <div data-testid="landed">{state?.signoutWarning ?? ''}</div>
}

function renderAt(path: string, guard?: (go: () => void) => void) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <WorkspaceExitHost>
        <Routes>
          <Route path="/login" element={<Landed />} />
          <Route
            path="*"
            element={
              <AppShell>
                {guard && <RegistersAGuard guard={guard} />}
                <Where />
              </AppShell>
            }
          />
        </Routes>
      </WorkspaceExitHost>
    </MemoryRouter>,
  )
}

describe('the navigation the boards draw', () => {
  it('renders all five entries in board order for an administrator', async () => {
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

  it('hides the Admin entry entirely from a citizen, and asks for no count on their behalf', async () => {
    renderAt('/projects')
    await screen.findByTestId('nav-panel')
    // Absence, PAIRED WITH LIVENESS: the other four rendered, so this is a gate rather than a
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

describe('every destination leaves the workspace through its guard — not just the logo', () => {
  it.each([
    ['nav-projects', '/projects'],
    ['nav-shared-applications', '/shared-applications'],
    ['nav-marketplace', '/marketplace'],
    ['nav-admin', '/admin'],
  ])('%s runs the exit routine BEFORE navigating', async (testId, expected) => {
    h.getStoredUser.mockReturnValue(ADMIN)
    const order: string[] = []
    const guard = (go: () => void) => {
      order.push('guard')
      go()
    }
    renderAt('/chat/c1', guard)
    await summonNav()
    fireEvent.click(await screen.findByTestId(testId))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe(expected))
    order.push('arrived')
    // ORDERING, NOT ARRIVAL. A shell that navigated without the guard would still arrive.
    expect(order).toEqual(['guard', 'arrived'])
  })

  it('the logo runs it too, and lands on the remembered list rather than page one', async () => {
    h.projectsListHref.mockReturnValue('/projects?q=belt&page=3')
    const seen: string[] = []
    renderAt('/chat/c1', (go) => { seen.push('guard'); go() })
    await summonNav()
    fireEvent.click(await screen.findByRole('button', { name: /BIAL Citizen Developer/ }))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/projects?q=belt&page=3'))
    expect(seen).toEqual(['guard'])
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

  it('the Integrations entry opens the dialog and tears nothing down — it is an overlay', async () => {
    // INTERIM, until Integrations is a page. An overlay is not a navigation, so it deliberately
    // does not run the exit guard the four destinations do.
    const guard = vi.fn((go: () => void) => go())
    renderAt('/chat/c1', guard)
    await summonNav()
    fireEvent.click(await screen.findByTestId('nav-integrations'))
    await waitFor(() => expect(h.listConnectors).toHaveBeenCalled())
    expect(guard).not.toHaveBeenCalled()
    expect(screen.getByTestId('where').textContent).toBe('/chat/c1')
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

  it('signs out through the workspace guard, so unsaved work is not lost in silence', async () => {
    const guard = vi.fn((go: () => void) => go())
    h.logout.mockResolvedValue(true)
    renderAt('/chat/c1', guard)
    await summonNav()
    openRadix(await screen.findByTestId('profile-cluster'))
    await settle()
    fireEvent.click(await screen.findByRole('menuitem', { name: /Sign out/ }))
    expect(guard).toHaveBeenCalled()
    await waitFor(() => expect(h.logout).toHaveBeenCalled())
  })
})
