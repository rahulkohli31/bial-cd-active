import { describe, it, expect, vi } from 'vitest'
import { newBuild, createBuild } from '../builderHistory'

const deps = (fetchImpl) => ({ fetchImpl, getToken: () => 'tok', refresh: vi.fn() })

describe('builderHistory', () => {
  it('newBuild mints a client UUID synchronously (no network)', () => {
    const a = newBuild()
    const b = newBuild()
    expect(a).toMatch(/^[0-9a-f-]{36}$/i)
    expect(a).not.toBe(b)
  })

  it('createBuild POSTs the builder-kind create body (U7: row exists before the first turn)', async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      status: 201,
      json: async () => ({ conversation: { _id: 'build-1', kind: 'builder', projectId: 'p1', mode: 'plan' } }),
    }))
    await createBuild('build-1', { projectId: 'p1', title: 'T', context: { theme: 'bial' } }, deps(fetchImpl))
    const [url, opts] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/conversations')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({
      id: 'build-1',
      projectId: 'p1',
      kind: 'builder',
      title: 'T',
      context: { theme: 'bial' },
    })
  })
})
