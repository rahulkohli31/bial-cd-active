/**
 * ★ THE ONE WORKSPACE, AT SUBMIT — and nothing is asked about it.
 *
 * The server hands the one workspace to whichever project was asked for and tears the outgoing one
 * down behind it, so pressing send has nothing to arbitrate. What these scenarios pin is the
 * ORDER that survives that: the app is asked for BEFORE the address moves, so a citizen never
 * lands in a chat whose workspace then turns out to be somebody else's — and the one refusal the
 * server can still raise is reported where they are standing rather than in a question.
 *
 * They render through the REAL shell — every one is about the relationship between a composer, an
 * address that must not move early, and a pane — because a composer mounted alone sees none of it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import type { Project } from '../../../utils/projectApi'
import { ApiError } from '../../../utils/apiError'

const api = vi.hoisted(() => ({
  relaunchPreview: vi.fn(),
  fetchPreviewState: vi.fn(),
  fetchSaveState: vi.fn(),
}))

vi.mock('../../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/buildSessionApi')>()),
  relaunchPreview: api.relaunchPreview,
  fetchPreviewState: api.fetchPreviewState,
  fetchSaveState: api.fetchSaveState,
}))
vi.mock('../../PublishStatusChip', () => ({ default: () => <span data-testid="publish-chip-stub" /> }))
vi.mock('../../LivePreview', () => ({ default: () => <div data-testid="live-preview" /> }))
vi.mock('../../projects/ProjectDescriptionEditor', () => ({
  default: () => <div data-testid="description-editor" />,
}))
vi.mock('../../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/auth')>()),
  getStoredUser: () => ({
    chat_kinds: [
      { value: 'plan', name: 'Plan', description: 'Shape a plan first.' },
      { value: 'build', name: 'Build', description: 'Change the live app.' },
    ],
  }),
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
  hasSavedSnapshot: null,
  isServing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'owner',
}

/** The one refusal `POST /relaunch` can still raise: a colleague's shared view in the slot. */
const sharedViewHolds = (over: Record<string, unknown> = {}) =>
  new ApiError('“Car pool” is open for a colleague right now.', 409, 'sandbox_reclaim_blocked', {
    projectId: 'pA', projectName: 'Car pool', dirty: false, building: false, isSharedView: true, ...over,
  })

const STARTED = {
  appId: 'app-1', previewUrl: 'https://app/', status: 'ready', ready: true, restoredFromFailedBuild: false,
}

function Where() {
  const loc = useLocation()
  return <span data-testid="where">{loc.pathname + loc.search}</span>
}

/**
 * The chat that opens, standing in for the real surface — and showing WHAT THE NAVIGATION CARRIED.
 *
 * The message and its files travel in router state and are fired by the mounted chat
 * (`ConversationSurface`'s `fireHandoffPrompt` reads `location.state.pendingAttachments`), so the
 * state is where a hand-over either keeps them or eats them.
 */
function ChatOpened() {
  const carried = (useLocation().state ?? {}) as {
    prompt?: string
    pendingAttachments?: { name: string }[]
  }
  return (
    <div data-testid="chat-opened">
      <span data-testid="carried-prompt">{carried.prompt}</span>
      <span data-testid="carried-files">
        {(carried.pendingAttachments ?? []).map((file) => file.name).join(', ')}
      </span>
    </div>
  )
}

function Workspace({ project = PROJECT }: { project?: Project } = {}) {
  return (
    <MemoryRouter initialEntries={['/projects/pB']}>
      <Where />
      <Routes>
        <Route element={<WorkspaceShell />}>
          <Route
            path="/projects/:projectId"
            element={<ProjectWorkspace project={project} onProjectUpdate={() => {}} />}
          />
          <Route path="/chat/:chatId" element={<ChatOpened />} />
        </Route>
      </Routes>
    </MemoryRouter>
  )
}

// THE PLAN PLACEHOLDER, because the rail's kind picker defaults to Plan. The hand-over this
// file is about is identical on either kind; the picker only decides which wording is showing.
const composer = () => screen.getByPlaceholderText(/Describe what you have in mind/i)
const send = () => screen.getByTestId('composer-send')
const where = () => screen.getByTestId('where').textContent ?? ''
const type = (text: string) => fireEvent.change(composer(), { target: { value: text } })

beforeEach(() => {
  vi.clearAllMocks()
  // NOTHING FOR THE SCREEN ITSELF TO OPEN. Every scenario below is about what a SEND does when
  // the workspace is held elsewhere, and the screen now starts an app it finds restorable and
  // asleep — so a restorable reading here would have the surface correctly opening the workspace
  // before the composer ever got to ask, which is a different story than the one under test.
  api.fetchPreviewState.mockResolvedValue({
    state: 'asleep', alive: false, previewUrl: null, occupyingProjectName: null,
    occupyingProjectId: null, restorable: false,
  })
  api.fetchSaveState.mockResolvedValue(null)
})
afterEach(cleanup)

describe('★ the app is asked for BEFORE the address moves', () => {
  it('★ a slot held by another project stops nothing, and asks nothing', async () => {
    // The server takes the workspace for the project that asked. So the ordering that used to
    // exist to get a question in before the navigation now exists purely to get the APP in before
    // it: the citizen lands in a chat whose workspace is already theirs.
    api.relaunchPreview.mockResolvedValue(STARTED)
    render(<Workspace />)
    type('add an out-time column')

    fireEvent.click(send())

    await waitFor(() => expect(screen.getByTestId('chat-opened')).toBeTruthy())
    expect(api.relaunchPreview).toHaveBeenCalledWith({ projectId: 'pB' })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('★ and the address does not move until the server has answered', async () => {
    // THE ORDERING, ASSERTED AS AN ORDERING. Mutation receipt: navigate before the start and this
    // goes red on the mid-wait assertion while everything else here stays green.
    let startTheApp: (() => void) | undefined
    api.relaunchPreview.mockImplementation(
      () => new Promise((resolve) => { startTheApp = () => resolve(STARTED) }),
    )
    render(<Workspace />)
    type('add an out-time column')

    fireEvent.click(send())

    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalled())
    expect(where()).toBe('/projects/pB')
    expect(screen.queryByTestId('chat-opened')).toBeNull()

    startTheApp?.()
    await waitFor(() => expect(screen.getByTestId('chat-opened')).toBeTruthy())
  })

  it('★ a colleague`s shared view is REPORTED, never asked about, and keeps the message', async () => {
    // Pressing again cannot move a shared view — the server refuses it whatever the citizen
    // answers — so a question would be one with no true answer. The refusal is read where they
    // are standing, their text is still theirs, and no chat opened onto a workspace they have not
    // got.
    api.relaunchPreview.mockRejectedValue(sharedViewHolds())
    render(<Workspace />)
    type('add an out-time column')

    fireEvent.click(send())

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByTestId('chat-opened')).toBeNull()
    expect(where()).toBe('/projects/pB')
    expect((composer() as HTMLTextAreaElement).value).toBe('add an out-time column')
  })
})

describe('a project with nothing built yet — the first message anybody sends', () => {
  /** What the server answers when there is no saved build to bring back: the snapshot gate, 404,
   *  `no_saved_build`, and deliberately no blank-template arm. */
  const nothingToRelaunch = () =>
    new ApiError('No saved build to relaunch. Build the app first.', 404, 'no_saved_build')

  /** The OTHER 404 the same endpoint answers — a project that is gone, or was never this
   *  citizen's. Same status, no code, and nothing to open a chat onto. */
  const projectGone = () => new ApiError('Project not found.', 404)

  const NEVER_BUILT: Project = { ...PROJECT, appId: null, hasRelaunchableSnapshot: false }

  beforeEach(() => {
    api.fetchPreviewState.mockResolvedValue({
      state: 'asleep', alive: false, previewUrl: null, occupyingProjectName: null,
      occupyingProjectId: null, restorable: false,
    })
  })

  it('★ opens the chat anyway — nothing to bring back is not a failed send', async () => {
    // THE ONBOARDING PATH THIS PREFLIGHT BROKE: a citizen's first-ever send has no snapshot, so
    // the workspace check answers 404 — treating that as a failure left them reading "That
    // message did not send" with no chat, on every attempt.
    api.relaunchPreview.mockRejectedValue(nothingToRelaunch())
    render(<Workspace project={NEVER_BUILT} />)
    type('an app to log visitors at the gate')

    fireEvent.click(send())

    await waitFor(() => expect(screen.getByTestId('chat-opened')).toBeTruthy())
    expect(where()).toMatch(/^\/chat\/[0-9a-f-]+\?projectId=pB&kind=plan$/)
    // …and nothing told them their message was lost. Paired with the assertion above, so an
    // absent alert cannot pass by the screen having failed to render at all.
    expect(screen.queryAllByRole('alert')).toHaveLength(0)
  })

  it('★ but a held workspace still stops the address, even with nothing built', async () => {
    // The 404-for-no-snapshot mapping must not become a way past the workspace refusal: the server
    // answers that ABOVE its snapshot gate, and a refusal it does raise must still stop the
    // address rather than opening a chat onto a workspace this citizen has not got.
    api.relaunchPreview.mockRejectedValue(sharedViewHolds())
    render(<Workspace project={NEVER_BUILT} />)
    type('an app to log visitors at the gate')

    fireEvent.click(send())

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(where()).toBe('/projects/pB')
    expect(screen.queryByTestId('chat-opened')).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('★ a project that is gone is reported, not opened — same 404, no code', async () => {
    // The arm is on the CODE, not the status: `owned_project_or_404` answers this endpoint with
    // an uncoded 404 for a deleted or someone else's project. Matching status alone opened a chat
    // onto it, which then died a beat later with no explanation attached to the send.
    // Mutation receipt: drop `&& err.code === 'no_saved_build'` from the rail and this
    // goes red while the scenario above stays green.
    api.relaunchPreview.mockRejectedValue(projectGone())
    render(<Workspace project={NEVER_BUILT} />)
    type('an app to log visitors at the gate')

    fireEvent.click(send())

    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/did not send/i))
    expect(screen.queryByTestId('chat-opened')).toBeNull()
    expect(where()).toBe('/projects/pB')
  })

  it('a genuine failure to start is still reported, and opens nothing', async () => {
    // The mapping is narrow on purpose: a 503 is "we could not do it", not "there was nothing to
    // do", and it must not open a chat onto a workspace that never came up.
    api.relaunchPreview.mockRejectedValue(
      new ApiError('The sandbox or build coordination is temporarily unavailable', 503),
    )
    render(<Workspace project={NEVER_BUILT} />)
    type('an app to log visitors at the gate')

    fireEvent.click(send())

    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/did not send/i))
    expect(screen.queryByTestId('chat-opened')).toBeNull()
    expect(where()).toBe('/projects/pB')
  })
})

describe('what the navigation carries', () => {
  it('★ carries the FILES into the chat it opens, not just the words', async () => {
    // The files are the half most easily lost: they live only as decoded bytes in a composer the
    // navigation is about to leave. Removing `pendingAttachments` from the navigation's state
    // passes every other scenario in this file.
    api.relaunchPreview.mockResolvedValue(STARTED)
    render(<Workspace />)
    type('add an out-time column')
    // STAGED BY DROP rather than through the add control, which opens an OS picker jsdom cannot
    // drive — the dropzone reaches the same `addAttachment`, so this is the real path.
    fireEvent.drop(screen.getByTestId('composer-dropzone'), {
      dataTransfer: {
        types: ['Files'],
        files: [new File(['id,name\n1,Priya'], 'visitors.csv', { type: 'text/csv' })],
      },
    })
    await waitFor(() =>
      expect(screen.getByTestId('composer-chips').textContent).toContain('visitors.csv'),
    )

    fireEvent.click(send())

    await waitFor(() => expect(screen.getByTestId('chat-opened')).toBeTruthy())
    expect(screen.getByTestId('carried-prompt').textContent).toBe('add an out-time column')
    expect(screen.getByTestId('carried-files').textContent).toContain('visitors.csv')
    // ONE chat, from the one send that was answered.
    expect(screen.getAllByTestId('chat-opened')).toHaveLength(1)
  })

  it('★ a refused send keeps the message AND the file, and opens nothing', async () => {
    api.relaunchPreview.mockRejectedValue(sharedViewHolds())
    render(<Workspace />)
    type('add an out-time column')
    fireEvent.drop(screen.getByTestId('composer-dropzone'), {
      dataTransfer: {
        types: ['Files'],
        files: [new File(['id,name\n1,Priya'], 'visitors.csv', { type: 'text/csv' })],
      },
    })
    await waitFor(() =>
      expect(screen.getByTestId('composer-chips').textContent).toContain('visitors.csv'),
    )

    fireEvent.click(send())

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect((composer() as HTMLTextAreaElement).value).toBe('add an out-time column')
    expect(screen.getByTestId('composer-chips').textContent).toContain('visitors.csv')
    expect(screen.queryByTestId('chat-opened')).toBeNull()
  })
})

/**
 * ★ AND THE WORKSPACE COMES BACK WITHOUT ANYBODY SENDING ANYTHING.
 *
 * WHY THIS BLOCK IS HERE AND NOT IN `AppPane.test.tsx`.
 *
 * This is the citizen who does NOT want to send a message: they want their own app open. This file
 * is the one that mounts the whole of that — the real shell, the real `ProjectWorkspace`, the real
 * rail composer, and a route that records what a navigation carried — so it is the only place the
 * guarantee can be stated as what it actually is: THE ADDRESS DOES NOT MOVE AND THE MESSAGE IS NOT
 * SENT. A pane-level suite has no composer and no router to be wrong about.
 */
describe('★ a held workspace comes back on its own, with nothing sent', () => {
  const held = {
    state: 'slot_taken' as const, alive: false, previewUrl: null,
    occupyingProjectName: 'Car pool', occupyingProjectId: 'pA', restorable: true,
  }

  it('★ opening the project takes the workspace, without opening a chat or sending the message', async () => {
    // A taken slot reads as the saved app it is, so the screen starts it the way it starts any
    // saved app — no press, no question, and nothing at all about the project that had it.
    api.fetchPreviewState.mockResolvedValue(held)
    api.relaunchPreview.mockResolvedValue(STARTED)
    render(<Workspace />)
    // The message is in the composer and stays there — this citizen never pressed send.
    type('add an out-time column')

    // LIVENESS: the app really was asked for, so the absences below are a start that happened.
    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalledWith({ projectId: 'pB' }))

    expect(screen.queryByRole('dialog')).toBeNull()
    expect(where()).toBe('/projects/pB')
    expect(screen.queryByTestId('chat-opened')).toBeNull()
    expect((composer() as HTMLTextAreaElement).value).toBe('add an out-time column')
  })

  it('★ and names the project that had it nowhere on the screen', async () => {
    api.fetchPreviewState.mockResolvedValue(held)
    api.relaunchPreview.mockResolvedValue(STARTED)
    render(<Workspace />)

    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalled())
    // The wire carried the attribution; no surface repeats it. A tab preempted from elsewhere has
    // no cause to name, so naming one here would be the screen guessing.
    expect(document.body.textContent).not.toContain('Car pool')
  })
})
