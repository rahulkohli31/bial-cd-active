/**
 * THE ONE-WORKSPACE RULE, ASKED AT SUBMIT.
 *
 * The defect: the browser navigated before the server's refusal arrived, so someone building in
 * project A for two minutes could be interrupted by a question about a chat that had already
 * opened. The backend's ordering was always right; the browser jumped ahead of it.
 *
 * These scenarios render through the REAL shell — every one is about the relationship between a
 * composer, a dialog the shell mounts, and an address that must not change — because a composer
 * mounted alone could not see any of it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import type { Project } from '../../../utils/projectApi'
import { ApiError } from '../../../utils/apiError'

const api = vi.hoisted(() => ({
  relaunchPreview: vi.fn(),
  fetchPreviewState: vi.fn(),
  fetchSaveState: vi.fn(),
  handOverWorkspace: vi.fn(),
}))

vi.mock('../../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/buildSessionApi')>()),
  relaunchPreview: api.relaunchPreview,
  fetchPreviewState: api.fetchPreviewState,
  fetchSaveState: api.fetchSaveState,
  handOverWorkspace: api.handOverWorkspace,
}))
vi.mock('../../layout/Navbar', () => ({ default: () => <div data-testid="navbar" /> }))
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

/** The refusal the server raises when the one workspace is held by another project. */
const heldBy = (over: Record<string, unknown> = {}) =>
  Object.assign(new Error('“Car pool” is still open.'), {
    code: 'sandbox_reclaim_blocked',
    details: { projectId: 'pA', projectName: 'Car pool', dirty: false, building: false, ...over },
  })

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

// THE PLAN PLACEHOLDER, because the rail's kind picker now defaults to Plan (2026-09-10).
// The hand-over this file is about is identical on either kind — what changed is only which
// wording the untouched picker is showing when the citizen starts typing.
const composer = () => screen.getByPlaceholderText(/Describe what you have in mind/i)
const send = () => screen.getByTestId('composer-send')
const where = () => screen.getByTestId('where').textContent ?? ''
const type = (text: string) => fireEvent.change(composer(), { target: { value: text } })

beforeEach(() => {
  vi.clearAllMocks()
  api.fetchPreviewState.mockResolvedValue({
    state: 'asleep', alive: false, previewUrl: null, occupyingProjectName: null,
    occupyingProjectId: null, restorable: true,
  })
  api.fetchSaveState.mockResolvedValue(null)
  api.handOverWorkspace.mockResolvedValue(undefined)
})
afterEach(cleanup)

describe('the question arrives BEFORE anything moves', () => {
  it('★ opens the dialog and does NOT change the address', async () => {
    api.relaunchPreview.mockRejectedValue(heldBy())
    render(<Workspace />)
    type('add an out-time column')

    fireEvent.click(send())

    expect(await screen.findByRole('dialog')).toBeTruthy()
    // THE WHOLE DEFECT, IN ONE ASSERTION. Mutation receipt: navigate before the preflight and
    // this goes red while every other scenario here stays green.
    expect(where()).toBe('/projects/pB')
    expect(screen.queryByTestId('chat-opened')).toBeNull()
  })

  it('★ leaves the typed message exactly where it was', async () => {
    api.relaunchPreview.mockRejectedValue(heldBy())
    render(<Workspace />)
    type('add an out-time column')

    fireEvent.click(send())

    await screen.findByRole('dialog')
    expect((composer() as HTMLTextAreaElement).value).toBe('add an out-time column')
  })

  it('names BOTH projects, in citizen language, with no infrastructure words', async () => {
    api.relaunchPreview.mockRejectedValue(heldBy())
    render(<Workspace />)
    type('go')
    fireEvent.click(send())

    const dialog = await screen.findByRole('dialog')
    const text = dialog.textContent ?? ''
    expect(text).toContain('Visitor Log') // the one being started
    expect(text).toContain('Car pool') // the one in the way
    for (const word of [/container/i, /sandbox/i, /workspace slot/i, /session/i, /409/]) {
      expect(text, String(word)).not.toMatch(word)
    }
  })

  it('★ says the other project\'s agent is still working, BEFORE the choice', async () => {
    // `agentWorking` is the WIDE fact — an agent mid-turn of any kind — and it changes what the
    // citizen is agreeing to, so it cannot arrive as a consequence of pressing.
    api.relaunchPreview.mockRejectedValue(heldBy({ agentWorking: true }))
    render(<Workspace />)
    type('go')
    fireEvent.click(send())

    await screen.findByRole('dialog')
    expect(screen.getByTestId('reclaim-agent-working').textContent).toMatch(/still working/i)
  })

  it('says nothing about a working agent when there is not one', async () => {
    api.relaunchPreview.mockRejectedValue(heldBy({ agentWorking: false }))
    render(<Workspace />)
    type('go')
    fireEvent.click(send())

    await screen.findByRole('dialog')
    expect(screen.queryByTestId('reclaim-agent-working')).toBeNull()
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

  it('★ still asks the one-workspace question first, even with nothing built', async () => {
    // The 404-for-no-snapshot mapping must not become a way past this gate: the server refuses a
    // held workspace ABOVE its snapshot gate, and that refusal must still stop the address.
    api.relaunchPreview.mockRejectedValue(heldBy())
    render(<Workspace project={NEVER_BUILT} />)
    type('an app to log visitors at the gate')

    fireEvent.click(send())

    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(where()).toBe('/projects/pB')
    expect(screen.queryByTestId('chat-opened')).toBeNull()
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

describe('cancelling', () => {
  it('★ leaves the message, closes the dialog, and touches the other project not at all', async () => {
    api.relaunchPreview.mockRejectedValue(heldBy())
    render(<Workspace />)
    type('add an out-time column')
    fireEvent.click(send())
    await screen.findByRole('dialog')

    // The keydown goes to the SAME element Radix listens on — its focusable content wrapper, not
    // the role node — so this asserts the element exists rather than asserting it away with `!`.
    const content = screen.getByRole('dialog').querySelector('[tabindex="-1"]')
    expect(content).not.toBeNull()
    fireEvent.keyDown(content as Element, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect((composer() as HTMLTextAreaElement).value).toBe('add an out-time column')
    expect(api.handOverWorkspace).not.toHaveBeenCalled()
    expect(where()).toBe('/projects/pB')
  })
})

describe('transferring', () => {
  it('★ drains the other project, then opens the chat exactly once, carrying the message', async () => {
    api.relaunchPreview
      .mockRejectedValueOnce(heldBy())
      .mockResolvedValue({ appId: 'app-1', previewUrl: 'https://app/', status: 'ready', ready: true, restoredFromFailedBuild: false })
    render(<Workspace />)
    type('add an out-time column')
    fireEvent.click(send())
    await screen.findByRole('dialog')

    fireEvent.click(screen.getByRole('button', { name: /stop “Car pool”/i }))

    await waitFor(() => expect(screen.getByTestId('chat-opened')).toBeTruthy())
    expect(api.handOverWorkspace).toHaveBeenCalledTimes(1)
    expect(api.handOverWorkspace.mock.calls[0]?.[0]).toMatchObject({ projectId: 'pA' })
    // ONE chat, not two: the first attempt was refused before it navigated.
    expect(screen.getAllByTestId('chat-opened')).toHaveLength(1)
    expect(where()).toMatch(/^\/chat\/[0-9a-f-]+\?projectId=pB&kind=plan$/)
  })

  it('★ the order is stop-then-start-then-open, never start-then-ask', async () => {
    const order: string[] = []
    api.handOverWorkspace.mockImplementation(async () => {
      order.push('handover')
    })
    api.relaunchPreview
      .mockImplementationOnce(async () => {
        order.push('preflight')
        throw heldBy()
      })
      .mockImplementation(async () => {
        order.push('start')
        return { appId: 'app-1', previewUrl: 'https://app/', status: 'ready', ready: true, restoredFromFailedBuild: false }
      })
    render(<Workspace />)
    type('go')
    fireEvent.click(send())
    await screen.findByRole('dialog')

    fireEvent.click(screen.getByRole('button', { name: /stop “Car pool”/i }))

    await waitFor(() => expect(screen.getByTestId('chat-opened')).toBeTruthy())
    expect(order).toEqual(['preflight', 'handover', 'start'])
  })

  it('★ a failure part-way leaves the citizen with an explanation AND their message', async () => {
    api.relaunchPreview.mockRejectedValue(heldBy())
    api.handOverWorkspace.mockRejectedValue(
      new Error('The other app is still saving its work. Nothing has changed — give it a moment and try again.'),
    )
    render(<Workspace />)
    type('add an out-time column')
    fireEvent.click(send())
    await screen.findByRole('dialog')

    fireEvent.click(screen.getByRole('button', { name: /stop “Car pool”/i }))

    // THE DIALOG SAYS WHAT FAILED, and it is the only thing that says anything: the composer's
    // own refusal is SILENT here, because a second sentence under the box repeating the dialog in
    // weaker words is noise over the top of it.
    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toMatch(/nothing has changed/i),
    )
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    // The dialog is still there to be tried again, and nothing was opened.
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.queryByTestId('chat-opened')).toBeNull()
    expect((composer() as HTMLTextAreaElement).value).toBe('add an out-time column')
  })

  it('★ narrates the start too, then hands the telling over to the chat itself', async () => {
    // The rule asks every transition to narrate itself, and the start is the longest of them: the
    // dialog stays up across it saying so, rather than spinning. It stops there on purpose —
    // opening the chat unmounts the surface that publishes this dialog, so the sequence ends
    // where the destination begins narrating itself.
    let startTheApp: (() => void) | undefined
    api.relaunchPreview.mockRejectedValueOnce(heldBy()).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          startTheApp = () =>
            resolve({ appId: 'app-1', previewUrl: 'https://app/', status: 'ready', ready: true, restoredFromFailedBuild: false })
        }),
    )
    render(<Workspace />)
    type('add an out-time column')
    fireEvent.click(send())
    await screen.findByRole('dialog')

    fireEvent.click(screen.getByRole('button', { name: /stop “Car pool”/i }))

    await waitFor(() =>
      expect(screen.getByTestId('reclaim-step').textContent).toMatch(/starting your app/i),
    )
    // …for the WHOLE of the start: the chat has not opened yet, so this is the sentence standing
    // in front of the citizen while they wait, not one that flashed as it finished.
    expect(screen.queryByTestId('chat-opened')).toBeNull()
    startTheApp?.()

    await waitFor(() => expect(screen.getByTestId('chat-opened')).toBeTruthy())
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('★ carries the FILES across the hand-over too, not just the words', async () => {
    // This test's claim is that "a refused send never loses a message, and a hand-over never eats
    // an attachment". The refused half was pinned; the files were not — and they are the half most
    // easily lost, because they live only as decoded bytes in a composer the hand-over is about to
    // navigate away from. Removing `pendingAttachments` from the navigation's state passed every
    // test before this one.
    api.relaunchPreview
      .mockRejectedValueOnce(heldBy())
      .mockResolvedValue({ appId: 'app-1', previewUrl: 'https://app/', status: 'ready', ready: true, restoredFromFailedBuild: false })
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
    await screen.findByRole('dialog')

    // THE REFUSAL KEPT BOTH — the message and the file are still in the composer that never sent.
    expect((composer() as HTMLTextAreaElement).value).toBe('add an out-time column')
    expect(screen.getByTestId('composer-chips').textContent).toContain('visitors.csv')

    fireEvent.click(screen.getByRole('button', { name: /stop “Car pool”/i }))

    await waitFor(() => expect(screen.getByTestId('chat-opened')).toBeTruthy())
    // …and the hand-over carried BOTH into the chat it opened, in one navigation.
    expect(screen.getByTestId('carried-prompt').textContent).toBe('add an out-time column')
    expect(screen.getByTestId('carried-files').textContent).toContain('visitors.csv')
  })

  it('★ a reload part-way through the transfer strands neither a chat nor a message', async () => {
    // The transfer spans a stop that can run for over a minute, and a citizen can reload in the
    // middle of it. The dialog is in-memory and goes with the tab; what must NOT happen is a chat
    // opening without anybody asking, or the typed message disappearing with the dialog.
    //
    // THE CONTAINER HALF OF THIS CLAIM IS THE SERVER'S and is proven there —
    // `test_a_dropped_connection_mid_stop_loses_no_work_and_takes_no_container`. The stop runs as a
    // detached task and the state read is idempotent, so nothing here can take a container.
    let finishTheStop: (() => void) | undefined
    api.handOverWorkspace.mockImplementation(
      () => new Promise<void>((resolve) => { finishTheStop = resolve }),
    )
    api.relaunchPreview
      .mockRejectedValueOnce(heldBy())
      .mockResolvedValue({ appId: 'app-1', previewUrl: 'https://app/', status: 'ready', ready: true, restoredFromFailedBuild: false })
    const first = render(<Workspace />)
    type('add an out-time column')
    fireEvent.click(send())
    await screen.findByRole('dialog')
    fireEvent.click(screen.getByRole('button', { name: /stop “Car pool”/i }))
    await waitFor(() => expect(api.handOverWorkspace).toHaveBeenCalledTimes(1))

    // THE RELOAD, mid-stop: everything in memory goes, and the tab comes back on the project.
    first.unmount()
    render(<Workspace />)
    finishTheStop?.()

    // Nothing opened behind them while the tab was reloading.
    expect(screen.queryByTestId('chat-opened')).toBeNull()
    expect(where()).toBe('/projects/pB')
    // …and their message came back with the screen, so the send they were part-way through is one
    // press away rather than retyped.
    await waitFor(() => expect((composer() as HTMLTextAreaElement).value).toBe('add an out-time column'))

    fireEvent.click(send())

    await waitFor(() => expect(screen.getByTestId('chat-opened')).toBeTruthy())
    // ONE chat, from the one send that was actually answered.
    expect(screen.getAllByTestId('chat-opened')).toHaveLength(1)
    expect(screen.getByTestId('carried-prompt').textContent).toBe('add an out-time column')
  })

  it('narrates while it works, rather than spinning', async () => {
    let releaseHandover: (() => void) | undefined
    api.handOverWorkspace.mockImplementation(
      (_p: string, _s: boolean, _d: unknown, narrate: (step: string) => void) =>
        new Promise<void>((resolve) => {
          narrate('stopping')
          releaseHandover = resolve
        }),
    )
    api.relaunchPreview.mockRejectedValue(heldBy())
    render(<Workspace />)
    type('go')
    fireEvent.click(send())
    await screen.findByRole('dialog')

    fireEvent.click(screen.getByRole('button', { name: /stop “Car pool”/i }))

    await waitFor(() =>
      expect(screen.getByTestId('reclaim-step').textContent).toMatch(/closing the other app/i),
    )
    releaseHandover?.()
  })
})

/**
 * ★ AND THE SAME QUESTION, REACHED WITHOUT SENDING ANYTHING.
 *
 * WHY THIS BLOCK IS HERE AND NOT IN `AppPane.test.tsx`.
 *
 * This is the citizen who does NOT want to send a message: they want their own app open, and
 * until now the only thing on the screen that could get it for them was a composer send. This file
 * is the one that mounts the whole of that — the real shell, the real `ProjectWorkspace`, the real
 * rail composer, and a route that records what a navigation carried — so it is the only place the
 * guarantee can be stated as what it actually is: THE ADDRESS DOES NOT MOVE AND THE MESSAGE IS NOT
 * SENT. A pane-level suite has no composer and no router to be wrong about.
 *
 * The trap it is written against: `resolveReclaim` awaits `retry()`, which on this surface
 * is the rail's own "open the chat with what they typed". A take-back routed through that slot
 * would post the citizen's held message as a build instruction — the exact thing the owner forbade.
 */
describe('★ taking the workspace back, with nothing sent', () => {
  const held = {
    state: 'slot_taken' as const, alive: false, previewUrl: null,
    occupyingProjectName: 'Car pool', occupyingProjectId: 'pA', restorable: true,
  }
  const takeBack = () => screen.getByRole('button', { name: /^Stop “Car pool” and open this app instead$/ })
  const started = { appId: 'app-1', previewUrl: 'https://app/', status: 'ready', ready: true, restoredFromFailedBuild: false }

  it('★ brings this app up without opening a chat or sending the held message', async () => {
    api.fetchPreviewState.mockResolvedValue(held)
    api.relaunchPreview.mockRejectedValueOnce(heldBy({ dirty: true })).mockResolvedValue(started)
    render(<Workspace />)
    // The message is in the composer and stays there — this citizen never pressed send.
    type('add an out-time column')

    fireEvent.click(await screen.findByRole('button', { name: /^Stop “Car pool” and open this app instead$/ }))
    fireEvent.click(await screen.findByRole('button', { name: /^Stop “Car pool” without saving$/ }))

    await waitFor(() =>
      expect(api.handOverWorkspace).toHaveBeenCalledWith(
        expect.objectContaining({ projectId: 'pA' }),
        false,
        {},
        expect.any(Function),
      ),
    )
    // LIVENESS: the app really came up, so the absences below are a take-back that worked.
    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalledTimes(2))
    // ★ THE GUARANTEE. Reuse the rail's own retry closure here and all three go red.
    expect(where()).toBe('/projects/pB')
    expect(screen.queryByTestId('chat-opened')).toBeNull()
    expect((composer() as HTMLTextAreaElement).value).toBe('add an out-time column')
  })

  it('★ and does none of it through the slot a refused send already holds', async () => {
    // FIRST REFUSAL WINS is that slot's rule. A take-back sharing it would have its own refusal
    // discarded and would resolve the SEND — which is a navigation with the message attached.
    api.fetchPreviewState.mockResolvedValue(held)
    api.relaunchPreview.mockRejectedValue(heldBy({ dirty: true }))
    render(<Workspace />)
    type('add an out-time column')
    fireEvent.click(send())
    await screen.findByRole('dialog')

    api.relaunchPreview.mockReset()
    api.relaunchPreview.mockRejectedValueOnce(heldBy({ dirty: true })).mockResolvedValue(started)
    fireEvent.click(takeBack())
    // Wait for the take-back's own ask to have landed and been dealt with, WHICHEVER WAY the code
    // chose — then press the last question on screen, which is the take-back's when it has one and
    // the send's when it does not. Waiting for two dialogs instead would fail first under the very
    // mutant this scenario exists to catch, and the assertions that matter would never run.
    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalledTimes(1))
    await act(async () => { await Promise.resolve() })
    const stop = screen.getAllByRole('button', { name: /^Stop “Car pool” without saving$/ })
    fireEvent.click(stop[stop.length - 1])
    await waitFor(() => expect(api.handOverWorkspace).toHaveBeenCalled())

    // ★ Share the slot and the confirm resolves the SEND, which is a navigation with the message
    // attached — both of these then go red.
    expect(where()).toBe('/projects/pB')
    expect(screen.queryByTestId('chat-opened')).toBeNull()
    // LIVENESS: this app came up.
    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalledTimes(2))
  })
})
