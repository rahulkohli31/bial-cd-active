/**
 * The route table.
 *
 * Project-first put every chat on a flat `/chat/:id`, and the standalone App Builder /
 * Sandbox scheme is now fully retired: `/workspace*`, `/sandbox`, and `/builder` have NO
 * routes and NO redirect shims. Any such stray URL falls through to the `*` catch-all
 * (→ /login) rather than being carried anywhere.
 *
 * Every page is stubbed: this file asserts routing, nothing else. RequireAuth is
 * stubbed to render its children so no session is needed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'

// `vi.mock` factories are hoisted above every top-level binding, so both the auth
// mocks (need per-test override for the pending-status tests below) and the stub
// helper live inside `vi.hoisted` rather than as plain consts.
const { page, authMocks } = vi.hoisted(() => ({
  page: (name) => ({ default: () => <div data-testid={name} /> }),
  authMocks: {
    isAuthenticated: vi.fn(() => true),
    getStoredUser: vi.fn(() => ({ id: 'u1', status: 'approved' })),
    bootstrapSession: vi.fn(async () => ({ id: 'u1', status: 'approved' })),
  },
}))
vi.mock('./utils/auth', () => authMocks)
vi.mock('./pages/LoginPage', () => page('login'))
vi.mock('./pages/Dashboard', () => page('dashboard'))
vi.mock('./pages/HelpPage', () => page('help'))
vi.mock('./pages/AdminPage', () => page('admin'))
vi.mock('./pages/ProjectsPage', () => page('projects'))
vi.mock('./pages/ProjectPage', () => page('project-home'))
vi.mock('./pages/AwaitingApprovalPage', () => page('awaiting-approval'))
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

beforeEach(() => {
  vi.clearAllMocks()
  // Reset to the approved baseline every test — individual pending-status tests
  // below override these for their own body only.
  authMocks.isAuthenticated.mockReturnValue(true)
  authMocks.getStoredUser.mockReturnValue({ id: 'u1', status: 'approved' })
  authMocks.bootstrapSession.mockResolvedValue({ id: 'u1', status: 'approved' })
})
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

describe('App — the retired App Builder / Sandbox URLs fall through to the catch-all', () => {
  it.each([
    ['/workspace', 'the App Builder hero'],
    ['/workspace/sandbox', 'the Sandbox build page'],
    ['/workspace/chat/abc', 'a legacy flat-chat redirect'],
    ['/workspace/builder/abc', 'a legacy flat-build redirect'],
    ['/workspace/history', 'the flat all-chats list'],
    ['/workspace/builder', 'the id-less builder'],
    ['/workspace/chat', 'the id-less chat'],
    ['/builder', 'the legacy builder shortcut'],
    ['/sandbox', 'the old sandbox alias'],
  ])('%s has no route and lands on /login (%s)', (path) => {
    renderAt(path)
    expect(screen.getByTestId('login')).toBeTruthy()
    expect(window.location.pathname).toBe('/login')
    expect(screen.queryByTestId('chat-route')).toBeNull()
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

describe('App — RequireAuth renders AwaitingApprovalPage for a pending user', () => {
  it('via the async bootstrap path (first visit, nothing cached yet)', async () => {
    authMocks.isAuthenticated.mockReturnValue(false)
    authMocks.bootstrapSession.mockResolvedValue({ id: 'u1', status: 'pending' })

    renderAt('/dashboard')
    expect(await screen.findByTestId('awaiting-approval')).toBeTruthy()
    expect(screen.queryByTestId('dashboard')).toBeNull()
    // Not a redirect — the URL stays put, unlike an unauthenticated visitor.
    expect(window.location.pathname).toBe('/dashboard')
  })

  it('via the CACHED fast path — the regression this guards against', () => {
    // Before the fix, RequireAuth's fast path branched on isAuthenticated() alone
    // (cachedUser != null), so a pending user's own cached profile would render
    // the real route on every subsequent navigation instead of the awaiting page.
    authMocks.isAuthenticated.mockReturnValue(true)
    authMocks.getStoredUser.mockReturnValue({ id: 'u1', status: 'pending' })

    renderAt('/dashboard')
    // Synchronous — no `findBy`/await needed if the fast path is correct.
    expect(screen.getByTestId('awaiting-approval')).toBeTruthy()
    expect(screen.queryByTestId('dashboard')).toBeNull()
  })

  it('an approved user\'s cached session still renders the real route', () => {
    // The baseline from beforeEach (status: 'approved') — proving the fix didn't
    // just make everything pending.
    renderAt('/dashboard')
    expect(screen.getByTestId('dashboard')).toBeTruthy()
    expect(screen.queryByTestId('awaiting-approval')).toBeNull()
  })
})
