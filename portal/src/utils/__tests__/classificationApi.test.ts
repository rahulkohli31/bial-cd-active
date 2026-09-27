/**
 * The classification client's PARSE CONTRACT — the half the dialog tests structurally cannot
 * cover, because they mock this module and hand the component ready-made readouts.
 *
 * The narrowing must fail LOUD on a malformed body — a class silently dropped would change every
 * other class's share of the score — and it must carry nothing about a class beyond its key,
 * title, kind and weight: a class description is the reviewer's instruction, never the owner's.
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

const POLICY = { threshold: 100, ownersCanChangeAnswers: true }
const CLASSES = [
  { key: 'pii', title: 'PII', kind: 'hard_block', weight: null },
  { key: 'ai_usage', title: 'AI usage', kind: 'scored', weight: 20 },
]
const VERDICTS = {
  pii: { verdict: 'no', reason: 'We found no sign of this.' },
  ai_usage: { verdict: 'yes', reason: 'summarises comments with an AI model (summarise.ts).' },
}
const COMPLETE = {
  status: 'complete',
  policy: POLICY,
  classes: CLASSES,
  headSha: 'a1b2c3d4e5f6',
  savedAt: '2026-09-26T08:35:00Z',
  reviewedSha: 'a1b2c3d4e5f6',
  checkedAt: '2026-09-26T08:36:00Z',
  current: true,
  verdicts: VERDICTS,
  failureCode: null,
  failureMessage: null,
  retryable: null,
}

async function parse(body: unknown) {
  return ensureClassificationReview('p1', deps(vi.fn(async () => ok(body))))
}

describe('ensureClassificationReview', () => {
  it('POSTs to the review route and parses a settled body', async () => {
    const fetchImpl = vi.fn(async (_url: string, _init?: RequestInit) => ok(COMPLETE))

    const review = await ensureClassificationReview('p1', deps(fetchImpl))

    expect(fetchImpl.mock.calls[0][0]).toBe('/api/projects/p1/classification-review')
    expect(fetchImpl.mock.calls[0][1]?.method).toBe('POST')
    expect(review).toEqual({
      status: 'complete',
      policy: { threshold: 100, ownersCanChangeAnswers: true },
      classes: [
        { key: 'pii', title: 'PII', kind: 'hard_block', weight: null },
        { key: 'ai_usage', title: 'AI usage', kind: 'scored', weight: 20 },
      ],
      headSha: 'a1b2c3d4e5f6',
      savedAt: '2026-09-26T08:35:00Z',
      reviewedSha: 'a1b2c3d4e5f6',
      checkedAt: '2026-09-26T08:36:00Z',
      current: true,
      verdicts: {
        pii: { verdict: 'no', reason: 'We found no sign of this.' },
        ai_usage: { verdict: 'yes', reason: 'summarises comments with an AI model (summarise.ts).' },
      },
      failureCode: null,
      failureMessage: null,
      retryable: false,
    })
  })

  it('keeps the classes in the order the server sent them', async () => {
    const review = await parse({ ...COMPLETE, classes: [...CLASSES].reverse() })

    expect(review.classes.map((c) => c.key)).toEqual(['ai_usage', 'pii'])
  })

  it('carries no class description, even when a body sends one', async () => {
    const withDescription = CLASSES.map((c) => ({ ...c, description: 'Yes if the app stores a PAN.' }))

    const review = await parse({ ...COMPLETE, classes: withDescription })

    expect(review.classes[0]).toEqual({ key: 'pii', title: 'PII', kind: 'hard_block', weight: null })
    expect(JSON.stringify(review)).not.toContain('PAN')
  })

  it('a 202 running body resolves too — in-flight is a state, not an error', async () => {
    const running = { ...COMPLETE, status: 'running', checkedAt: null, verdicts: null }

    const review = await ensureClassificationReview('p1', deps(vi.fn(async () => ok(running, 202))))

    expect(review.status).toBe('running')
    expect(review.verdicts).toBeNull()
  })

  it('passes a multi-line reason through verbatim — no trim, no collapse', async () => {
    const reason = 'A saved password sits in your app.\nRemove it and save again.'
    const review = await parse({
      ...COMPLETE,
      verdicts: { ...VERDICTS, ai_usage: { verdict: 'yes', reason } },
    })

    expect(review.verdicts?.ai_usage.reason).toBe(reason)
  })

  it('reads a missing current flag as not current', async () => {
    const { current: _dropped, ...noFlag } = COMPLETE

    expect((await parse(noFlag)).current).toBe(false)
  })

  it('normalizes a missing retryable flag to false — no server flag, no re-check', async () => {
    const failed = {
      ...COMPLETE,
      status: 'failed',
      verdicts: null,
      failureCode: 'review_failed',
      failureMessage: "The automatic check couldn't run.",
    }
    expect((await parse(failed)).retryable).toBe(false)
    expect((await parse({ ...failed, retryable: true })).retryable).toBe(true)
  })

  it('refuses a body with an unknown status rather than guessing', async () => {
    await expect(parse({ ...COMPLETE, status: 'ok' })).rejects.toThrow(/could not read.*status/i)
  })

  it('refuses a body with no policy', async () => {
    const { policy: _dropped, ...noPolicy } = COMPLETE
    await expect(parse(noPolicy)).rejects.toThrow(/could not read.*policy/i)
  })

  it('refuses a class of an unknown kind', async () => {
    const body = { ...COMPLETE, classes: [{ ...CLASSES[0], kind: 'maybe' }] }
    await expect(parse(body)).rejects.toThrow(/could not read.*kind/i)
  })

  it('refuses a scored class with no weight', async () => {
    const body = { ...COMPLETE, classes: [{ ...CLASSES[1], weight: null }] }
    await expect(parse(body)).rejects.toThrow(/could not read.*weight/i)
  })

  it('refuses a verdict outside yes and no', async () => {
    const body = { ...COMPLETE, verdicts: { ...VERDICTS, pii: { verdict: 'unanswered', reason: 'hm' } } }
    await expect(parse(body)).rejects.toThrow(/could not read.*pii\.verdict/i)
  })

  it('refuses a failed review with no owner sentence — an empty failure is unrenderable', async () => {
    const body = { ...COMPLETE, status: 'failed', verdicts: null, failureCode: 'review_failed' }
    await expect(parse(body)).rejects.toThrow(/could not read.*failureMessage/i)
  })

  it('surfaces the storage 503 as an ApiError carrying the code and the owner sentence', async () => {
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

  it('parses the nothing-to-review state: the live policy and classes, and no version', async () => {
    const review = await getClassificationReview(
      'p1',
      deps(vi.fn(async () => ok({ status: 'nothing_to_review', policy: POLICY, classes: CLASSES }))),
    )

    expect(review.status).toBe('nothing_to_review')
    expect(review.classes).toHaveLength(2)
    expect(review.headSha).toBeNull()
    expect(review.savedAt).toBeNull()
    expect(review.reviewedSha).toBeNull()
    expect(review.checkedAt).toBeNull()
    expect(review.verdicts).toBeNull()
    expect(review.retryable).toBe(false)
  })
})
