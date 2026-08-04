import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { getContextLimits, CONTEXT_SOFT_LIMIT, CONTEXT_HARD_LIMIT } from '../../hooks/useClaudeAPI.js'
import { bootstrapSession, invalidateSession } from '../auth'

// The user profile now comes from the once-cached GET /auth/me (not localStorage),
// so seed the cached session by resolving a mocked /me carrying the limits.
async function storeUser(limits) {
  invalidateSession()
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ username: 'u', ...(limits && { limits }) }),
  })
  await bootstrapSession()
}

describe('getContextLimits', () => {
  beforeEach(() => invalidateSession())
  afterEach(() => {
    invalidateSession()
    vi.restoreAllMocks()
  })

  it('falls back to the default constants when there is no stored user', () => {
    expect(getContextLimits()).toEqual({ soft: CONTEXT_SOFT_LIMIT, hard: CONTEXT_HARD_LIMIT })
  })

  it('falls back to defaults when the stored user carries no limits', async () => {
    await storeUser(null)
    expect(getContextLimits()).toEqual({ soft: CONTEXT_SOFT_LIMIT, hard: CONTEXT_HARD_LIMIT })
  })

  it('uses per-user overrides when present', async () => {
    await storeUser({ contextSoftLimit: 50_000, contextHardLimit: 120_000 })
    expect(getContextLimits()).toEqual({ soft: 50_000, hard: 120_000 })
  })

  it('forces soft strictly below hard', async () => {
    await storeUser({ contextSoftLimit: 120_000, contextHardLimit: 100_000 })
    const { soft, hard } = getContextLimits()
    expect(hard).toBe(100_000)
    expect(soft).toBeLessThan(100_000)
  })

  it('ignores an invalid override field (falls back to the default)', async () => {
    await storeUser({ contextHardLimit: -5 })
    expect(getContextLimits().hard).toBe(CONTEXT_HARD_LIMIT)
  })
})
