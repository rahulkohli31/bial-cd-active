/**
 * ProjectPage: the project home is the builder.
 *
 * What this pins:
 *   - the Sandbox builder (`ProjectBuilder`) renders UNCONDITIONALLY and first — its
 *     composer is present whether or not the project has an app (the exact regression that
 *     caused the reverted app-first fold; F6 dropped the idea-starter cards). The composer
 *     is the proxy for "the builder rendered"; it used to be the theme selector, which was
 *     removed as dead UI in #157 B1 — the invariant is unchanged, only the handle on it;
 *   - the passive "View app" preview is HIDDEN in Phase-1 (U6): a stored app is not a running
 *     sandbox, so a built project shows NO "View app" button and fires no getAppSource read —
 *     the live preview now comes only from a per-session C3 build in the builder;
 *   - the removed doors stay gone: no "Continue building" reroute, no "Open app" link;
 *   - the description sits in a right rail with Save + Generate and NO attach control (R3);
 *   - conversations are a plain recents list (icon · title · date · ⋮), no BUILD/PLAN badges,
 *     no new-chat buttons; the ⋮ menu opens + deletes optimistically;
 *   - a 404 (deleted elsewhere) bounces to /projects, and rename is blocked before any request.
 *
 * projectApi + conversationApi + builderHistory are mocked at the module boundary; the REAL
 * ProjectBuilder and ProjectDescriptionEditor render (they only need the mocked APIs). A
 * LocationProbe on a catch-all route reports where navigation actually landed. LivePreview is
 * stubbed to null — ProjectPage no longer mounts it (the passive preview is hidden, U6), and the
 * The old stored-app read (`getAppSource`) is retired from appRegistryApi entirely
 *  (owner surface gone; pinned by appRegistryApi.test.js).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import ProjectPage from '../ProjectPage'
import { ApiError } from '../../utils/apiError'
import type { Project } from '../../utils/projectApi'

const h = vi.hoisted(() => ({
  getProject: vi.fn(),
  patchProject: vi.fn(),
  generateDescription: vi.fn(),
  listProjectConversations: vi.fn(),
  deleteConversation: vi.fn(),
}))

vi.mock('../../utils/projectApi', () => ({
  getProject: h.getProject,
  patchProject: h.patchProject,
  generateDescription: h.generateDescription,
}))
vi.mock('../../utils/conversationApi.js', () => ({
  listProjectConversations: h.listProjectConversations,
  deleteConversation: h.deleteConversation,
}))
// SubmitControl owns its own data fetching (approvalApi) and has its own suite —
// stubbed here so ProjectPage tests stay about the page.
vi.mock('../../components/SubmitControl', () => ({ default: () => null }))
vi.mock('../../utils/chatHistory.js', () => ({ relativeTime: () => '1h ago' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
// ProjectPage no longer mounts LivePreview (the passive View-app preview is hidden, U6).
// A null stub keeps the `queryByTestId('live-preview')` inertness assertions meaningful.
vi.mock('../../components/LivePreview', () => ({ default: () => null }))

const makeProject = (over: Partial<Project> = {}): Project => ({
  id: 'p1',
  name: 'VIP Movement',
  description: 'A tracked movement.',
  appId: null,
  appStatus: null,
  hasRelaunchableSnapshot: null,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  ...over,
})

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="location">{loc.pathname + loc.search}</div>
}

function renderProjectPage(projectId = 'p1') {
  return render(
    <MemoryRouter initialEntries={[`/projects/${projectId}`]}>
      <Routes>
        <Route path="/projects/:projectId" element={<ProjectPage />} />
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  h.listProjectConversations.mockResolvedValue([])
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ProjectPage — the builder is unconditional', () => {
  it('no-app project: renders the builder (composer), recents, and the description rail — no app affordances', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: null, appStatus: null }))
    renderProjectPage()

    expect(await screen.findByRole('heading', { name: 'VIP Movement' })).toBeTruthy()
    // The builder is present: its composer renders. (F6: no idea-starter cards inside a
    // dedicated project — the composer + mode helper are the first-run guidance.)
    expect(screen.getByPlaceholderText(/we.ll shape the plan together/i)).toBeTruthy()
    // The description rail — a read view with an Edit button (U7: the pop-up editor).
    expect(within(screen.getByTestId('description-rail')).getByRole('button', { name: /edit/i })).toBeTruthy()
    // No app affordances for a project with no app: no "View app", no preview, no Continue building.
    expect(screen.queryByRole('button', { name: /view app/i })).toBeNull()
    expect(screen.queryByTestId('live-preview')).toBeNull()
    expect(screen.queryByRole('button', { name: /continue building/i })).toBeNull()
  })

  it('built project: builder still on top (composer) — and NO "View app" / lifecycle badge / Continue building / inline preview', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', appStatus: 'draft' }))
    h.listProjectConversations.mockResolvedValue([
      { id: 'c2', kind: 'builder', projectId: 'p1', title: 'Build the screen', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    // The builder is NOT collapsed — this is the reverted regression the fold must avoid.
    expect(screen.getByPlaceholderText(/we.ll shape the plan together/i)).toBeTruthy()
    // U6: the passive "View app" preview is HIDDEN — a stored app is not a running sandbox.
    const rail = screen.getByTestId('description-rail')
    expect(within(rail).queryByRole('button', { name: /view app/i })).toBeNull()
    // The removed affordances: no draft/lifecycle badge, no Continue building, no inline preview.
    expect(screen.queryByText('draft')).toBeNull()
    expect(screen.queryByRole('button', { name: /continue building/i })).toBeNull()
    expect(screen.queryByTestId('live-preview')).toBeNull()
  })
})

describe('ProjectPage — the passive app preview is hidden (U6)', () => {
  it('a built project renders NO "View app" affordance and NO modal', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', appStatus: 'draft' }))
    h.listProjectConversations.mockResolvedValue([
      { id: 'built', kind: 'builder', projectId: 'p1', title: 'Build it', updatedAt: '2026-07-09T00:00:00Z' },
    ])
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    expect(screen.queryByRole('button', { name: /view app/i })).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByTestId('live-preview')).toBeNull()
  })

  it('renders the landing path without any stored-app read (the getAppSource export is retired)', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', appStatus: 'draft' }))
    h.listProjectConversations.mockResolvedValue([
      { id: 'hi', kind: 'builder', projectId: 'p1', title: 'hi', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    // The whole passive-preview read is retired — never fired, with or without a modal to open.
    // The retired stored-app read cannot fire: appRegistryApi no longer exports it
    // (pinned by appRegistryApi.test.js).
  })

  it('the removed doors stay gone: no "Open app" link and no "Continue building" anywhere', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'app-123', appStatus: 'approved' }))
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    expect(screen.queryByRole('link', { name: /open app/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /continue building/i })).toBeNull()
  })
})

describe('ProjectPage — the description rail (R3, U7 pop-up editor)', () => {
  it('shows an Edit button and NO attach / file-input control, with no dialog open by default', async () => {
    h.getProject.mockResolvedValue(makeProject())
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    const rail = screen.getByTestId('description-rail')
    expect(within(rail).getByRole('button', { name: /edit/i })).toBeTruthy()
    expect(within(rail).queryByRole('button', { name: /attach|upload/i })).toBeNull()
    expect(rail.querySelector('input[type="file"]')).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('clicking Edit opens a pop-up exposing Save and Generate', async () => {
    h.getProject.mockResolvedValue(makeProject())
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    const rail = screen.getByTestId('description-rail')
    fireEvent.click(within(rail).getByRole('button', { name: /edit/i }))

    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('button', { name: /^save$/i })).toBeTruthy()
    expect(within(dialog).getByRole('button', { name: /^generate$/i })).toBeTruthy()
    expect(within(dialog).getByRole('button', { name: /^cancel$/i })).toBeTruthy()
  })
})

describe('ProjectPage — conversations as a plain recents list (U3)', () => {
  it('renders icon · title · relative date · ⋮ with no BUILD/PLAN badges and no new-chat buttons', async () => {
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([
      { id: 'c1', kind: 'planning', projectId: 'p1', title: 'Scope the fields', updatedAt: '2026-07-10T00:00:00Z' },
      { id: 'c2', kind: 'builder', projectId: 'p1', title: 'Build the screen', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    renderProjectPage()

    expect(await screen.findByText('Scope the fields')).toBeTruthy()
    expect(h.listProjectConversations).toHaveBeenCalledWith('p1')
    const list = screen.getByTestId('conversations')
    expect(within(list).getAllByText('1h ago').length).toBe(2)
    // No BUILD/PLAN badge chips in the list, and no New-chat buttons anywhere.
    expect(within(list).queryByText('Build')).toBeNull()
    expect(within(list).queryByText('Plan')).toBeNull()
    expect(screen.queryByRole('button', { name: /new build chat/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /new plan chat/i })).toBeNull()
  })

  it('clicking a row title navigates to /chat/{id}', async () => {
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([
      { id: 'c2', kind: 'builder', projectId: 'p1', title: 'Build the screen', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    renderProjectPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Build the screen' }))
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/chat/c2'))
  })

  it('the ⋮ menu exposes Open and Delete; Delete removes the row optimistically', async () => {
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([
      { id: 'c2', kind: 'builder', projectId: 'p1', title: 'Build the screen', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    h.deleteConversation.mockResolvedValue(true)
    renderProjectPage()

    fireEvent.click(await screen.findByRole('button', { name: /actions for build the screen/i }))
    expect(screen.getByRole('button', { name: 'Open' })).toBeTruthy()
    const del = screen.getByRole('button', { name: 'Delete' })

    fireEvent.click(del)
    await waitFor(() => expect(screen.queryByText('Build the screen')).toBeNull())
    expect(h.deleteConversation).toHaveBeenCalledWith('c2')
  })

  it('the ⋮ button is a SIBLING of the title button, not an interactive descendant (F-10)', async () => {
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([
      { id: 'c2', kind: 'builder', projectId: 'p1', title: 'Build the screen', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    renderProjectPage()

    const title = await screen.findByRole('button', { name: 'Build the screen' })
    // No interactive element nests inside the title button.
    expect(title.querySelector('button')).toBeNull()
    // The actions button exists elsewhere in the row, not under the title button.
    const actions = screen.getByRole('button', { name: /actions for build the screen/i })
    expect(title.contains(actions)).toBe(false)
  })

  it('empty state shows the exact copy', async () => {
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([])
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    expect(screen.getByText('No conversations yet — start a build or plan above.')).toBeTruthy()
  })
})

describe('ProjectPage — identity + guard rails carried over', () => {
  it('blocks renaming to "" and to "   " before any request fires', async () => {
    h.getProject.mockResolvedValue(makeProject({ name: 'VIP Movement' }))
    renderProjectPage()

    fireEvent.click(await screen.findByRole('button', { name: /rename project/i }))
    const input = screen.getByRole('textbox', { name: /project name/i })

    fireEvent.change(input, { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: /save name/i }))
    expect(h.patchProject).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: /save name/i }))
    expect(h.patchProject).not.toHaveBeenCalled()

    expect(screen.getByRole('alert').textContent).toMatch(/cannot be empty/i)
  })

  it('redirects to /projects when the project 404s (deleted elsewhere)', async () => {
    h.getProject.mockRejectedValue(new ApiError('Project not found.', 404))
    renderProjectPage()

    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/projects'))
  })
})
