/**
 * The toolbar's `⋯` menu on a chat address: Settings and Share open on the chat's own project, in
 * every state a planning chat passes through, and the menu is absent while there is no project
 * for it to open on.
 *
 * Mounted through the real shell and the real slot, with a stand-in for `ChatRoute` publishing the
 * heading. The two dialogs are stubbed: their suites own what they draw, and this one owns which
 * opens, for which project.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, act, within, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useParams } from 'react-router-dom'
import type { Project } from '../../../utils/projectApi'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), resolvePlanOptions: vi.fn(),
  stopTurn: vi.fn(), fetchPreviewState: vi.fn(), fetchSaveState: vi.fn(), fetchCompileState: vi.fn(),
  checkWorkspace: vi.fn(), renewPresence: vi.fn(), onProjectUpdate: vi.fn(),
}))

vi.mock('../../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t: string) => (t || '').slice(0, 40),
}))
vi.mock('../../../utils/conversationApi', async (orig) => ({
  ...(await orig<typeof import('../../../utils/conversationApi')>()),
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../../utils/attachmentStore', async (orig) => ({
  ...(await orig<typeof import('../../../utils/attachmentStore')>()),
  buildUserParts: h.buildUserParts,
}))
vi.mock('../../../utils/turnStreamApi', async (orig) => ({
  ...(await orig<typeof import('../../../utils/turnStreamApi')>()),
  startTurn: (...a: unknown[]) => h.startTurn(...a),
  readTurnStream: (...a: unknown[]) => h.readTurnStream(...a),
  buildFromPlan: (...a: unknown[]) => h.buildFromPlan(...a),
  resolvePlanOptions: (...a: unknown[]) => h.resolvePlanOptions(...a),
  stopTurn: (...a: unknown[]) => h.stopTurn(...a),
}))
vi.mock('../../../utils/buildSessionApi', async (orig) => ({
  ...(await orig<typeof import('../../../utils/buildSessionApi')>()),
  fetchPreviewState: (...a: unknown[]) => h.fetchPreviewState(...a),
  fetchSaveState: (...a: unknown[]) => h.fetchSaveState(...a),
  fetchCompileState: (...a: unknown[]) => h.fetchCompileState(...a),
  checkWorkspace: (...a: unknown[]) => h.checkWorkspace(...a),
  renewPresence: (...a: unknown[]) => h.renewPresence(...a),
}))
vi.mock('../../PublishStatusChip', () => ({ default: () => <span data-testid="publish-chip-stub" /> }))
vi.mock('../../projects/AppSettingsDialog', () => ({
  default: ({ project, onClose }: { project: Project; onClose: () => void }) => (
    <div role="dialog" aria-label={`Settings for ${project.name}`} data-project={project.id}>
      <button type="button" onClick={onClose}>close</button>
    </div>
  ),
}))
vi.mock('../../projects/SharePanel', () => ({
  default: ({ projectId, projectName, onClose }: { projectId: string; projectName: string; onClose: () => void }) => (
    <div role="dialog" aria-label={`Share ${projectName}`} data-project={projectId}>
      <button type="button" onClick={onClose}>close</button>
    </div>
  ),
}))

const { default: WorkspaceShell } = await import('../../workspace/WorkspaceShell')
const { default: ConversationSlot } = await import('../../workspace/ConversationSlot')
const { usePublishHeading } = await import('../../workspace/workspaceChannel')
const { primeTurn, waitForGateOpen, T_DELTA, T_CARD, T_END, BUILD_CHAT_ID } =
  await import('../../../pages/__tests__/_builderSession.jsx')

const PROJECT: Project = {
  id: 'p1',
  name: 'Visitor Log',
  description: null,
  appId: 'app-1',
  appStatus: null,
  hasRelaunchableSnapshot: false,
  hasSavedSnapshot: null,
  isServing: false,
  isPublishing: false,
  createdAt: '2026-09-01T00:00:00Z',
  updatedAt: '2026-09-01T00:00:00Z',
  access: 'owner',
}

/** What `ChatRoute` does for this address: publish the heading, and mount the slot. */
function ChatAddress({ project }: { project: Project | null }) {
  const { chatId = '' } = useParams()
  const kind = chatId === BUILD_CHAT_ID ? 'build' : 'plan'
  usePublishHeading({ projectId: 'p1', projectName: project?.name ?? null, chatTitle: null, chatKind: kind })
  return (
    <ConversationSlot
      conversation={{ chatId, kind, projectId: 'p1', project, projectHasSavedBuild: null }}
      onProjectUpdate={h.onProjectUpdate}
    />
  )
}

const tree = (project: Project | null) => (
  <MemoryRouter initialEntries={['/chat/plan-1']}>
    <Routes>
      <Route element={<WorkspaceShell />}>
        <Route path="/chat/:chatId" element={<ChatAddress project={project} />} />
      </Route>
    </Routes>
  </MemoryRouter>
)

/** The menu is Radix: it opens on pointerdown, never on click. */
async function press(item: 'Settings…' | 'Share…'): Promise<void> {
  fireEvent.pointerDown(await screen.findByTestId('workspace-menu'))
  fireEvent.click(await screen.findByRole('menuitem', { name: item }))
}

async function settingsOpensAndCloses(): Promise<void> {
  await press('Settings…')
  const dialog = await screen.findByRole('dialog', { name: 'Settings for Visitor Log' })
  expect(dialog.getAttribute('data-project')).toBe('p1')
  fireEvent.click(within(dialog).getByRole('button', { name: 'close' }))
  expect(screen.queryByRole('dialog', { name: 'Settings for Visitor Log' })).toBeNull()
}

/** A turn whose stream stays open after its first words, until `finish` plays the rest. */
function heldPlanTurn() {
  let emit: ((frame: unknown) => void) | null = null
  let close: ((outcome: string) => void) | null = null
  h.readTurnStream.mockImplementation(({ onFrame }: { onFrame: (frame: unknown) => void }) => {
    emit = onFrame
    onFrame(T_DELTA('Here is a plan for a visitor log.'))
    return new Promise((resolve) => {
      close = resolve
    })
  })
  return {
    finish: async () => {
      await act(async () => {
        emit?.(T_CARD())
        emit?.(T_END())
        close?.('completed')
        await Promise.resolve()
      })
    },
  }
}

async function sendPlanRequest(): Promise<void> {
  await waitForGateOpen()
  const box = screen.getByPlaceholderText('Tell me what to change…')
  fireEvent.change(box, { target: { value: 'a visitor log for the terminal' } })
  fireEvent.keyDown(box, { key: 'Enter' })
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  h.newBuild.mockReturnValue('plan-1')
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockImplementation(async (id: string) => ({ id, kind: id === BUILD_CHAT_ID ? 'build' : 'plan', messages: [] }))
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text: string) => [{ type: 'text', text }])
  h.fetchSaveState.mockResolvedValue({ appId: 'app-1', dirty: false, savedHead: null, containerHead: null })
  h.fetchPreviewState.mockResolvedValue({ state: 'asleep', alive: false, previewUrl: null, restorable: false, startingSince: null, startFailure: null })
  h.fetchCompileState.mockResolvedValue('unknown')
  h.checkWorkspace.mockResolvedValue(false)
  h.renewPresence.mockResolvedValue('renewed')
  primeTurn(h)
})
afterEach(() => cleanup())

describe('the ⋯ menu on a chat address', () => {
  it('opens the settings dialog for the chat’s project once the plan has finished', async () => {
    render(tree(PROJECT))
    await sendPlanRequest()
    expect(await screen.findByRole('button', { name: /^Build this plan$/ })).toBeTruthy()

    await settingsOpensAndCloses()
  })

  it('is there while the reply streams, after the plan completes, and after the plan is approved', async () => {
    const turn = heldPlanTurn()
    render(tree(PROJECT))
    await sendPlanRequest()

    expect(await screen.findByText(/Here is a plan for a visitor log\./)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Build this plan$/ })).toBeNull()
    await settingsOpensAndCloses()

    await turn.finish()
    const build = await screen.findByRole('button', { name: /^Build this plan$/ })
    await settingsOpensAndCloses()

    fireEvent.click(build)
    await waitFor(() => expect(screen.getByTestId('chat-panel').getAttribute('data-chat-kind')).toBe('build'))
    expect(h.buildFromPlan).toHaveBeenCalledTimes(1)
    await settingsOpensAndCloses()
  })

  it('opens the share dialog for the same project', async () => {
    render(tree(PROJECT))
    await waitForGateOpen()

    await press('Share…')
    const dialog = await screen.findByRole('dialog', { name: 'Share Visitor Log' })
    expect(dialog.getAttribute('data-project')).toBe('p1')
  })

  it('offers no menu while the project is missing, and a working one the moment it arrives', async () => {
    const view = render(tree(null))
    await waitForGateOpen()

    expect(screen.getByTestId('chat-panel')).toBeTruthy()
    expect(screen.getByTestId('workspace-toolbar').textContent).toContain('Your application')
    expect(screen.queryByTestId('workspace-menu')).toBeNull()

    view.rerender(tree(PROJECT))

    await settingsOpensAndCloses()
    await press('Share…')
    expect(await screen.findByRole('dialog', { name: 'Share Visitor Log' })).toBeTruthy()
  })
})
