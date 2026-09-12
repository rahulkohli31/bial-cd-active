import { useEffect, useState, type ReactNode } from 'react'
import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom'
import LoginPage from './pages/LoginPage'
import HelpPage from './pages/HelpPage'
import AdminPage from './pages/AdminPage'
import ChatRoute from './pages/ChatRoute'
import MarketplacePage from './pages/MarketplacePage'
import ProjectsPage from './pages/ProjectsPage'
import ProjectPage from './pages/ProjectPage'
import SharedProjectPage from './pages/SharedProjectPage'
import WorkspaceShell from './components/workspace/WorkspaceShell'
import { isAuthenticated, bootstrapSession } from './utils/auth'
import { BusyGlyph } from './components/ui/Waiting'

/**
 * The boot / silent-refresh wait — WORDS, not only a spinner.
 *
 * `index.css`'s reduced-motion block suppresses `.animate-spin` outright, so for a citizen who
 * asks for less motion this screen was a stationary circle and nothing else: a full-bleed white
 * page with no sentence on it, at the one moment nothing else is on screen to explain the pause.
 * The sentence is what carries the wait now and the glyph is decoration beside it — `aria-hidden`
 * rather than `aria-label="Loading"`, because a named spinner next to a sentence saying the same
 * thing is the wait announced twice.
 *
 * THE GLYPH GOES THROUGH `BusyGlyph` LIKE EVERY OTHER WAIT. A hand-rolled `<svg className=
 * "animate-spin">` is invisible to a sweep that matches icon components, which is exactly how this
 * one survived the first pass — and a stationary circle beside "Getting things ready…" still reads
 * as a boot that died, sentence or no sentence.
 *
 * `aria-busy` sits on the box and announces nothing; the words are the announcement, read out of
 * the permanent region in `RequireAuth` below.
 */
function AuthLoading() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-white" aria-busy="true">
      <div className="flex flex-col items-center gap-3">
        <BusyGlyph size={28} className="text-primary" />
        <p className="text-sm font-medium text-neutral">Getting things ready…</p>
      </div>
    </div>
  )
}

/**
 * Route guard. Auth state derives from a ONCE-CACHED GET /auth/me: the session JWT lives in an
 * HttpOnly cookie the SPA cannot read, so the server is the source of truth. bootstrapSession()
 * resolves it once, silently refreshing an expired JWT, and later navigations reuse the cache:
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

  // THE POLITE REGION IS PERMANENT AND THE WAIT BOX IS WHAT APPEARS INSIDE IT.
  //
  // A live region inserted together with its text is missed entirely by several reader-and-browser
  // combinations — `TurnBanner` and `LivePreview` both record it — and this wait has no leaf of its
  // own that outlives it, so the region cannot live in `AuthLoading`. It lives here, where the
  // guard is mounted whichever way the session resolved, and only its contents change. That is
  // what makes the SILENT REFRESH audible: a later navigation flips a decided guard back to
  // `loading` against a region that has been sitting in the accessibility tree since the first
  // paint.
  //
  // It WRAPS the visible sentence rather than adding an `sr-only` copy of it. Two elements
  // carrying one sentence is that sentence read twice to anything reading the DOM, and
  // `Announcer.tsx` records that writing it the other way broke three tests.
  return (
    <>
      <div role="status" aria-live="polite" data-testid="auth-wait">
        {status === 'loading' ? <AuthLoading /> : null}
      </div>
      {status === 'ok' ? children : null}
    </>
  )
}

export default function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Routes>
        <Route path="/" element={<Navigate to="/login" replace />} />
        <Route path="/login" element={<LoginPage />} />
        {/* THE PROJECT LIST IS THE LANDING SCREEN. `/dashboard` used to be a
            welcome page whose only job was a button to `/projects`; once the project list
            carries the summary numbers, that hop has nothing left to do. Both addresses
            still resolve so existing links, bookmarks and the navbar keep working — the
            welcome page is what went, not the URL. */}
        <Route path="/dashboard" element={<Navigate to="/projects" replace />} />
        {/* Enterprise Space and Team Space are not features of this product. These two
            addresses resolve only so old links and bookmarks land on the list rather than
            on nothing; they are safe to delete once nothing points at them. */}
        <Route path="/enterprise" element={<Navigate to="/projects" replace />} />
        <Route path="/teamspace" element={<Navigate to="/projects" replace />} />

        {/* Project-first: a project is the thing you open, name, and return to. */}
        <Route path="/projects" element={<RequireAuth><ProjectsPage /></RequireAuth>} />
        {/* Cross-user by design: every signed-in BIAL user sees the same catalog. */}
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
            this layout. BOTH edges send `/apps/` to the control plane — nginx (`portal/nginx.conf`)
            and the Vite dev proxy (`vite.config.js`) — so a route declared here would be shadowed
            before React Router ever saw it, in dev and in the container alike. A deployed app is
            reached on the apps hostname at `/a/<key>/` (nginx SITE 2), never from here. */}
        <Route element={<RequireAuth><WorkspaceShell /></RequireAuth>}>
          <Route path="/projects/:projectId" element={<ProjectPage />} />
          {/* One flat chat URL for both kinds: `ChatRoute` mounts the same surface whatever the
              conversation is, and the project is a breadcrumb resolved from the chat rather than
              a path segment. */}
          <Route path="/chat/:chatId" element={<ChatRoute />} />
        </Route>

        {/* A colleague's restricted view of a project shared with them (#198) — deliberately
            OUTSIDE `WorkspaceShell`. That layout exists to carry a builder's own running app
            across chat/rail/toolbar surfaces a shared recipient must never reach; this route
            gets its own minimal chrome instead of a share of that one. */}
        <Route path="/shared/:projectId" element={<RequireAuth><SharedProjectPage /></RequireAuth>} />

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
