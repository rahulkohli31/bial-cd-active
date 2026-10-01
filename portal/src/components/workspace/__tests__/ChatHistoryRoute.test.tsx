/**
 * The chat list inside the real workspace, driven through the REAL route table by address and by
 * the links a citizen presses: the composer's Last chat card, the list route, a chat opened from
 * it, and the way back. A hand-built table would prove the components and not the wiring.
 *
 * The real `ProjectPage`, `WorkspaceRail`, `ChatRoute`, `ConversationSlot`, toolbar and pane host
 * render. Mocked: the wire reads, the framed app (an iframe whose identity is the claim), and the
 * conversation body, which publishes to the channel exactly what a real one does for its kind.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ConversationHeader } from '../../../utils/conversationApi'
import type { Project } from '../../../utils/projectApi'

const APP_URL = 'https://app-a.example.azurecontainerapps.io/'

const h = vi.hoisted(() => ({
  getProject: vi.fn(),
  list: vi.fn(),
  getConversation: vi.fn(),
  rename: vi.fn(),
  fetchPreviewState: vi.fn(),
}))

vi.mock('../../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/auth')>()),
  isAuthenticated: () => true,
  bootstrapSession: async () => ({ id: 'u1' }),
  getStoredUser: () => ({
    chat_kinds: [
      { value: 'plan', name: 'Plan', description: 'Shape a plan first.' },
      { value: 'build', name: 'Build', description: 'Change the live app.' },
    ],
  }),
}))
// The observation beacons' transport; nothing here is about them.
vi.mock('../../../utils/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/api')>()),
  authFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}))
vi.mock('../../../utils/projectApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/projectApi')>()),
  getProject: h.getProject,
}))
vi.mock('../../../utils/conversationApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/conversationApi')>()),
  listProjectConversations: h.list,
  getConversation: h.getConversation,
  renameConversation: h.rename,
}))
vi.mock('../../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/buildSessionApi')>()),
  fetchPreviewState: h.fetchPreviewState,
  fetchSaveState: vi.fn(async () => ({ appId: 'a1', dirty: false, containerHead: null, savedHead: null })),
  fetchCompileState: vi.fn(async () => 'unknown'),
  checkWorkspace: vi.fn(async () => false),
  relaunchPreview: vi.fn(async () => ({})),
}))
vi.mock('../../LivePreview', () => ({
  default: ({ previewUrl }: { previewUrl: string | null }) => (
    <iframe data-testid="framed-app" title="Your app" src={previewUrl ?? undefined} />
  ),
}))
vi.mock('../../PublishStatusChip', () => ({ default: () => <span data-testid="publish-chip-stub" /> }))
vi.mock('../../../hooks/useUsageToday', () => ({ useUsageToday: () => null }))
vi.mock('../../layout/AppShell', () => ({
  default: ({ children }: { children: React.ReactNode }) => <div data-testid="app-shell">{children}</div>,
}))
vi.mock('../RailComposer', () => ({ default: () => <div data-testid="rail-composer" /> }))
// The conversation body publishes what a real one does: its project, the app it frames, and
// whether its kind wants the app seen. A build chat does; a plan chat does not.
vi.mock('../../chat/ConversationSurface', async () => {
  const channel = await import('../workspaceChannel')
  const PANE_WANTED: Record<string, boolean> = { plan: false, build: true }
  return {
    default: function ConversationBodyStub({ chatId, kind, projectId }: { chatId: string; kind: string; projectId: string | null }) {
      channel.useWorkspaceProject(projectId)
      channel.usePublishAddress({ url: 'https://app-a.example.azurecontainerapps.io/', status: 'ready', serving: true }, projectId)
      channel.useAppPaneVisible(PANE_WANTED[kind] ?? false)
      return <div data-testid="conversation-body">{chatId}</div>
    },
  }
})

import App from '../../../App'

const PROJECT: Project = {
  id: 'p1',
  name: 'Visitor Log',
  description: null,
  appId: 'a1',
  appStatus: null,
  hasRelaunchableSnapshot: true,
  hasSavedSnapshot: null,
  isServing: false,
  isPublishing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'owner',
}

const HOUR = 3_600_000

/** `n` chats of project p1, newest first, kinds alternating build / plan. */
function headers(n: number): ConversationHeader[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `c${i + 1}`,
    kind: i % 2 === 0 ? 'build' : 'plan',
    projectId: 'p1',
    title: `Chat ${i + 1}`,
    createdAt: '2026-09-01T00:00:00Z',
    updatedAt: new Date(Date.now() - (i + 1) * HOUR).toISOString(),
  }))
}

function renderAt(path: string) {
  window.history.pushState({}, '', path)
  return render(<App />)
}

const where = () => window.location.pathname + window.location.search
const frame = () => screen.getByTestId('framed-app')
const pane = () => screen.getByTestId('app-pane')
const rail = () => screen.getByTestId('workspace-outlet')
const history = () => screen.getByRole('button', { name: 'Chat history' })

/** What the mocked API holds; a rename changes it, and every read answers from it. */
let server: ConversationHeader[] = []

beforeEach(() => {
  vi.clearAllMocks()
  server = headers(23)
  h.getProject.mockResolvedValue(PROJECT)
  h.list.mockImplementation(async () => server.map((chat) => ({ ...chat })))
  h.rename.mockImplementation(async (id: string, title: string) => {
    server = server.map((chat) => (chat.id === id ? { ...chat, title } : chat))
  })
  h.fetchPreviewState.mockResolvedValue({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true })
  h.getConversation.mockImplementation(async (id: string) => {
    const header = server.find((chat) => chat.id === id)
    return header ? { ...header, messages: [], activeTurn: null, contextTokens: null } : null
  })
})

afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/')
})

describe('from the application to its chats', () => {
  it('★ shows the newest chat under the composer, and View all opens the list beside the same running app', async () => {
    renderAt('/projects/p1')

    const card = await screen.findByTestId('last-chat-card')
    expect(card.textContent).toContain('Chat 1')
    expect(screen.getByTestId('rail-composer')).toBeTruthy()
    const app = await screen.findByTestId('framed-app')
    expect(history().getAttribute('aria-pressed')).toBe('false')

    fireEvent.click(screen.getByRole('link', { name: /view all/i }))

    expect(where()).toBe('/projects/p1/chats')
    expect(await screen.findAllByTestId('chat-row')).toHaveLength(8)
    expect(history().getAttribute('aria-pressed')).toBe('true')
    expect(rail().getAttribute('data-rail-mode')).toBe('history')
    // THE CLAIM: the app beside the list is the same frame, still shown, and nothing was reloaded.
    expect(frame()).toBe(app)
    expect(frame().getAttribute('src')).toBe(APP_URL)
    expect(pane().getAttribute('aria-hidden')).toBe('false')
    expect(h.getProject).toHaveBeenCalledTimes(1)
    expect(h.list).toHaveBeenCalledTimes(1)
    expect(h.list).toHaveBeenCalledWith('p1')
  })

  it('★ keeps the toolbar and Save in place on the list, and History pressed again returns to the composer', async () => {
    renderAt('/projects/p1/chats')
    await screen.findAllByTestId('chat-row')
    const app = await screen.findByTestId('framed-app')
    expect(screen.getByTestId('toolbar-title').textContent).toBe('Visitor Log')
    expect(screen.getByTestId('workspace-menu')).toBeTruthy()
    expect(await screen.findByTestId('save-project')).toBeTruthy()

    fireEvent.click(history())
    expect(where()).toBe('/projects/p1')
    expect(await screen.findByTestId('rail-composer')).toBeTruthy()
    expect(frame()).toBe(app)
    expect(pane().getAttribute('aria-hidden')).toBe('false')
  })

  it('the toolbar\'s back control on the list lands on the application, not on My Applications', async () => {
    renderAt('/projects/p1/chats')
    await screen.findAllByTestId('chat-row')
    fireEvent.click(screen.getByRole('button', { name: 'Back to the application' }))
    expect(where()).toBe('/projects/p1')
  })

  it('★ an application with no chats keeps the rail as it was, and History still opens "No chats yet"', async () => {
    h.list.mockResolvedValue([])
    renderAt('/projects/p1')

    await screen.findByTestId('rail-composer')
    await waitFor(() => expect(h.list).toHaveBeenCalled())
    expect(screen.queryByTestId('last-chat')).toBeNull()
    expect(screen.queryByText(/last chat/i)).toBeNull()

    fireEvent.click(await screen.findByRole('button', { name: 'Chat history' }))
    expect(await screen.findByText('No chats yet')).toBeTruthy()
  })
})

describe('a chat opened from the list, and back', () => {
  it('★ opens beside the same app, and "All chats" returns to the same tab, search and page', async () => {
    renderAt('/projects/p1/chats?kind=build&page=2')
    const rows = await screen.findAllByTestId('chat-row')
    const app = await screen.findByTestId('framed-app')
    const link = within(rows[0]).getAllByRole('link')[0]
    expect(link.getAttribute('href')).toBe('/chat/c17')

    fireEvent.click(link)
    expect(where()).toBe('/chat/c17')
    const strip = await screen.findByTestId('all-chats-strip')
    await waitFor(() => expect(within(strip).getByTestId('all-chats-strip-title').textContent).toBe('Chat 17'))
    expect(history().getAttribute('aria-pressed')).toBe('true')
    expect(frame()).toBe(app)

    fireEvent.click(screen.getByRole('link', { name: 'Back to all chats' }))
    expect(where()).toBe('/projects/p1/chats?kind=build&page=2')
    expect((await screen.findByRole('radio', { name: 'Build' })).getAttribute('aria-checked')).toBe('true')
    await waitFor(() => expect(screen.getByRole('button', { name: '2' }).getAttribute('aria-current')).toBe('page'))
    await waitFor(() => expect(document.activeElement?.getAttribute('href')).toBe('/chat/c17'))
    expect(frame()).toBe(app)
  })

  it('★ a plan chat hides the app without unmounting it, so the list it returns to has the same frame', async () => {
    renderAt('/projects/p1/chats?kind=plan')
    const rows = await screen.findAllByTestId('chat-row')
    const app = await screen.findByTestId('framed-app')

    fireEvent.click(within(rows[0]).getAllByRole('link')[0])
    await screen.findByTestId('all-chats-strip')
    expect(pane().getAttribute('aria-hidden')).toBe('true')
    expect(frame()).toBe(app)

    fireEvent.click(screen.getByRole('link', { name: 'Back to all chats' }))
    await screen.findAllByTestId('chat-row')
    expect(pane().getAttribute('aria-hidden')).toBe('false')
    expect(frame()).toBe(app)
  })

  it('★ the browser\'s own Back from that chat lands on the list it came from', async () => {
    renderAt('/projects/p1/chats?q=Chat%201')
    const rows = await screen.findAllByTestId('chat-row')
    fireEvent.click(within(rows[0]).getAllByRole('link')[0])
    await screen.findByTestId('all-chats-strip')

    window.history.back()

    await waitFor(() => expect(where()).toBe('/projects/p1/chats?q=Chat%201'))
    expect(await screen.findAllByTestId('chat-row')).not.toHaveLength(0)
    expect((screen.getByRole('searchbox') as HTMLInputElement).value).toBe('Chat 1')
  })

  it('a chat opened from the Last chat card is not drawn as opened from the list', async () => {
    renderAt('/projects/p1')
    fireEvent.click(await screen.findByTestId('last-chat-card'))
    expect(where()).toBe('/chat/c1')
    await screen.findByTestId('all-chats-strip')
    expect(history().getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByRole('link', { name: 'Back to all chats' }).getAttribute('href')).toBe('/projects/p1/chats')
  })
})

describe('a chat renamed in the list', () => {
  it('★ is called by its new name in the chat\'s own strip and the toolbar, with no reload', async () => {
    renderAt('/projects/p1/chats')
    await screen.findAllByTestId('chat-row')

    fireEvent.pointerDown(document.querySelector('[data-chat-menu="c1"]') as HTMLElement)
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Rename' }))
    const input = await screen.findByRole('textbox', { name: 'Chat name' })
    fireEvent.change(input, { target: { value: 'Add an out-time column' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Chat name' })).toBeNull())

    const row = screen.getAllByTestId('chat-row')[0]
    expect(row.textContent).toContain('Add an out-time column')
    fireEvent.click(within(row).getByRole('link'))
    await waitFor(() => expect(screen.getByTestId('all-chats-strip-title').textContent).toBe('Add an out-time column'))
    expect(screen.getByTestId('toolbar-title').textContent).toBe('Add an out-time column')
  })
})
