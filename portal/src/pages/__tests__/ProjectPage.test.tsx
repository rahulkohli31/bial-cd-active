/**
 * ProjectPage (U5): the project home. Covers the identity/URL rules that the
 * cross-platform deploy depends on —
 *   - "Open app" is a plain <a href="/apps/{id}"> full-page nav (NOT a router
 *     <Link>), gated on appStatus === 'approved';
 *   - a new chat opens at a flat /chat/{uuid}?projectId=&kind= URL;
 *   - a 404 (deleted elsewhere) bounces to /projects;
 * plus rename-is-blocked-before-any-request and the chats list.
 *
 * projectApi + conversationApi are mocked at the module boundary; the REAL
 * ProjectDescriptionEditor renders (it only needs the mocked projectApi). A
 * LocationProbe on a catch-all route reports where navigation actually landed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import ProjectPage from '../ProjectPage'
import { ApiError } from '../../utils/apiError'
import type { Project } from '../../utils/projectApi'

const h = vi.hoisted(() => ({
  getProject: vi.fn(),
  patchProject: vi.fn(),
  generateDescription: vi.fn(),
  listProjectConversations: vi.fn(),
}))

vi.mock('../../utils/projectApi', () => ({
  getProject: h.getProject,
  patchProject: h.patchProject,
  generateDescription: h.generateDescription,
}))
vi.mock('../../utils/conversationApi.js', () => ({
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))

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
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ProjectPage', () => {
  it('renders the name, description, app status badge, and the project chats', async () => {
    h.getProject.mockResolvedValue(
      makeProject({ name: 'VIP Movement', description: 'Tracks VIP movements.', appId: 'a1', appStatus: 'approved' }),
    )
    h.listProjectConversations.mockResolvedValue([
      { id: 'c1', kind: 'planning', projectId: 'p1', title: 'Plan the flow', updatedAt: '' },
      { id: 'c2', kind: 'builder', projectId: 'p1', title: 'Build the screen', updatedAt: '' },
    ])
    renderProjectPage()

    expect(await screen.findByRole('heading', { name: 'VIP Movement' })).toBeTruthy()
    expect((screen.getByRole('textbox', { name: /project description/i }) as HTMLTextAreaElement).value).toBe(
      'Tracks VIP movements.',
    )
    expect(screen.getByText('approved')).toBeTruthy()
    expect(screen.getByText('Plan the flow')).toBeTruthy()
    expect(screen.getByText('Build the screen')).toBeTruthy()
    // Distinct per-kind badges.
    expect(screen.getByText('Plan')).toBeTruthy()
    expect(screen.getByText('Build')).toBeTruthy()
  })

  it('"New build chat" navigates to /chat/{uuid}?projectId=p1&kind=builder', async () => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(FIXED_UUID)
    h.getProject.mockResolvedValue(makeProject())
    renderProjectPage()

    fireEvent.click(await screen.findByRole('button', { name: /new build chat/i }))

    await waitFor(() =>
      expect(screen.getByTestId('location').textContent).toBe(`/chat/${FIXED_UUID}?projectId=p1&kind=builder`),
    )
  })

  it('"New plan chat" navigates with kind=planning', async () => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(FIXED_UUID)
    h.getProject.mockResolvedValue(makeProject())
    renderProjectPage()

    fireEvent.click(await screen.findByRole('button', { name: /new plan chat/i }))

    await waitFor(() =>
      expect(screen.getByTestId('location').textContent).toBe(`/chat/${FIXED_UUID}?projectId=p1&kind=planning`),
    )
  })

  it('renders "No app yet" and no Open app link when appId is null', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: null, appStatus: null }))
    renderProjectPage()

    expect(await screen.findByText(/no app yet/i)).toBeTruthy()
    expect(screen.queryByRole('link', { name: /open app/i })).toBeNull()
  })

  it('renders the status badge but NO Open app link when appStatus is draft', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', appStatus: 'draft' }))
    renderProjectPage()

    expect(await screen.findByText('draft')).toBeTruthy()
    expect(screen.queryByRole('link', { name: /open app/i })).toBeNull()
  })

  it('renders Open app as a plain anchor to /apps/{appId} when approved — not a router Link', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'app-123', appStatus: 'approved' }))
    renderProjectPage()

    const link = await screen.findByRole('link', { name: /open app/i })
    expect(link.tagName).toBe('A')
    expect(link.getAttribute('href')).toBe('/apps/app-123')

    // A react-router <Link> would client-route to /apps/app-123 on click and unmount
    // this page. A plain <a> does not — the SPA location must stay put, so the page
    // stays mounted and the catch-all LocationProbe never renders.
    fireEvent.click(link)
    expect(screen.getByRole('heading', { name: 'VIP Movement' })).toBeTruthy()
    expect(screen.queryByTestId('location')).toBeNull()
  })

  it('blocks renaming to "" and to "   " before any request fires', async () => {
    h.getProject.mockResolvedValue(makeProject({ name: 'VIP Movement' }))
    renderProjectPage()

    fireEvent.click(await screen.findByRole('button', { name: /rename project/i }))
    const input = screen.getByRole('textbox', { name: /project name/i }) as HTMLInputElement

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

  it('requests the project’s conversations and badges plan vs build chats distinctly', async () => {
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([
      { id: 'c1', kind: 'planning', projectId: 'p1', title: 'Design chat', updatedAt: '' },
      { id: 'c2', kind: 'builder', projectId: 'p1', title: 'Builder chat', updatedAt: '' },
    ])
    renderProjectPage()

    expect(await screen.findByText('Design chat')).toBeTruthy()
    expect(h.listProjectConversations).toHaveBeenCalledWith('p1')
    expect(screen.getByText('Plan')).toBeTruthy()
    expect(screen.getByText('Build')).toBeTruthy()
  })
})
