/**
 * THE RENAME PENCIL KEEPS THE SAME IDENTITY GUARD `ChatRoute.tsx` ALREADY CARRIES.
 *
 * WHY THIS RENDERS THROUGH THE REAL SHELL, SEPARATELY FROM `ProjectPage.test.tsx`
 *
 * The rename pencil is drawn by `WorkspaceToolbar`, above the Outlet, off `heading.projectName` —
 * `ProjectPage.test.tsx`'s own docblock says so plainly: "a render of this page alone can no
 * longer reach [the rename control] ... covered by `WorkspaceToolbar.test.tsx`". So this suite
 * mounts the real `WorkspaceShell` around the real `ProjectPage`.
 *
 * `ProjectWorkspace` is STUBBED rather than mounted for real. The defect lives entirely in
 * `ProjectPage`'s own `usePublishHeading` call — whether the NAME it publishes agrees with the
 * project actually on screen — and `ProjectWorkspace`'s network stack (preview polling, deployment
 * reads, save state) has nothing to do with that. Its own suite, `ProjectWorkspace.test.tsx`,
 * covers what it renders.
 *
 * THE BUG
 *
 * `projectId` is a route param on a route that is NOT remounted when it changes (`ProjectPage`'s
 * own "three branches are one return" note records this). So moving from one project to another
 * re-renders the SAME `ProjectPage` instance with the OLD `project` state still in hand for the
 * whole width of the new fetch. Without a guard, `usePublishHeading` published the stale
 * project's name against the NEW `projectId` throughout that window — the pencil stayed visually
 * enabled, gated on a name belonging to a project no longer on screen, while `ProjectWorkspace`
 * (the only registrar of the actual rename handler) had already unmounted for the load. A press in
 * that window is a no-op: a live-LOOKING, dead control. `ChatRoute.tsx`'s `projectName` gate
 * already carries the fix
 * for the identical hazard — gate the name on `project.id === projectId`, not on `project` alone.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useNavigate } from 'react-router-dom'
import WorkspaceShell from '../../components/workspace/WorkspaceShell'
import ProjectPage from '../ProjectPage'
import type { Project } from '../../utils/projectApi'

const h = vi.hoisted(() => ({
  authFetch: vi.fn(),
  getProject: vi.fn(),
}))

vi.mock('../../utils/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/api')>()),
  authFetch: h.authFetch,
}))
vi.mock('../../utils/projectApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/projectApi')>()),
  getProject: h.getProject,
}))
// STUBBED — see docblock. Its own suite covers what it renders; this file is only about whether
// the HEADING it would need to agree with agrees with the project actually on screen.
vi.mock('../../components/workspace/ProjectWorkspace', () => ({
  default: () => <div data-testid="project-workspace-stub" />,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))

const makeProject = (over: Partial<Project> = {}): Project => ({
  id: 'pA',
  name: 'Project Alpha',
  description: null,
  appId: null,
  appStatus: null,
  hasRelaunchableSnapshot: null,
  hasSavedSnapshot: null,
  isServing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'owner',
  ...over,
})

/** Switches to another project WITHOUT leaving the route — the real product path: `projectId` is
 *  a route param on a route that is not remounted when it changes (see `ProjectPage.test.tsx`'s
 *  identical control). */
function ProjectSwitch({ to, label }: { to: string; label: string }) {
  const navigate = useNavigate()
  return (
    <button type="button" data-testid={`switch-${to}`} onClick={() => navigate(`/projects/${to}`)}>
      {label}
    </button>
  )
}

/** A re-render with NO change of address at all — the same project, forced from OUTSIDE the
 *  route. A guard keyed on "how many times has this rendered" rather than on identity would be
 *  free to misbehave here, since nothing about the address moved. */
function RerenderHarness({ from }: { from: string }) {
  const [tick, setTick] = useState(0)
  return (
    <>
      <button type="button" data-testid="force-rerender" onClick={() => setTick((t) => t + 1)}>
        rerender {tick}
      </button>
      <MemoryRouter initialEntries={[from]}>
        <Routes>
          <Route element={<WorkspaceShell />}>
            <Route path="/projects/:projectId" element={<ProjectPage />} />
          </Route>
          <Route path="/projects" element={<div data-testid="projects-index" />} />
        </Routes>
      </MemoryRouter>
    </>
  )
}

function renderSwitchable(from: string) {
  return render(
    <MemoryRouter initialEntries={[from]}>
      <Routes>
        <Route element={<WorkspaceShell />}>
          <Route
            path="/projects/:projectId"
            element={
              <>
                <ProjectSwitch to="pA" label="to A" />
                <ProjectSwitch to="pB" label="to B" />
                <ProjectPage />
              </>
            }
          />
        </Route>
        <Route path="/projects" element={<div data-testid="projects-index" />} />
      </Routes>
    </MemoryRouter>,
  )
}

const pencil = () => screen.queryByRole('button', { name: 'Rename project' })

beforeEach(() => {
  vi.clearAllMocks()
  h.authFetch.mockResolvedValue({ ok: true, status: 204, json: async () => ({}) })
})

afterEach(() => cleanup())

describe('the rename pencil keeps the identity guard `ChatRoute.tsx` already has', () => {
  it('★ stays enabled — with the RIGHT name — after navigating between two projects', async () => {
    h.getProject.mockImplementation(async (id: string) =>
      id === 'pA' ? makeProject({ id: 'pA', name: 'Project Alpha' }) : makeProject({ id: 'pB', name: 'Project Bravo' }),
    )
    renderSwitchable('/projects/pA')

    expect(await screen.findByRole('button', { name: 'Rename project' })).toBeTruthy()
    // LIVENESS pairing: the surrounding surface actually rendered, not a crash under a boundary.
    expect(screen.getByTestId('project-workspace-stub')).toBeTruthy()

    fireEvent.click(screen.getByTestId('switch-pB'))

    await waitFor(() => expect(screen.getByTestId('project-workspace-stub')).toBeTruthy())
    expect(pencil()).toBeTruthy()
  })

  it('★ goes quiet rather than dead while the next project is still loading, never a live-LOOKING control bound to the WRONG project', async () => {
    h.getProject.mockResolvedValueOnce(makeProject({ id: 'pA', name: 'Project Alpha' }))
    renderSwitchable('/projects/pA')
    expect(await screen.findByRole('button', { name: 'Rename project' })).toBeTruthy()

    // Project B's fetch hangs — the loading window this unit is about.
    let resolveB: (p: Project) => void = () => {}
    h.getProject.mockReturnValueOnce(
      new Promise<Project>((resolve) => {
        resolveB = resolve
      }),
    )

    fireEvent.click(screen.getByTestId('switch-pB'))

    // LIVENESS FIRST: the screen is doing something, not sitting inside a crashed boundary.
    await screen.findByText('Loading this project…')
    // The pencil is gone rather than standing over a project that is no longer on screen — a
    // guard keyed on identity, so a mismatch hides it instead of leaving it enabled against A's
    // name while the address reads B.
    expect(pencil()).toBeNull()

    resolveB(makeProject({ id: 'pB', name: 'Project Bravo' }))
    await waitFor(() => expect(pencil()).toBeTruthy())
  })

  it('★ the guard keys on IDENTITY, not on render count — an unrelated re-render of the SAME project leaves it enabled', async () => {
    h.getProject.mockResolvedValue(makeProject({ id: 'pA', name: 'Project Alpha' }))
    render(<RerenderHarness from="/projects/pA" />)
    expect(await screen.findByRole('button', { name: 'Rename project' })).toBeTruthy()

    // A guard implemented as "hide on the render right after a project change, show from the
    // render after that" — a RENDER-COUNT heuristic rather than an identity check — would still
    // pass the scenario above (by the time it is observed, it is past its one hidden render) but
    // would flip unpredictably here, where the address and the project never change at all.
    fireEvent.click(screen.getByTestId('force-rerender'))
    fireEvent.click(screen.getByTestId('force-rerender'))
    fireEvent.click(screen.getByTestId('force-rerender'))

    expect(pencil()).toBeTruthy()
    // LIVENESS pairing: still the same, correctly loaded project underneath — not an absence
    // that happens to read as "enabled" because the whole surface fell over.
    expect(screen.getByTestId('project-workspace-stub')).toBeTruthy()
  })
})
