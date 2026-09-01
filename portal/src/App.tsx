import { useEffect, useState, type ReactNode } from 'react'
import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom'
import LoginPage from './pages/LoginPage'
import Dashboard from './pages/Dashboard'
import HelpPage from './pages/HelpPage'
import AdminPage from './pages/AdminPage'
import ChatRoute from './pages/ChatRoute'
import MarketplacePage from './pages/MarketplacePage'
import ProjectsPage from './pages/ProjectsPage'
import ProjectPage from './pages/ProjectPage'
import WorkspaceShell from './components/workspace/WorkspaceShell'
import { isAuthenticated, bootstrapSession } from './utils/auth'

// Full-screen silent-refresh spinner. Reuses the app's inline-SVG animate-spin
// idiom (LoginPage) so we don't introduce a new shared component.
function AuthLoading() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-white">
      <svg className="animate-spin h-7 w-7 text-primary" viewBox="0 0 24 24" fill="none" aria-label="Loading">
        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
      </svg>
    </div>
  )
}

/**
 * Route guard. Auth state derives from a ONCE-CACHED GET /auth/me (the session
 * context): the session JWT lives in an HttpOnly cookie the SPA cannot read, so
 * the server is the source of truth. bootstrapSession() resolves it once —
 * transparently attempting a silent cookie refresh if the session JWT has
 * expired — and every later navigation reuses the cache with no refetch and no
 * spinner:
 *   - session cached           → render immediately (no async, no flicker)
 *   - first visit / bootstrap  → spinner while /auth/me resolves; render on hit
 *   - no valid session         → redirect to /login
 */
function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation()
  // 'ok' | 'loading' | 'redirect'. Initialized synchronously: if a prior
  // navigation already cached the session, render children on first paint.
  const [status, setStatus] = useState(() => (isAuthenticated() ? 'ok' : 'loading'))

  // Re-evaluate on every navigation. location.key changes even for same-route
  // param changes (/chat/:a → /chat/:b) where this guard is not remounted.
  useEffect(() => {
    let cancelled = false

    if (isAuthenticated()) {
      setStatus('ok')
      return undefined
    }

    setStatus('loading')
    bootstrapSession().then((user) => {
      if (cancelled) return
      setStatus(user ? 'ok' : 'redirect')
    })
    return () => {
      cancelled = true
    }
  }, [location.key])

  if (status === 'redirect') return <Navigate to="/login" replace />
  if (status === 'loading') return <AuthLoading />
  return children
}

export default function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Routes>
        <Route path="/" element={<Navigate to="/login" replace />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/dashboard" element={<RequireAuth><Dashboard /></RequireAuth>} />
        {/* Enterprise Space + Team Space removed (POC dummy features) — redirect old links. */}
        <Route path="/enterprise" element={<Navigate to="/dashboard" replace />} />
        <Route path="/teamspace" element={<Navigate to="/dashboard" replace />} />

        {/* Project-first: a project is the thing you open, name, and return to. */}
        <Route path="/projects" element={<RequireAuth><ProjectsPage /></RequireAuth>} />
        {/* Cross-user by design (#145): every signed-in BIAL user sees the same catalog. */}
        <Route path="/marketplace" element={<RequireAuth><MarketplacePage /></RequireAuth>} />
        {/* THE WORKSPACE. A pathless layout route wrapping both addresses inside a project, so
            the shell — and above all the running app it holds — is preserved across a move
            between them: React Router renders the same layout element at the same position
            through a sibling route change, and only the outlet content is replaced.

            THE URLS DO NOT NEST. `/projects/:projectId` and `/chat/:chatId` keep the flat
            addressing they have; the layout route adds a shared frame, not a path segment.

            THE AUTH WRAPPER IS ABOVE THE SHELL, not around each child. `RequireAuth` is a
            component re-run per `location.key`, so one instance here is one guard for the whole
            workspace rather than one per address re-running its effect on every move between
            them.

            NOTE: `/apps/:appId` is deliberately NOT a route here, and deliberately not part of
            this layout — nginx proxies /apps/ to the backend runner, and the Vite dev proxy does
            not, so an SPA route there would work locally and 404 in the deployed container. */}
        <Route element={<RequireAuth><WorkspaceShell /></RequireAuth>}>
          <Route path="/projects/:projectId" element={<ProjectPage />} />
          {/* One flat chat URL for both kinds, and ONE surface behind it: `ChatRoute` renders
              `ConversationSurface` whatever the conversation's `kind` is — the kind changes the
              tools a turn is handed on the server, never which component mounts here. (This
              used to fork between two pages, which is exactly the branch the unified surface
              removed.) The project is a breadcrumb resolved from the chat, never a path
              segment. */}
          <Route path="/chat/:chatId" element={<ChatRoute />} />
        </Route>

        <Route path="/help" element={<RequireAuth><HelpPage /></RequireAuth>} />
        <Route path="/admin" element={<RequireAuth><AdminPage /></RequireAuth>} />
        {/* The standalone App Builder / Sandbox scheme is fully retired: `/workspace*`,
            `/sandbox`, and `/builder` have no routes. Stray old bookmarks fall through
            to this catch-all rather than dead redirect shims. */}
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
