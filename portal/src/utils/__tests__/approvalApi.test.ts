import { describe, it, expect, vi } from 'vitest'
import { getApprovalStatus, submitForReview } from '../approvalApi'
import { ApiError } from '../apiError'

// A real WHATWG Response so `res.ok` / `res.status` / `res.json()` behave exactly as
// production fetch would — the same real-boundary approach as projectApi.test.ts, no
// module mocking: the file's whole value is the unknown→narrowed parsing, so the real
// narrowers must run in CI.
const jsonResponse = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const fetchReturning = (status: number, body: unknown) =>
  vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => jsonResponse(status, body))

// authFetch deps injection — no real token/network. getToken returns null: the
// cookie-session model carries no client bearer token.
const deps = (fetchImpl: typeof fetch) => ({ fetchImpl, getToken: () => null, refresh: async () => false })

const SHA = 'a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0'

const validStatus = {
  appId: 'app-1',
  status: 'pending',
  rejectionNote: null,
  submissionId: 'sub-1',
  commitSha: SHA,
  submittedAt: '2026-07-16T10:00:00Z',
}

describe('getApprovalStatus', () => {
  it('GETs /api/apps/:id/status (URL-encoded) and returns the narrowed status', async () => {
    const fetchImpl = fetchReturning(200, validStatus)
    const result = await getApprovalStatus('app-1', deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/apps/app-1/status')
    expect(fetchImpl.mock.calls[0][1]?.method).toBeUndefined() // a plain GET
    expect(result).toEqual(validStatus)
  })

  it('URL-encodes an appId with unsafe characters', async () => {
    const fetchImpl = fetchReturning(200, { ...validStatus, appId: 'a/b' })
    await getApprovalStatus('a/b', deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/apps/a%2Fb/status')
  })

  it('throws an ApiError carrying status + server message on a non-2xx', async () => {
    const fetchImpl = fetchReturning(404, { error: { message: 'App not found.' } })
    const err = await getApprovalStatus('missing', deps(fetchImpl)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(404)
    expect((err as ApiError).message).toBe('App not found.')
  })
})

describe('submitForReview', () => {
  it('POSTs /api/apps/:id/submit with NO body and returns the narrowed submit result', async () => {
    const fetchImpl = fetchReturning(200, validStatus)
    const result = await submitForReview('app-1', deps(fetchImpl))
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/apps/app-1/submit')
    expect(init?.method).toBe('POST')
    expect(init?.body).toBeUndefined() // the artifact is the server-side snapshot (R19)
    expect(result).toEqual({
      appId: 'app-1',
      status: 'pending',
      submissionId: 'sub-1',
      commitSha: SHA,
      submittedAt: '2026-07-16T10:00:00Z',
    })
  })

  it('surfaces the server 409 copy verbatim', async () => {
    const fetchImpl = fetchReturning(409, {
      error: { message: 'A build session is still running — end it before submitting.' },
    })
    const err = await submitForReview('app-1', deps(fetchImpl)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(409)
    expect((err as ApiError).message).toContain('build session')
  })
})

describe('response narrowing (parse, do not validate)', () => {
  it('throws rather than trust an unknown status literal (toAppStatus)', async () => {
    const fetchImpl = fetchReturning(200, { ...validStatus, status: 'exploded' })
    const err = await getApprovalStatus('app-1', deps(fetchImpl)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(500)
    expect((err as ApiError).message).toMatch(/status/i)
  })

  it('throws on a non-record body (toApprovalStatus)', async () => {
    const fetchImpl = fetchReturning(200, 'not an object')
    const err = await getApprovalStatus('app-1', deps(fetchImpl)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(500)
  })

  it('throws on a missing appId rather than coerce it to "" (toApprovalStatus)', async () => {
    const { appId: _dropped, ...noAppId } = validStatus
    const fetchImpl = fetchReturning(200, noAppId)
    const err = await getApprovalStatus('app-1', deps(fetchImpl)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toMatch(/could not read/i)
  })

  it('throws when a "successful" submit response is missing submissionId/commitSha/submittedAt (toSubmitResult)', async () => {
    for (const missing of ['submissionId', 'commitSha', 'submittedAt'] as const) {
      const fetchImpl = fetchReturning(200, { ...validStatus, [missing]: null })
      const err = await submitForReview('app-1', deps(fetchImpl)).catch((e: unknown) => e)
      expect(err).toBeInstanceOf(ApiError)
      expect((err as ApiError).status).toBe(500)
    }
  })

  it('collapses an empty-string rejectionNote to null, and passes a real note through', async () => {
    const emptyNote = fetchReturning(200, { ...validStatus, status: 'rejected', rejectionNote: '' })
    expect((await getApprovalStatus('app-1', deps(emptyNote))).rejectionNote).toBeNull()

    const realNote = fetchReturning(200, {
      ...validStatus,
      status: 'rejected',
      rejectionNote: 'Remove the sample data.',
    })
    expect((await getApprovalStatus('app-1', deps(realNote))).rejectionNote).toBe('Remove the sample data.')
  })
})
