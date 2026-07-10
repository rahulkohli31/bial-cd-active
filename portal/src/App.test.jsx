/**
 * The route table, and specifically the legacy-URL redirects.
 *
 * Project-first moved every chat to a flat `/chat/:id`. A bookmark from before that
 * must land on the same conversation — the id never changed, only the shape of the
 * address — and the surfaces that contradict project-first (the flat all-chats list,
 * the id-less builder) must land on /projects rather than 404.
 *
 * Every page is stubbed: this file asserts routing, nothing else. RequireAuth is
 * stubbed to render its children so no session is needed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'

vi.mock('./utils/auth', () => ({
  isAuthenticated: () => true,
  bootstrapSession: async () => ({ id: 'u1' }),
}))

// `vi.mock` factories are hoisted above every top-level binding, so the stub helper has
// to live inside `vi.hoisted` rather than as a plain const.
const { page } = vi.hoisted(() => ({
  page: (name) => ({ default: () => <div data-testid={name} /> }),
}))
vi.mock('./pages/LoginPage', () => page('login'))
vi.mock('./pages/Dashboard', () => page('dashboard'))
vi.mock('./pages/Workspace', () => page('workspace'))
vi.mock('./pages/SandboxPage', () => page('sandbox'))
vi.mock('./pages/HelpPage', () => page('help'))
vi.mock('./pages/AdminPage', () => page('admin'))
vi.mock('./pages/ProjectsPage', () => page('projects'))
vi.mock('./pages/ProjectPage', () => page('project-home'))
vi.mock('./pages/ChatRoute', () => ({
  default: () => {
    const { chatId } = useParams()
    return <div data-testid="chat-route">{chatId}</div>
  },
}))

import { useParams } from 'react-router-dom'
import App from './App'

/** Drive the real <App/> (BrowserRouter) by setting the URL first. */
function renderAt(path) {
  window.history.pushState({}, '', path)
  return render(<App />)
}

beforeEach(() => vi.clearAllMocks())
afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/')
})

describe('App — project-first routes', () => {
  it('serves the projects index at /projects', () => {
    renderAt('/projects')
    expect(screen.getByTestId('projects')).toBeTruthy()
  })

  it('serves the project home at /projects/:projectId', () => {
    renderAt('/projects/p1')
    expect(screen.getByTestId('project-home')).toBeTruthy()
  })

  it('serves one flat chat route for both kinds', () => {
    renderAt('/chat/abc')
    expect(screen.getByTestId('chat-route').textContent).toBe('abc')
  })
})

describe('App — legacy redirects preserve the conversation id', () => {
  it('/workspace/chat/abc → /chat/abc', () => {
    renderAt('/workspace/chat/abc')
    expect(screen.getByTestId('chat-route').textContent).toBe('abc')
    expect(window.location.pathname).toBe('/chat/abc')
  })

  it('/workspace/builder/abc → /chat/abc (a build is a chat of kind builder)', () => {
    renderAt('/workspace/builder/abc')
    expect(screen.getByTestId('chat-route').textContent).toBe('abc')
    expect(window.location.pathname).toBe('/chat/abc')
  })
})

describe('App — surfaces that contradict project-first land on /projects', () => {
  it.each([
    ['/workspace/history', 'the flat all-chats list'],
    ['/chat/history', 'the retired assistant history'],
    ['/workspace/builder', 'the id-less builder'],
    ['/workspace/chat', 'the id-less chat'],
    ['/builder', 'the legacy builder shortcut'],
  ])('%s redirects to /projects (%s)', (path) => {
    renderAt(path)
    expect(screen.getByTestId('projects')).toBeTruthy()
    expect(window.location.pathname).toBe('/projects')
  })
})

describe('App — the SPA must never claim /apps/*', () => {
  it('does not match /apps/:appId — nginx proxies it to the backend runner', () => {
    // If the SPA ever routed this, it would work under `vite dev` (no /apps proxy) and
    // 404 in the deployed container. The catch-all sends it to /login, proving no match.
    renderAt('/apps/some-app-id')
    expect(screen.queryByTestId('chat-route')).toBeNull()
    expect(screen.queryByTestId('project-home')).toBeNull()
    expect(screen.getByTestId('login')).toBeTruthy()
  })
})
