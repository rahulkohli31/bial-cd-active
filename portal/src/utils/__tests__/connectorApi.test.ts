/**
 * The connector client's routes and PARSE contract, for the parts no component test can reach —
 * every component test mocks these functions, so a wrong path or a tolerant parse would ship green.
 *
 * WHY THIS MODULE IS STRICTER THAN `marketplaceApi`. The catalog drops an unreadable row and
 * renders the rest, because one bad app must not blank a list of hundreds. This list is the WHOLE
 * of what an application can read: dropping its only row would render "nothing is connected",
 * which is false. So a row we cannot read throws.
 */
import { describe, it, expect, vi } from 'vitest'
import { listProjectConnectors, setProjectConnector } from '../connectorApi'
import { ApiError } from '../apiError'

const deps = (fetchImpl: unknown) =>
  ({ fetchImpl, getToken: () => null, refresh: vi.fn() }) as never

// authFetch peeks a 403 body through res.clone(), so a faked Response must be cloneable.
const res = (init: Record<string, unknown>): Record<string, unknown> => ({
  ...init,
  clone: () => res(init),
})
const ok = (json: unknown) => res({ ok: true, status: 200, json: async () => json })

const ENTRY = {
  key: 'orbit',
  displayName: 'ORBIT',
  dataNoun: 'stand and gate data',
  enabled: false,
  effectivelyOn: false,
  window: null,
}

describe('reading what one application reads', () => {
  it('asks for this application and keeps exactly the project facts', async () => {
    const fetchImpl = vi.fn(async () => ok({ connectors: [{ ...ENTRY, state: 'approved' }] }))

    const [row] = await listProjectConnectors('p1', deps(fetchImpl))

    const [url] = fetchImpl.mock.calls[0] as unknown as [string]
    expect(url).toBe('/api/projects/p1/connectors')
    // Whatever else the wire carries, the switch is the whole of access: a person-level field
    // riding along must not reach a component that could start branching on it.
    expect(row).toEqual(ENTRY)
  })

  it('throws on a row it cannot read rather than rendering a thinner one', async () => {
    const { effectivelyOn: _gone, ...unreadable } = ENTRY
    await expect(
      listProjectConnectors('p1', deps(vi.fn(async () => ok({ connectors: [unreadable] })))),
    ).rejects.toBeInstanceOf(ApiError)
  })
})

describe('switching a connector on', () => {
  it('puts the switch at this application and connector, and reads back what the server resolved', async () => {
    const fetchImpl = vi.fn(async () => ok({ ...ENTRY, enabled: true, effectivelyOn: true }))

    const row = await setProjectConnector('p1', 'orbit', { enabled: true }, deps(fetchImpl))

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/projects/p1/connectors/orbit')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(String(init.body))).toEqual({ enabled: true })
    expect(row.effectivelyOn).toBe(true)
  })
})
