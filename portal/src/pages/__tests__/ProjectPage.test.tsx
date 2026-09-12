/**
 * WHY THIS EXISTS: ProjectPage renders WITHOUT the workspace shell, so there's no pane host, no
 * iframe and no toolbar row in the tree — the project's name, status chip, back control and
 * rename control are drawn by the shell above the Outlet and are covered instead by
 * `WorkspaceToolbar.test.tsx`. The pane's own behaviour (identity across a navigation, the
 * stacked crossing, the framed URL) belongs to `ProjectWorkspace.test.tsx`, which renders through
 * the real shell — a page mounted alone can't see any of it. What this file owns: the page's
 * data, its beacon, the rail's contents, and the affordances that must and must not be on it.
 *
 * projectApi, conversationApi and buildSessionApi are mocked at the module boundary; the real
 * `ProjectWorkspace`, `WorkspaceRail`, `RailComposer` and `ProjectDescriptionEditor` render.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { StrictMode } from 'react'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom'
import ProjectPage from '../ProjectPage'
import ProjectsPage, { PROJECT_GONE_NOTICE } from '../ProjectsPage'
import { ApiError, extractApiMessage } from '../../utils/apiError'
import { beaconsFrom } from './_observeBeacons'
import type { Project } from '../../utils/projectApi'

const h = vi.hoisted(() => ({
  authFetch: vi.fn(),
  getProject: vi.fn(),
  patchProject: vi.fn(),
  listProjectConversations: vi.fn(),
  // THE PROJECTS INDEX'S OWN READS. This is a two-page behaviour — a bounce OUT of this page and
  // a sentence ON that one — so the arrival cases below mount the REAL `ProjectsPage` behind the
  // `/projects` route. It reads a page of rows and the three summary numbers on mount, and an
  // unmocked read would make those tests about the network.
  listProjects: vi.fn(),
  listProjectCounts: vi.fn(),
  fetchPreviewState: vi.fn(),
  fetchSaveState: vi.fn(),
  relaunchPreview: vi.fn(),
}))

// The real `observe` module runs here — its once-per-project-id-per-page-load guard IS the thing
// under test; only the transport (authFetch) is mocked. Every test below therefore uses its OWN
// project id, since module state is per page load and a shared id would silence the next test's mark.
vi.mock('../../utils/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/api')>()),
  authFetch: h.authFetch,
}))
// SPREAD FROM THE REAL MODULE rather than listed exhaustively, because `ProjectsPage` and the row
// and card components under it import names this file has no opinion about (`deleteProject`,
// `createProject`); with a hand-written factory Vitest throws "No X export is defined on the mock"
// at IMPORT time, which fails the file rather than the test. The three overrides are unchanged —
// everything else stays real and goes through the already-mocked `authFetch`.
vi.mock('../../utils/projectApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/projectApi')>()),
  getProject: h.getProject,
  patchProject: h.patchProject,
  listProjects: h.listProjects,
  listProjectCounts: h.listProjectCounts,
}))
vi.mock('../../utils/conversationApi.js', () => ({
  listProjectConversations: h.listProjectConversations,
}))
// Stubbed to a MARKER, not null, so tests can assert WHERE it is mounted — a null stub would let
// the chip silently vanish from either header branch.
vi.mock('../../components/PublishStatusChip', () => ({
  default: ({ projectId }: { projectId: string }) => (
    <span data-testid="publish-chip-stub" data-project={projectId} />
  ),
}))
// `ProjectWorkspace` polls `fetchPreviewState` on a cadence; each scenario sets its own answer.
// The two container-exec reads are stubbed to REJECT rather than resolve, so a regression that
// starts calling them on a stopped project fails loudly here instead of quietly costing an attach.
vi.mock('../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/buildSessionApi')>()),
  fetchPreviewState: h.fetchPreviewState,
  fetchSaveState: h.fetchSaveState,
  fetchCompileState: vi.fn(async () => { throw new Error('a container exec on the project screen') }),
  checkWorkspace: vi.fn(async () => { throw new Error('a container exec on the project screen') }),
  relaunchPreview: h.relaunchPreview,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
// Nothing on this page frames a preview; the stub keeps a transitive import from mounting one.
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
// `chatKindFor` reads the kind catalogue off the cached bootstrap profile — without this mock,
// badges fall back to "Chat"; `chatKind.test.ts` proves the sourcing is dynamic.
vi.mock('../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/auth')>()),
  getStoredUser: () => ({
    chat_kinds: [
      { value: 'plan', name: 'Plan', description: 'Shape a plan first.' },
      { value: 'build', name: 'Build', description: 'Change the live app.' },
    ],
  }),
}))

const makeProject = (over: Partial<Project> = {}): Project => ({
  id: 'p1',
  name: 'VIP Movement',
  description: 'A tracked movement.',
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

/**
 * WHERE THE NAVIGATION LANDED, AND WHAT IT CARRIED.
 *
 * The address and the router state are TWO nodes on purpose: every existing case asserts
 * `getByTestId('location').textContent` against a bare pathname, and nesting the state inside that
 * div would append to the same `textContent` and turn a dozen green assertions red for no reason.
 */
function LocationProbe() {
  const loc = useLocation()
  const carried = (loc.state as { notice?: unknown } | null)?.notice
  return (
    <>
      <div data-testid="location">{loc.pathname + loc.search}</div>
      <div data-testid="location-notice">{typeof carried === 'string' ? carried : ''}</div>
    </>
  )
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

/** A preview-state read, in the shape the wire parser produces one. */
const preview = (over: Record<string, unknown> = {}) => ({
  state: 'never_built',
  alive: false,
  previewUrl: null,
  occupyingProjectName: null,
  occupyingProjectId: null,
  restorable: null,
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  h.authFetch.mockResolvedValue({ ok: true } as Response)
  h.listProjectConversations.mockResolvedValue([])
  h.listProjects.mockResolvedValue({ items: [], page: 1, pageSize: 8, total: 0, totalPages: 0 })
  h.listProjectCounts.mockResolvedValue({ inProduction: 0, totalApplications: 0, inPipeline: 0 })
  h.fetchPreviewState.mockResolvedValue(preview())
  h.fetchSaveState.mockResolvedValue({ appId: 'a1', dirty: false, containerHead: null, savedHead: null })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

/** The observation bodies this render posted — see `_observeBeacons` for the mock contract. */
const beacons = () => beaconsFrom(h.authFetch)

describe('ProjectPage — the composer is unconditional', () => {
  it('no-app project: renders the composer, the description block and the recents — and no app affordances', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: null, appStatus: null }))
    renderProjectPage()

    expect(await screen.findByTestId('rail-app-status')).toBeTruthy()
    // Regression guard: this control must show regardless of whether the project has an app.
    expect(screen.getByPlaceholderText(/Describe what you have in mind/i)).toBeTruthy()
    expect(within(screen.getByTestId('description-rail')).getByRole('button', { name: /edit/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /view app/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /continue building/i })).toBeNull()
  })

  it('built project: the composer is still on top, and the retired affordances are still retired', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', appStatus: 'draft' }))
    h.listProjectConversations.mockResolvedValue([
      { id: 'c2', kind: 'build', projectId: 'p1', title: 'Build the screen', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    renderProjectPage()

    await screen.findByTestId('rail-app-status')
    // NOT collapsed under an app card — the reverted app-first-fold regression.
    expect(screen.getByPlaceholderText(/Describe what you have in mind/i)).toBeTruthy()
    // Inertness guards: a passive code view, a lifecycle badge and a chat reroute do not come
    // back with the running sandbox.
    expect(screen.queryByRole('button', { name: /view app/i })).toBeNull()
    expect(screen.queryByText('draft')).toBeNull()
    expect(screen.queryByRole('button', { name: /continue building/i })).toBeNull()
    expect(screen.queryByRole('link', { name: /open app/i })).toBeNull()
  })
})

describe('ProjectPage — the app arrives behind one deliberate press', () => {
  it('a saved, not-running project offers exactly one start control, and says what IS', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', hasRelaunchableSnapshot: true }))
    h.fetchPreviewState.mockResolvedValue(preview({ state: 'asleep', restorable: true }))
    renderProjectPage()

    await screen.findByTestId('rail-app-status')
    await waitFor(() => expect(h.fetchPreviewState).toHaveBeenCalled())
    // R-16's forbidden words — the positive half ("Your app is saved.") is pinned in
    // `AppPane.test.tsx` and `ProjectWorkspace.test.tsx`, which render the pane this file doesn't.
    expect(document.body.textContent).not.toMatch(/not running/i)
    expect(document.body.textContent).not.toMatch(/\bstopped\b/i)
  })

  it('a project with nothing built offers no start control at all', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: null, hasRelaunchableSnapshot: false }))
    h.fetchPreviewState.mockResolvedValue(preview({ state: 'never_built', restorable: false }))
    renderProjectPage()

    await screen.findByTestId('rail-app-status')
    await waitFor(() => expect(h.fetchPreviewState).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /launch application/i })).toBeNull()
  })

  it('★ opening the screen STARTS NOTHING — the read is the only call it makes', async () => {
    // Mutation receipt: make `ProjectWorkspace` call `relaunchPreview` on mount and this goes red.
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', hasRelaunchableSnapshot: true }))
    h.fetchPreviewState.mockResolvedValue(preview({ state: 'asleep', restorable: true }))
    renderProjectPage()

    await screen.findByTestId('rail-app-status')
    await waitFor(() => expect(h.fetchPreviewState).toHaveBeenCalledWith('p1'))
    expect(h.relaunchPreview).not.toHaveBeenCalled()
  })

  it('★ never asks a stopped project whether it has unsaved work', async () => {
    // `fetchSaveState` runs two `git` execs inside the container — on a stopped workspace that
    // would be an attach the screen caused, so the rail shows the status sentence instead.
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', hasRelaunchableSnapshot: true }))
    h.fetchPreviewState.mockResolvedValue(preview({ state: 'asleep', restorable: true }))
    renderProjectPage()

    await screen.findByTestId('rail-app-status')
    await waitFor(() => expect(h.fetchPreviewState).toHaveBeenCalled())
    expect(h.fetchSaveState).not.toHaveBeenCalled()
    expect(screen.queryByTestId('rail-save-state')).toBeNull()
  })

  it('shows the save half only once the workspace is alive', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', hasRelaunchableSnapshot: true }))
    h.fetchPreviewState.mockResolvedValue(preview({ state: 'alive', alive: true, previewUrl: 'https://app.example/' }))
    h.fetchSaveState.mockResolvedValue({ appId: 'a1', dirty: true, containerHead: 'deadbeefcafe', savedHead: 'abc1234def' })
    renderProjectPage()

    await screen.findByTestId('rail-app-status')
    const saved = await screen.findByTestId('rail-save-state')
    expect(saved.textContent).toMatch(/not saved yet/i)
    // No commit hash here — only whether the container holds work the saved bundle doesn't.
    expect(saved.textContent).not.toContain('abc1234')
  })

  it('an unreadable save state says so rather than reporting that everything is saved', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1' }))
    h.fetchPreviewState.mockResolvedValue(preview({ state: 'alive', alive: true, previewUrl: 'https://app.example/' }))
    h.fetchSaveState.mockResolvedValue({ appId: 'a1', dirty: null, containerHead: null, savedHead: null })
    renderProjectPage()

    const saved = await screen.findByTestId('rail-save-state')
    expect(saved.textContent).toMatch(/could not check/i)
    expect(saved.textContent).not.toMatch(/everything is saved/i)
  })

  it('the removed doors stay gone: no "Open app" link and no "Continue building" anywhere', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'app-123', appStatus: 'approved' }))
    renderProjectPage()

    await screen.findByTestId('rail-app-status')
    expect(screen.queryByRole('link', { name: /open app/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /continue building/i })).toBeNull()
  })
})

describe('ProjectPage — the description rail (pop-up editor)', () => {
  it('shows an Edit button and NO attach / file-input control, with no dialog open by default', async () => {
    h.getProject.mockResolvedValue(makeProject())
    renderProjectPage()

    await screen.findByTestId('rail-app-status')
    const rail = screen.getByTestId('description-rail')
    expect(within(rail).getByRole('button', { name: /edit/i })).toBeTruthy()
    expect(within(rail).queryByRole('button', { name: /attach|upload/i })).toBeNull()
    expect(rail.querySelector('input[type="file"]')).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('clicking Edit opens a pop-up exposing Save and Cancel', async () => {
    h.getProject.mockResolvedValue(makeProject())
    renderProjectPage()

    await screen.findByTestId('rail-app-status')
    const rail = screen.getByTestId('description-rail')
    fireEvent.click(within(rail).getByRole('button', { name: /edit/i }))

    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('button', { name: /^save$/i })).toBeTruthy()
    expect(within(dialog).getByRole('button', { name: /^cancel$/i })).toBeTruthy()
  })
})

/* The chip itself is not on this page — its three scenarios (names the project, gets one even
   with nothing built, never moves or remounts) are pinned in `WorkspaceToolbar.test.tsx`. What
   this page can answer for: the rail says nothing about publishing. */
describe('ProjectPage — publishing is not in the rail', () => {
  it('keeps every word about publishing out of the description section', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1' }))
    renderProjectPage()

    await screen.findByTestId('rail-app-status')
    const rail = screen.getByTestId('description-rail')
    expect(within(rail).queryByTestId('publish-chip-stub')).toBeNull()
    expect(within(rail).getByRole('button', { name: /edit/i })).toBeTruthy()
    expect(rail.textContent).not.toMatch(/publish/i)
    expect(rail.textContent).not.toMatch(/review/i)
  })
})

describe('ProjectPage — an outlet child that owns its own scroller', () => {
  // Without its own scroller, a long conversation list clips with no way to reach the bottom.
  // jsdom does no layout, so what's assertable is the model (classes) rather than the pixels.
  it('declares its own scroller and brings no page frame of its own', async () => {
    h.getProject.mockResolvedValue(makeProject())
    const { container } = renderProjectPage()
    await screen.findByTestId('rail-app-status')

    const main = container.querySelector('main') as HTMLElement
    expect(main).toBeTruthy()
    expect(main.className).toMatch(/overflow-y-auto/)
    // `min-h-0` is what actually lets a flex child scroll: without it the child's min-content
    // height wins and the overflow never has anywhere to happen.
    expect(main.className).toMatch(/min-h-0/)
    expect(container.innerHTML).not.toMatch(/min-h-screen/)
    expect(container.innerHTML).not.toMatch(/100vh/)
  })

  it('★ builds NO second two-column frame of its own', async () => {
    // A rail-plus-pane rebuilt in here would nest a second grid inside the shell's own, and every
    // "the app did not remount" assertion elsewhere would fail on the first navigation to a chat.
    h.getProject.mockResolvedValue(makeProject())
    const { container } = renderProjectPage()
    await screen.findByTestId('rail-app-status')

    // The pane is the shell's sibling of the Outlet, not rebuilt here.
    expect(container.innerHTML).not.toMatch(/grid-cols-/)
    expect(container.querySelector('iframe')).toBeNull()
  })
})

/**
 * No recents list — not hidden, not an empty state: the list, its read, the prop chain and the
 * delete handler are all absent. This is pinned by an absence assertion paired with a liveness
 * check, plus a search over every piece of copy that would have offered it.
 *
 * Deliberate: the only route back to an existing chat, and the only way to delete one, are the
 * owner's decision, not collateral — chats, plans and uploaded files all stay in the database.
 * `chatKindFor`'s own fallback is pinned separately in `utils/__tests__/chatKind.test.ts`.
 */
describe('ProjectPage — nothing points back to a past chat', () => {
  it('★ renders no conversations section, and asks the server for no list', async () => {
    h.getProject.mockResolvedValue(makeProject())
    renderProjectPage()

    await screen.findByTestId('rail-app-status')
    expect(screen.queryByTestId('conversations')).toBeNull()
    expect(h.listProjectConversations).not.toHaveBeenCalled()
    // Paired with a liveness check: an absence assertion passes just as happily when the page
    // crashed and rendered nothing at all.
    expect(screen.getByTestId('description-rail')).toBeTruthy()
    expect(screen.getByPlaceholderText(/Describe what you have in mind/i)).toBeTruthy()
  })

  it('★ offers no way to reach or delete an existing chat, however many the project has', async () => {
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([
      { id: 'c1', kind: 'plan', projectId: 'p1', title: 'Scope the fields', updatedAt: '2026-07-10T00:00:00Z' },
      { id: 'c2', kind: 'build', projectId: 'p1', title: 'Build the screen', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    renderProjectPage()

    await screen.findByTestId('rail-app-status')
    expect(screen.queryByText('Scope the fields')).toBeNull()
    expect(screen.queryByText('Build the screen')).toBeNull()
    expect(screen.queryByRole('button', { name: /^delete$/i })).toBeNull()
    expect(screen.getByTestId('description-rail')).toBeTruthy()
  })

  it('offers no copy anywhere on the screen that promises past conversations', async () => {
    // A removed control isn't fully gone while its advertising copy remains.
    h.getProject.mockResolvedValue(makeProject())
    renderProjectPage()

    await screen.findByTestId('rail-app-status')
    const screenText = document.body.textContent ?? ''
    expect(screenText).not.toMatch(/conversations · this project/i)
    expect(screenText).not.toMatch(/no conversations yet/i)
    expect(screenText).not.toMatch(/past (chats|conversations)/i)
    expect(screenText).not.toMatch(/recent (chats|conversations|builds)/i)
  })
})

describe('ProjectPage — identity + guard rails carried over', () => {
  /* The rename pencil and its dialog live in the shell/`ProjectWorkspace`, unreachable from a lone
     render of this page — pinned instead in `ProjectRenameDialog.test.tsx` (the empty/whitespace
     guard) and `WorkspaceToolbar.test.tsx` (the press that opens it). */

  it('redirects to /projects when the project 404s (deleted elsewhere)', async () => {
    h.getProject.mockRejectedValue(new ApiError('Project not found.', 404))
    renderProjectPage()

    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/projects'))
  })
})

describe('ProjectPage — a dead address says something on the way out', () => {
  /* THE BOUNCE ITSELF IS NOT WHAT CHANGED — the case above still pins it, and both routes
     document the redirect deliberately. What these cases pin is the half that was thrown away:
     the page had the server's own 404 in its hand at the exact moment it decided to say nothing.

     THEY MOUNT THE REAL `ProjectsPage`, because this is a two-page behaviour and neither half is
     worth much alone. A test that asserted only "the navigation carried a `notice`" would stay
     green through an arrival screen that silently ignores it, which is the state the platform
     was actually in. */

  /** Leaves the list, then comes back — the Back press, driven through the router the way a person drives it. */
  function Detour() {
    const navigate = useNavigate()
    return (
      <>
        <button onClick={() => navigate('/elsewhere')}>leave the list</button>
        <button onClick={() => navigate(-1)}>press back</button>
      </>
    )
  }

  function renderThroughToTheList(projectId: string) {
    return render(
      <MemoryRouter initialEntries={[`/projects/${projectId}`]}>
        <Routes>
          <Route path="/projects/:projectId" element={<ProjectPage />} />
          <Route
            path="/projects"
            element={
              <>
                <ProjectsPage />
                <LocationProbe />
                <Detour />
              </>
            }
          />
          <Route path="/elsewhere" element={<><div data-testid="elsewhere" /><Detour /></>} />
        </Routes>
      </MemoryRouter>,
    )
  }

  /** The sentence the list is showing, read from the notice region and nowhere else. `''` when it
   *  is showing none. Scoped rather than `getByText`, because `LocationProbe` deliberately carries
   *  the same words: a whole-document query would answer "found two" — an ambiguity error — where
   *  the test means to answer a question about one region. */
  const listSaid = () => screen.getByTestId('projects-notice').textContent ?? ''
  /** Present only while a notice is up: the region itself is mounted on every render, empty. */
  const noticeIsUp = () => screen.findByTestId('projects-notice-marker')

  it('a 404 bounces to the list AND says one neutral line there', async () => {
    h.getProject.mockRejectedValue(new ApiError('Project not found.', 404))
    renderThroughToTheList('p-206-gone')

    await noticeIsUp()
    expect(listSaid()).toBe(PROJECT_GONE_NOTICE)
    expect(screen.getByTestId('location').textContent).toBe('/projects')
  })

  it('★ the cross-user id and the nonexistent id say the SAME words, byte for byte', async () => {
    /* THE WHOLE POINT IS THAT THEY ARE INDISTINGUISHABLE. A project id belonging to another
       citizen is a deliberately non-leaking 404, so any sentence that could differ
       between these two causes is a sentence that can confirm someone else's project exists.

       THE TWO ERRORS CARRY DIFFERENT SERVER MESSAGES ON PURPOSE, and that is what makes this
       test bite rather than compare a string to itself. The realistic pair is identical — the
       server sends "Project not found." for both — so feeding identical input would assert
       nothing at all. Feeding a message that WOULD leak, and requiring the same client sentence
       anyway, pins the actual invariant: the line is a constant, not `err.message` piped through.
       Mutation check: carry `err.message` instead of `PROJECT_GONE_NOTICE` and this goes red. */
    async function noticeCarriedBy(error: ApiError, projectId: string): Promise<string> {
      h.getProject.mockRejectedValue(error)
      renderThroughToTheList(projectId)
      await noticeIsUp()
      const said = listSaid()
      cleanup()
      return said
    }

    const nonexistent = await noticeCarriedBy(new ApiError('Project not found.', 404), 'p-206-a')
    const crossUser = await noticeCarriedBy(new ApiError('Not permitted for this user.', 404), 'p-206-b')

    expect(crossUser).toBe(nonexistent)
    expect(crossUser).toBe(PROJECT_GONE_NOTICE)
    // …and neither one repeats what the server happened to say.
    expect(crossUser).not.toMatch(/permitted|access|permission/i)
    expect(nonexistent).not.toMatch(/permitted|access|permission/i)
  })

  it('★ a dropped connection says nothing — and does not bounce at all', async () => {
    /* `fetch` rejects with a plain `TypeError` when the connection drops, which is not an
       `ApiError` and carries no status. The project page's 404 branch already refuses it (the
       `instanceof` guard), and this is the case that keeps that guard honest: widen it — drop
       the `instanceof`, or match on "no status" — and a wifi blink starts telling a citizen
       their project is gone. */
    h.getProject.mockRejectedValue(new TypeError('Failed to fetch'))
    renderThroughToTheList('p-206-offline')

    // LIVENESS FIRST: the page is on screen and settled, so the three absences below are absences
    // rather than a crashed tree that renders nothing at all.
    expect(await screen.findByText(/Couldn’t load this project/i)).toBeTruthy()
    expect(screen.queryByText(PROJECT_GONE_NOTICE)).toBeNull()
    expect(screen.queryByTestId('projects-notice')).toBeNull()
    expect(screen.queryByTestId('location')).toBeNull()
  })

  it('★ the line is neutral in presentation, not the red failure toast', async () => {
    /* `ProjectsPage`'s toast channel is documented failure-only — red, `role="alert"`, an
       `AlertCircle`, no auto-dismiss — and nothing here failed. Reusing it would tell a citizen
       in colour that a stale bookmark was their mistake. */
    h.getProject.mockRejectedValue(new ApiError('Project not found.', 404))
    renderThroughToTheList('p-206-neutral')

    await noticeIsUp()
    const said = within(screen.getByTestId('projects-notice')).getByText(PROJECT_GONE_NOTICE)
    // POLITE, NOT ASSERTIVE — and the whole page holds no alert while this is the only thing said.
    expect(screen.getByTestId('projects-notice').getAttribute('role')).toBe('status')
    expect(document.querySelectorAll('[role="alert"]').length).toBe(0)
    // Not the failure channel, and not wearing its clothes.
    expect(screen.queryByTestId('projects-toast')).toBeNull()
    expect(screen.getByTestId('projects-notice-marker')).toBeTruthy()
    expect(screen.getByTestId('projects-notice').innerHTML).not.toMatch(/bg-red/)
    // In the page's own flow, not a bar floating over it.
    expect(said.closest('.fixed')).toBeNull()
  })

  it('★ the notice does not survive a Back press — nor the reload that reads the same entry', async () => {
    /* React Router keeps this in `window.history.state`, which the browser RESTORES on reload and
       REPLAYS on back. Without the consume-and-replace, a citizen who refreshes their list — or
       wanders back to it an hour later — is told again about a project they dealt with long ago.

       The scrubbed entry is asserted directly as well as through the Back press: `location-notice`
       going empty IS what a reload of this address would read. */
    h.getProject.mockRejectedValue(new ApiError('Project not found.', 404))
    renderThroughToTheList('p-206-once')

    await noticeIsUp()
    expect(listSaid()).toBe(PROJECT_GONE_NOTICE)
    // The history entry no longer carries it — which is the reload half.
    await waitFor(() => expect(screen.getByTestId('location-notice').textContent).toBe(''))

    fireEvent.click(screen.getByRole('button', { name: 'leave the list' }))
    expect(await screen.findByTestId('elsewhere')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'press back' }))

    // LIVENESS: we are genuinely back on the list, and it is the list that is silent.
    expect(await screen.findByRole('heading', { name: /your apps/i })).toBeTruthy()
    expect(listSaid()).toBe('')
    expect(screen.queryByTestId('projects-notice-marker')).toBeNull()
  })
})

describe('ProjectPage — a mangled address never shows the validator', () => {
  /* THE OTHER HALF OF "a bad project address". The dead-address case above is the id that
     RESOLVED and stopped existing; this is the id that never resolved at all — a link that
     lost characters on its way to a citizen, which the backend answers with a Pydantic
     `detail[]` about UUID groups.
     The page had that parser sentence in its hand and painted it as the card body.

     THESE CASES STAY ON THE PAGE. That is the deliberate difference from the bounce above: a 422
     never addressed a project, so there is nothing to be sent back from — the card says the
     sentence and the way out is a control, not a redirect. */

  /** The exact envelope a mangled id produces, read through the REAL envelope reader.
   *
   *  Hand-writing the message would make this test about a literal somebody chose; running the
   *  production body through `extractApiMessage` is what makes it about the string the page would
   *  actually be handed — including on the day `flattenValidationDetail` starts joining the
   *  `type` in as well. */
  const MANGLED_ID = '01a06ba7-1d89-7091-8eca-e109f4b'
  const MANGLED_BODY = {
    detail: [
      {
        type: 'uuid_parsing',
        loc: ['path', 'project_id'],
        msg: 'Input should be a valid UUID, invalid group length in group 4: expected 12, found 7',
      },
    ],
  }
  const mangled = () => new ApiError(extractApiMessage(MANGLED_BODY, 422, 'Could not load this project'), 422)

  it('★ says one neutral sentence, and NOT the parser’s account of the id', async () => {
    h.getProject.mockRejectedValue(mangled())
    renderProjectPage(MANGLED_ID)

    // PRESENCE FIRST, and it is what makes the four absences below mean anything: a page that
    // crashed on this branch would satisfy every `not.toMatch` for free.
    expect(await screen.findByText(PROJECT_GONE_NOTICE)).toBeTruthy()
    expect(screen.getByText(/Couldn’t load this project/i)).toBeTruthy()

    const onScreen = document.body.textContent ?? ''
    // The `type`, which no rendering path carries today — pinned so that a future change which
    // starts surfacing `detail[].type` or `err.code` cannot land here quietly.
    expect(onScreen).not.toMatch(/uuid_parsing/i)
    // And the `msg`, which the page genuinely WAS painting.
    expect(onScreen).not.toMatch(/Input should be a valid UUID/i)
    expect(onScreen).not.toMatch(/invalid group length|expected 12|group 4/i)
    // It stays. A 422 is not the 404's involuntary exit — nothing navigated, so the catch-all
    // route never rendered.
    expect(screen.queryByTestId('location')).toBeNull()
  })

  it('★ keeps the way out: the card’s back control survives the load-error branch', async () => {
    /* THE CONTROL THAT MUST NOT BE GATED BY WHAT SILENCES THE PENCIL. Everything else on this
       branch is text; press this and the citizen is somewhere they can act. Gate it on the
       loaded project — the fact the rename control is now gated on — and a dead address
       becomes a dead end with no keyboard route out of it. */
    h.getProject.mockRejectedValue(mangled())
    renderProjectPage(MANGLED_ID)

    fireEvent.click(await screen.findByRole('button', { name: /back to projects/i }))
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/projects'))
    // The involuntary-exit sentence belongs to the dead-address bounce; a press the citizen made carries nothing.
    expect(screen.getByTestId('location-notice').textContent).toBe('')
  })

  it('a server error that is not a 422 still says what the server said', async () => {
    // The narrow catch, kept narrow. Envelope-1 messages are written for citizens and replacing
    // every one of them with the neutral line would tell somebody their project is gone when the
    // control-plane merely fell over.
    h.getProject.mockRejectedValue(new ApiError('The workspace service is restarting.', 503))
    renderProjectPage('p-207-503')

    expect(await screen.findByText('The workspace service is restarting.')).toBeTruthy()
    expect(screen.queryByText(PROJECT_GONE_NOTICE)).toBeNull()
  })
})

describe('ProjectPage — the project-open mark', () => {
  /** The page under React's development double-mount, which is how it actually runs in dev. */
  function renderTwiceOver(projectId: string) {
    return render(
      <StrictMode>
        <MemoryRouter initialEntries={[`/projects/${projectId}`]}>
          <Routes>
            <Route path="/projects/:projectId" element={<ProjectPage />} />
            <Route path="*" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    )
  }

  it('marks the project open ONCE under StrictMode’s double mount', async () => {
    // Feeds the project-to-chat drop-off ratio (`1 − project_opened_chat / project_opened`), in
    // the mode the app actually runs in during development. The load effect's own `active` flag
    // already drops the first invocation's continuation, so this alone would pass even with the
    // once-per-project-id guard removed — the guard itself is pinned by the next test and by
    // `observe.test.ts` — where removing it goes red.
    h.getProject.mockResolvedValue(makeProject({ id: 'p-strict', appId: 'a1' }))
    renderTwiceOver('p-strict')

    await screen.findByTestId('rail-app-status')
    await waitFor(() => expect(beacons()).toEqual([{ name: 'project_opened' }]))
  })

  it('★ counts ONE visit when the citizen comes back to the same project in one page load', async () => {
    // The case the `active` flag above does NOT cover: a real second mount with its own effect
    // that runs to completion. "A visit" is one project id per page LOAD — opening a project,
    // leaving, and returning counts once.
    //
    // Mutation check: remove the once-per-project-id guard and this goes red.
    h.getProject.mockResolvedValue(makeProject({ id: 'p-return', appId: 'a1' }))
    renderProjectPage('p-return')
    await screen.findByTestId('rail-app-status')
    await waitFor(() => expect(beacons()).toEqual([{ name: 'project_opened' }]))

    cleanup()
    renderProjectPage('p-return')
    await screen.findByTestId('rail-app-status')

    expect(beacons()).toEqual([{ name: 'project_opened' }])
  })

  it('★ starts no first-view clock for a project with nothing built', async () => {
    // Still OPENED, but there's no app to first-see — a later reveal must record nothing, or
    // this number and the sandbox-first number would answer different questions.
    h.getProject.mockResolvedValue(makeProject({ id: 'p-noapp', appId: null }))
    renderProjectPage('p-noapp')

    await screen.findByTestId('rail-app-status')
    await waitFor(() => expect(beacons()).toEqual([{ name: 'project_opened' }]))

    const { markAppVisible } = await import('../../utils/observe')
    markAppVisible('p-noapp')
    expect(beacons()).toEqual([{ name: 'project_opened' }])
  })

  it('marks nothing at all when the project cannot be loaded', async () => {
    // A visit that never resolved a project is not a visit to one.
    h.getProject.mockRejectedValue(new ApiError('boom', 500))
    renderProjectPage('p-broken')

    await screen.findByText(/couldn.t load this project/i)
    expect(beacons()).toEqual([])
  })

  it('★ fires the beacon from exactly ONE production call site (the double-fire guard)', async () => {
    // Structural, catching what the four behavioural tests above cannot: a second tracker added
    // inside `ProjectWorkspace.tsx` would bypass `observe.ts`'s per-project guard rather than
    // defeat it, and nothing in the UI would reflect the count — it would be wrong in silence.
    const files = import.meta.glob('../../{pages,components}/**/*.{ts,tsx}', {
      query: '?raw',
      import: 'default',
      eager: true,
    })
    const callers = Object.entries(files as Record<string, string>)
      // Vite normalises a glob key relative to THIS file, so a sibling suite comes back as
      // `./ChatRoute.test.tsx` with no `__tests__` left in it — filter on the suffix, not the dir.
      .filter(([path]) => !/\.test\.tsx?$/.test(path) && !path.includes('__tests__'))
      .filter(([, source]) => /markProjectOpened\s*\(/.test(source))
      .map(([path]) => path.replace(/^.*\/src\//, '').replace(/^\.\.\/\.\.\//, ''))
      .sort()

    expect(callers).toEqual(['../ProjectPage.tsx'])
  })
})

describe('the project skeleton keeps WORDS and a busy state', () => {
  /** Every live region currently SAYING the given thing — see the twin helper in `App.test.jsx`. */
  const regionsSaying = (re: RegExp): Element[] =>
    Array.from(document.querySelectorAll('[aria-live], [role="status"], [role="alert"]')).filter(
      (el) => re.test(el.textContent ?? ''),
    )

  /** A control that moves to ANOTHER project without leaving the route — see the test below. */
  function ProjectSwitch({ to }: { to: string }) {
    const navigate = useNavigate()
    return (
      <button type="button" data-testid="switch-project" onClick={() => navigate(`/projects/${to}`)}>
        switch
      </button>
    )
  }

  function renderSwitchable(from: string, to: string) {
    return render(
      <MemoryRouter initialEntries={[`/projects/${from}`]}>
        <Routes>
          <Route
            path="/projects/:projectId"
            element={
              <>
                <ProjectPage />
                <ProjectSwitch to={to} />
              </>
            }
          />
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    )
  }

  it('★ says what it is doing, marks the box busy, and does it in exactly ONE region', () => {
    // `index.css` suppresses `.animate-pulse`, so with motion off these two grey bars sit
    // perfectly still and the screen says nothing at all about why it is empty.
    h.getProject.mockReturnValue(new Promise(() => {}))
    renderProjectPage('p-wait-words')

    expect(screen.getByText('Loading this project…')).toBeTruthy()
    // Said ONCE — no `sr-only` duplicate beside the visible sentence.
    expect(screen.getAllByText('Loading this project…')).toHaveLength(1)
    const regions = regionsSaying(/Loading this project/)
    expect(regions).toHaveLength(1)
    const region = screen.getByTestId('project-wait')
    expect(regions[0]).toBe(region)
    expect(region.querySelector('[aria-busy="true"]')).toBeTruthy()
  })

  it('★ the region is already in the tree, EMPTY, before the skeleton appears — and it is the SAME node', async () => {
    // WHY THIS ARM EXISTS. This page used to be three early returns, and an early return
    // cannot carry a live region: the region is born holding the sentence, which several
    // reader-and-browser combinations miss entirely. Move the region back inside the `loading`
    // branch — mount it together with its text — and the empty-region assertion below goes red.
    h.getProject.mockResolvedValue(makeProject({ id: 'p-wait-before' }))
    renderSwitchable('p-wait-before', 'p-wait-after')
    // The project has landed: the wait is NOT running.
    expect(await screen.findByTestId('rail-app-status')).toBeTruthy()

    const before = screen.getByTestId('project-wait')
    expect(before.textContent).toBe('')
    expect(regionsSaying(/Loading this project/)).toHaveLength(0)

    // `projectId` is a param on a route that is NOT remounted when it changes, so this is the
    // real product path in which a settled screen flips back to loading.
    h.getProject.mockReturnValue(new Promise(() => {}))
    fireEvent.click(screen.getByTestId('switch-project'))

    await waitFor(() => expect(before.textContent).toContain('Loading this project…'))
    expect(screen.getByTestId('project-wait')).toBe(before)
    expect(regionsSaying(/Loading this project/)).toHaveLength(1)
  })
})
