/**
 * Admin-side registry client + the OWNER-SURFACE INERTNESS GUARD (flipped).
 *
 * The owner group (provisionApp / submitApp / getAppStatus / getAppSource) was retired
 * with the JSX-era submit flow: the open-sandbox submit lives in the typed
 * `approvalApi.ts`, carries no client-compiled artifact, and provisioning happens
 * server-side inside the build session. The guard below pins the retirement — if an
 * owner export creeps back in, this fails and the reintroduction must be deliberate.
 */
import { describe, it, expect, vi } from 'vitest'
import * as registry from '../appRegistryApi'

const deps = (fetchImpl) => ({ fetchImpl, getToken: () => null, refresh: vi.fn() })

// authFetch peeks a 403's body through res.clone(), so a faked Response must be cloneable.
const res = (init) => ({ ...init, clone: () => res(init) })
const ok = (json) => res({ ok: true, status: 200, json: async () => json })
const fail = (status, json) => res({ ok: false, status, json: async () => json })

describe('owner-surface retirement (inertness guard)', () => {
  it.each(['provisionApp', 'submitApp', 'getAppStatus', 'getAppSource', 'bundleDownloadUrl'])(
    'no longer exports %s',
    (name) => {
      expect(registry[name]).toBeUndefined()
    },
  )
})

describe('approveApp', () => {
  it('POSTs the REVIEWED submission id', async () => {
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

describe('markDeployed', () => {
  const marked = (over = {}) => ok({ appId: 'a1', deployedSubmissionId: 's1', deployedAt: 'now', deployedUrl: null, ...over })

  it('POSTs the marker endpoint', async () => {
    const fetchImpl = vi.fn(async () => marked())
    await registry.markDeployed('a1', '', deps(fetchImpl))
    const [url, opts] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/admin/apps/a1/mark-deployed')
    expect(opts.method).toBe('POST')
  })

  it('sends the deployed URL when one is given', async () => {
    const live = 'https://apps.bial.example.com/gate-ops'
    const fetchImpl = vi.fn(async () => marked({ deployedUrl: live }))
    const body = await registry.markDeployed('a1', live, deps(fetchImpl))
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toEqual({ deployedUrl: live })
    expect(body.deployedUrl).toBe(live)
  })

  it.each([['', 'blank'], [undefined, 'omitted']])(
    'sends NO deployedUrl key when the url is %s (%s) — the server keeps the recorded one',
    async (url) => {
      // A bare `{}` is the wire shape AND the "leave the URL alone" signal.
      // Sending `deployedUrl: ''`/`null` instead would 422 (or blank a live link).
      const fetchImpl = vi.fn(async () => marked())
      await registry.markDeployed('a1', url, deps(fetchImpl))
      expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toEqual({})
    },
  )

  it('surfaces the server 422 for a non-https url rather than pre-checking it', async () => {
    const fetchImpl = vi.fn(async () => fail(422, { error: { message: 'URL scheme should be https' } }))
    const err = await registry.markDeployed('a1', 'http://nope.example.com', deps(fetchImpl)).catch((e) => e)
    expect(err.status).toBe(422)
  })
})

describe('deleteApp', () => {
  /**
   * ★ THE REQUEST SHAPE, not a mock of it.
   *
   * `DELETE /v1/admin/apps/{id}` REQUIRES a word-bounded reason. This client used to send
   * `{ method: 'DELETE' }` with no body while the route already required one, so every admin
   * delete through the SPA answered 422 — and the panel suite never caught it, because it
   * mocks `deleteApp` wholesale. Both sides were green while disagreeing. This test is the
   * one that would have failed.
   */
  it('sends the administrator’s reason as a JSON body', async () => {
    let seen = null
    const fetchImpl = vi.fn(async (url, init) => { seen = { url, init }; return ok({ ok: true }) })

    await registry.deleteApp('app-7', 'Duplicate app created in error, owner asked for removal', deps(fetchImpl))

    expect(seen.url).toContain('/api/admin/apps/app-7')
    expect(seen.init.method).toBe('DELETE')
    // A body on a DELETE, mirroring `deleteProject` — RFC 9110 leaves it undefined but nginx
    // and the ingress both forward it, and the admin SPA is the only client.
    expect(JSON.parse(seen.init.body)).toEqual({
      reason: 'Duplicate app created in error, owner asked for removal',
    })
    expect(seen.init.headers['Content-Type']).toBe('application/json')
  })

  it('surfaces the server’s refusal rather than swallowing it', async () => {
    const fetchImpl = vi.fn(async () => fail(422, { detail: 'Say why in 2 to 50 words.' }))
    await expect(registry.deleteApp('app-7', 'too short', deps(fetchImpl))).rejects.toThrow()
  })
})
