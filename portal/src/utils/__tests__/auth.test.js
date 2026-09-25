import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest'

// The auth store keeps module-level session cache + one-time-purge state, so each
// test re-imports a FRESH module (vi.resetModules) for full isolation.
let auth

function clearCookies() {
  document.cookie.split(';').forEach((c) => {
    const name = c.split('=')[0].trim()
    if (name) document.cookie = `${name}=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/`
  })
}

function res({ ok = true, status = 200, json = {} }) {
  return { ok, status, json: async () => json }
}

// `window.location.assign` throws "Not implemented: navigation" in jsdom, so the whole object is
// swapped for a stub exposing the `assign` spy `hardRedirect` drives — and a `pathname`, which the
// login-screen guard reads. It is stubbed for EVERY test rather than only the ones asserting a
// navigation: a dead session now tears the page down, so any test that 401s would otherwise drive a
// real jsdom navigation while asserting something else entirely.
let assign
let originalLocation

/** Put the stubbed browser on a page. */
function standOn(pathname) {
  Object.defineProperty(window, 'location', { configurable: true, value: { assign, pathname } })
}

beforeEach(async () => {
  vi.resetModules()
  localStorage.clear()
  clearCookies()
  global.fetch = vi.fn()
  delete navigator.locks
  originalLocation = window.location
  assign = vi.fn()
  standOn('/projects/p1')
  auth = await import('../auth')
})

afterEach(() => {
  Object.defineProperty(window, 'location', { configurable: true, value: originalLocation })
  vi.restoreAllMocks()
})

describe('bootstrapSession (once-cached /auth/me session context)', () => {
  it('fetches /auth/me once and caches the profile across calls', async () => {
    global.fetch.mockResolvedValue(res({ json: { id: '1', email: 'a@bial.com' } }))
    const u1 = await auth.bootstrapSession()
    const u2 = await auth.bootstrapSession()
    expect(u1).toMatchObject({ email: 'a@bial.com' })
    expect(u2).toBe(u1)
    expect(global.fetch).toHaveBeenCalledTimes(1)
    expect(global.fetch.mock.calls[0][0]).toContain('/api/v1/auth/me')
    expect(auth.isAuthenticated()).toBe(true)
    expect(auth.getStoredUser()).toMatchObject({ id: '1' })
  })

  it('purges any pre-cookie Bearer tokens from localStorage on bootstrap', async () => {
    localStorage.setItem('bial_access_token', 'stale')
    localStorage.setItem('bial_refresh_token', 'stale')
    localStorage.setItem('bial_user', '{}')
    global.fetch.mockResolvedValue(res({ json: { id: '1' } }))
    await auth.bootstrapSession()
    expect(localStorage.getItem('bial_access_token')).toBeNull()
    expect(localStorage.getItem('bial_refresh_token')).toBeNull()
    expect(localStorage.getItem('bial_user')).toBeNull()
  })

  it('retries /auth/me after a silent refresh when the session JWT is expired', async () => {
    document.cookie = 'csrf=tok'
    global.fetch
      .mockResolvedValueOnce(res({ ok: false, status: 401 })) // /me
      .mockResolvedValueOnce(res({ ok: true, status: 200 })) // /refresh
      .mockResolvedValueOnce(res({ json: { id: '1', email: 'a@bial.com' } })) // /me retry
    const user = await auth.bootstrapSession()
    expect(user).toMatchObject({ email: 'a@bial.com' })
    expect(global.fetch).toHaveBeenCalledTimes(3)
    expect(global.fetch.mock.calls[1][0]).toContain('/api/v1/auth/refresh')
  })

  it('resolves null, records EXPIRED and bounces when refresh also fails', async () => {
    document.cookie = 'csrf=tok' // we HAD a session
    global.fetch
      .mockResolvedValueOnce(res({ ok: false, status: 401 })) // /me
      .mockResolvedValueOnce(res({ ok: false, status: 401 })) // /refresh
    const user = await auth.bootstrapSession()
    expect(user).toBeNull()
    expect(auth.isAuthenticated()).toBe(false)
    expect(auth.consumeSignoutReason()).toBe('session_expired')
    // ONE ANSWER TO "THE SESSION IS DEAD", not two. The route guard would also have routed this
    // cold load to the login screen, but a dead session gets the same teardown wherever it is
    // discovered — and the guard is exactly what does NOT run for a tab parked on one route.
    expect(assign).toHaveBeenCalledWith('/login')
  })
})

describe('refreshAccessToken (cookie-based, single-flight)', () => {
  it('coalesces concurrent callers into ONE network refresh', async () => {
    navigator.locks = { request: (_name, cb) => Promise.resolve().then(cb) }
    global.fetch.mockResolvedValue(res({ ok: true, status: 200 }))
    const [a, b] = await Promise.all([auth.refreshAccessToken(), auth.refreshAccessToken()])
    expect(a).toBe(true)
    expect(b).toBe(true)
    expect(global.fetch).toHaveBeenCalledTimes(1)
    expect(global.fetch.mock.calls[0][0]).toContain('/api/v1/auth/refresh')
  })

  it('sends the csrf cookie as X-CSRF-Token with credentials included', async () => {
    document.cookie = 'csrf=csrf-value'
    global.fetch.mockResolvedValue(res({ ok: true, status: 200 }))
    await auth.refreshAccessToken()
    expect(global.fetch.mock.calls[0][1].headers['X-CSRF-Token']).toBe('csrf-value')
    expect(global.fetch.mock.calls[0][1].credentials).toBe('include')
  })

  it('signs out with the EXPIRED banner on a 401 when a session existed', async () => {
    document.cookie = 'csrf=tok'
    global.fetch.mockResolvedValue(res({ ok: false, status: 401 }))
    const result = await auth.refreshAccessToken()
    expect(result).toBeNull()
    expect(auth.consumeSignoutReason()).toBe('session_expired')
  })

  it('does NOT show EXPIRED for a cold visitor (no csrf cookie) on 401', async () => {
    global.fetch.mockResolvedValue(res({ ok: false, status: 401 }))
    await auth.refreshAccessToken()
    expect(auth.consumeSignoutReason()).toBeNull()
  })

  it('keeps the session on a transient 5xx (fails open)', async () => {
    global.fetch.mockResolvedValue(res({ ok: false, status: 503 }))
    const result = await auth.refreshAccessToken()
    expect(result).toBeNull()
    expect(auth.consumeSignoutReason()).toBeNull()
  })
})

/**
 * ★ A DEAD SESSION STOPS THE PAGE INSTEAD OF STRANDING IT.
 *
 * A 401 from refresh means the session cannot be revived, so the page is discarded: a tab parked
 * on one route never re-runs `RequireAuth`'s guard, and its pollers (`preview-state`, `renew`)
 * would otherwise keep running with no heartbeat until the lease lapses.
 *
 * The remedy is the bounce that was already built for a SUSPENDED session, reached by a second
 * caller. Nothing downstream learns to interpret a status code: the pollers stop because the page
 * they live on is discarded.
 */
describe('★ an expired session bounces, once, and only when there was one to lose', () => {
  it('hard-navigates to the login screen and leaves the expired banner behind it', async () => {
    document.cookie = 'csrf=tok'
    global.fetch.mockResolvedValue(res({ ok: false, status: 401 }))

    await auth.refreshAccessToken()

    expect(assign).toHaveBeenCalledWith('/login')
    expect(auth.isAuthenticated()).toBe(false)
    // NO QUERY OF ITS OWN: the login screen reads this banner from the one-time reason, which is
    // the path `clearSession` has always written and `LoginPage` has always consumed.
    expect(auth.consumeSignoutReason()).toBe('session_expired')
  })

  it('leaves a first-time visitor alone — there was no session to expire', async () => {
    // No csrf cookie means no session was ever established. Hard-navigating somebody who simply
    // opened a guarded URL would replace the ordinary signed-out path with a bounce and a banner
    // about an expiry that never happened.
    global.fetch.mockResolvedValue(res({ ok: false, status: 401 }))

    await auth.refreshAccessToken()

    expect(assign).not.toHaveBeenCalled()
    expect(auth.consumeSignoutReason()).toBeNull()
  })

  it('is single-flight: several dead requests produce ONE navigation', async () => {
    // A page whose session has died usually has several requests in flight, and each one reaches
    // here. Three navigations would be three page loads racing each other.
    document.cookie = 'csrf=tok'
    global.fetch.mockResolvedValue(res({ ok: false, status: 401 }))

    await auth.refreshAccessToken()
    await auth.refreshAccessToken()
    await auth.refreshAccessToken()

    expect(assign).toHaveBeenCalledTimes(1)
  })

  it('★ does not bounce on a 5xx or a network blip — that is the fail-open arm', async () => {
    // THE ARM THAT MUST NOT CHANGE. A refresh that could not be COMPLETED says nothing about the
    // session; bouncing on it would sign people out of a working app every time the auth service
    // hiccuped. Only a definitive 401/403 is evidence.
    document.cookie = 'csrf=tok'
    global.fetch.mockResolvedValue(res({ ok: false, status: 503 }))
    await auth.refreshAccessToken()
    expect(assign).not.toHaveBeenCalled()

    global.fetch.mockRejectedValue(new Error('network is down'))
    await auth.refreshAccessToken()
    expect(assign).not.toHaveBeenCalled()
    expect(auth.consumeSignoutReason()).toBeNull()
  })

  it('★ the login screen does not bounce itself', async () => {
    // Its own bootstrap 401s for every signed-out visitor — the ordinary state of that page — and
    // a hard navigation to the page you are on is a reload, which asks again and reloads again.
    standOn('/login')
    document.cookie = 'csrf=tok'
    global.fetch.mockResolvedValue(res({ ok: false, status: 401 }))

    await auth.refreshAccessToken()

    expect(assign).not.toHaveBeenCalled()
    // The session is still dropped — only the navigation is skipped.
    expect(auth.isAuthenticated()).toBe(false)
    expect(auth.consumeSignoutReason()).toBe('session_expired')
  })

  it('a login screen carrying a banner is still the login screen', async () => {
    // `/login?authError=…` is where `handleSuspendedSession` lands, and an equality check on the
    // path would read it as somewhere else and bounce it into itself.
    standOn('/login')
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { assign, pathname: '/login', search: '?authError=account_suspended' },
    })
    document.cookie = 'csrf=tok'
    global.fetch.mockResolvedValue(res({ ok: false, status: 401 }))

    await auth.refreshAccessToken()

    expect(assign).not.toHaveBeenCalled()
  })

  it('bounces anyway when the location cannot say where it is', async () => {
    // The guard reads `window.location.pathname`, and the fallback when that cannot be read is
    // "not the login screen" — bounce. Asserted rather than assumed: a silent `true` there would
    // switch the whole teardown off, and every other test in this file supplies a pathname, so
    // nothing else can reach this branch.
    Object.defineProperty(window, 'location', { configurable: true, value: { assign } })
    document.cookie = 'csrf=tok'
    global.fetch.mockResolvedValue(res({ ok: false, status: 401 }))

    await auth.refreshAccessToken()

    expect(assign).toHaveBeenCalledWith('/login')
  })

  it('does not disarm a later bounce by being called from the login screen first', async () => {
    // The latch means "we are navigating". Setting it on a call that deliberately does NOT
    // navigate would make the login screen's own 401 silently switch the teardown off for the
    // rest of the session.
    standOn('/login')
    document.cookie = 'csrf=tok'
    global.fetch.mockResolvedValue(res({ ok: false, status: 401 }))
    await auth.refreshAccessToken()
    expect(assign).not.toHaveBeenCalled()

    standOn('/projects/p1')
    await auth.refreshAccessToken()

    expect(assign).toHaveBeenCalledWith('/login')
  })
})

describe('logout', () => {
  it('POSTs /auth/logout, records LOGGED_OUT, returns true on success', async () => {
    document.cookie = 'csrf=tok'
    global.fetch.mockResolvedValue(res({ ok: true, status: 200 }))
    const ok = await auth.logout()
    expect(ok).toBe(true)
    expect(global.fetch.mock.calls[0][0]).toContain('/api/v1/auth/logout')
    expect(global.fetch.mock.calls[0][1].headers['X-CSRF-Token']).toBe('tok')
    expect(auth.consumeSignoutReason()).toBe('logged_out')
  })

  it('still records LOGGED_OUT and returns false when the request fails', async () => {
    global.fetch.mockRejectedValue(new Error('network'))
    const ok = await auth.logout()
    expect(ok).toBe(false)
    expect(auth.consumeSignoutReason()).toBe('logged_out')
  })
})

describe('legacy shims + signout reason', () => {
  it('getAccessToken is a null shim (no bearer token in the cookie model)', () => {
    expect(auth.getAccessToken()).toBeNull()
  })

  it('consumeSignoutReason returns the reason once then clears it', () => {
    auth.clearSession('session_expired')
    expect(auth.consumeSignoutReason()).toBe('session_expired')
    expect(auth.consumeSignoutReason()).toBeNull()
  })

  it('never reads or writes the legacy bial_access_token/bial_refresh_token keys', async () => {
    global.fetch.mockResolvedValue(res({ json: { id: '1' } }))
    await auth.bootstrapSession()
    await auth.refreshAccessToken().catch(() => {})
    expect(localStorage.getItem('bial_access_token')).toBeNull()
    expect(localStorage.getItem('bial_refresh_token')).toBeNull()
  })
})

describe('handleSuspendedSession (mid-session suspension bounce)', () => {
  it('clears the session (SUSPENDED reason) and hard-redirects to the suspension login URL', () => {
    // Prime a cached session so we can prove it is dropped.
    auth.clearSession()
    auth.handleSuspendedSession()
    expect(assign).toHaveBeenCalledWith('/login?authError=account_suspended')
    expect(auth.isAuthenticated()).toBe(false)
    expect(auth.consumeSignoutReason()).toBe('account_suspended')
  })

  it('is single-flight: concurrent 403 firings produce exactly ONE navigation', () => {
    // Several in-flight requests all 403-suspend and each calls the teardown; the
    // module latch collapses them into a single hard navigation.
    auth.handleSuspendedSession()
    auth.handleSuspendedSession()
    auth.handleSuspendedSession()
    expect(assign).toHaveBeenCalledTimes(1)
  })

  it('does not bounce the login screen into itself either', () => {
    // `/login?authError=account_suspended` is where this very function lands, and the page it
    // lands on makes its own authed calls. Both dead-session paths share one guard, so proving it
    // on one of them is not proof for the other.
    standOn('/login')
    auth.handleSuspendedSession()
    expect(assign).not.toHaveBeenCalled()
    expect(auth.consumeSignoutReason()).toBe('account_suspended')
  })
})
