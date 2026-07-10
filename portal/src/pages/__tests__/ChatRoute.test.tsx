/**
 * ChatRoute — the flat `/chat/:chatId` kind dispatcher.
 *
 * Both pages are stubbed so this file tests exactly one thing: which page gets
 * rendered, with which project, and when the route bails to /projects instead.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

const h = vi.hoisted(() => ({
  getConversation: vi.fn(),
  getProject: vi.fn(),
}))

vi.mock('../../utils/conversationApi.js', () => ({ getConversation: h.getConversation }))
vi.mock('../../utils/projectApi', () => ({ getProject: h.getProject }))

// Stub both pages: which one mounts, and what props it received, is the whole contract.
vi.mock('../ChatPage', () => ({
  default: ({ chatId, projectId, projectName }: { chatId?: string; projectId?: string | null; projectName?: string | null }) => (
    <div data-testid="chat-page">{`planning|${chatId}|${projectId}|${projectName}`}</div>
  ),
}))
vi.mock('../BuilderPage', () => ({
  default: ({ chatId, projectId, projectName }: { chatId?: string; projectId?: string | null; projectName?: string | null }) => (
    <div data-testid="builder-page">{`builder|${chatId}|${projectId}|${projectName}`}</div>
  ),
}))

import ChatRoute from '../ChatRoute'

function renderRoute(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/chat/:chatId" element={<ChatRoute />} />
        <Route path="/projects" element={<div data-testid="projects-index">projects</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

const conversation = (over: Record<string, unknown> = {}) => ({
  id: 'c1',
  kind: 'planning',
  projectId: 'p1',
  title: 'T',
  messages: [],
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  h.getProject.mockResolvedValue({ id: 'p1', name: 'VIP Movement', description: null, appId: null, appStatus: null, createdAt: '', updatedAt: '' })
})
afterEach(() => cleanup())

describe('ChatRoute — kind dispatch', () => {
  it('renders ChatPage for a planning conversation', async () => {
    h.getConversation.mockResolvedValue(conversation({ kind: 'planning' }))
    renderRoute('/chat/c1')
    expect(await screen.findByTestId('chat-page')).toBeTruthy()
    expect(screen.queryByTestId('builder-page')).toBeNull()
  })

  it('renders BuilderPage for a builder conversation', async () => {
    h.getConversation.mockResolvedValue(conversation({ kind: 'builder' }))
    renderRoute('/chat/c1')
    expect(await screen.findByTestId('builder-page')).toBeTruthy()
  })

  it('lets the SERVER win when its kind disagrees with ?kind=', async () => {
    h.getConversation.mockResolvedValue(conversation({ kind: 'planning' }))
    renderRoute('/chat/c1?kind=builder')
    // A stale or hand-edited query must never render a builder over a planning transcript.
    expect(await screen.findByTestId('chat-page')).toBeTruthy()
    expect(screen.queryByTestId('builder-page')).toBeNull()
  })

  it('issues exactly ONE getConversation to resolve the kind', async () => {
    h.getConversation.mockResolvedValue(conversation())
    renderRoute('/chat/c1')
    await screen.findByTestId('chat-page')
    expect(h.getConversation).toHaveBeenCalledTimes(1)
  })
})

describe('ChatRoute — a conversation whose row does not exist yet', () => {
  it('renders the ?kind= page when the id 404s but a ?projectId= is present', async () => {
    h.getConversation.mockResolvedValue(null)
    renderRoute('/chat/new-uuid?projectId=p1&kind=builder')
    // The row appears on the first appendMessage; until then only the query knows the project.
    expect(await screen.findByTestId('builder-page')).toBeTruthy()
    expect(screen.getByTestId('builder-page').textContent).toContain('|new-uuid|p1|')
  })

  it('defaults an unrecognised ?kind= to planning rather than trusting it', async () => {
    h.getConversation.mockResolvedValue(null)
    renderRoute('/chat/new-uuid?projectId=p1&kind=wat')
    expect(await screen.findByTestId('chat-page')).toBeTruthy()
  })

  it('redirects to /projects when the id 404s with NO query', async () => {
    h.getConversation.mockResolvedValue(null)
    renderRoute('/chat/ghost')
    expect(await screen.findByTestId('projects-index')).toBeTruthy()
  })

  it('redirects to /projects when the literal word "new" is routed as a chat id', async () => {
    // Workspace's old "Plan with AI" CTA navigated to /workspace/chat/new, which the
    // redirect table now lands here. There is no such conversation and no project.
    h.getConversation.mockResolvedValue(null)
    renderRoute('/chat/new')
    expect(await screen.findByTestId('projects-index')).toBeTruthy()
  })
})

describe('ChatRoute — the project breadcrumb', () => {
  it('passes projectName down once getProject resolves', async () => {
    h.getConversation.mockResolvedValue(conversation())
    renderRoute('/chat/c1')
    await waitFor(() => expect(screen.getByTestId('chat-page').textContent).toContain('VIP Movement'))
    expect(h.getProject).toHaveBeenCalledWith('p1')
  })

  it('passes projectName: null and does NOT redirect when the project 404s', async () => {
    // A chat whose project vanished should still render its transcript.
    h.getConversation.mockResolvedValue(conversation())
    h.getProject.mockRejectedValue(new Error('gone'))
    renderRoute('/chat/c1')
    await waitFor(() => expect(screen.getByTestId('chat-page').textContent).toContain('|null'))
    expect(screen.queryByTestId('projects-index')).toBeNull()
  })

  it('falls back to the query projectId when the conversation carries none', async () => {
    h.getConversation.mockResolvedValue(conversation({ projectId: undefined }))
    renderRoute('/chat/c1?projectId=p9')
    await waitFor(() => expect(screen.getByTestId('chat-page').textContent).toContain('|p9|'))
  })
})

describe('ChatRoute — load failure', () => {
  it('falls back to the query rather than stranding the user on a spinner', async () => {
    h.getConversation.mockRejectedValue(new Error('boom'))
    renderRoute('/chat/c1?projectId=p1&kind=builder')
    expect(await screen.findByTestId('builder-page')).toBeTruthy()
  })

  it('bails to /projects when the load fails and there is no query to fall back on', async () => {
    h.getConversation.mockRejectedValue(new Error('boom'))
    renderRoute('/chat/c1')
    expect(await screen.findByTestId('projects-index')).toBeTruthy()
  })
})
