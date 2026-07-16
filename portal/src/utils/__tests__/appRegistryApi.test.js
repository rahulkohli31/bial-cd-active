/**
 * Admin-side registry client + the OWNER-SURFACE INERTNESS GUARD (flipped, APPROVAL R20).
 *
 * The owner group (provisionApp / submitApp / getAppStatus / getAppSource) was retired
 * with the JSX-era submit flow: the open-sandbox submit lives in the typed
 * `approvalApi.ts`, carries no client-compiled artifact, and provisioning happens
 * server-side inside the build session. The guard below pins the retirement — if an
 * owner export creeps back in, this fails and the reintroduction must be deliberate.
 */
import { describe, it, expect, vi } from 'vitest'
import * as registry from '../appRegistryApi.js'

const deps = (fetchImpl) => ({ fetchImpl, getToken: () => null, refresh: vi.fn() })

// authFetch peeks a 403's body through res.clone(), so a faked Response must be cloneable.
const res = (init) => ({ ...init, clone: () => res(init) })
const ok = (json) => res({ ok: true, status: 200, json: async () => json })
const fail = (status, json) => res({ ok: false, status, json: async () => json })

describe('owner-surface retirement (inertness guard)', () => {
  it.each(['provisionApp', 'submitApp', 'getAppStatus', 'getAppSource'])(
    'no longer exports %s',
    (name) => {
      expect(registry[name]).toBeUndefined()
    },
  )
})

describe('approveApp', () => {
  it('POSTs the REVIEWED submission id (the D5 guard has something to check)', async () => {
    const fetchImpl = vi.fn(async () => ok({ appId: 'a1', status: 'approved' }))
    await registry.approveApp('a1', 'sub-1', deps(fetchImpl))

    const [url, opts] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/admin/apps/a1/approve')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ submissionId: 'sub-1' })
  })

  it('surfaces the 409 re-submitted-since-review copy verbatim', async () => {
    const message = 'This app was re-submitted since you reviewed it — please re-review.'
    const fetchImpl = vi.fn(async () => fail(409, { error: { message } }))
    const err = await registry.approveApp('a1', 'sub-1', deps(fetchImpl)).catch((e) => e)
    expect(err.status).toBe(409)
    expect(err.message).toBe(message)
  })
})

describe('bundleDownloadUrl', () => {
  it('GETs the audited download endpoint and returns the minted payload', async () => {
    const payload = { url: 'https://x/sas', submissionId: 's1', commitSha: 'c'.repeat(40), expiresInSeconds: 900 }
    const fetchImpl = vi.fn(async () => ok(payload))
    const minted = await registry.bundleDownloadUrl('a1', deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/admin/apps/a1/bundle-url')
    expect(minted).toEqual(payload)
  })
})

describe('markDeployed', () => {
  it('POSTs the marker endpoint', async () => {
    const fetchImpl = vi.fn(async () => ok({ appId: 'a1', deployedSubmissionId: 's1', deployedAt: 'now' }))
    await registry.markDeployed('a1', deps(fetchImpl))
    const [url, opts] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/admin/apps/a1/mark-deployed')
    expect(opts.method).toBe('POST')
  })
})
