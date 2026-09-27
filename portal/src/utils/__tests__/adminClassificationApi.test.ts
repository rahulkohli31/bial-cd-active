import { describe, it, expect, vi } from 'vitest'
import {
  addClassificationClass,
  editClassificationClass,
  fetchClassificationConfig,
  scoredTotal,
  shareOf,
  updateClassificationPolicy,
} from '../adminClassificationApi'
import { ApiError } from '../apiError'

const deps = (fetchImpl: unknown) => ({ fetchImpl, getToken: () => null, refresh: vi.fn() }) as never

// authFetch peeks a 403 body through res.clone(), so a faked Response must be cloneable.
const res = (init: Record<string, unknown>): Record<string, unknown> => ({ ...init, clone: () => res(init) })
const ok = (json: unknown) => res({ ok: true, status: 200, json: async () => json })

const PII = {
  key: 'pii',
  title: 'PII',
  description: 'Yes if the app stores identity documents.',
  kind: 'hard_block',
  weight: null,
  active: true,
  updatedAt: '2026-09-26T09:00:00Z',
  updatedByName: 'admin',
}
const AI = { ...PII, key: 'ai_usage', title: 'AI usage', kind: 'scored', weight: 20, updatedByName: null }
const CONFIG = { policy: { threshold: 100, ownersCanChangeAnswers: true }, classes: [PII, AI] }

describe('reading the configuration', () => {
  it('asks the admin route and keeps the policy and every class', async () => {
    const fetchImpl = vi.fn(async () => ok(CONFIG))

    const config = await fetchClassificationConfig(deps(fetchImpl))

    const [url] = fetchImpl.mock.calls[0] as unknown as [string]
    expect(url).toBe('/api/admin/classification')
    expect(config).toEqual(CONFIG)
  })

  it('throws on a class it cannot read rather than showing a thinner table', async () => {
    const { kind: _gone, ...unreadable } = AI
    const fetchImpl = vi.fn(async () => ok({ ...CONFIG, classes: [PII, unreadable] }))

    await expect(fetchClassificationConfig(deps(fetchImpl))).rejects.toBeInstanceOf(ApiError)
  })

  it('throws on a scored class with no weight', async () => {
    const fetchImpl = vi.fn(async () => ok({ ...CONFIG, classes: [{ ...AI, weight: null }] }))

    await expect(fetchClassificationConfig(deps(fetchImpl))).rejects.toBeInstanceOf(ApiError)
  })
})

describe('writing the configuration', () => {
  it('patches the policy with only the setting that changed', async () => {
    const fetchImpl = vi.fn(async () => ok(CONFIG))

    await updateClassificationPolicy({ threshold: 50 }, deps(fetchImpl))

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/admin/classification/policy')
    expect(init.method).toBe('PATCH')
    expect(JSON.parse(String(init.body))).toEqual({ threshold: 50 })
  })

  it('posts a new class and reads back the whole configuration', async () => {
    const fetchImpl = vi.fn(async () => res({ ok: true, status: 201, json: async () => CONFIG }))
    const fields = { title: 'File uploads', description: 'Yes if it keeps uploads.', kind: 'scored', weight: 20, active: true } as const

    const config = await addClassificationClass(fields, deps(fetchImpl))

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/admin/classification/classes')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual(fields)
    expect(config.classes).toHaveLength(2)
  })

  it('patches one class by its key', async () => {
    const fetchImpl = vi.fn(async () => ok(CONFIG))

    await editClassificationClass('ai_usage', { active: false }, deps(fetchImpl))

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/admin/classification/classes/ai_usage')
    expect(init.method).toBe('PATCH')
    expect(JSON.parse(String(init.body))).toEqual({ active: false })
  })

  it('carries the server’s code and message on a duplicate title', async () => {
    const body = { error: { message: 'A class called “PII” already exists.', code: 'duplicate_title' } }
    const fetchImpl = vi.fn(async () => res({ ok: false, status: 409, json: async () => body }))

    const failure = await editClassificationClass('ai_usage', { title: 'PII' }, deps(fetchImpl)).catch((e: unknown) => e)

    expect(failure).toBeInstanceOf(ApiError)
    expect((failure as ApiError).code).toBe('duplicate_title')
    expect((failure as ApiError).message).toBe('A class called “PII” already exists.')
  })
})

describe('shares of the score', () => {
  it('rounds half up in whole numbers', () => {
    expect(shareOf(20, 120)).toBe(17)
    expect(shareOf(1, 8)).toBe(13)
    expect(shareOf(20, 100)).toBe(20)
    expect(shareOf(0, 100)).toBe(0)
  })

  it('has no share when the total weight is 0', () => {
    expect(shareOf(0, 0)).toBeNull()
  })

  it('totals only active scored classes', () => {
    const classes = [
      { kind: 'scored', weight: 20, active: true },
      { kind: 'scored', weight: 30, active: false },
      { kind: 'hard_block', weight: null, active: true },
      { kind: 'scored', weight: 5, active: true },
    ] as const
    expect(scoredTotal(classes)).toBe(25)
  })
})
