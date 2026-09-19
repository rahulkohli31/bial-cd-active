/**
 * THE UPHELD DEFECTS IN THESE FILES.
 *
 * What remains, gathered here because they share one property: every one of them is a silence.
 * Nothing on the screen was wrong — something simply did not happen, and the citizen had no way
 * to know. A suite that asserts what IS rendered would have stayed green through all of them,
 * which is why each scenario below asserts the thing that was missing.
 *
 * THE FIRST DESCRIBE IS INVERTED, NOT DELETED. It used to pin a dialog that asked before an exit
 * discarded unsaved work. Shutdown now writes the work back automatically before a container is
 * destroyed, so there is nothing left to lose by asking, and the prompt is gone. The exits it
 * enumerated (sign-out, the back control) still exist and still have to complete cleanly; only the
 * question changes.
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
const ProfileCluster = (await import('../../layout/ProfileCluster')).default

const PROJECT: Project = {
  id: 'pB',
  name: 'Visitor Log',
  description: 'A tracked movement.',
  appId: 'app-1',
  appStatus: null,
  hasRelaunchableSnapshot: true,
  hasSavedSnapshot: null,
  isServing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'owner',
}

function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>
}

/** THE ROUTE'S HALF, which the real `ProjectPage` performs. */
function Route_({ children }: { children: React.ReactNode }) {
  usePublishHeading({ projectId: PROJECT.id, projectName: PROJECT.name, chatTitle: null, chatKind: null })
  return <>{children}</>
}

/**
 * THE PROFILE CLUSTER IS MOUNTED BESIDE THE WORKSPACE, NOT INSIDE IT, because that is where it
 * is: the navigation frames the workspace rather than living in it — the same arrangement
 * `App.tsx` ships.
 */
function Workspace() {
  return (
    <MemoryRouter initialEntries={['/projects/pB']}>
      <Where />
      <ProfileCluster />
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

const exitPrompt = () => screen.queryByText(/save your changes before you go/i)

/**
 * Sign out through the navigation's profile menu. It is a Radix `DropdownMenu`, so the trigger
 * opens on POINTERDOWN — a `click` does nothing — and `Sign out` carries `role="menuitem"`,
 * which wins over the underlying element, so a `getByRole('button')` finds nothing.
 */
const signOutFromTheProfileMenu = async () => {
  fireEvent.pointerDown(screen.getByTestId('profile-cluster'))
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Sign out' }))
}

describe('★ no exit asks before leaving, whether or not there is unsaved work', () => {
  it('SIGNING OUT completes at once, with unsaved work in play', async () => {
    // INVERTED, NOT DELETED: this used to pin a dialog stopping the most final button on the
    // screen. Shutdown now writes the work back automatically under an ancestry guard, so sign-out
    // has nothing left to ask about.
    dirtyAndAlive()
    render(<Workspace />)
    await waitFor(() => expect(screen.getByTestId('save-project')).toBeTruthy())

    await signOutFromTheProfileMenu()

    await waitFor(() => expect(api.logout).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('login')).toBeTruthy())
    expect(exitPrompt()).toBeNull()
  })

  it('signing out with nothing unsaved still signs out', async () => {
    // PAIRED WITH A LIVENESS CHECK, because "it signed out" also passes when the component threw
    // and the click landed on nothing: the login screen has to actually be reached.
    render(<Workspace />)
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalled())

    await signOutFromTheProfileMenu()

    await waitFor(() => expect(api.logout).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('login')).toBeTruthy())
    expect(exitPrompt()).toBeNull()
  })

  it('THE BACK CONTROL completes at once too, with unsaved work in play', async () => {
    dirtyAndAlive()
    render(<Workspace />)
    await waitFor(() => expect(screen.getByTestId('save-project')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: 'Back to My Applications' }))

    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/projects'))
    expect(exitPrompt()).toBeNull()
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
