import { describe, it, expect, vi } from 'vitest'
import * as approvalApi from '../approvalApi'
import { getApprovalStatus, withdrawSubmission } from '../approvalApi'
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
  deployedAt: null,
  deployedUrl: null,
}

describe('getApprovalStatus', () => {
  it('GETs /api/apps/:id/status (URL-encoded) and returns the narrowed status', async () => {
    const fetchImpl = fetchReturning(200, validStatus)
    const result = await getApprovalStatus('app-1', deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/apps/app-1/status')
    expect(fetchImpl.mock.calls[0][1]?.method).toBeUndefined() // a plain GET
    expect(result).toEqual(validStatus)
  })

  it('narrows the deploy marker (R5), collapsing an absent URL to null', async () => {
    const live = 'https://apps.bial.example.com/gate-ops'
    const deployed = fetchReturning(200, {
      ...validStatus,
      status: 'approved',
      deployedAt: '2026-07-16T12:00:00Z',
      deployedUrl: live,
    })
    const result = await getApprovalStatus('app-1', deps(deployed))
    expect(result.deployedUrl).toBe(live)
    expect(result.deployedAt).toBe('2026-07-16T12:00:00Z')

    // A pre-R5 server (no such keys) and an empty string both mean "no Live link" —
    // the control renders on `deployedUrl !== null`, so neither may become `''`.
    const legacy = fetchReturning(200, { ...validStatus, deployedUrl: '', deployedAt: undefined })
    const narrowed = await getApprovalStatus('app-1', deps(legacy))
    expect(narrowed.deployedUrl).toBeNull()
    expect(narrowed.deployedAt).toBeNull()
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

// --- the retired submit verb ---------------------------------------------------------

describe('the citizen submit verb is gone', () => {
  /**
   * A GUARD, not a deletion. This block used to POST `/api/apps/:id/submit` and pin its
   * narrowed result and its 409 copy; the route it called was retired backend-side in U8
   * and the control that called it lost its button in U12, because R15a allows exactly
   * ONE route into the review queue and it runs through the publish request — which
   * attaches both answer sets and the citizen's explanation. The retired route attached
   * none of that, so a queue item could reach an administrator with nothing to read.
   *
   * Deleting this file's submit coverage is what an implementer meeting a red suite
   * reaches for first, and it would leave nothing at all stopping the second way in from
   * being quietly re-added. `toSubmitResult`'s narrowing test went with the verb: there
   * is no submit response left to narrow.
   */
  it('exports no submitForReview — publishing is the only way into the queue', () => {
    expect('submitForReview' in approvalApi).toBe(false)
    // Belt and braces against a re-export that resolves to undefined rather than being
    // absent: either shape must fail to be callable.
    expect((approvalApi as Record<string, unknown>).submitForReview).toBeUndefined()
  })

  it('exports no SubmitResult narrowing helper surface either', () => {
    // The type is compile-time only; what a runtime guard can pin is that no value-level
    // submit machinery survived the retirement.
    expect(Object.keys(approvalApi).filter((k) => /submit/i.test(k))).toEqual([])
  })
})

describe('withdrawSubmission', () => {
  it('POSTs /api/apps/:id/withdraw with NO body and returns the narrowed result', async () => {
    const fetchImpl = fetchReturning(200, { appId: 'app-1', status: 'draft' })
    const result = await withdrawSubmission('app-1', deps(fetchImpl))
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/apps/app-1/withdraw')
    expect(init?.method).toBe('POST')
    // The server knows which submission is pending — a body would let a client name one.
    expect(init?.body).toBeUndefined()
    expect(result).toEqual({ appId: 'app-1', status: 'draft' })
  })

  it('URL-encodes an appId with unsafe characters', async () => {
    const fetchImpl = fetchReturning(200, { appId: 'a/b', status: 'draft' })
    await withdrawSubmission('a/b', deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/apps/a%2Fb/withdraw')
  })

  it('surfaces the server 409 copy verbatim when it is no longer pending', async () => {
    // An administrator decided it first. The server's sentence says so; the control
    // renders it rather than string-matching its way to a guess.
    const fetchImpl = fetchReturning(409, {
      error: { message: 'Only a submission that is waiting for review can be withdrawn.' },
    })
    const err = await withdrawSubmission('app-1', deps(fetchImpl)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(409)
    expect((err as ApiError).message).toContain('waiting for review')
  })

  it('throws rather than trust an unknown status literal in the withdraw response', async () => {
    const fetchImpl = fetchReturning(200, { appId: 'app-1', status: 'evaporated' })
    const err = await withdrawSubmission('app-1', deps(fetchImpl)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(500)
  })

  it('throws on a missing appId rather than coerce it to ""', async () => {
    const fetchImpl = fetchReturning(200, { status: 'draft' })
    const err = await withdrawSubmission('app-1', deps(fetchImpl)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toMatch(/could not read/i)
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
