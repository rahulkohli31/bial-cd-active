/**
 * Moving between applications, chats and the chat list, through the real route table. What it
 * pins: the list belongs to the application and never to the conversation (the conversation
 * unmounts when the address moves to the list, and the framed app never does), and a list never
 * carries one application's chats into another.
 *
 * Mocked: the wire reads, the framed app, and the conversation body, which only reports when it
 * mounts and unmounts.
 */
import { useEffect } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ConversationHeader } from '../../../utils/conversationApi'
import type { Project } from '../../../utils/projectApi'

const h = vi.hoisted(() => ({
  getProject: vi.fn(),
  list: vi.fn(),
  getConversation: vi.fn(),
  fetchPreviewState: vi.fn(),
  mounts: [] as string[],
  unmounts: [] as string[],
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
vi.mock('../../PublishStatusChip', () => ({ default: () => <span /> }))
vi.mock('../../../hooks/useUsageToday', () => ({ useUsageToday: () => null }))
vi.mock('../../layout/AppShell', () => ({
  default: ({ children }: { children: React.ReactNode }) => <div data-testid="app-shell">{children}</div>,
}))
vi.mock('../RailComposer', () => ({ default: () => <div data-testid="rail-composer" /> }))
vi.mock('../../chat/ConversationSurface', async () => {
  const channel = await import('../workspaceChannel')
  return {
    default: function ConversationBodyStub({ chatId, projectId }: { chatId: string; projectId: string | null }) {
      channel.useWorkspaceProject(projectId)
      channel.usePublishAddress({ url: 'https://app.example.azurecontainerapps.io/', status: 'ready', serving: true }, projectId)
      channel.useAppPaneVisible(true)
      useEffect(() => {
        h.mounts.push(chatId)
        return () => {
          h.unmounts.push(chatId)
        }
      }, [chatId])
      return <div data-testid="conversation-body">{chatId}</div>
    },
  }
})

import App from '../../../App'

const APP_URL = 'https://app.example.azurecontainerapps.io/'
const HOUR = 3_600_000

function project(id: string, name: string): Project {
  return {
    id,
    name,
    description: null,
    appId: `app-${id}`,
    appStatus: null,
    hasRelaunchableSnapshot: true,
    hasSavedSnapshot: null,
    isServing: false,
    isPublishing: false,
    createdAt: '2026-07-10T00:00:00Z',
    updatedAt: '2026-07-10T00:00:00Z',
    access: 'owner',
  }
}

/** Three chats of `projectId`, titled `<label> chat 1..3`, newest first. */
function chatsOf(projectId: string, label: string): ConversationHeader[] {
  return [1, 2, 3].map((n) => ({
    id: `${projectId}-c${n}`,
    kind: 'build',
    projectId,
    title: `${label} chat ${n}`,
    createdAt: '2026-09-01T00:00:00Z',
    updatedAt: new Date(Date.now() - n * HOUR).toISOString(),
  }))
}

const ALPHA = chatsOf('pA', 'Alpha')
const BETA = chatsOf('pB', 'Beta')

/** Move the address the way the browser's own Back and Forward do, without a reload. */
function goTo(path: string) {
  act(() => {
    window.history.pushState({}, '', path)
    window.dispatchEvent(new PopStateEvent('popstate'))
  })
}

function renderAt(path: string) {
  window.history.pushState({}, '', path)
  return render(<App />)
}

const where = () => window.location.pathname + window.location.search
const titles = () => screen.queryAllByTestId('chat-row').map((row) => row.textContent ?? '')
const rowFor = async (chatId: string) => {
  const rows = await screen.findAllByTestId('chat-row')
  const row = rows.find((candidate) =>
    within(candidate).getAllByRole('link').some((link) => link.getAttribute('href') === `/chat/${chatId}`),
  )
  if (!row) throw new Error(`no row opens ${chatId}`)
  return within(row).getAllByRole('link')[0]
}

beforeEach(() => {
  vi.clearAllMocks()
  h.mounts.length = 0
  h.unmounts.length = 0
  h.getProject.mockImplementation(async (id: string) => project(id, id === 'pA' ? 'Alpha App' : 'Beta App'))
  h.list.mockImplementation(async (id: string) => (id === 'pA' ? ALPHA : BETA))
  h.getConversation.mockImplementation(async (id: string) => {
    const header = [...ALPHA, ...BETA].find((chat) => chat.id === id)
    return header ? { ...header, messages: [], activeTurn: null, contextTokens: null } : null
  })
  h.fetchPreviewState.mockResolvedValue({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true })
})

afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/')
})

describe('the chat list and the conversation', () => {
  it('unmounts the conversation when the address moves from a chat to the list, and keeps the same framed app', async () => {
    renderAt('/chat/pA-c1')
    expect((await screen.findByTestId('conversation-body')).textContent).toBe('pA-c1')
    const frame = await screen.findByTestId('framed-app')
    expect(h.unmounts).toEqual([])

    fireEvent.click(await screen.findByRole('link', { name: 'Back to all chats' }))

    expect(await screen.findAllByTestId('chat-row')).toHaveLength(3)
    expect(where()).toBe('/projects/pA/chats')
    expect(h.unmounts).toEqual(['pA-c1'])
    expect(screen.queryByTestId('conversation-body')).toBeNull()
    expect(screen.queryByTestId('conversation-slot')).toBeNull()
    expect(screen.getByTestId('framed-app')).toBe(frame)
    expect(frame.getAttribute('src')).toBe(APP_URL)
  })

  it('draws the list outside the conversation slot, so opening a chat swaps one for the other', async () => {
    renderAt('/projects/pA/chats')
    const frame = await screen.findByTestId('framed-app')
    await screen.findAllByTestId('chat-row')
    expect(screen.queryByTestId('conversation-slot')).toBeNull()
    expect(h.mounts).toEqual([])

    fireEvent.click(await rowFor('pA-c2'))

    expect(await screen.findByTestId('conversation-slot')).toBeTruthy()
    expect(within(screen.getByTestId('conversation-slot')).getByTestId('conversation-body').textContent).toBe('pA-c2')
    expect(screen.queryByTestId('chat-row')).toBeNull()
    expect(h.mounts).toEqual(['pA-c2'])
    expect(screen.getByTestId('framed-app')).toBe(frame)
  })

  it('remounts a conversation each time it is opened from the list, with the list between', async () => {
    renderAt('/projects/pA/chats')
    fireEvent.click(await rowFor('pA-c1'))
    await screen.findByTestId('conversation-body')
    fireEvent.click(screen.getByRole('link', { name: 'Back to all chats' }))
    fireEvent.click(await rowFor('pA-c1'))

    await waitFor(() => expect(screen.getByTestId('conversation-body').textContent).toBe('pA-c1'))
    expect(h.mounts).toEqual(['pA-c1', 'pA-c1'])
    expect(h.unmounts).toEqual(['pA-c1'])
  })
})

describe('switching application', () => {
  it('never shows the first application\'s chats under the second, not even while the second loads', async () => {
    let answerBeta: (rows: ConversationHeader[]) => void = () => {}
    h.list.mockImplementation((id: string) =>
      id === 'pA' ? Promise.resolve(ALPHA) : new Promise<ConversationHeader[]>((resolve) => { answerBeta = resolve }),
    )
    renderAt('/projects/pA/chats')
    await waitFor(() => expect(titles().join('|')).toContain('Alpha chat 1'))

    goTo('/projects/pB/chats')
    await waitFor(() => expect(h.list).toHaveBeenLastCalledWith('pB'))
    expect(screen.getByTestId('chat-history-loading')).toBeTruthy()
    expect(titles()).toEqual([])

    await act(async () => {
      answerBeta(BETA)
      await Promise.resolve()
    })
    await waitFor(() => expect(titles().join('|')).toContain('Beta chat 1'))
    expect(titles()).toHaveLength(3)
    expect(titles().join('|')).not.toContain('Alpha')
  })

  it('leaves out a chat of another application that a read carries', async () => {
    h.list.mockImplementation(async (id: string) => (id === 'pA' ? ALPHA : [ALPHA[0], ...BETA]))
    renderAt('/projects/pA/chats')
    await waitFor(() => expect(titles()).toHaveLength(3))

    goTo('/projects/pB/chats')

    await waitFor(() => expect(titles().join('|')).toContain('Beta chat 1'))
    expect(titles()).toHaveLength(3)
    expect(titles().join('|')).not.toContain('Alpha')
  })

  it('drops a slow answer for the first application that arrives after the switch', async () => {
    let answerAlpha: (rows: ConversationHeader[]) => void = () => {}
    h.list.mockImplementation((id: string) =>
      id === 'pA' ? new Promise<ConversationHeader[]>((resolve) => { answerAlpha = resolve }) : Promise.resolve(BETA),
    )
    renderAt('/projects/pA/chats')
    await waitFor(() => expect(h.list).toHaveBeenCalledWith('pA'))

    goTo('/projects/pB/chats')
    await waitFor(() => expect(titles().join('|')).toContain('Beta chat 1'))

    await act(async () => {
      answerAlpha(ALPHA)
      await Promise.resolve()
    })

    expect(titles()).toHaveLength(3)
    expect(titles().join('|')).not.toContain('Alpha')
    expect(screen.getAllByTestId('chat-row').every((row) => row.textContent?.includes('Beta'))).toBe(true)
  })
})
