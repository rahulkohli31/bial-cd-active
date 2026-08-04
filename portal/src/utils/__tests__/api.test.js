import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { authFetch } from '../api'
import { handleSuspendedSession } from '../auth'

// The suspension teardown lives in auth.js so authFetch and fetchClaudeStream
// share ONE implementation. Here we spy it to assert authFetch's wiring without
// triggering a real jsdom navigation; its single-flight guarantee (concurrent
// 403s → exactly one navigation) is unit-tested against the real function in
// auth.test.js.
vi.mock('../auth', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, handleSuspendedSession: vi.fn() }
})

beforeEach(() => {
  handleSuspendedSession.mockClear()
})

// A 403 Response whose CLONE yields the given detail body. `json` returns the
// ORIGINAL body so a test can prove the caller can still read it after the peek.
function res403(detail, { nonJson = false } = {}) {
  const body = { detail }
  const clonedJson = nonJson
    ? async () => {
        throw new SyntaxError('Unexpected token < in JSON')
      }
    : async () => body
  return {
    status: 403,
    clone: vi.fn(() => ({ json: clonedJson })),
    json: async () => body,
  }
}

describe('authFetch', () => {
  // Evolved from the characterization test: once the interceptor exists, a 403
  // CSRF failure IS peeked (one clone) to rule out suspension, then handed back
  // to the caller untouched — no redirect, no throw. (AdminPage / CSRF retry
  // paths keep owning their own 403s.)
  it('403 CSRF failure is peeked then returned to the caller — no redirect, no throw', async () => {
    const res = res403('CSRF check failed')
    const out = await authFetch('/api/x', {}, { getToken: () => 't', refresh: vi.fn(), fetchImpl: async () => res })
    expect(out).toBe(res)
    expect(res.clone).toHaveBeenCalledTimes(1)
    expect(handleSuspendedSession).not.toHaveBeenCalled()
  })

  it('403 {"detail":"Account suspended"} tears down the session and REJECTS (no usable response)', async () => {
    const res = res403('Account suspended')
    await expect(
      authFetch('/api/x', {}, { getToken: () => 't', refresh: vi.fn(), fetchImpl: async () => res }),
    ).rejects.toMatchObject({ name: 'ApiError', status: 403 })
    expect(handleSuspendedSession).toHaveBeenCalledTimes(1)
    expect(res.clone).toHaveBeenCalledTimes(1)
  })

  it('403 "Super-admin privileges required." is returned so AdminPage renders its own error', async () => {
    const res = res403('Super-admin privileges required.')
    const out = await authFetch('/api/admin/x', {}, { getToken: () => 't', refresh: vi.fn(), fetchImpl: async () => res })
    expect(out).toBe(res)
    expect(handleSuspendedSession).not.toHaveBeenCalled()
  })

  it('a 403 whose body is not JSON is NOT treated as suspension — no redirect, returned', async () => {
    const res = res403('', { nonJson: true })
    const out = await authFetch('/api/x', {}, { getToken: () => 't', refresh: vi.fn(), fetchImpl: async () => res })
    expect(out).toBe(res)
    expect(handleSuspendedSession).not.toHaveBeenCalled()
  })

  it('leaves the original body readable by the caller after peeking it (clone semantics)', async () => {
    const res = res403('CSRF check failed')
    const out = await authFetch('/api/x', {}, { getToken: () => 't', refresh: vi.fn(), fetchImpl: async () => res })
    // authFetch consumed res.clone().json(); the ORIGINAL res.json() is untouched.
    await expect(out.json()).resolves.toEqual({ detail: 'CSRF check failed' })
  })

  it('passes a 200 straight through — no clone, no parse — for JSON and binary alike', async () => {
    // A 200 app-status payload holding an appKey must not be routed through a parse.
    const jsonRes = { status: 200, clone: vi.fn(), json: vi.fn() }
    const r1 = await authFetch('/api/projects', {}, { getToken: () => 't', refresh: vi.fn(), fetchImpl: async () => jsonRes })
    expect(r1).toBe(jsonRes)
    expect(jsonRes.clone).not.toHaveBeenCalled()
    expect(jsonRes.json).not.toHaveBeenCalled()

    // Raw attachment bytes: a 200 the interceptor must never clone.
    const binRes = { status: 200, clone: vi.fn(), arrayBuffer: vi.fn() }
    const r2 = await authFetch('/api/attachments/1', {}, { getToken: () => 't', refresh: vi.fn(), fetchImpl: async () => binRes })
    expect(r2).toBe(binRes)
    expect(binRes.clone).not.toHaveBeenCalled()
  })

  it('attaches the Bearer access token', async () => {
    const fetchImpl = vi.fn(async () => ({ status: 200 }))
    await authFetch('/api/x', {}, { getToken: () => 'tok', refresh: vi.fn(), fetchImpl })
    expect(fetchImpl).toHaveBeenCalledOnce()
    expect(fetchImpl.mock.calls[0][1].headers.Authorization).toBe('Bearer tok')
  })

  it('refreshes once on a 401 and retries with NO bearer header (cookie-session model)', async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce({ status: 401 }).mockResolvedValueOnce({ status: 200 })
    // refreshAccessToken() resolves a SUCCESS BOOLEAN in the cookie model, not a token.
    const refresh = vi.fn(async () => true)
    const res = await authFetch('/api/x', {}, { getToken: () => 'stale', refresh, fetchImpl })
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    // The retry must NOT template the boolean into `Authorization: Bearer true` —
    // the refreshed session cookie is sent automatically.
    expect(fetchImpl.mock.calls[1][1].headers.Authorization).toBeUndefined()
    expect(res.status).toBe(200)
  })

  it('does not retry when the refresh fails (returns the 401)', async () => {
    const fetchImpl = vi.fn(async () => ({ status: 401 }))
    const refresh = vi.fn(async () => null)
    const res = await authFetch('/api/x', {}, { getToken: () => 'stale', refresh, fetchImpl })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(res.status).toBe(401)
  })

  it('preserves the caller method + headers', async () => {
    const fetchImpl = vi.fn(async () => ({ status: 200 }))
    await authFetch(
      '/api/x',
      { method: 'PATCH', headers: { 'Content-Type': 'application/json' } },
      { getToken: () => 't', refresh: vi.fn(), fetchImpl },
    )
    const opts = fetchImpl.mock.calls[0][1]
    expect(opts.method).toBe('PATCH')
    expect(opts.headers['Content-Type']).toBe('application/json')
    expect(opts.headers.Authorization).toBe('Bearer t')
  })
})

describe('authFetch — the suspension gate covers the post-refresh retry too', () => {
  // A cloneable non-403 stub; the 403s reuse the file's res403 helper.
  const res401 = () => ({ ok: false, status: 401, json: async () => ({ detail: 'Not authenticated' }), clone() { return this } })

  it('intercepts a suspension that only surfaces on the RETRIED response', async () => {
    // An admin can suspend the user between the first 401 and the refreshed retry. Gating only
    // the first response would hand the caller a bare 403 and strand a dead session.
    const fetchImpl = vi.fn().mockResolvedValueOnce(res401()).mockResolvedValueOnce(res403('Account suspended'))
    const refresh = vi.fn(async () => true)

    await expect(authFetch('/api/projects', {}, { fetchImpl, getToken: () => null, refresh })).rejects.toThrow('Account suspended')
    expect(handleSuspendedSession).toHaveBeenCalledTimes(1)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })

  it('still hands a NON-suspension 403 on the retried response back to the caller', async () => {
    // A genuine CSRF rejection (a forged or truly invalid token) still belongs to the caller.
    // NOTE this is no longer the *stale-token* shape — see the N11 guard below, which is why the
    // request here is a safe GET rather than the mutation this test used to assert.
    const fetchImpl = vi.fn().mockResolvedValueOnce(res401()).mockResolvedValueOnce(res403('CSRF check failed'))
    const refresh = vi.fn(async () => true)

    const out = await authFetch('/api/projects', {}, { fetchImpl, getToken: () => null, refresh })
    expect(out.status).toBe(403)
    expect(handleSuspendedSession).not.toHaveBeenCalled()
  })
})

// N11 REGRESSION GUARD (U1 / KTD-9). `/auth/refresh` ROTATES the `csrf` cookie. authFetch used to
// read the token ONCE before the first attempt and close over it, so the post-refresh retry re-sent
// a token the refresh had just invalidated — trading the 401 for a 403 on every mutating call. It
// looked healthy only because the observed recovery was a GET, which carries no token at all. This
// is the half of U1 that must land WITH the turn-transport routing: routing the six turn calls
// through a wrapper with this bug would have converted a dead transport into a 403ing one.
describe('authFetch — the retry carries the POST-refresh CSRF token (N11)', () => {
  const res401 = () => ({ ok: false, status: 401, json: async () => ({ detail: 'Not authenticated' }), clone() { return this } })
  const setCsrf = (value) => {
    document.cookie = `csrf=${value}`
  }

  afterEach(() => {
    document.cookie = 'csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
  })

  it('re-reads the rotated token — the retry must NOT send the pre-refresh value', async () => {
    setCsrf('before-refresh')
    const fetchImpl = vi.fn().mockResolvedValueOnce(res401()).mockResolvedValueOnce({ status: 200 })
    const refresh = vi.fn(async () => {
      setCsrf('after-refresh') // what /auth/refresh actually does
      return true
    })

    await authFetch('/api/conversations/c1/mode', { method: 'POST' }, { fetchImpl, getToken: () => null, refresh })

    expect(fetchImpl.mock.calls[0][1].headers['X-CSRF-Token']).toBe('before-refresh')
    expect(fetchImpl.mock.calls[1][1].headers['X-CSRF-Token']).toBe('after-refresh')
  })

  it('a token that did NOT rotate is re-sent unchanged (the re-read is not a mutation of its own)', async () => {
    setCsrf('unrotated')
    const fetchImpl = vi.fn().mockResolvedValueOnce(res401()).mockResolvedValueOnce({ status: 200 })
    const refresh = vi.fn(async () => true)

    await authFetch('/api/conversations', { method: 'POST' }, { fetchImpl, getToken: () => null, refresh })

    expect(fetchImpl.mock.calls[1][1].headers['X-CSRF-Token']).toBe('unrotated')
  })

  it('a GET still sends no CSRF header on either attempt', async () => {
    setCsrf('irrelevant-to-a-get')
    const fetchImpl = vi.fn().mockResolvedValueOnce(res401()).mockResolvedValueOnce({ status: 200 })
    const refresh = vi.fn(async () => true)

    await authFetch('/api/projects', {}, { fetchImpl, getToken: () => null, refresh })

    expect(fetchImpl.mock.calls[0][1].headers['X-CSRF-Token']).toBeUndefined()
    expect(fetchImpl.mock.calls[1][1].headers['X-CSRF-Token']).toBeUndefined()
  })
})

// F1 REGRESSION GUARD. Business routes (conversation create, mode-switch, turn start/stop,
// Build-it) now enforce the signed double-submit token via `RequireCsrf`. authFetch must ride
// `X-CSRF-Token` on every MUTATING method and stay silent on safe ones — a blind create/switch
// (the P0 that gated the whole unified-chat flow) is exactly a missing header here. getCsrfToken()
// reads the JS-readable `csrf` cookie, so we drive it through jsdom's document.cookie.
describe('authFetch — CSRF double-submit on mutating methods (F1 regression guard)', () => {
  const setCsrf = (value) => {
    document.cookie = `csrf=${value}`
  }
  const clearCsrf = () => {
    document.cookie = 'csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
  }

  afterEach(clearCsrf)

  it.each(['POST', 'PUT', 'PATCH', 'DELETE'])('attaches X-CSRF-Token (from the csrf cookie) on %s', async (method) => {
    setCsrf('signed-csrf-123')
    const fetchImpl = vi.fn(async () => ({ status: 200 }))
    await authFetch('/api/conversations', { method }, { getToken: () => 't', refresh: vi.fn(), fetchImpl })
    expect(fetchImpl.mock.calls[0][1].headers['X-CSRF-Token']).toBe('signed-csrf-123')
  })

  it('omits X-CSRF-Token on a safe GET (the default method)', async () => {
    setCsrf('signed-csrf-123')
    const fetchImpl = vi.fn(async () => ({ status: 200 }))
    await authFetch('/api/conversations', {}, { getToken: () => 't', refresh: vi.fn(), fetchImpl })
    expect(fetchImpl.mock.calls[0][1].headers['X-CSRF-Token']).toBeUndefined()
  })

  it('omits X-CSRF-Token on a safe HEAD', async () => {
    setCsrf('signed-csrf-123')
    const fetchImpl = vi.fn(async () => ({ status: 200 }))
    await authFetch('/api/conversations', { method: 'HEAD' }, { getToken: () => 't', refresh: vi.fn(), fetchImpl })
    expect(fetchImpl.mock.calls[0][1].headers['X-CSRF-Token']).toBeUndefined()
  })

  it('omits the header when no csrf cookie is present, and still issues the request (an un-verifying route ignores it)', async () => {
    clearCsrf()
    const fetchImpl = vi.fn(async () => ({ status: 200 }))
    const out = await authFetch('/api/conversations', { method: 'POST' }, { getToken: () => 't', refresh: vi.fn(), fetchImpl })
    expect(fetchImpl).toHaveBeenCalledOnce()
    expect(fetchImpl.mock.calls[0][1].headers['X-CSRF-Token']).toBeUndefined()
    expect(out.status).toBe(200)
  })
})
