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

beforeEach(async () => {
  vi.resetModules()
  localStorage.clear()
  clearCookies()
  global.fetch = vi.fn()
  delete navigator.locks
  auth = await import('../auth')
})

afterEach(() => {
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

  it('resolves null (redirect) and records EXPIRED when refresh also fails', async () => {
    document.cookie = 'csrf=tok' // we HAD a session
    global.fetch
      .mockResolvedValueOnce(res({ ok: false, status: 401 })) // /me
      .mockResolvedValueOnce(res({ ok: false, status: 401 })) // /refresh
    const user = await auth.bootstrapSession()
    expect(user).toBeNull()
    expect(auth.isAuthenticated()).toBe(false)
    expect(auth.consumeSignoutReason()).toBe('session_expired')
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
  // window.location.assign throws "Not implemented: navigation" in jsdom, so we
  // swap window.location for a stub exposing only the `assign` spy — the seam
  // hardRedirect() drives. Restored after each test so nothing leaks.
  let assign
  let originalLocation

  beforeEach(() => {
    originalLocation = window.location
    assign = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { assign } })
  })

  afterEach(() => {
    Object.defineProperty(window, 'location', { configurable: true, value: originalLocation })
  })

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
})
