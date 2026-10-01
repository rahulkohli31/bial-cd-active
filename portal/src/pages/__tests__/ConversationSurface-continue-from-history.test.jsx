/**
 * Continuing a chat that was opened from the history list, through the REAL route table and the
 * REAL conversation surface: open an old chat, reload into a running turn, stop it, have a send
 * refused, and move between the list and a running chat.
 *
 * Mocked: the wire (reads, the turn transport), the framed app, the rail composer and the page
 * chrome. Everything between the address bar and the transcript is real.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { TurnStartError } from '../../utils/turnStreamApi'
import { turnStreaming, textReply, waitForGateOpen } from './_builderSession.jsx'

const APP_URL = 'https://app-a.example.azurecontainerapps.io/'

const h = vi.hoisted(() => ({
  getProject: vi.fn(),
  list: vi.fn(),
  getConversation: vi.fn(),
  fetchPreviewState: vi.fn(),
  startTurn: vi.fn(),
  readTurnStream: vi.fn(),
  stopTurn: vi.fn(),
}))

vi.mock('../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal()),
  isAuthenticated: () => true,
  bootstrapSession: async () => ({ id: 'u1' }),
  getStoredUser: () => ({
    chat_kinds: [
      { value: 'plan', name: 'Plan', description: 'Shape a plan first.' },
      { value: 'build', name: 'Build', description: 'Change the live app.' },
    ],
  }),
}))
vi.mock('../../utils/api', async (importOriginal) => ({
  ...(await importOriginal()),
  authFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}))
vi.mock('../../utils/projectApi', async (importOriginal) => ({
  ...(await importOriginal()),
  getProject: h.getProject,
}))
vi.mock('../../utils/conversationApi', async (importOriginal) => ({
  ...(await importOriginal()),
  listProjectConversations: h.list,
  getConversation: h.getConversation,
  createConversation: async () => ({ id: 'conv-created' }),
}))
vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: async () => [],
  getBuild: (id) => h.getConversation(id),
  deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/attachmentStore', async (importOriginal) => ({
  ...(await importOriginal()),
  buildUserParts: async (text) => [{ type: 'text', text }],
}))
vi.mock('../../utils/turnStreamApi', async (importOriginal) => ({
  ...(await importOriginal()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  stopTurn: (...a) => h.stopTurn(...a),
}))
vi.mock('../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal()),
  fetchPreviewState: h.fetchPreviewState,
  fetchSaveState: vi.fn(async () => ({ appId: 'a1', dirty: false, containerHead: null, savedHead: null })),
  fetchCompileState: vi.fn(async () => 'unknown'),
  checkWorkspace: vi.fn(async () => false),
  relaunchPreview: vi.fn(async () => ({})),
}))
vi.mock('../../components/LivePreview', () => ({
  default: ({ previewUrl }) => <iframe data-testid="framed-app" title="Your app" src={previewUrl ?? undefined} />,
}))
vi.mock('../../components/PublishStatusChip', () => ({ default: () => <span /> }))
vi.mock('../../hooks/useUsageToday', () => ({ useUsageToday: () => null }))
vi.mock('../../components/layout/AppShell', () => ({
  default: ({ children }) => <div data-testid="app-shell">{children}</div>,
}))
vi.mock('../../components/workspace/RailComposer', () => ({ default: () => <div data-testid="rail-composer" /> }))

import App from '../../App'

const PROJECT = {
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

const say = (id, role, seq, text) => ({ id, role, seq, parts: [{ type: 'text', text }] })

/** The server's record of each chat, newest first: c1 is the newest, c3 the oldest. */
const CHATS = {
  c1: { title: 'Newest chat', messages: [say('m1', 'user', 0, 'newest question')] },
  c2: {
    title: 'Older chat',
    messages: [say('m2u', 'user', 0, 'older question'), say('m2a', 'assistant', 1, 'older answer')],
  },
  c3: { title: 'Oldest chat', messages: [say('m3', 'user', 0, 'oldest question')] },
}

const headers = () =>
  Object.keys(CHATS).map((id, i) => ({
    id,
    kind: 'build',
    projectId: 'p1',
    title: CHATS[id].title,
    createdAt: '2026-09-01T00:00:00Z',
    updatedAt: new Date(Date.now() - (i + 1) * HOUR).toISOString(),
  }))

/** Ids whose read carries a running turn. */
let running

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  Element.prototype.scrollIntoView = vi.fn()
  running = new Map()
  h.getProject.mockResolvedValue(PROJECT)
  h.list.mockImplementation(async () => headers())
  h.fetchPreviewState.mockResolvedValue({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true })
  h.getConversation.mockImplementation(async (id) => {
    const header = headers().find((chat) => chat.id === id)
    return header
      ? { ...header, messages: CHATS[id].messages, activeTurn: running.get(id) ?? null, contextTokens: null }
      : null
  })
  h.startTurn.mockResolvedValue({ turnId: 't-new' })
  h.readTurnStream.mockImplementation(turnStreaming(textReply('a fresh answer')))
  h.stopTurn.mockResolvedValue('stopping')
})

afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/')
})

function renderAt(path) {
  window.history.pushState({}, '', path)
  return render(<App />)
}

const where = () => window.location.pathname + window.location.search
const composer = () => screen.getByTestId('composer-input')
const type = (text) => fireEvent.change(composer(), { target: { value: text } })
const submit = () => fireEvent.keyDown(composer(), { key: 'Enter' })

/** The row's own link, found by where it goes: row order is the list's business, not this suite's. */
async function rowFor(chatId) {
  const rows = await screen.findAllByTestId('chat-row')
  const row = rows.find((candidate) =>
    within(candidate).getAllByRole('link').some((link) => link.getAttribute('href') === `/chat/${chatId}`),
  )
  if (!row) throw new Error(`no row opens ${chatId}`)
  return within(row).getAllByRole('link')[0]
}

const backToList = () => fireEvent.click(screen.getByRole('link', { name: 'Back to all chats' }))

/**
 * A `readTurnStream` that holds every subscription open until the test ends it by hand. A
 * reattach opens with the snapshot frame, which is what tells the surface which turn it is on.
 */
function heldStreams() {
  const subscriptions = []
  h.readTurnStream.mockImplementation(({ onFrame, signal, ...rest }) => new Promise((resolve) => {
    const entry = { onFrame, signal, ...rest, end: resolve }
    subscriptions.push(entry)
    if (rest.turnId) {
      onFrame({ type: 'snapshot', seq: 1, turnId: rest.turnId, turnStatus: 'running', parts: [], working: false, items: [] })
    }
    signal.addEventListener('abort', () => resolve('aborted'))
  }))
  return subscriptions
}

describe('a chat opened from the list', () => {
  it('loads its transcript, and a send appends to that chat', async () => {
    renderAt('/projects/p1/chats')
    fireEvent.click(await rowFor('c2'))

    expect(where()).toBe('/chat/c2')
    const thread = await screen.findByTestId('thread-messages')
    await within(thread).findByText('older answer')
    expect(within(thread).getByText('older question')).toBeTruthy()

    await waitForGateOpen()
    type('and a follow-up')
    submit()

    await waitFor(() =>
      expect(h.startTurn).toHaveBeenCalledWith('c2', expect.objectContaining({ text: 'and a follow-up' })),
    )
    expect(h.startTurn).toHaveBeenCalledTimes(1)
    await within(thread).findByText('a fresh answer')
    expect(within(thread).getByText('and a follow-up')).toBeTruthy()
    // What was there before is still there, above the new turn.
    expect(within(thread).getByText('older question')).toBeTruthy()
    expect(within(thread).getByText('older answer')).toBeTruthy()
  })
})

describe('a reload into a running turn', () => {
  beforeEach(() => running.set('c2', { turnId: 't-live', lastSeq: 7 }))

  it('reattaches by turn id from the start of the stream and finishes', async () => {
    h.readTurnStream.mockImplementation(
      turnStreaming([
        { type: 'text_delta', seq: 1, text: 'picked up where it was' },
        { type: 'turn_ended', seq: 2, turnId: 't-live', status: 'completed' },
      ]),
    )
    renderAt('/chat/c2')

    expect(await screen.findByText('picked up where it was')).toBeTruthy()
    const [args] = h.readTurnStream.mock.calls[0]
    expect(args.conversationId).toBe('c2')
    expect(args.turnId).toBe('t-live')
    // `lastSeq` counts frames this tab never saw; resuming from it would drop the reply's start.
    expect(args.cursor).toBe(0)
    await waitFor(() => expect(screen.queryByTestId('stop-turn')).toBeNull())
    expect(composer().disabled).toBe(false)
  })

  it('retries a truncated stream once and then shows the reply', async () => {
    let reads = 0
    h.readTurnStream.mockImplementation(async ({ onFrame }) => {
      reads += 1
      if (reads === 1) return 'truncated'
      onFrame({ type: 'text_delta', seq: 1, text: 'recovered reply' })
      onFrame({ type: 'turn_ended', seq: 2, turnId: 't-live', status: 'completed' })
      return 'completed'
    })
    renderAt('/chat/c2')

    expect(await screen.findByText('recovered reply')).toBeTruthy()
    expect(h.readTurnStream).toHaveBeenCalledTimes(2)
    expect(h.readTurnStream.mock.calls[1][0].turnId).toBe('t-live')
    expect(screen.queryByText(/the connection dropped/i)).toBeNull()
  })
})

describe('stopping a running turn', () => {
  it('stops that turn, and once the response ends the next send is accepted', async () => {
    running.set('c2', { turnId: 't-live', lastSeq: 0 })
    const streams = heldStreams()
    renderAt('/chat/c2')

    const stop = await screen.findByTestId('stop-turn')
    await waitFor(() => expect(streams).toHaveLength(1))
    fireEvent.click(stop)
    await waitFor(() => expect(h.stopTurn).toHaveBeenCalledWith('c2', 't-live'))

    await act(async () => {
      streams[0].onFrame({ type: 'turn_ended', seq: 3, turnId: 't-live', status: 'stopped', reason: 'stopped_by_user' })
      streams[0].end('completed')
    })
    await waitFor(() => expect(screen.queryByTestId('stop-turn')).toBeNull())
    expect(composer().disabled).toBe(false)

    h.readTurnStream.mockImplementation(turnStreaming(textReply('after the stop')))
    await waitForGateOpen()
    type('carry on')
    submit()
    await waitFor(() =>
      expect(h.startTurn).toHaveBeenCalledWith('c2', expect.objectContaining({ text: 'carry on' })),
    )
    expect(await screen.findByText('after the stop')).toBeTruthy()
  })

  it('offers no Stop while idle, and the composer is enabled', async () => {
    renderAt('/chat/c2')

    await screen.findByText('older answer')
    await waitForGateOpen()
    expect(composer().disabled).toBe(false)
    expect(screen.queryByTestId('stop-turn')).toBeNull()
    expect(h.readTurnStream).not.toHaveBeenCalled()
  })
})

describe('a refused send', () => {
  it('keeps what was typed when a turn is already running elsewhere', async () => {
    h.startTurn.mockRejectedValue(
      new TurnStartError(409, 'Another chat is building right now.', 'already_building_here'),
    )
    renderAt('/chat/c2')
    await screen.findByText('older answer')
    await waitForGateOpen()

    type('please add a filter')
    submit()

    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))
    await act(async () => { await Promise.resolve() })
    expect(composer().value).toBe('please add a filter')
    expect(composer().disabled).toBe(false)
    expect(h.startTurn).toHaveBeenCalledTimes(1)
  })
})

describe('moving between a running chat and the list', () => {
  beforeEach(() => running.set('c1', { turnId: 't-run', lastSeq: 4 }))

  it('aborts the stream on the way out, keeps the running chat reachable, and reattaches on the way back', async () => {
    const streams = heldStreams()
    renderAt('/chat/c1')
    await screen.findByTestId('stop-turn')
    await waitFor(() => expect(streams).toHaveLength(1))
    expect(streams[0].signal.aborted).toBe(false)

    backToList()
    await screen.findAllByTestId('chat-row')
    expect(streams[0].signal.aborted).toBe(true)
    // The surface is gone with its stream, so nothing on the list is still reading the turn.
    expect(screen.queryByTestId('composer-input')).toBeNull()
    expect(screen.queryByTestId('stop-turn')).toBeNull()

    fireEvent.click(await rowFor('c1'))
    expect(where()).toBe('/chat/c1')
    await screen.findByTestId('stop-turn')
    await waitFor(() => expect(streams).toHaveLength(2))
    expect(streams[1].turnId).toBe('t-run')
    expect(streams[1].cursor).toBe(0)
    expect(streams[1].signal.aborted).toBe(false)
  })

  it('opens another chat from the list while the first is still running, without carrying its Stop', async () => {
    const streams = heldStreams()
    renderAt('/chat/c1')
    await screen.findByTestId('stop-turn')

    backToList()
    fireEvent.click(await rowFor('c3'))
    await screen.findByText('oldest question')
    await waitForGateOpen()

    expect(streams[0].signal.aborted).toBe(true)
    expect(screen.queryByTestId('stop-turn')).toBeNull()
    expect(composer().disabled).toBe(false)
  })

  it('keeps Stop wired to the running turn after chat, list, chat', async () => {
    const streams = heldStreams()
    renderAt('/chat/c1')
    await screen.findByTestId('stop-turn')

    backToList()
    fireEvent.click(await rowFor('c1'))
    const stop = await screen.findByTestId('stop-turn')
    await waitFor(() => expect(streams).toHaveLength(2))
    fireEvent.click(stop)
    await waitFor(() => expect(h.stopTurn).toHaveBeenCalledWith('c1', 't-run'))
    expect(h.stopTurn).toHaveBeenCalledTimes(1)
  })
})

describe('the list is not part of the conversation', () => {
  it('replaces the conversation surface rather than sitting inside it', async () => {
    renderAt('/chat/c2')
    await screen.findByText('older answer')
    const frame = await screen.findByTestId('framed-app')
    expect(screen.getByTestId('conversation-slot')).toBeTruthy()
    expect(screen.queryByTestId('chat-row')).toBeNull()

    backToList()

    expect((await screen.findAllByTestId('chat-row')).length).toBe(3)
    expect(screen.queryByTestId('conversation-slot')).toBeNull()
    expect(screen.queryByTestId('composer-input')).toBeNull()
    expect(screen.getByTestId('framed-app')).toBe(frame)
  })
})
