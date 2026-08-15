import { afterEach, beforeEach, describe, it, expect } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import LoginPage from '../LoginPage'
import { LOGIN_URL } from '../../utils/auth'

function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LoginPage />
    </MemoryRouter>,
  )
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
    // The POC username/password inputs are gone.
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

  // #105 — regression test for the prototype-pollution fix (c2822c7, PR #93). Before the
  // Object.hasOwn guard, AUTH_ERROR_BANNERS[authError] resolved a key like `__proto__` to a
  // real Object.prototype value; the `|| GENERIC_AUTH_ERROR` fallback never fired because
  // that value is truthy, and React threw "Objects are not valid as a React child" rendering
  // it — with no error boundary anywhere in portal/src, that white-screens the unauthenticated
  // /login page for anyone who clicks a crafted link.
  //
  // Reverting the guard to a bare index fails all four, but NOT the same way — worth keeping
  // exactly because each key exercises a different failure mode, not a redundant repeat of
  // one:
  //   __proto__      -> Object.prototype (an object) -> React throws "Objects are not
  //                      valid as a React child", the crash the issue describes
  //   constructor     -> Object (a function)          -> no error at all; notice renders
  //                      as an empty string, so the banner text assertion fails
  //   toString        -> a function                   -> renders as the string
  //                      "[object Undefined]" (a function coerced to a child), not
  //                      "Sign-in failed"
  //   hasOwnProperty  -> a function                   -> TypeError inside React's own
  //                      state reducer, converting undefined/null to an object
  // React does not throw on a function child, so only __proto__ reproduces the issue's exact
  // crash; the other three are real regressions of a different shape, and trimming this list
  // to just __proto__ would silently drop the only TypeError case and the only no-error case.
  it.each(['__proto__', 'constructor', 'toString', 'hasOwnProperty'])(
    'does not crash and shows the generic banner for ?authError=%s (prototype-pollution guard)',
    (key) => {
      renderAt(`/login?authError=${key}`)
      const text = screen.getByTestId('login-notice').textContent
      expect(text).toContain('Sign-in failed')
    },
  )

  // The companion assertion for the same commit's SIGNOUT_BANNERS[reason] guard — lower
  // severity since `reason` comes from localStorage (an existing same-origin write
  // primitive), not directly off the URL. Unlike authError there is no generic fallback
  // here: an unrecognized reason simply never calls setNotice, so the correct behavior is
  // no banner at all, not a crash and not a substitute message.
  it('does not crash and shows no banner for a poisoned signout-reason key (prototype-pollution guard)', () => {
    localStorage.setItem('bial_signout_reason', '__proto__')
    renderAt('/login')
    // Page survived FIRST: an absent notice is also what a crashed page renders, so on its
    // own that assertion is satisfied by the exact failure this test exists to catch — add
    // an error boundary around LoginPage and this goes green with the guard fully reverted,
    // because vitest's unhandled-rejection surfacing (not this test) is what makes the bare
    // `queryByTestId('login-notice')).toBeNull()` discriminate today. Asserting the sign-in
    // button is still there proves the page rendered at all.
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
})
