import { describe, it, expect, vi } from 'vitest'
import { fetchUsers, updateUserLimits } from '../admin.js'

// Inject authFetch's deps so no real token/localStorage/network is touched.
const deps = (fetchImpl) => ({ fetchImpl, getToken: () => 'tok', refresh: vi.fn() })

describe('fetchUsers', () => {
  it('returns { users, defaults } from the GET payload', async () => {
    const payload = { users: [{ username: 'a' }], defaults: { dailyTokenLimit: 1000 } }
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 200, json: async () => payload }))
    const result = await fetchUsers(deps(fetchImpl))
    expect(result).toEqual({ users: payload.users, defaults: payload.defaults })
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/admin/users')
  })

  it('throws the server message on a non-ok response', async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: false,
      status: 403,
      json: async () => ({ error: { message: 'Admin access required.' } }),
    }))
    await expect(fetchUsers(deps(fetchImpl))).rejects.toThrow('Admin access required.')
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

  it('throws the server message on a 400', async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: false,
      status: 400,
      json: async () => ({ error: { message: 'contextSoftLimit must be less than contextHardLimit.' } }),
    }))
    await expect(
      updateUserLimits('0190c3a1-2b4c-7def-8a01-1234567890ab', { contextSoftLimit: 9, contextHardLimit: 9 }, deps(fetchImpl)),
    ).rejects.toThrow(
      /less than/,
    )
  })
})
