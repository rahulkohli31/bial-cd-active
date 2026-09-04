import { describe, it, expect, vi } from 'vitest'
import * as approvalApi from '../approvalApi'
import { withdrawSubmission } from '../approvalApi'
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

describe('the app-scoped status read is gone', () => {
  /**
   * A GUARD, not deleted coverage (plan 001, unit 6). `getApprovalStatus` was the typed client
   * for `GET /apps/:id/status`, written for the approval card at the foot of the chat — the
   * canvas's `Removals` board took that card out, and nothing reached the getter, its
   * `AppApprovalStatus` interface or its narrower afterwards. The publish and review surfaces
   * read the lifecycle off the PROJECT-scoped deploy status instead, so both share one poll
   * lifetime and cannot end up telling the citizen two different things; re-adding a second,
   * app-scoped poll here is precisely what would break that. The SERVER route is untouched.
   *
   * `toAppStatus`'s unknown-literal refusal and the non-record / missing-appId refusals are the
   * only narrowing this file lost a caller for, and all three are still exercised — through
   * `withdrawSubmission`, in the block above. What went with the getter was
   * `nonEmptyStringOrNull`, which had no other caller.
   */
  it('exports no app-scoped status read — the lifecycle comes off the deploy poll', () => {
    expect('getApprovalStatus' in approvalApi).toBe(false)
    expect((approvalApi as Record<string, unknown>).getApprovalStatus).toBeUndefined()
    // Paired with a liveness assertion so the absences above cannot false-green on an
    // empty module namespace.
    expect(typeof (approvalApi as Record<string, unknown>).withdrawSubmission).toBe('function')
  })
})
