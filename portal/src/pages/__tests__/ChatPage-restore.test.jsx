/**
 * The upload-failure restore path (#154). `doSend` clears the composer optimistically and
 * `fireMessage` then awaits `buildUserParts`; when that rejects — an upload error, or the
 * per-user storage cap, which is routine rather than exotic — the draft and the staged files
 * have to come back. Three properties hold that together, and none of them had a test:
 *
 *   1. the restore is CHAT-SCOPED (the leak that held #128 for two review rounds),
 *   2. it actually restores (both halves — text and attachments),
 *   3. it does not DESTROY what the user typed while the upload was in flight.
 *
 * The composer stays live across the whole await (no spinner — `generating` is set only once
 * the upload resolves), so every one of these races is ordinary user behaviour, not a corner
 * case. `buildUserParts` is mocked with a DEFERRED rejection so each test can interleave a
 * chat switch or more typing before the failure lands.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

const h = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  abort: vi.fn(),
  loadHistory: vi.fn(),
  newConversation: vi.fn(),
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  deleteConversation: vi.fn(),
  listProjectConversations: vi.fn(),
  buildUserParts: vi.fn(),
}))

vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null, clearError: vi.fn(), abort: h.abort }),
  getContextLimits: () => ({ soft: 1e9, hard: 1e9 }),
  estimateConversationTokens: () => 0,
}))
vi.mock('../../utils/chatHistory', () => ({
  loadHistory: h.loadHistory,
  newConversation: h.newConversation,
  createConversation: h.createConversation,
  getConversation: h.getConversation,
  deleteConversation: h.deleteConversation,
  relativeTime: () => 'now',
  deriveTitle: (t) => (t || '').slice(0, 40),
}))
// Only `buildUserParts` is replaced — everything else in the module keeps its real behaviour,
// so nothing here is asserting against a hand-written stand-in for the attachment pipeline.
vi.mock('../../utils/attachmentStore', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, buildUserParts: h.buildUserParts }
})
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/chat/MessageContent', () => ({ default: () => null }))
vi.mock('../../utils/conversationApi', () => ({
  listProjectConversations: h.listProjectConversations,
  uuidv7: () => '01900000-0000-7000-8000-000000000000',
}))

import ChatPage from '../ChatPage'

function renderChat(entry) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/chat/:chatId" element={<ChatPage projectId="p1" projectName="VIP Movement" />} />
        <Route path="/projects/:projectId" element={<div>project home</div>} />
        <Route path="/projects" element={<div>projects index</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

const composerText = () => screen.getByPlaceholderText(/Describe what you're thinking/i).value

/** Stage a file the way a user does — the composer's own drop target, so this exercises the
 *  real validate → read → commit path rather than poking state. */
async function stageFile(name) {
  const composer = await screen.findByTestId('composer')
  fireEvent.drop(composer, {
    dataTransfer: { types: ['Files'], files: [new File(['x'.repeat(50)], name, { type: 'image/png' })] },
  })
  return screen.findByText(name)
}

function typeAndSend(text) {
  const textarea = screen.getByPlaceholderText(/Describe what you're thinking/i)
  fireEvent.change(textarea, { target: { value: text } })
  fireEvent.keyDown(textarea, { key: 'Enter' })
}

/** A `buildUserParts` that hangs until the test decides the upload has failed. */
function deferredUploadFailure() {
  let reject
  h.buildUserParts.mockImplementation(() => new Promise((_res, rej) => { reject = rej }))
  return async () => {
    await act(async () => {
      reject(new Error('Storage limit reached.'))
      await Promise.resolve()
    })
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  h.loadHistory.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.getConversation.mockResolvedValue(null)
  h.createConversation.mockResolvedValue({ id: 'chat-1', kind: 'planning', mode: 'plan' })
})
afterEach(() => cleanup())

describe('ChatPage — the upload-failure restore is chat-scoped', () => {
  it('does not push a failed send\'s draft or files into the chat the user switched to', async () => {
    h.listProjectConversations.mockResolvedValue([
      { id: 'chat-1', kind: 'planning', title: 'First', updatedAt: new Date().toISOString() },
      { id: 'chat-2', kind: 'planning', title: 'Second', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    h.getConversation.mockImplementation(async (id) => ({
      id, kind: 'planning', title: id, messages: [], updatedAt: new Date().toISOString(),
    }))
    const failUpload = deferredUploadFailure()

    renderChat('/chat/chat-1')
    expect(await screen.findByText(/Plan your next app/i)).toBeTruthy()

    await stageFile('CONFIDENTIAL-chat1.png')
    typeAndSend('CHAT ONE SECRET DRAFT')
    await waitFor(() => expect(h.buildUserParts).toHaveBeenCalledTimes(1))

    // The upload is still in flight, the composer is still live, and there is no spinner —
    // so switching chats here is ordinary behaviour, not a stress case.
    fireEvent.click(screen.getByText('Second'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-2'))
    fireEvent.change(screen.getByPlaceholderText(/Describe what you're thinking/i), {
      target: { value: 'chat two draft' },
    })

    await failUpload()

    // chat-2's own draft is untouched, and chat-1's file did not follow the user across.
    expect(composerText()).toBe('chat two draft')
    expect(screen.queryByText('CONFIDENTIAL-chat1.png')).toBeNull()
    // The toast stays UNCONDITIONAL — the guard skips the restore, never the notification.
    // Silently swallowing it would leave the user with no idea the send failed at all.
    expect(await screen.findByText(/Storage limit reached/i)).toBeTruthy()
  })
})

describe('ChatPage — the upload-failure restore actually restores', () => {
  it('brings back both the draft and the staged file when the user never left the chat', async () => {
    const failUpload = deferredUploadFailure()
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    await stageFile('report.png')
    typeAndSend('my draft')
    await waitFor(() => expect(h.buildUserParts).toHaveBeenCalledTimes(1))
    // doSend clears optimistically — this is the state the restore has to undo.
    expect(composerText()).toBe('')

    await failUpload()

    expect(composerText()).toBe('my draft')
    expect(screen.getByText('report.png')).toBeTruthy()
    expect(await screen.findByText(/Storage limit reached/i)).toBeTruthy()
    // The turn never reached the model.
    expect(h.sendMessage).not.toHaveBeenCalled()
  })
})

describe('ChatPage — the restore does not destroy what was typed during the upload', () => {
  it('prepends the failed message instead of overwriting the next thought', async () => {
    const failUpload = deferredUploadFailure()
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    await stageFile('report.png')
    typeAndSend('MESSAGE ONE')
    await waitFor(() => expect(h.buildUserParts).toHaveBeenCalledTimes(1))

    // The composer is live with no spinner, so starting the next thought here is the
    // natural thing to do. A blind setText would erase it — and unrecoverably, since
    // assistant-ui's controlled textarea bypasses the browser's native undo stack.
    fireEvent.change(screen.getByPlaceholderText(/Describe what you're thinking/i), {
      target: { value: 'MESSAGE TWO' },
    })

    await failUpload()

    expect(composerText()).toBe('MESSAGE ONE\n\nMESSAGE TWO')
  })
})
