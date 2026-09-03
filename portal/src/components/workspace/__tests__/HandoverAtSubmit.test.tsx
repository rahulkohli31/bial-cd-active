/**
 * THE ONE-WORKSPACE RULE, ASKED AT SUBMIT (plan 002, U9) — issue #161.
 *
 * ═══ THE DEFECT, IN ONE SENTENCE ═══
 *
 * The browser navigated first. A citizen typed into project B, the address changed, the chat
 * mounted, its send hit the server — and only THEN did the refusal arrive, so somebody who had
 * been building in project A for two minutes was interrupted by a question about a chat that had
 * already opened in front of them. The backend's ordering was always right; the browser jumped
 * ahead of it.
 *
 * ═══ WHAT THESE SCENARIOS PIN ═══
 *
 * Rendered through the REAL shell, because every one of them is about the relationship between a
 * composer, a dialog the shell mounts, and an address that must not change. A test that mounted
 * the composer alone could not see any of it — which is exactly how the defect survived.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import type { Project } from '../../../utils/projectApi'

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
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
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

function Workspace() {
  return (
    <MemoryRouter initialEntries={['/projects/pB']}>
      <Where />
      <Routes>
        <Route element={<WorkspaceShell />}>
          <Route
            path="/projects/:projectId"
            element={<ProjectWorkspace project={PROJECT} onProjectUpdate={() => {}} />}
          />
          <Route path="/chat/:chatId" element={<div data-testid="chat-opened" />} />
        </Route>
      </Routes>
    </MemoryRouter>
  )
}

const composer = () => screen.getByPlaceholderText(/Describe the change you need/i)
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
    expect(text).toContain('Visitor Log') // the one being started — issue #161's framing half
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

describe('cancelling', () => {
  it('★ leaves the message, closes the dialog, and touches the other project not at all', async () => {
    api.relaunchPreview.mockRejectedValue(heldBy())
    render(<Workspace />)
    type('add an out-time column')
    fireEvent.click(send())
    await screen.findByRole('dialog')

    fireEvent.keyDown(screen.getByRole('dialog').querySelector('[tabindex="-1"]')!, { key: 'Escape' })

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
    expect(api.handOverWorkspace.mock.calls[0]?.[0]).toBe('pA')
    // ONE chat, not two: the first attempt was refused before it navigated.
    expect(screen.getAllByTestId('chat-opened')).toHaveLength(1)
    expect(where()).toMatch(/^\/chat\/[0-9a-f-]+\?projectId=pB&kind=build$/)
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
      new Error('The other project has not finished what it was doing yet. Nothing has changed — try again in a moment.'),
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
