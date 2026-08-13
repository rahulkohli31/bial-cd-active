/**
 * A2: drag-and-drop is net-new for the home chat's composer, added alongside the
 * assistant-ui migration. It must go through the exact same validation as the paperclip
 * picker (usePendingAttachments' shared `handleFiles`) — same accept/reject rules, same
 * toast wording, same preview row. These tests exercise the drop path specifically;
 * ChatPage-sendpath.test.jsx already covers the picker path and the send path itself.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
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

function dropFiles(target, files) {
  fireEvent.drop(target, { dataTransfer: { files } })
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

describe('ChatPage — composer drag-and-drop (A2)', () => {
  it('accepts a valid dropped file — same preview row as the picker', async () => {
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const composer = await screen.findByTestId('composer')

    const file = new File(['x'.repeat(100)], 'photo.png', { type: 'image/png' })
    dropFiles(composer, [file])

    expect(await screen.findByText('photo.png')).toBeTruthy()
  })

  it('rejects an oversized dropped file with the same toast as the picker', async () => {
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const composer = await screen.findByTestId('composer')

    // MAX_FILE_SIZE is 4 MB for binary attachments (attachmentInput.ts) — one byte over.
    const big = new File([new Uint8Array(4 * 1024 * 1024 + 1)], 'huge.png', { type: 'image/png' })
    dropFiles(composer, [big])

    expect(await screen.findByText(/exceeds the 4 MB limit/i)).toBeTruthy()
    expect(screen.queryByText('huge.png')).toBeNull()
  })

  it('rejects a disallowed dropped file type with the same toast as the picker', async () => {
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const composer = await screen.findByTestId('composer')

    const file = new File(['x'], 'archive.zip', { type: 'application/zip' })
    dropFiles(composer, [file])

    expect(await screen.findByText(/isn't supported/i)).toBeTruthy()
    expect(screen.queryByText('archive.zip')).toBeNull()
  })
})
