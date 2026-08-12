/**
 * ChatRoute — the flat `/chat/:chatId` kind dispatcher.
 *
 * Both pages are stubbed so this file tests exactly one thing: which page gets
 * rendered, with which project, and when the route bails to /projects instead.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useNavigate } from 'react-router-dom'

const h = vi.hoisted(() => ({
  getConversation: vi.fn(),
  getProject: vi.fn(),
}))

vi.mock('../../utils/conversationApi.js', () => ({ getConversation: h.getConversation }))
vi.mock('../../utils/projectApi', () => ({ getProject: h.getProject }))

// Stub both pages: which one mounts, and what props it received, is the whole contract.
vi.mock('../ChatPage', () => ({
  // Named, not an anonymous arrow: this stub calls `useNavigate`, and a hook is only legal
  // inside something lint can SEE is a component. As `default: () => …` the rule reads the
  // function's name as "default" — lowercase, so "not a component" — and errors.
  default: function ChatPageStub({ chatId, projectId, projectName }: { chatId?: string; projectId?: string | null; projectName?: string | null }) {
    const navigate = useNavigate()
    return (
      <div data-testid="chat-page">
        {`planning|${chatId}|${projectId}|${projectName}`}
        <button onClick={() => navigate(`/chat/${chatId}`, { replace: true })}>drop query</button>
        <button onClick={() => navigate('/chat/c2')}>go to c2</button>
      </div>
    )
  },
}))
vi.mock('../BuilderPage', () => ({
  default: ({ chatId, projectId, projectName }: { chatId?: string; projectId?: string | null; projectName?: string | null }) => (
    <div data-testid="builder-page">{`builder|${chatId}|${projectId}|${projectName}`}</div>
  ),
}))

import ChatRoute from '../ChatRoute'

/**
 * `state` is the freshly-minted marker's carrier. Entries without one stay plain strings so the
 * existing cases exercise the exact same router input they always did.
 */
function renderRoute(entry: string, state?: unknown) {
  const [pathname, search = ''] = entry.split('?')
  const initial = state === undefined ? entry : { pathname, search: search ? `?${search}` : '', state }
  return render(
    <MemoryRouter initialEntries={[initial]}>
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
    // A bare word like "new" landing at /chat/new is not a real conversation and carries
    // no project query, so it resolves to "gone" and bounces to the projects index.
    h.getConversation.mockResolvedValue(null)
    renderRoute('/chat/new')
    expect(await screen.findByTestId('projects-index')).toBeTruthy()
  })
})

describe('ChatRoute — the GET that cannot succeed', () => {
  // A chat this session just minted has no row until the send path creates it (U7), so its
  // `getConversation` is a guaranteed 404 on every cold new-chat open — and it is not the only
  // one: both pages keep their own hydration fetch, and StrictMode doubles the pair again in dev.
  it('a freshly-minted open issues NO getConversation and renders from the query', async () => {
    renderRoute('/chat/fresh-id?projectId=p1&kind=builder', { freshlyMinted: true })

    expect(await screen.findByTestId('builder-page')).toBeTruthy()
    expect(screen.getByTestId('builder-page').textContent).toContain('|fresh-id|p1|')
    expect(h.getConversation).not.toHaveBeenCalled()
  })

  // THE IMPORTANT ONE. Keying the skip on "the URL has query params" would be a security
  // regression, not just a wrong optimisation: `?kind=` is user-controllable, and a saved chat's
  // URL only loses its query after the FIRST append — so a shared or bookmarked
  // `/chat/{id}?kind=builder` for an already-saved planning chat is an ordinary URL that MUST
  // still be resolved by the server. Router state does not survive a reload and does not travel
  // in a link, which is exactly what makes the marker safe to trust.
  it('an open WITHOUT the marker still fetches, and the server beats a conflicting ?kind=', async () => {
    h.getConversation.mockResolvedValue(conversation({ kind: 'planning' }))
    renderRoute('/chat/c1?projectId=p1&kind=builder')

    expect(await screen.findByTestId('chat-page')).toBeTruthy()
    expect(h.getConversation).toHaveBeenCalledWith('c1')
    expect(screen.queryByTestId('builder-page')).toBeNull()
  })

  it('falls back to the fetch when the marker arrives with no projectId to resolve from', async () => {
    // Fail-safe direction: the skip only ever removes a request whose answer the query already
    // holds. No project in the query means nothing to render from, so ask the server.
    h.getConversation.mockResolvedValue(conversation({ kind: 'planning' }))
    renderRoute('/chat/c1', { freshlyMinted: true })

    expect(await screen.findByTestId('chat-page')).toBeTruthy()
    expect(h.getConversation).toHaveBeenCalledTimes(1)
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

describe('ChatRoute — the page is never torn down mid-turn', () => {
  // A brand-new chat rewrites `/chat/{id}?projectId=…` to `/chat/{id}` the instant its first
  // append lands. If that rewrite re-runs the resolve effect, ChatRoute falls back to its
  // spinner, the page unmounts, and useClaudeAPI's unmount cleanup ABORTS the very stream the
  // append was for — killing the first turn of every new chat.
  it('dropping the transient query does not re-resolve the conversation or unmount the page', async () => {
    h.getConversation.mockResolvedValue(conversation())
    render(
      <MemoryRouter initialEntries={['/chat/c1?projectId=p1&kind=planning']}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatRoute />} />
        </Routes>
      </MemoryRouter>,
    )
    const page = await screen.findByTestId('chat-page')
    expect(h.getConversation).toHaveBeenCalledTimes(1)

    // The page rewrites its own URL, exactly as ChatPage/BuilderPage do after the first append.
    fireEvent.click(screen.getByText('drop query'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBe(page)) // same node: no remount

    expect(h.getConversation).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('status', { name: /loading chat/i })).toBeNull()
  })

  it('keeps the current chat rendered while the next one resolves', async () => {
    // Navigating build chat A → build chat B must not flash the spinner: A's in-flight turn
    // lives in the page's state, and the pages are reconciled, not remounted.
    let resolveSecond: ((value: unknown) => void) | undefined
    h.getConversation
      .mockResolvedValueOnce(conversation({ id: 'c1' }))
      .mockImplementationOnce(() => new Promise((res) => { resolveSecond = res }))

    render(
      <MemoryRouter initialEntries={['/chat/c1']}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatRoute />} />
        </Routes>
      </MemoryRouter>,
    )
    const page = await screen.findByTestId('chat-page')
    expect(page.textContent).toContain('|c1|')

    fireEvent.click(screen.getByText('go to c2'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledTimes(2))

    // c2 has not resolved. The page is still mounted, still showing c1.
    expect(screen.queryByRole('status', { name: /loading chat/i })).toBeNull()
    expect(screen.getByTestId('chat-page').textContent).toContain('|c1|')

    resolveSecond?.({ id: 'c2', kind: 'planning', projectId: 'p1', messages: [] })
    await waitFor(() => expect(screen.getByTestId('chat-page').textContent).toContain('|c2|'))
  })
})
