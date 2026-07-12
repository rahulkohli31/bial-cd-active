/**
 * ProjectPage (U2/U3): the project home is the builder.
 *
 * What this pins:
 *   - the Sandbox builder (`ProjectBuilder`) renders UNCONDITIONALLY — theme selector and
 *     sample prompts present whether or not the project has an app (the exact regression
 *     that caused the reverted app-first fold);
 *   - a built project ADDS an App block above the builder (status badge + Continue building
 *     + inline preview), never substitutes it;
 *   - "Open app" is a plain <a href="/apps/{id}"> (NOT a router <Link>), gated on approved;
 *   - the description sits in a right rail with Save + Generate and NO attach control (R3);
 *   - conversations are a plain recents list (icon · title · date · ⋮), no BUILD/PLAN badges,
 *     no new-chat buttons; the ⋮ menu opens + deletes optimistically;
 *   - a 404 (deleted elsewhere) bounces to /projects, and rename is blocked before any request.
 *
 * projectApi + conversationApi + builderHistory are mocked at the module boundary; the REAL
 * ProjectBuilder and ProjectDescriptionEditor render (they only need the mocked APIs). A
 * LocationProbe on a catch-all route reports where navigation actually landed. LivePreview is
 * stubbed to a marker so the inline preview's presence is asserted without an iframe.
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
  getBuild: vi.fn(),
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
vi.mock('../../utils/builderHistory.js', () => ({ getBuild: h.getBuild }))
vi.mock('../../utils/chatHistory.js', () => ({ relativeTime: () => '1h ago' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({
  default: ({ previewCode }: { previewCode: string | null }) => (
    <div data-testid="live-preview">{previewCode ?? 'no-code'}</div>
  ),
}))

const FIXED_UUID = '11111111-1111-4111-8111-111111111111'

const makeProject = (over: Partial<Project> = {}): Project => ({
  id: 'p1',
  name: 'VIP Movement',
  description: 'A tracked movement.',
  appId: null,
  appStatus: null,
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
  h.getBuild.mockResolvedValue({ code: { current: { source: 'export default function PreviewApp(){}' } } })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ProjectPage — the builder is unconditional', () => {
  it('no-app project: renders the builder (theme selector + sample prompts), recents, and the description rail — no App block', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: null, appStatus: null }))
    renderProjectPage()

    expect(await screen.findByRole('heading', { name: 'VIP Movement' })).toBeTruthy()
    // The builder is present: its theme selector and sample prompts render.
    expect(screen.getByRole('button', { name: /Bangalore Airport Theme/i })).toBeTruthy()
    expect(screen.getByText('Resource Management')).toBeTruthy()
    // The description rail.
    expect(screen.getByRole('textbox', { name: /project description/i })).toBeTruthy()
    // No App block: no inline preview, no status badge, no Continue building.
    expect(screen.queryByTestId('live-preview')).toBeNull()
    expect(screen.queryByRole('button', { name: /continue building/i })).toBeNull()
  })

  it('built project (draft): App block (badge + Continue building + inline preview) AND the builder still below it with its theme selector + sample prompts', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', appStatus: 'draft' }))
    h.listProjectConversations.mockResolvedValue([
      { id: 'c2', kind: 'builder', projectId: 'p1', title: 'Build the screen', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    renderProjectPage()

    // App block.
    expect(await screen.findByText('draft')).toBeTruthy()
    expect(screen.getByRole('button', { name: /continue building/i })).toBeTruthy()
    expect(screen.getByTestId('live-preview')).toBeTruthy()
    // The builder is NOT collapsed — this is the reverted regression the fold must avoid.
    expect(screen.getByRole('button', { name: /Bangalore Airport Theme/i })).toBeTruthy()
    expect(screen.getByText('Resource Management')).toBeTruthy()
  })

  it('inline preview is fed the app’s stored current code (getBuild of the newest builder chat)', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', appStatus: 'draft' }))
    h.listProjectConversations.mockResolvedValue([
      { id: 'old', kind: 'builder', projectId: 'p1', title: 'Old build', updatedAt: '2026-07-09T00:00:00Z' },
      { id: 'new', kind: 'builder', projectId: 'p1', title: 'New build', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    h.getBuild.mockResolvedValue({ code: { current: { source: 'THE-CURRENT-CODE' } } })
    renderProjectPage()

    await waitFor(() => expect(screen.getByTestId('live-preview').textContent).toBe('THE-CURRENT-CODE'))
    // Read from the NEWEST builder conversation, not the older one.
    expect(h.getBuild).toHaveBeenCalledWith('new')
  })
})

describe('ProjectPage — the App block affordances', () => {
  it('approved: renders "Open app" as a plain anchor to /apps/{appId}, not a router Link', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'app-123', appStatus: 'approved' }))
    renderProjectPage()

    const link = await screen.findByRole('link', { name: /open app/i })
    expect(link.tagName).toBe('A')
    expect(link.getAttribute('href')).toBe('/apps/app-123')

    // A plain <a> does not client-route — the page stays mounted, the catch-all never renders.
    fireEvent.click(link)
    expect(screen.getByRole('heading', { name: 'VIP Movement' })).toBeTruthy()
    expect(screen.queryByTestId('location')).toBeNull()
  })

  it('draft: no "Open app" link', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', appStatus: 'draft' }))
    renderProjectPage()

    await screen.findByText('draft')
    expect(screen.queryByRole('link', { name: /open app/i })).toBeNull()
  })

  it('Continue building navigates to /chat/{uuid}?projectId=p1&kind=builder — no ProjectPicker', async () => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(FIXED_UUID)
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', appStatus: 'draft' }))
    renderProjectPage()

    fireEvent.click(await screen.findByRole('button', { name: /continue building/i }))
    await waitFor(() =>
      expect(screen.getByTestId('location').textContent).toBe(`/chat/${FIXED_UUID}?projectId=p1&kind=builder`),
    )
    expect(screen.queryByText(/lives in a project/i)).toBeNull()
  })
})

describe('ProjectPage — the description rail (R3)', () => {
  it('exposes Save and Generate and NO attach / file-input control', async () => {
    h.getProject.mockResolvedValue(makeProject())
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    const rail = screen.getByTestId('description-rail')
    expect(within(rail).getByRole('button', { name: /save/i })).toBeTruthy()
    expect(within(rail).getByRole('button', { name: /^generate$/i })).toBeTruthy()
    expect(within(rail).queryByRole('button', { name: /attach|upload/i })).toBeNull()
    expect(rail.querySelector('input[type="file"]')).toBeNull()
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
