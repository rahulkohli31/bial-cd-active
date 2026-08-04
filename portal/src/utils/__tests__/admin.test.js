import { describe, it, expect, vi } from 'vitest'
import { fetchUsers, updateUserLimits, deactivateUser, reactivateUser } from '../admin'
import { ApiError } from '../apiError'

// Inject authFetch's deps so no real token/localStorage/network is touched.
const deps = (fetchImpl) => ({ fetchImpl, getToken: () => 'tok', refresh: vi.fn() })

// authFetch peeks a 403's body through res.clone() to tell the "Account suspended"
// gate apart from the super-admin gate, so a faked Response must be cloneable — a
// real one always is.
const res = (init) => ({ ...init, clone: () => res(init) })

describe('fetchUsers', () => {
  it('sends cursor, limit and q as URL-encoded query params', async () => {
    const payload = { defaults: {}, users: [], nextCursor: null, hasMore: false }
    const fetchImpl = vi.fn(async () => res({ ok: true, status: 200, json: async () => payload }))
    await fetchUsers({ cursor: 'c', limit: 50, q: 'ana' }, deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/admin/users?cursor=c&limit=50&q=ana')
  })

  it('sends NO query string when called with no params', async () => {
    const payload = { defaults: {}, users: [], nextCursor: null, hasMore: false }
    const fetchImpl = vi.fn(async () => res({ ok: true, status: 200, json: async () => payload }))
    await fetchUsers({}, deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/admin/users')
  })

  it('URL-encodes a query with spaces/special chars', async () => {
    const payload = { defaults: {}, users: [], nextCursor: null, hasMore: false }
    const fetchImpl = vi.fn(async () => res({ ok: true, status: 200, json: async () => payload }))
    await fetchUsers({ q: 'a b&c' }, deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/admin/users?q=a+b%26c')
  })

  it('returns { defaults, users, nextCursor, hasMore } from the keyset envelope', async () => {
    const payload = {
      defaults: { dailyTokenLimit: 100000 },
      users: [{ userId: 'u1', email: 'a@x.com' }],
      nextCursor: 'c1',
      hasMore: true,
    }
    const fetchImpl = vi.fn(async () => res({ ok: true, status: 200, json: async () => payload }))
    const out = await fetchUsers({}, deps(fetchImpl))
    expect(out).toEqual({
      defaults: { dailyTokenLimit: 100000 },
      users: [{ userId: 'u1', email: 'a@x.com' }],
      nextCursor: 'c1',
      hasMore: true,
    })
  })

  it('surfaces the {detail} super-admin gate message, not the generic fallback', async () => {
    // The old body.error?.message read was blind to {detail}, so the gate message
    // was invisible and collapsed to "Failed to load users (403)."
    const fetchImpl = vi.fn(async () =>
      res({ ok: false, status: 403, json: async () => ({ detail: 'Super-admin privileges required.' }) }),
    )
    await expect(fetchUsers({}, deps(fetchImpl))).rejects.toThrow('Super-admin privileges required.')
    await expect(fetchUsers({}, deps(fetchImpl))).rejects.not.toThrow('Failed to load users')
  })
})

describe('updateUserLimits', () => {
  it('PATCHes the user uuid path with the patch body', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ username: 'a' }) }))
    const uid = '0190c3a1-2b4c-7def-8a01-1234567890ab'
    await updateUserLimits(uid, { dailyTokenLimit: 5000, contextSoftLimit: null }, deps(fetchImpl))
    const [url, opts] = fetchImpl.mock.calls[0]
    expect(url).toBe(`/api/admin/users/${uid}/limits`)
    expect(opts.method).toBe('PATCH')
    expect(JSON.parse(opts.body)).toEqual({ dailyTokenLimit: 5000, contextSoftLimit: null })
  })

  it('round-trips a null to clear an override', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ userId: 'u1' }) }))
    await updateUserLimits('u1', { dailyTokenLimit: null }, deps(fetchImpl))
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toEqual({ dailyTokenLimit: null })
  })

  it('throws the server message on a 400', async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: false,
      status: 400,
      json: async () => ({ error: { message: 'contextSoftLimit must be less than contextHardLimit.' } }),
    }))
    await expect(
      updateUserLimits('0190c3a1-2b4c-7def-8a01-1234567890ab', { contextSoftLimit: 9, contextHardLimit: 9 }, deps(fetchImpl)),
    ).rejects.toThrow(/less than/)
  })
})

describe('deactivateUser / reactivateUser', () => {
  it('deactivateUser POSTs and returns { userId, suspendedAt }', async () => {
    const body = { userId: 'u1', suspendedAt: '2026-07-10T09:00:00Z' }
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 200, json: async () => body }))
    const out = await deactivateUser('u1', deps(fetchImpl))
    const [url, opts] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/admin/users/u1/deactivate')
    expect(opts.method).toBe('POST')
    expect(out).toEqual(body)
  })

  it('reactivateUser POSTs and returns { userId, suspendedAt: null }', async () => {
    const body = { userId: 'u1', suspendedAt: null }
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 200, json: async () => body }))
    const out = await reactivateUser('u1', deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/admin/users/u1/reactivate')
    expect(out).toEqual(body)
  })

  it('deactivateUser 403 (super-admin target) throws ApiError with status 403', async () => {
    const fetchImpl = vi.fn(async () =>
      res({ ok: false, status: 403, json: async () => ({ error: { message: 'A super-admin cannot be suspended.' } }) }),
    )
    const err = await deactivateUser('u1', deps(fetchImpl)).catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(403)
    expect(err.message).toBe('A super-admin cannot be suspended.')
  })

  it('deactivateUser 409 (already suspended) throws ApiError with status 409', async () => {
    const fetchImpl = vi.fn(async () =>
      ({ ok: false, status: 409, json: async () => ({ error: { message: 'User is already suspended.' } }) }),
    )
    const err = await deactivateUser('u1', deps(fetchImpl)).catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(409)
  })

  it('reactivateUser 409 (not suspended) throws ApiError with status 409', async () => {
    const fetchImpl = vi.fn(async () =>
      ({ ok: false, status: 409, json: async () => ({ error: { message: 'User is not suspended.' } }) }),
    )
    const err = await reactivateUser('u1', deps(fetchImpl)).catch((e) => e)
    expect(err.status).toBe(409)
  })
})
