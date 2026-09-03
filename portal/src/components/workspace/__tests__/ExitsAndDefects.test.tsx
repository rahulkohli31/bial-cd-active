/**
 * THE UPHELD DEFECTS IN THESE FILES (plan 002, U11).
 *
 * Five things the review upheld, gathered here because they share one property: every one of them
 * is a silence. Nothing on the screen was wrong — something simply did not happen, and the citizen
 * had no way to know. A suite that asserts what IS rendered would have stayed green through all
 * five, which is why each scenario below asserts the thing that was missing.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import type { Project } from '../../../utils/projectApi'
import { usePublishHeading } from '../workspaceChannel'

const api = vi.hoisted(() => ({
  fetchPreviewState: vi.fn(),
  fetchSaveState: vi.fn(),
  relaunchPreview: vi.fn(),
  saveProject: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('../../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/buildSessionApi')>()),
  fetchPreviewState: api.fetchPreviewState,
  fetchSaveState: api.fetchSaveState,
  relaunchPreview: api.relaunchPreview,
  saveProject: api.saveProject,
}))
vi.mock('../../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/auth')>()),
  isAuthenticated: () => true,
  getStoredUser: () => ({
    email: 'asha@rvaiglobal.com',
    display_name: 'Asha',
    chat_kinds: [
      { value: 'plan', name: 'Plan', description: 'Shape a plan first.' },
      { value: 'build', name: 'Build', description: 'Change the live app.' },
    ],
  }),
  logout: api.logout,
}))
vi.mock('../../../utils/usage', () => ({ fetchUsageToday: vi.fn(), onUsageChanged: () => () => {} }))
vi.mock('../../../utils/appRegistryApi', () => ({ fetchAppStatusCounts: vi.fn() }))
vi.mock('../../../utils/attachmentApi', () => ({ revokeAllAttachmentUrls: vi.fn() }))
vi.mock('../../FeedbackModal', () => ({ default: () => null }))
vi.mock('../../PublishStatusChip', () => ({ default: () => <span data-testid="publish-chip-stub" /> }))
vi.mock('../../LivePreview', () => ({ default: () => <div data-testid="live-preview" /> }))
vi.mock('../../projects/ProjectDescriptionEditor', () => ({
  default: () => <div data-testid="description-editor" />,
}))

const WorkspaceShell = (await import('../WorkspaceShell')).default
const ProjectWorkspace = (await import('../ProjectWorkspace')).default

const PROJECT: Project = {
  id: 'pB',
  name: 'Visitor Log',
  description: 'A tracked movement.',
  appId: 'app-1',
  appStatus: null,
  hasRelaunchableSnapshot: true,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
}

function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>
}

/**
 * THE ROUTE'S HALF, which the real `ProjectPage` performs. Reproduced rather than skipped because
 * one of the scenarios below is about the guard NAMING the project, and the name reaches it
 * through the heading the route publishes — a harness that omitted it would assert the fallback
 * and call it a pass.
 */
function Route_({ children }: { children: React.ReactNode }) {
  usePublishHeading({ projectId: PROJECT.id, projectName: PROJECT.name, chatTitle: null, chatKind: null })
  return <>{children}</>
}

function Workspace() {
  return (
    <MemoryRouter initialEntries={['/projects/pB']}>
      <Where />
      <Routes>
        <Route element={<WorkspaceShell />}>
          <Route
            path="/projects/:projectId"
            element={
              <Route_>
                <ProjectWorkspace project={PROJECT} onProjectUpdate={() => {}} />
              </Route_>
            }
          />
        </Route>
        <Route path="/login" element={<div data-testid="login" />} />
        <Route path="/projects" element={<div data-testid="projects-list" />} />
      </Routes>
    </MemoryRouter>
  )
}

/** The workspace is alive and has work the saved bundle does not — the state every exit matters in. */
const dirtyAndAlive = () => {
  api.fetchPreviewState.mockResolvedValue({
    state: 'alive', alive: true, previewUrl: 'https://app/', occupyingProjectName: null,
    occupyingProjectId: null, restorable: true,
  })
  api.fetchSaveState.mockResolvedValue({ appId: 'app-1', dirty: true, containerHead: 'aaa', savedHead: 'bbb' })
}

beforeEach(() => {
  vi.clearAllMocks()
  api.logout.mockResolvedValue(true)
  api.saveProject.mockResolvedValue({ appId: 'app-1', headSha: 'ccc' })
  api.fetchPreviewState.mockResolvedValue({
    state: 'asleep', alive: false, previewUrl: null, occupyingProjectName: null,
    occupyingProjectId: null, restorable: true,
  })
  api.fetchSaveState.mockResolvedValue(null)
})
afterEach(cleanup)

const guardDialog = () => screen.queryByText(/save your changes before you go/i)

describe('★ no exit discards unsaved work in silence', () => {
  it('SIGNING OUT asks first — the most final button on the screen, and it did not', async () => {
    // Every nav LINK in the bar was routed through the guard and the one control that ends the
    // session entirely was not. Mutation receipt: call `handleLogout` directly again and this
    // goes red while the sign-out-when-clean scenario below stays green.
    dirtyAndAlive()
    render(<Workspace />)
    await waitFor(() => expect(screen.getByTestId('rail-save-state')).toBeTruthy())

    fireEvent.click(screen.getByText('Asha'))
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))

    await waitFor(() => expect(guardDialog()).toBeTruthy())
    expect(api.logout).not.toHaveBeenCalled()
    expect(screen.queryByTestId('login')).toBeNull()
  })

  it('signing out with nothing unsaved still signs out', async () => {
    // PAIRED WITH A LIVENESS CHECK, because "it signed out" also passes when the component threw
    // and the click landed on nothing: the login screen has to actually be reached.
    render(<Workspace />)
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalled())

    fireEvent.click(screen.getByText('Asha'))
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))

    await waitFor(() => expect(api.logout).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('login')).toBeTruthy())
    expect(guardDialog()).toBeNull()
  })

  it('THE BACK CONTROL asks first too', async () => {
    dirtyAndAlive()
    render(<Workspace />)
    await waitFor(() => expect(screen.getByTestId('rail-save-state')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: 'Back to projects' }))

    await waitFor(() => expect(guardDialog()).toBeTruthy())
    expect(screen.getByTestId('where').textContent).toBe('/projects/pB')
  })

  it('cancelling the guard leaves the citizen signed in and where they were', async () => {
    dirtyAndAlive()
    render(<Workspace />)
    await waitFor(() => expect(screen.getByTestId('rail-save-state')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Back to projects' }))
    await waitFor(() => expect(guardDialog()).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: /stay/i }))

    await waitFor(() => expect(guardDialog()).toBeNull())
    expect(screen.getByTestId('where').textContent).toBe('/projects/pB')
    expect(api.logout).not.toHaveBeenCalled()
    // LIVENESS: the workspace is still rendering, not merely un-navigated.
    expect(screen.getByTestId('description-editor')).toBeTruthy()
  })

  it("★ names WHICH project's work is at risk", async () => {
    // "This app has changes that are not saved yet" is ambiguous the moment somebody has more than
    // one project — and both exits this dialog covers are taken while thinking about another one.
    dirtyAndAlive()
    render(<Workspace />)
    await waitFor(() => expect(screen.getByTestId('rail-save-state')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: 'Back to projects' }))

    await waitFor(() => expect(guardDialog()).toBeTruthy())
    expect(document.body.textContent).toContain('“Visitor Log” has changes that are not saved yet')
  })
})

describe('★ save is reachable from the project screen', () => {
  it('offers a pressable Save when the workspace is dirty, and it saves', async () => {
    // THE DEFECT: the only writer of the bundle lived on the conversation surface, so a citizen
    // who had built something, come back to the project screen and closed the tab lost it — with
    // the rail telling them, correctly, that they had unsaved changes and offering nothing to do
    // about it.
    dirtyAndAlive()
    render(<Workspace />)

    const button = await screen.findByTestId('save-project')
    expect(button.tagName).toBe('BUTTON')
    fireEvent.click(button)

    await waitFor(() => expect(api.saveProject).toHaveBeenCalledWith('pB'))
  })

  it('says so when a save fails, rather than letting it look successful', async () => {
    dirtyAndAlive()
    api.saveProject.mockRejectedValue(new Error('Your workspace is no longer running.'))
    render(<Workspace />)

    fireEvent.click(await screen.findByTestId('save-project'))

    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/no longer running/i))
  })
})
