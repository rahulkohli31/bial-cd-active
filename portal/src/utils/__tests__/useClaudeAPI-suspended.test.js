import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fetchClaudeStream } from '../../hooks/useClaudeAPI'
import { handleSuspendedSession } from '../../utils/auth'

// The real handleSuspendedSession hard-navigates (jsdom throws "Not implemented:
// navigation"); spy it to assert the wiring. Everything else in auth.js is
// preserved so the module loads exactly as in production.
vi.mock('../../utils/auth', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, handleSuspendedSession: vi.fn() }
})

beforeEach(() => {
  handleSuspendedSession.mockClear()
})

// A pre-stream response carrying an SSE reader we can watch. `getReader` is a spy:
// on a suspended 403 it must NEVER be called — the stream stays shut. `current_user`
// runs before the first SSE byte, so a mid-stream suspension cannot happen.
function response({ status, detail }) {
  const getReader = vi.fn(() => ({ read: async () => ({ done: true }), cancel: async () => {} }))
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => ({ detail }),
    body: { getReader },
  }
}

describe('fetchClaudeStream — mid-session suspension', () => {
  it('a pre-stream 403 {"detail":"Account suspended"} bounces to login and never opens the reader', async () => {
    const res = response({ status: 403, detail: 'Account suspended' })

    await expect(
      fetchClaudeStream({ body: { messages: [] }, fetchImpl: async () => res, getToken: () => 't', refresh: vi.fn() }),
    ).rejects.toMatchObject({ name: 'ApiError', status: 403 })

    expect(handleSuspendedSession).toHaveBeenCalledTimes(1)
    expect(res.body.getReader).not.toHaveBeenCalled()
  })

  it('does not refresh-and-retry a suspended 403 (a fresh session is still suspended)', async () => {
    const res = response({ status: 403, detail: 'Account suspended' })
    const fetchImpl = vi.fn(async () => res)
    const refresh = vi.fn(async () => true)

    await expect(
      fetchClaudeStream({ body: { messages: [] }, fetchImpl, getToken: () => 't', refresh }),
    ).rejects.toMatchObject({ status: 403 })

    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(refresh).not.toHaveBeenCalled()
  })

  it('a non-suspension 403 (super-admin gate) is NOT a suspension — no bounce, generic error, reader shut', async () => {
    const res = response({ status: 403, detail: 'Super-admin privileges required.' })

    await expect(
      fetchClaudeStream({ body: { messages: [] }, fetchImpl: async () => res, getToken: () => 't', refresh: vi.fn() }),
    ).rejects.toThrow()

    expect(handleSuspendedSession).not.toHaveBeenCalled()
    expect(res.body.getReader).not.toHaveBeenCalled()
  })
})
