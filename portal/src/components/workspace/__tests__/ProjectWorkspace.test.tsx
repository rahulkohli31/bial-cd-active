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
  toolbarLeading: null, toolbarTrailing: null,
  iterating: false, reconnecting: false,
  relaunching: false, relaunchError: null, lastBuildFailed: false,
  restoredFromFailedBuild: false, completedLive: true, hasSavedBuild: null,
  previewState: null, occupyingProjectName: null, turnRunning: false,
  compileState: null, workspaceLost: false,
  saveDirty: null, saving: false, saveError: null,
}

/** A build chat, publishing its own address — the OTHER publisher on this channel. */
function ChatSurface({ projectId = 'pA' }: { projectId?: string }) {
  useWorkspaceProject(projectId)
  usePublishAddress({ url: APP_URL, status: 'ready' }, projectId)
  usePublishPaneView(EMPTY_PANE)
  useAppPaneVisible(true)
  return <div data-testid="chat-surface" />
}

const noop = () => {}

/** The project surface, with every prop its owner would have loaded. */
function Surface({ project = PROJECT }: { project?: Project }) {
  return (
    <ProjectWorkspace
      project={project}
      chats={[]}
      chatsError={null}
      onProjectUpdate={noop}
      onBack={noop}
      onOpenChat={noop}
      onDeleteChat={noop}
      editingName={false}
      nameDraft=""
      nameError={null}
      onStartRename={noop}
      onNameDraftChange={noop}
      onSubmitRename={noop}
      onCancelRename={noop}
      menuOpenId={null}
      onToggleMenu={noop}
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
        </Route>
      </Routes>
    </MemoryRouter>
  )
}

const frame = () => document.querySelector('iframe')
const pane = () => screen.queryByTestId('app-pane')
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
    expect(pane()).toBeTruthy()
    expect(pane()?.getAttribute('aria-hidden')).toBe('false')
    expect(frame()).toBeNull()
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
  it('★ lives in the PANE, so it is still reachable once the rail is hidden', async () => {
    // The failure this is written against: a toggle inside the rail. A collapsed rail is `w-0` and
    // `invisible` — out of the tab order and out of the accessibility tree — so the control that
    // would restore it would be unreachable, and nothing short of a reload could undo the press.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())

    const toggle = screen.getByRole('button', { name: /hide project details/i })
    expect(pane()?.contains(toggle)).toBe(true)
    expect(rail().contains(toggle)).toBe(false)

    fireEvent.click(toggle)
    // `\bw-0\b` would also match the `min-w-0` this element always carries — the boundary has to
    // be whitespace, not a word boundary.
    expect(rail().className).toMatch(/(^|\s)w-0(\s|$)/)
    expect(rail().className).toMatch(/invisible/)
    // Still there, still pressable, now offering the other direction.
    const back = screen.getByRole('button', { name: /show project details/i })
    expect(back.getAttribute('aria-expanded')).toBe('false')
    expect(back.getAttribute('aria-controls')).toBe(rail().id)

    fireEvent.click(back)
    expect(rail().className).not.toMatch(/(^|\s)w-0(\s|$)/)
  })

  it('keeps the rail MOUNTED while collapsed, so nothing inside it is discarded', async () => {
    render(<Workspace />)
    await waitFor(() => expect(screen.getByTestId('description-editor')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: /hide project details/i }))

    // The subtree is still in the document — a draft and a scroll position survive the cycle.
    expect(screen.getByTestId('description-editor')).toBeTruthy()
    expect(rail().className).toMatch(/invisible/)
  })

  it('leaves the frame alone across a collapse — it is a class change, not a remount', async () => {
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())
    const original = frame()

    fireEvent.click(screen.getByRole('button', { name: /hide project details/i }))

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
