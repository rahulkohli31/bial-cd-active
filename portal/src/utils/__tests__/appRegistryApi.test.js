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

describe('listApps', () => {
  it('asks for every app, with no status filter', async () => {
    const fetchImpl = vi.fn(async () => ok({ apps: [], truncated: false }))
    await registry.listApps(deps(fetchImpl))

    expect(fetchImpl.mock.calls[0][0]).toBe('/api/admin/apps')
  })

  it('reads each row’s registry status and live version, and whether the list was cut short', async () => {
    const fetchImpl = vi.fn(async () =>
      ok({
        apps: [
          {
            appId: 'a1',
            status: 'approved',
            registryStatus: 'live',
            liveVersion: { commitSha: 'f0e1d2c3b4', since: '2026-09-25T16:40:00Z' },
          },
          { appId: 'a2', status: 'pending', registryStatus: 'waiting_for_review', liveVersion: null },
        ],
        truncated: true,
      }),
    )

    const list = await registry.listApps(deps(fetchImpl))

    expect(list.truncated).toBe(true)
    expect(list.apps.map((a) => [a.registryStatus, a.liveVersion])).toEqual([
      ['live', { commitSha: 'f0e1d2c3b4', since: '2026-09-25T16:40:00Z' }],
      ['waiting_for_review', null],
    ])
  })

  it('reads an unknown registry status as a draft, and a missing flag as a whole list', async () => {
    const fetchImpl = vi.fn(async () => ok({ apps: [{ appId: 'a1', registryStatus: 'teleported' }] }))

    const list = await registry.listApps(deps(fetchImpl))

    expect(list.apps[0].registryStatus).toBe('draft')
    expect(list.apps[0].liveVersion).toBeNull()
    expect(list.truncated).toBe(false)
  })
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
