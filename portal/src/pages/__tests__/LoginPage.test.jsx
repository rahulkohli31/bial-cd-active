import { afterEach, beforeEach, describe, it, expect } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import LoginPage from '../LoginPage'
import { LOGIN_URL } from '../../utils/auth'

function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LoginPage />
    </MemoryRouter>,
  )
}

// `ProfileCluster`'s handleLogout hands this exact shape to `navigate('/login', { state })` on
// a failed revoke; a real in-SPA `initialEntries` array entry (not a path string) is how
// MemoryRouter seeds that router state without signing out for real. `pathname`/`search` are
// split out so a query string in `path` still reaches `useSearchParams()`.
function renderAtWithState(path, state) {
  const [pathname, search = ''] = path.split('?')
  return render(
    <MemoryRouter initialEntries={[{ pathname, search: search ? `?${search}` : '', state }]}>
      <LoginPage />
    </MemoryRouter>,
  )
}

// Records the router's CURRENT query string on every render, so a test can prove the
// page rewrote its own URL (LoginPage consumes ?authError via setSearchParams, which
// re-renders the tree rather than touching window.location).
function renderAtTrackingSearch(path) {
  const seen = { search: null }
  function LocationSpy() {
    seen.search = useLocation().search
    return null
  }
  render(
    <MemoryRouter initialEntries={[path]}>
      <LoginPage />
      <LocationSpy />
    </MemoryRouter>,
  )
  return seen
}

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  cleanup()
})

describe('LoginPage — Entra "Sign in with Microsoft" only', () => {
  it('renders the Microsoft sign-in action and NO password/email fields', () => {
    renderAt('/login')
    expect(screen.getByTestId('login-microsoft')).toBeTruthy()
    expect(screen.getByTestId('login-microsoft').textContent).toContain('Sign in with Microsoft')
    expect(screen.queryByTestId('login-password')).toBeNull()
    expect(screen.queryByTestId('login-email')).toBeNull()
  })

  it('full-page-navigates to the FastAPI /auth/login on click', () => {
    Object.defineProperty(window, 'location', { configurable: true, value: { href: '' } })
    renderAt('/login')
    fireEvent.click(screen.getByTestId('login-microsoft'))
    expect(window.location.href).toBe(LOGIN_URL)
    expect(window.location.href).toContain('/api/v1/auth/login')
  })

  it('shows distinct wrong-tenant copy for ?authError=wrong_tenant', () => {
    renderAt('/login?authError=wrong_tenant')
    expect(screen.getByTestId('login-notice').textContent).toContain('BIAL organization')
  })

  it('shows a generic failure banner for other authError reasons', () => {
    renderAt('/login?authError=invalid_callback')
    const text = screen.getByTestId('login-notice').textContent
    expect(text).toContain('Sign-in failed')
    expect(text).not.toContain('BIAL organization')
  })

  // Regression test for the prototype-pollution fix (c2822c7): before the Object.hasOwn guard,
  // AUTH_ERROR_BANNERS[authError] resolved a key like `__proto__` to a real Object.prototype
  // value, the `|| GENERIC_AUTH_ERROR` fallback never fired (truthy), and React threw —
  // white-screening the unauthenticated /login page for anyone who clicks a crafted link.
  //
  // Mutation receipt: reverting the guard to a bare index fails all four keys, but NOT the
  // same way — `__proto__` reproduces the crash, `constructor` renders an empty notice,
  // `toString`/`hasOwnProperty` fail differently again — so trimming this list to just
  // `__proto__` would silently drop the other failure shapes.
  it.each(['__proto__', 'constructor', 'toString', 'hasOwnProperty'])(
    'does not crash and shows the generic banner for ?authError=%s (prototype-pollution guard)',
    (key) => {
      renderAt(`/login?authError=${key}`)
      const text = screen.getByTestId('login-notice').textContent
      expect(text).toContain('Sign-in failed')
    },
  )

  // Companion assertion for the same commit's SIGNOUT_BANNERS[reason] guard — lower severity
  // since `reason` comes from localStorage, not the URL. Unlike authError there is no generic
  // fallback: an unrecognized reason never calls setNotice, so the correct behavior is no
  // banner at all, not a crash and not a substitute message.
  it('does not crash and shows no banner for a poisoned signout-reason key (prototype-pollution guard)', () => {
    localStorage.setItem('bial_signout_reason', '__proto__')
    renderAt('/login')
    // Page survived FIRST: an absent notice is ALSO what a crashed page renders, so on its own
    // that assertion would false-green with the guard fully reverted — add an error boundary
    // around LoginPage and this goes green with the guard fully reverted, because vitest's
    // unhandled-rejection surfacing (not this test) is what makes the bare
    // `queryByTestId('login-notice')).toBeNull()` discriminate today. Asserting the sign-in
    // button is still there is what proves the page actually rendered.
    expect(screen.getByTestId('login-microsoft')).toBeTruthy()
    expect(screen.queryByTestId('login-notice')).toBeNull()
  })

  it('shows distinct, non-alarming suspension copy for ?authError=account_suspended', () => {
    renderAt('/login?authError=account_suspended')
    const text = screen.getByTestId('login-notice').textContent
    expect(text).toContain('administrator')
    // Not the user's fault: never the generic "sign-in failed", never the tenant copy.
    expect(text).not.toContain('Sign-in failed')
    expect(text).not.toContain('BIAL organization')
  })

  it('keeps the generic banner for ?authError=auth_failed', () => {
    renderAt('/login?authError=auth_failed')
    const text = screen.getByTestId('login-notice').textContent
    expect(text).toContain('Sign-in failed')
    expect(text).not.toContain('administrator')
  })

  it('shows the signout-reason banner when there is no authError', () => {
    localStorage.setItem('bial_signout_reason', 'logged_out')
    renderAt('/login')
    expect(screen.getByTestId('login-notice').textContent).toContain('signed out')
  })

  it('shows no banner on a clean visit', () => {
    renderAt('/login')
    expect(screen.queryByTestId('login-notice')).toBeNull()
  })

  // Rendering half of the fix — `AppShell.test.tsx` proves the state reaches this route at all.
  it('shows the sign-out warning carried as router state', () => {
    renderAtWithState('/login', { signoutWarning: 'Sign-out may be incomplete on this device.' })
    expect(screen.getByTestId('login-notice').textContent).toBe(
      'Sign-out may be incomplete on this device.',
    )
  })

  it('does not crash and shows no banner when the router state has no signoutWarning key', () => {
    renderAtWithState('/login', { somethingElse: true })
    expect(screen.getByTestId('login-microsoft')).toBeTruthy()
    expect(screen.queryByTestId('login-notice')).toBeNull()
  })

  it('an authError still wins over a carried sign-out warning', () => {
    renderAtWithState('/login?authError=wrong_tenant', {
      signoutWarning: 'Sign-out may be incomplete on this device.',
    })
    expect(screen.getByTestId('login-notice').textContent).toContain('BIAL organization')
  })
})

// The 2026-09-10 incident: a failed sign-in left `?authError=auth_failed` pinned to the
// URL, so reload / hard-reload / close-and-reopen all re-rendered the same failure banner
// forever. The banner must be consumed like consumeSignoutReason() consumes its key.
describe('LoginPage — the failure banner is consumed, not pinned to the URL', () => {
  it('strips ?authError from the URL after showing the banner', () => {
    const seen = renderAtTrackingSearch('/login?authError=auth_failed')
    // Shown once...
    expect(screen.getByTestId('login-notice').textContent).toContain('Sign-in failed')
    // ...and gone from the URL, so a reload lands on a clean login screen.
    expect(seen.search).not.toContain('authError')
  })

  it('keeps the banner on screen after cleaning the URL', () => {
    // The guard against "fixing" this by re-running the effect and wiping the notice.
    renderAtTrackingSearch('/login?authError=wrong_tenant')
    expect(screen.getByTestId('login-notice').textContent).toContain('BIAL organization')
  })

  it('does not let the signout reason overwrite the sign-in failure', () => {
    localStorage.setItem('bial_signout_reason', 'logged_out')
    renderAtTrackingSearch('/login?authError=auth_failed')
    const text = screen.getByTestId('login-notice').textContent
    expect(text).toContain('Sign-in failed')
    expect(text).not.toContain('signed out')
  })

  it('preserves unrelated query params while consuming authError', () => {
    const seen = renderAtTrackingSearch('/login?authError=auth_failed&next=%2Fprojects')
    expect(seen.search).toContain('next=')
    expect(seen.search).not.toContain('authError')
  })
})

describe('LoginPage — the correlation reference', () => {
  it('shows the ref the callback supplied, so a screenshot maps to a log line', () => {
    const seen = renderAtTrackingSearch('/login?authError=auth_failed&ref=a1b2c3d4')
    expect(screen.getByTestId('login-notice-ref').textContent).toContain('a1b2c3d4')
    expect(seen.search).not.toContain('ref=')
  })

  it('ignores a ref that is not the shape the backend mints', () => {
    // ?ref is attacker-chosen text on an unauthenticated route. Rendering it verbatim
    // would let a crafted link put arbitrary copy inside the official-looking banner.
    renderAtTrackingSearch('/login?authError=auth_failed&ref=call+1800-SCAM+now')
    expect(screen.queryByTestId('login-notice-ref')).toBeNull()
  })

  it('shows no reference line when the callback supplied none', () => {
    renderAtTrackingSearch('/login?authError=auth_failed')
    expect(screen.queryByTestId('login-notice-ref')).toBeNull()
  })
})

// Production, 2026-09-11 (refs b005f1e8, d172da84): Entra reused a browser session whose MFA had
// expired, and every "try again" re-minted a code from that same session. The callback now retries
// once with a forced sign-in; when that is not enough it lands here with `reauth_required`, and the
// button must ask Entra for a fresh sign-in rather than the same silent one.
describe('LoginPage — a sign-in Conditional Access refused (reauth_required)', () => {
  it('says the organization needs the sign-in confirmed again, and keeps the reference', () => {
    renderAt('/login?authError=reauth_required&ref=d172da84')
    const text = screen.getByTestId('login-notice').textContent
    expect(text).toContain('confirm your sign-in again')
    expect(text).not.toContain('Sign-in failed')
    expect(screen.getByText(/Reference: d172da84/)).toBeTruthy()
  })

  it('makes the next Sign in with Microsoft a fresh sign-in (prompt=login)', () => {
    Object.defineProperty(window, 'location', { configurable: true, value: { href: '' } })
    renderAt('/login?authError=reauth_required')
    fireEvent.click(screen.getByTestId('login-microsoft'))
    expect(window.location.href).toBe(`${LOGIN_URL}?prompt=login`)
  })

  it('keeps any other failure on the ordinary sign-in', () => {
    Object.defineProperty(window, 'location', { configurable: true, value: { href: '' } })
    renderAt('/login?authError=auth_failed')
    fireEvent.click(screen.getByTestId('login-microsoft'))
    expect(window.location.href).toBe(LOGIN_URL)
  })
})

