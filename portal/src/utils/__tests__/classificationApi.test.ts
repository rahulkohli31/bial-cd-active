/**
 * The classification client's PARSE CONTRACT — the half the modal tests structurally
 * cannot cover, because they mock this module and hand the component ready-made
 * `ClassificationReview` objects, so `toClassificationReview` never runs there.
 *
 * Two things carry the weight here. The narrowing must fail LOUD on a malformed body —
 * a verdict silently dropped or coerced would hand a question to nobody — and the
 * multi-line REASON must pass through byte-for-byte, because the render path's promise
 * (verbatim prose, no markdown collapse) is only as good as what the parser hands it.
 */
import { describe, it, expect, vi } from 'vitest'
import {
  ensureClassificationReview,
  getClassificationReview,
  STORAGE_UNAVAILABLE,
} from '../classificationApi'
import { ApiError } from '../apiError'

const deps = (fetchImpl: unknown) => ({
  fetchImpl,
  getToken: () => null,
  refresh: vi.fn(),
}) as never

// authFetch peeks a 403 body through res.clone(), so a faked Response must be cloneable.
const res = (init: Record<string, unknown>): Record<string, unknown> => ({
  ...init,
  clone: () => res(init),
})
const ok = (json: unknown, status = 200) => res({ ok: true, status, json: async () => json })

const QUESTION = { verdict: 'no', reason: 'We found no sign of this.' }
const VERDICTS = {
  credentialsSecrets: QUESTION,
  healthData: QUESTION,
  personalInformation: QUESTION,
  financialData: QUESTION,
  confidentialBusinessData: QUESTION,
  publicData: QUESTION,
}
const COMPLETE = {
  status: 'complete',
  headSha: 'a1b2c3d4e5f6',
  savedAt: '2026-08-19T10:15:00Z',
  reviewedSha: 'a1b2c3d4e5f6',
  verdicts: VERDICTS,
  failureCode: null,
  failureMessage: null,
  retryable: null,
}

describe('ensureClassificationReview', () => {
  it('POSTs to the review route and parses a settled body', async () => {
    const fetchImpl = vi.fn(async (_url: string, _init?: RequestInit) => ok(COMPLETE))

    const review = await ensureClassificationReview('p1', deps(fetchImpl))

    expect(fetchImpl.mock.calls[0][0]).toBe('/api/projects/p1/classification-review')
    expect(fetchImpl.mock.calls[0][1]?.method).toBe('POST')
    expect(review.status).toBe('complete')
    expect(review.headSha).toBe('a1b2c3d4e5f6')
    expect(review.reviewedSha).toBe('a1b2c3d4e5f6')
    expect(review.verdicts?.credentialsSecrets).toEqual({
      verdict: 'no',
      reason: 'We found no sign of this.',
    })
    expect(review.verdicts?.publicData.verdict).toBe('no')
  })

  it('a 202 running body resolves too — in-flight is a state, not an error', async () => {
    const running = {
      status: 'running',
      headSha: 'a1b2c3d4e5f6',
      savedAt: '2026-08-19T10:15:00Z',
      reviewedSha: 'a1b2c3d4e5f6',
      verdicts: null,
    }

    const review = await ensureClassificationReview('p1', deps(vi.fn(async () => ok(running, 202))))

    expect(review.status).toBe('running')
    expect(review.verdicts).toBeNull()
  })

  it('passes a multi-line reason through verbatim — no trim, no collapse', async () => {
    // The render path promises whitespace-preserving prose; that promise starts here.
    const reason = 'A saved password sits in your app.\nRemove it and save again.'
    const body = {
      ...COMPLETE,
      verdicts: { ...VERDICTS, credentialsSecrets: { verdict: 'yes', reason } },
    }

    const review = await ensureClassificationReview('p1', deps(vi.fn(async () => ok(body))))

    expect(review.verdicts?.credentialsSecrets.reason).toBe(reason)
  })

  it('normalizes a missing retryable flag to false — no server flag, no re-check', async () => {
    const failed = {
      ...COMPLETE,
      status: 'failed',
      failureCode: 'review_failed',
      failureMessage: "The automatic check couldn't run.",
    }
    const noFlag = await ensureClassificationReview('p1', deps(vi.fn(async () => ok(failed))))
    expect(noFlag.retryable).toBe(false)

    const flagged = await ensureClassificationReview(
      'p1',
      deps(vi.fn(async () => ok({ ...failed, retryable: true }))),
    )
    expect(flagged.retryable).toBe(true)
  })

  it('refuses a body with an unknown status rather than guessing', async () => {
    await expect(
      ensureClassificationReview('p1', deps(vi.fn(async () => ok({ ...COMPLETE, status: 'ok' })))),
    ).rejects.toThrow(/could not read.*status/i)
  })

  it('refuses verdicts missing a question — six-of-six or nothing', async () => {
    const { publicData: _dropped, ...partial } = VERDICTS
    await expect(
      ensureClassificationReview(
        'p1',
        deps(vi.fn(async () => ok({ ...COMPLETE, verdicts: partial }))),
      ),
    ).rejects.toThrow(/could not read.*publicData/i)
  })

  it('refuses a verdict outside yes/no/unanswered', async () => {
    const body = {
      ...COMPLETE,
      verdicts: { ...VERDICTS, healthData: { verdict: 'maybe', reason: 'hm' } },
    }
    await expect(
      ensureClassificationReview('p1', deps(vi.fn(async () => ok(body)))),
    ).rejects.toThrow(/could not read.*healthData\.verdict/i)
  })

  it('refuses a failed review with no citizen sentence — an empty failure is unrenderable', async () => {
    const body = { ...COMPLETE, status: 'failed', failureCode: 'review_failed' }
    await expect(
      ensureClassificationReview('p1', deps(vi.fn(async () => ok(body)))),
    ).rejects.toThrow(/could not read.*failureMessage/i)
  })

  it('surfaces the storage 503 as an ApiError carrying the code and the citizen sentence', async () => {
    const fetchImpl = vi.fn(async () =>
      res({
        ok: false,
        status: 503,
        json: async () => ({
          error: {
            message: "We can't reach your saved app right now. Please try again in a moment.",
            code: STORAGE_UNAVAILABLE,
          },
        }),
      }),
    )

    const err = await ensureClassificationReview('p1', deps(fetchImpl)).catch((e: unknown) => e)

    expect(err).toBeInstanceOf(ApiError)
    if (err instanceof ApiError) {
      expect(err.status).toBe(503)
      expect(err.code).toBe(STORAGE_UNAVAILABLE)
      expect(err.message).toContain("can't reach your saved app")
    }
  })
})

describe('getClassificationReview', () => {
  it('GETs the same route — reading never starts a run', async () => {
    const fetchImpl = vi.fn(async (_url: string, _init?: RequestInit) => ok(COMPLETE))

    const review = await getClassificationReview('p1', deps(fetchImpl))

    expect(fetchImpl.mock.calls[0][0]).toBe('/api/projects/p1/classification-review')
    expect(fetchImpl.mock.calls[0][1]?.method).toBeUndefined()
    expect(review.status).toBe('complete')
  })

  it('parses the nothing-to-review state: all-null fields, no verdicts (R21)', async () => {
    const review = await getClassificationReview(
      'p1',
      deps(vi.fn(async () => ok({ status: 'nothing_to_review' }))),
    )

    expect(review.status).toBe('nothing_to_review')
    expect(review.headSha).toBeNull()
    expect(review.savedAt).toBeNull()
    expect(review.reviewedSha).toBeNull()
    expect(review.verdicts).toBeNull()
    expect(review.retryable).toBe(false)
  })
})
