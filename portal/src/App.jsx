import { useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom'
import LoginPage from './pages/LoginPage'
import Dashboard from './pages/Dashboard'
import HelpPage from './pages/HelpPage'
import AdminPage from './pages/AdminPage'
import ChatRoute from './pages/ChatRoute'
import ProjectsPage from './pages/ProjectsPage'
import ProjectPage from './pages/ProjectPage'
import AwaitingApprovalPage from './pages/AwaitingApprovalPage'
import { isAuthenticated, getStoredUser, bootstrapSession } from './utils/auth'

// True once a cached session exists AND it isn't pending — the fast path that
// skips bootstrapSession() entirely on ordinary in-SPA navigation. Checking
// isAuthenticated() alone here would let a pending user's cached profile (from
// their own first /auth/me) sail through every subsequent navigation, never
// re-rendering AwaitingApprovalPage.
const isFullyAuthenticated = () => isAuthenticated() && getStoredUser()?.status !== 'pending'

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
function RequireAuth({ children }) {
  const location = useLocation()
  // 'ok' | 'loading' | 'redirect' | 'pending'. Initialized synchronously: if a
  // prior navigation already cached the session, render immediately on first
  // paint (skipping straight to 'pending' too, if that's what's cached).
  const [status, setStatus] = useState(() => {
    if (!isAuthenticated()) return 'loading'
    return getStoredUser()?.status === 'pending' ? 'pending' : 'ok'
  })

  // Re-evaluate on every navigation. location.key changes even for same-route
  // param changes (/chat/:a → /chat/:b) where this guard is not remounted.
  useEffect(() => {
    let cancelled = false

    if (isFullyAuthenticated()) {
      setStatus('ok')
      return undefined
    }
    if (isAuthenticated()) {
      // Cached, but pending — still a fast path (no need to re-hit /auth/me).
      setStatus('pending')
      return undefined
    }

    setStatus('loading')
    bootstrapSession().then((user) => {
      if (cancelled) return
      if (!user) setStatus('redirect')
      else setStatus(user.status === 'pending' ? 'pending' : 'ok')
    })
    return () => {
      cancelled = true
    }
  }, [location.key])

  if (status === 'redirect') return <Navigate to="/login" replace />
  if (status === 'loading') return <AuthLoading />
  if (status === 'pending') return <AwaitingApprovalPage />
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
        <Route path="/projects/:projectId" element={<RequireAuth><ProjectPage /></RequireAuth>} />

        {/* One flat chat URL for both kinds. ChatRoute reads the conversation's `kind`
            and renders ChatPage (planning) or BuilderPage (builder). The project is a
            breadcrumb resolved from the chat, never a path segment.
            NOTE: `/apps/:appId` is deliberately NOT a route here — nginx proxies /apps/
            to the backend runner, and the Vite dev proxy does not, so an SPA route there
            would work locally and 404 in the deployed container. */}
        <Route path="/chat/:chatId" element={<RequireAuth><ChatRoute /></RequireAuth>} />

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
