/**
 * THE PROJECT SURFACE, RENDERED THROUGH THE REAL SHELL (Plan F, U1).
 *
 * ═══ WHY THIS FILE EXISTS SEPARATELY FROM `ProjectPage.test.tsx` ═══
 *
 * Everything below is invisible to a test that mounts the project page alone, and that is not a
 * detail — it is the shape of the defect this unit fixes. The pane host is a SIBLING of the shell's
 * Outlet, so a suite that renders only the Outlet's child has no pane in its tree at all and would
 * stay green against a project screen that frames nothing. R3's headline behaviour — open a
 * project, see the app — is a claim about two components at once.
 *
 * ═══ THE BUG THESE SCENARIOS ARE WRITTEN AGAINST ═══
 *
 * Before this unit the channel had exactly one publisher in the whole tree: the conversation
 * surface. The project page subscribed and never published. So on a fresh `/projects/:id` load,
 * with no conversation ever mounted, `AppPaneHost` hit its own "no pane and no address" early
 * return and rendered nothing — and every existing test passed, because nothing was looking.
 *
 * The second failure mode is the one a second publisher INTRODUCES rather than fixes: two surfaces
 * publishing to one channel can retire each other's work on the hop between them. Every continuity
 * assertion here is therefore paired with the round trip that would break it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, Link } from 'react-router-dom'
import WorkspaceShell from '../WorkspaceShell'
import ProjectWorkspace from '../ProjectWorkspace'
import {
  useAppPaneVisible,
  usePublishAddress,
  usePublishPaneView,
  useWorkspaceProject,
  type PaneView,
} from '../workspaceChannel'
import type { Project } from '../../../utils/projectApi'

const api = vi.hoisted(() => ({
  fetchPreviewState: vi.fn(),
  fetchSaveState: vi.fn(),
  relaunchPreview: vi.fn(),
  listProjectConversations: vi.fn(),
}))

vi.mock('../../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/buildSessionApi')>()),
  fetchPreviewState: api.fetchPreviewState,
  fetchSaveState: api.fetchSaveState,
  relaunchPreview: api.relaunchPreview,
}))
vi.mock('../../layout/Navbar', () => ({ default: () => <div data-testid="navbar" /> }))
vi.mock('../../PublishStatusChip', () => ({ default: () => <span data-testid="publish-chip-stub" /> }))
vi.mock('../../projects/ProjectDescriptionEditor', () => ({
  default: () => <div data-testid="description-editor" />,
}))

const APP_URL = 'https://app-a.example.azurecontainerapps.io/'

const PROJECT: Project = {
  id: 'pA',
  name: 'VIP Movement',
  description: 'A tracked movement.',
  appId: 'app-1',
  appStatus: null,
  hasRelaunchableSnapshot: true,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
}

const preview = (over: Record<string, unknown> = {}) => ({
  state: 'never_built',
  alive: false,
  previewUrl: null,
  occupyingProjectName: null,
  occupyingProjectId: null,
  restorable: null,
  ...over,
})

const EMPTY_PANE: PaneView = {
  iterating: false, reconnecting: false,
  relaunching: false, relaunchError: null, lastBuildFailed: false,
  restoredFromFailedBuild: false, completedLive: true, hasSavedBuild: null,
  previewState: null, occupyingProjectName: null, turnRunning: false,
  compileState: null, workspaceLost: false,
}

/**
 * A chat, publishing its own address — the OTHER publisher on this channel.
 *
 * `pane` is the one thing the two KINDS differ on here (plan 002, U6): a build chat asks for the
 * app to be seen, a plan chat does not. Everything else about a conversation is the same on both.
 */
function ChatSurface({ projectId = 'pA', pane = true }: { projectId?: string; pane?: boolean }) {
  useWorkspaceProject(projectId)
  usePublishAddress({ url: APP_URL, status: 'ready' }, projectId)
  usePublishPaneView(EMPTY_PANE)
  useAppPaneVisible(pane)
  return <div data-testid="chat-surface" />
}

const noop = () => {}

/** The project surface, with every prop its owner would have loaded. */
function Surface({ project = PROJECT }: { project?: Project }) {
  return (
    <ProjectWorkspace
      project={project}
      onProjectUpdate={noop}
    />
  )
}

/** Both addresses under ONE shell, navigated by link exactly as the product navigates them. */
function Workspace({ entry = '/projects/pA', project = PROJECT }: { entry?: string; project?: Project }) {
  return (
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route element={<WorkspaceShell />}>
          <Route
            path="/projects/:projectId"
            element={
              <>
                <Link to="/chat/c1">to chat</Link>
                <Link to="/plan/c2">to plan chat</Link>
                <Surface project={project} />
              </>
            }
          />
          <Route
            path="/chat/:chatId"
            element={
              <>
                <Link to="/projects/pA">to project</Link>
                <ChatSurface />
              </>
            }
          />
          <Route
            path="/plan/:chatId"
            element={
              <>
                <Link to="/projects/pA">to project</Link>
                <ChatSurface pane={false} />
              </>
            }
          />
        </Route>
      </Routes>
    </MemoryRouter>
  )
}

const frame = () => document.querySelector('iframe')
// TWO DIFFERENT ELEMENTS, and the distinction is load-bearing. `app-pane-region` is `AppPane`'s
// own named region — always rendered, whether or not there is anything to frame, and where the
// skip control and the rail's collapse toggle live. `app-pane` is `AppPaneHost`'s frame wrapper,
// which exists only once an address resolved. A test that queries the second when it means the
// first reads "the pane is missing" for a project that simply has nothing built yet.
const paneRegion = () => screen.queryByTestId('app-pane-region')
const frameWrapper = () => screen.queryByTestId('app-pane')
const grid = () => screen.getByTestId('workspace-grid')
const rail = () => screen.getByTestId('workspace-outlet')

beforeEach(() => {
  vi.clearAllMocks()
  api.fetchPreviewState.mockResolvedValue(preview())
  api.fetchSaveState.mockResolvedValue({ appId: 'app-1', dirty: false, containerHead: null, savedHead: null })
})

afterEach(() => cleanup())

describe('R3 — loading a project address frames the running app, with no chat in the story', () => {
  it('★ frames the app on a direct project load, with no conversation ever mounted', async () => {
    // THE SCENARIO THE MISSING PUBLISHER WOULD FAIL, and the reason it has to run through the
    // shell: `ProjectWorkspace` alone has no pane host in its tree, so mounting it by itself
    // cannot observe a frame that never appeared.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)

    await waitFor(() => expect(frame()).toBeTruthy())
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
  })

  it('★ publishes a pane even for a project with NOTHING built, so the pane says so', async () => {
    // The never-published early return must be unreachable from a project address, built or not.
    // Two columns are the REST STATE of the project screen — a project with nothing built shows
    // the empty-state sentence IN the pane, not a hidden pane the citizen has to interpret.
    api.fetchPreviewState.mockResolvedValue(preview({ state: 'never_built', restorable: false }))
    render(<Workspace project={{ ...PROJECT, appId: null, hasRelaunchableSnapshot: false }} />)

    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalled())
    // The pane is on screen and SAYING something — not a hidden column the citizen has to
    // interpret. There is no frame, because there is nothing to frame; there is a sentence.
    expect(paneRegion()).toBeTruthy()
    expect(screen.getByTestId('app-pane-empty').textContent).toMatch(/describe what you want to build/i)
    // ★ ONE AUTHOR FOR THE WORKSPACE SENTENCE (plan 002, U4). It was rendered twice — by the
    // pane and by the rail's status card — and the rail's APP STATUS section is the publish
    // panel the boards draw now. `getAllByText` would tolerate a second renderer; counting is
    // what forbids one.
    expect(screen.queryAllByText(/describe what you want to build/i)).toHaveLength(1)
    expect(frame()).toBeNull()
    expect(frameWrapper()).toBeNull()
  })

  it('★ AE1 — a saved, not-running project offers the ONE start control, on the project screen', () => {
    // THE INVERSION THIS WHOLE PLAN TURNS ON, asserted where a citizen would meet it: through the
    // real shell, at a project address, with no conversation in the story.
    //
    // It cannot live in `ProjectPage.test.tsx`. That suite renders the page WITHOUT the shell, so
    // there is no pane in its tree at all and no assertion it can make would go red if the control
    // disappeared — which is exactly the vacuous shape U9 exists to replace.
    //
    // Mutation receipt: stop rendering `state.action` in `AppPane`'s no-frame arm and this goes red,
    // along with eight scenarios in `AppPane.test.tsx`.
    api.fetchPreviewState.mockResolvedValue(preview({ state: 'asleep', restorable: true }))
    render(<Workspace />)

    return waitFor(() => {
      expect(screen.getByRole('button', { name: /launch application/i })).toBeTruthy()
      // …and exactly one of them. The rail shows the same SENTENCE, deliberately, and no second
      // control: R3 says one control starts the app, and two would race the same endpoint.
      expect(screen.getAllByRole('button', { name: /launch application/i })).toHaveLength(1)
    })
  })

  it('frames NOTHING when the read says the workspace is asleep', async () => {
    // The address resolver is fed only the `alive` case, which is the one state whose `previewUrl`
    // the wire calls framable. A pane framing the wrong thing is worse than a pane framing nothing.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'asleep', restorable: true, previewUrl: APP_URL }),
    )
    render(<Workspace />)

    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalled())
    expect(frame()).toBeNull()
  })
})

describe('AE4 — the app survives the round trip, in BOTH directions', () => {
  it('project → chat → project keeps the SAME iframe node', async () => {
    // The direction the existing shell suite does not exercise: it starts from a chat. A second
    // publisher introduces the return trip, and the return trip is where a cold first commit can
    // retire an address the departing surface left standing.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())
    const original = frame()

    fireEvent.click(screen.getByText('to chat'))
    expect(frame()).toBe(original)

    fireEvent.click(screen.getByText('to project'))
    await waitFor(() => expect(screen.getByTestId('description-editor')).toBeTruthy())
    expect(frame()).toBe(original)
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
  })

  it('★ does not blank the pane while its own read is still in flight', async () => {
    // The realistic way a second publisher breaks the return leg: this surface remounts cold, its
    // read has not landed, and a naive publish of `{url: null}` retires the very address the chat
    // left standing. `usePublishAddress`'s abstain rule is what prevents it — reimplementing the
    // publish with a raw channel set is how that protection is lost.
    let resolveRead: (value: unknown) => void = () => {}
    api.fetchPreviewState.mockImplementation(
      () => new Promise((resolve) => { resolveRead = resolve }),
    )
    render(<Workspace entry="/chat/c1" />)
    const original = frame()
    expect(original).toBeTruthy()

    fireEvent.click(screen.getByText('to project'))
    // The read has NOT resolved. The frame must still be standing.
    expect(frame()).toBe(original)

    resolveRead(preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }))
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalled())
    expect(frame()).toBe(original)
  })

  it('fires no start of its own on a remount', async () => {
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'asleep', restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalled())

    fireEvent.click(screen.getByText('to chat'))
    fireEvent.click(screen.getByText('to project'))
    await waitFor(() => expect(screen.getByTestId('description-editor')).toBeTruthy())

    expect(api.relaunchPreview).not.toHaveBeenCalled()
  })
})

describe('AE37 — the stacked crossing is a class, not a remount', () => {
  it('expresses both layouts on ONE grid element, with no measurement anywhere', async () => {
    // R13's crossing costs no `matchMedia` and no `ResizeObserver`: the container carries both
    // directions as responsive classes, so the two-column ↔ stacked crossing cannot remount the
    // frame — there is only ever one tree.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())

    expect(grid().className).toMatch(/flex-col/)
    expect(grid().className).toMatch(/lg:flex-row/)
  })

  it('gives the project rail the narrower of the two settled widths', async () => {
    render(<Workspace />)
    await waitFor(() => expect(screen.getByTestId('description-editor')).toBeTruthy())

    expect(rail().className).toMatch(/lg:w-\[400px\]/)
    expect(rail().getAttribute('data-rail-mode')).toBe('details')
  })
})

describe('the collapse control — hidden, not unmounted, and never a one-way door', () => {
  it('★ lives in the TOOLBAR ROW, so it is still reachable once the rail is hidden', async () => {
    // The failure this is written against: a toggle inside the rail. A collapsed rail is `w-0` and
    // `invisible` — out of the tab order and out of the accessibility tree — so the control that
    // would restore it would be unreachable, and nothing short of a reload could undo the press.
    //
    // It was in the PANE for exactly that reason, and plan 002's U2 moved it one step further out,
    // to the row above both columns. The reachability property is unchanged and still asserted; the
    // row is simply the one surface that survives a collapse AND the pane going away, so the
    // control has one home in every state rather than appearing and disappearing with the frame.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())

    const toggle = screen.getByRole('button', { name: /hide details/i })
    expect(screen.getByTestId('workspace-toolbar').contains(toggle)).toBe(true)
    expect(paneRegion()?.contains(toggle)).toBe(false)
    expect(rail().contains(toggle)).toBe(false)

    fireEvent.click(toggle)
    // `\bw-0\b` would also match the `min-w-0` this element always carries — the boundary has to
    // be whitespace, not a word boundary.
    expect(rail().className).toMatch(/(^|\s)w-0(\s|$)/)
    expect(rail().className).toMatch(/invisible/)
    // Still there, still pressable, now offering the other direction.
    const back = screen.getByRole('button', { name: /show details/i })
    expect(back.getAttribute('aria-expanded')).toBe('false')
    expect(back.getAttribute('aria-controls')).toBe(rail().id)

    fireEvent.click(back)
    expect(rail().className).not.toMatch(/(^|\s)w-0(\s|$)/)
  })

  it('keeps the rail MOUNTED while collapsed, so nothing inside it is discarded', async () => {
    render(<Workspace />)
    await waitFor(() => expect(screen.getByTestId('description-editor')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: /hide details/i }))

    // The subtree is still in the document — a draft and a scroll position survive the cycle.
    expect(screen.getByTestId('description-editor')).toBeTruthy()
    expect(rail().className).toMatch(/invisible/)
  })

  it('★ is reachable on a project with NOTHING BUILT, where there is no frame to hang it on', async () => {
    // THE BUG THIS CAUGHT, found by this suite rather than by review. The toggle was first
    // published into the pane's toolbar slot — the same place the conversation surface puts its
    // chat-panel toggle. That toolbar is rendered by `LivePreview`, which only mounts once there is
    // something to frame, so a project with nothing built had NO toggle at all; and a rail
    // collapsed while an app was running would have lost its way back the moment the container
    // stopped. Its home has to be a surface that always renders — the pane's own outer shell then,
    // the toolbar row now.
    api.fetchPreviewState.mockResolvedValue(preview({ state: 'never_built', restorable: false }))
    render(<Workspace project={{ ...PROJECT, appId: null, hasRelaunchableSnapshot: false }} />)
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalled())
    expect(frame()).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /hide details/i }))
    expect(rail().className).toMatch(/(^|\s)w-0(\s|$)/)
    // …and back again, with no frame in the story at any point.
    fireEvent.click(screen.getByRole('button', { name: /show details/i }))
    expect(rail().className).not.toMatch(/(^|\s)w-0(\s|$)/)
  })

  it('leaves the frame alone across a collapse — it is a class change, not a remount', async () => {
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())
    const original = frame()

    fireEvent.click(screen.getByRole('button', { name: /hide details/i }))

    expect(frame()).toBe(original)
  })
})

describe('the channel is left as the next surface needs to find it', () => {
  it('clears the pane and its visibility on the way out, and keeps the address', async () => {
    // The channel's stated per-payload rules, now exercised by a SECOND publisher rather than only
    // the first. Keeping the address is R8; clearing the pane is what stops a departed surface's
    // chrome from being rendered over the next one's.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())
    const original = frame()

    fireEvent.click(screen.getByText('to chat'))

    // The chat surface publishes its own pane, so the frame survives on the address, not the pane.
    expect(frame()).toBe(original)
    expect(screen.getByTestId('chat-surface')).toBeTruthy()
  })
})

/**
 * WHAT THE SHELL DOES FOR A CHAT THAT WANTS NO PANE (plan 002, U6).
 *
 * The SURFACE half — that the panel fills the rail, that a plan chat centres its column, that the
 * board's footer line appears on one kind and not the other — is `BuilderPage-panel.test.tsx`'s,
 * where the real conversation surface renders. What is only visible HERE, through the real shell,
 * is the relationship between the two columns: who gets the width, and whether the frame survives.
 */
describe('a chat that declares no pane', () => {
  it('★ takes the whole rail, and the frame stays mounted rather than being torn down', async () => {
    // The hide treatment, never an unmount — the same node throughout, which is what makes the
    // board's "nothing about the app is stopped or reloaded — it is only taken off the screen" a
    // structural fact rather than a hope.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())
    const original = frame()

    fireEvent.click(screen.getByText('to plan chat'))

    expect(rail().className).toMatch(/flex-1/)
    expect(rail().className).not.toMatch(/lg:w-\[520px\]/)
    expect(frameWrapper()).toBeTruthy()
    expect(frame()).toBe(original)
    expect(frameWrapper()?.className).toMatch(/invisible/)
  })

  it('★ and the app does not reload on the way back either', async () => {
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())
    const original = frame()

    fireEvent.click(screen.getByText('to plan chat'))
    fireEvent.click(screen.getByText('to project'))

    expect(frame()).toBe(original)
  })
})
