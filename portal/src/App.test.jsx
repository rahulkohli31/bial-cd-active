/**
 * The route table, and the workspace shell's wiring.
 *
 * Project-first put every chat on a flat `/chat/:id`; the standalone App Builder / Sandbox
 * scheme is fully retired, with no routes and no redirect shims — a stray URL under
 * `/workspace*`, `/sandbox`, or `/builder` falls through to the `*` catch-all (→ /login).
 *
 * This file renders the REAL `<App/>` and drives it by URL rather than a hand-built route
 * table, because a component test would prove the component and not the wiring — the part that
 * can be got wrong. Every page is stubbed except `RequireAuth`, which runs for real: where the
 * guard sits relative to the shell is one of the claims (an unauthenticated visit must not paint
 * the workspace frame around a redirect).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'

const h = vi.hoisted(() => ({
  /** Flipped per test. The guard reads this synchronously on every navigation. */
  authed: true,
  bootstrap: vi.fn(),
  /** Every destination the workspace scenarios move between. */
  destinations: ['/projects/p1', '/projects/p2', '/chat/abc', '/chat/in-project-a', '/chat/in-project-b'],
}))

vi.mock('./utils/auth', () => ({
  isAuthenticated: () => h.authed,
  bootstrapSession: (...a) => h.bootstrap(...a),
}))

// `vi.mock` factories are hoisted above every top-level binding, so the stub helper has
// to live inside `vi.hoisted` rather than as a plain const.
const { page } = vi.hoisted(() => ({
  page: (name) => ({ default: () => <div data-testid={name} /> }),
}))
vi.mock('./pages/LoginPage', () => page('login'))
vi.mock('./pages/AdminPage', () => page('admin'))
vi.mock('./pages/ProjectsPage', () => page('projects'))
// THE TWO WORKSPACE STUBS CARRY LINKS, so the shell's persistence claim is driven the way the
// product drives it — a router navigation from inside the outlet. Pushing onto `window.history`
// from outside would not reach the router at all, and a test that "navigated" that way would
// assert that an element it never re-rendered had not changed.
vi.mock('./pages/ProjectPage', async () => {
  const { Link } = await import('react-router-dom')
  return {
    default: function ProjectPageStub() {
      return (
        <div data-testid="project-home">
          {h.destinations.map((to) => (
            <Link key={to} to={to}>{`go ${to}`}</Link>
          ))}
        </div>
      )
    },
  }
})
vi.mock('./pages/ChatRoute', async () => {
  const { Link, useParams: useRouteParams } = await import('react-router-dom')
  return {
    // Named so lint can see this stub IS a component — it calls `useParams`, and a hook inside
    // an anonymous `default: () => …` reads as a plain function, which may not call hooks.
    default: function ChatRouteStub() {
      const { chatId } = useRouteParams()
      return (
        <div>
          <div data-testid="chat-route">{chatId}</div>
          {h.destinations.map((to) => (
            <Link key={to} to={to}>{`go ${to}`}</Link>
          ))}
        </div>
      )
    },
  }
})
// The WORKSPACE shell is REAL — it is this file's subject. The navigation shell around it is
// stubbed, because it reads a session, a usage meter and an admin count this file has none of,
// and counting page frames does not need the real one. `AppShell.test.tsx` is where its own
// behaviour is asserted. It renders a marker, and its children, so "exactly one frame per
// address" stays assertable and every routed page still mounts inside it.
vi.mock('./components/layout/AppShell', () => ({
  default: ({ children }) => <div data-testid="app-shell">{children}</div>,
}))

import App from './App'

/** Drive the real <App/> (BrowserRouter) by setting the URL first. */
function renderAt(path) {
  window.history.pushState({}, '', path)
  return render(<App />)
}

/** Navigate the SAME rendered app through the router, by clicking a link inside the outlet. */
function goTo(path) {
  fireEvent.click(screen.getByText(`go ${path}`))
}

const shell = () => screen.queryByTestId('workspace-grid')

beforeEach(() => {
  vi.clearAllMocks()
  h.authed = true
  h.bootstrap.mockResolvedValue({ id: 'u1' })
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

  it('and it is still outside the workspace layout, so no shell frames it', () => {
    // The layout route is the natural place for somebody to "tidily" add `/apps/:appId` next to
    // the two addresses it already holds. It must stay out of the table entirely.
    renderAt('/apps/some-app-id')
    expect(shell()).toBeNull()
  })
})

describe('App — the workspace shell is one element across a move inside a project, the wiring half', () => {
  it('keeps the SAME shell element across project → chat → project; only the outlet content changes', () => {
    renderAt('/projects/p1')
    const frame = shell()
    expect(frame).toBeTruthy()
    expect(screen.getByTestId('project-home')).toBeTruthy()

    goTo('/chat/abc')
    expect(shell()).toBe(frame) // the same DOM node, not merely another one like it
    expect(screen.getByTestId('chat-route').textContent).toBe('abc')
    expect(screen.queryByTestId('project-home')).toBeNull()

    goTo('/projects/p1')
    expect(shell()).toBe(frame)
    expect(screen.getByTestId('project-home')).toBeTruthy()
  })

  it('survives a move to a chat in a DIFFERENT project', () => {
    // The shell is the workspace's frame, not one project's. What happens to the PANE in this
    // case is the pane host's own scenario — a different project is a different app.
    renderAt('/chat/in-project-a')
    const frame = shell()

    goTo('/chat/in-project-b')

    expect(shell()).toBe(frame)
    expect(screen.getByTestId('chat-route').textContent).toBe('in-project-b')
  })

  it('renders exactly one navigation frame and one workspace frame at each address', () => {
    // The regression this catches is the obvious one: a surface that kept a frame of its own
    // after the shell grew one, so the workspace paints two.
    renderAt('/projects/p1')
    expect(screen.getAllByTestId('app-shell')).toHaveLength(1)
    expect(screen.getAllByTestId('workspace-grid')).toHaveLength(1)

    goTo('/chat/abc')
    expect(screen.getAllByTestId('app-shell')).toHaveLength(1)
    expect(screen.getAllByTestId('workspace-grid')).toHaveLength(1)
  })
})

describe('App — the auth guard sits ABOVE the shell', () => {
  // WHERE THE GUARD SITS IS ONLY OBSERVABLE WHILE IT IS UNDECIDED, and that is worth stating
  // because the obvious test does not work. A DENIED visit renders `<Navigate/>`, which changes
  // the matched route — so the workspace disappears whichever side of the shell the guard is on,
  // and a "no frame after a redirect" assertion passes against both arrangements. The state that
  // discriminates is `loading`: a guard nested inside the layout paints the workspace's navbar and
  // two-column frame around the auth spinner, so somebody who may not be signed in at all watches
  // the frame of a workspace assemble around a spinner first. (Mutation-checked both ways.)
  // THE WAIT IS FOUND BY ITS WORDS, not by a labelled glyph. The spinner is
  // `aria-hidden` now — `index.css` suppresses `.animate-spin` under `prefers-reduced-motion`, so
  // a named-but-frozen circle was the whole of what this screen said — and the sentence carries it.
  const wait = () => screen.queryByText(/Getting things ready/)

  it.each([
    ['/chat/abc', 'chat-route'],
    ['/projects/p1', 'project-home'],
  ])('while the session is still resolving at %s, no workspace frame is painted around the wait', (path, pageId) => {
    h.authed = false
    h.bootstrap.mockReturnValue(new Promise(() => {})) // never settles: hold the guard undecided

    renderAt(path)

    expect(wait()).toBeTruthy()
    expect(shell()).toBeNull()
    expect(screen.queryByTestId('app-shell')).toBeNull()
    expect(screen.queryByTestId(pageId)).toBeNull()
  })

  it('a denied visit lands on /login with nothing of the workspace left behind', async () => {
    h.authed = false
    h.bootstrap.mockResolvedValue(null)

    renderAt('/chat/abc')
    await waitFor(() => expect(screen.getByTestId('login')).toBeTruthy())

    expect(shell()).toBeNull()
    expect(screen.queryByTestId('app-shell')).toBeNull()
    expect(screen.queryByTestId('chat-route')).toBeNull()
  })
})

describe('the welcome page is gone — its old addresses now land on the project list', () => {
  it.each(['/dashboard', '/enterprise', '/teamspace'])(
    '%s lands on the project list instead of its own page',
    (path) => {
      // INERTNESS, not coverage. `/dashboard` was a welcome screen whose only job was a
      // button to `/projects`; the list now carries the summary numbers that made the hop
      // worth taking. The ADDRESS still resolves so links and bookmarks do not break — what
      // went is the page, and this asserts nothing renders in its place.
      renderAt(path)
      expect(screen.getByTestId('projects')).toBeTruthy()
      expect(screen.queryByTestId('dashboard')).toBeNull()
    },
  )
})

describe('App — addresses outside a project get no workspace frame', () => {
  it.each([
    ['/projects', 'projects'],
    ['/admin', 'admin'],
  ])('%s renders its page with no shell around it', (path, testId) => {
    // The layout wraps the two addresses INSIDE a project and nothing else. A projects index or a
    // dashboard inside the workspace frame would hold a pane for a project the user has left.
    renderAt(path)
    expect(screen.getByTestId(testId)).toBeTruthy()
    expect(shell()).toBeNull()
  })

  it('/shared-applications is its own address, and the list renders there', () => {
    // Which list is on screen is the ADDRESS rather than a control on the page, so that the
    // navigation has one entry per list and a pasted link lands every reader on their OWN
    // shared applications.
    renderAt('/shared-applications')
    expect(screen.getByTestId('projects')).toBeTruthy()
    expect(shell()).toBeNull()
  })
})

describe('the Help page is gone, and its address with it', () => {
  it('/help no longer resolves — it falls through to the catch-all', () => {
    // The five-link removal's last link: not merely "the component is deleted" but "the address
    // answers nothing". A route left behind rendering an empty element would pass a component
    // test and still ship a dead entry in the product.
    renderAt('/help')
    expect(screen.getByTestId('login')).toBeTruthy()
    expect(window.location.pathname).toBe('/login')
  })
})

describe('the boot / silent-refresh wait keeps WORDS and a busy state', () => {
  /**
   * Every live region in the document that is currently SAYING the given thing.
   *
   * Counted rather than merely looked for, because the two failure modes this arm has are
   * opposite: none at all (the region was never mounted, or the sentence was deleted) and two
   * (an `sr-only` copy added beside the visible sentence, which is that sentence read twice —
   * `Announcer.tsx` records exactly that mistake). Only a COUNT catches both.
   */
  const regionsSaying = (re) =>
    Array.from(document.querySelectorAll('[aria-live], [role="status"], [role="alert"]')).filter(
      (el) => re.test(el.textContent ?? ''),
    )

  it('★ says what it is doing, marks the box busy, and does it in exactly ONE region', () => {
    // `index.css` suppresses `.animate-spin` under `prefers-reduced-motion`, so this full-bleed
    // white screen used to be a stationary circle with no sentence anywhere on it.
    h.authed = false
    h.bootstrap.mockReturnValue(new Promise(() => {})) // never settles: hold the guard undecided

    renderAt('/projects')

    const sentence = screen.getByText('Getting things ready…')
    expect(sentence).toBeTruthy()
    // Said ONCE. Two nodes carrying it is one sentence rendered twice to anything reading the DOM.
    expect(screen.getAllByText('Getting things ready…')).toHaveLength(1)
    const regions = regionsSaying(/Getting things ready/)
    expect(regions).toHaveLength(1)
    expect(regions[0].contains(sentence)).toBe(true)
    // A property, not a speech — it must not be the thing carrying the meaning.
    expect(document.querySelector('[aria-busy="true"]')).toBeTruthy()
  })

  it('★ the region is already in the tree, EMPTY, before the text arrives — and it is the SAME node', () => {
    // THE MOUNT-BEFORE-FILL ORDERING THIS ARM GUARDS: a live region inserted together with its
    // first text is missed entirely by several reader-and-browser combinations, so the region
    // has to outlive the wait rather than arrive with it. Mount it together with its text —
    // render it inside `AuthLoading`, or gate the whole `<div role="status">` on
    // `status === 'loading'` — and the empty-region assertion below goes red. A leaf-component
    // fix is precisely what breaks here.
    renderAt('/projects/p1') // signed in: the guard is decided, no wait running

    const before = screen.getByTestId('auth-wait')
    expect(before.textContent).toBe('')
    expect(regionsSaying(/Getting things ready/)).toHaveLength(0)
    // Paired with a liveness assertion: a crashed render has an empty document, and "the region
    // is empty" would pass against it.
    expect(screen.getByTestId('project-home')).toBeTruthy()

    // THE SILENT REFRESH: a later navigation finds the cached session gone. The guard is NOT
    // remounted — both addresses are children of one pathless layout route — so this is the real
    // product path in which a settled region flips back to waiting.
    h.authed = false
    h.bootstrap.mockReturnValue(new Promise(() => {}))
    goTo('/chat/abc')

    expect(screen.getByTestId('auth-wait')).toBe(before)
    expect(before.textContent).toContain('Getting things ready…')
    expect(regionsSaying(/Getting things ready/)).toHaveLength(1)
  })
})
