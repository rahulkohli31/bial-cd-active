import { describe, it, expect, vi } from 'vitest'
import { createBuild } from '../builderHistory'

const deps = (fetchImpl) => ({ fetchImpl, getToken: () => 'tok', refresh: vi.fn() })

describe('builderHistory', () => {
  it('createBuild POSTs the build-kind create body (U7: row exists before the first turn)', async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      status: 201,
      json: async () => ({ conversation: { _id: 'build-1', kind: 'build', projectId: 'p1' } }),
    }))
    await createBuild('build-1', { projectId: 'p1', title: 'T', context: { theme: 'bial' } }, deps(fetchImpl))
    const [url, opts] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/conversations')
    expect(opts.method).toBe('POST')
    // U1 collapsed the old three-value kind + ask/plan/write mode into one two-valued ChatKind
    // (plan | build) — the server 422s on the retired 'builder' string.
    expect(JSON.parse(opts.body)).toEqual({
      id: 'build-1',
      projectId: 'p1',
      kind: 'build',
      title: 'T',
      context: { theme: 'bial' },
    })
  })
})
