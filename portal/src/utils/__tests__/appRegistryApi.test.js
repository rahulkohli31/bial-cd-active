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
            liveVersion: { number: 4, commitSha: 'f0e1d2c3b4', since: '2026-09-25T16:40:00Z' },
          },
          { appId: 'a2', status: 'pending', registryStatus: 'waiting_for_review', liveVersion: null },
        ],
        truncated: true,
      }),
    )

    const list = await registry.listApps(deps(fetchImpl))

    expect(list.truncated).toBe(true)
    expect(list.apps.map((a) => [a.registryStatus, a.liveVersion])).toEqual([
      ['live', { number: 4, commitSha: 'f0e1d2c3b4', since: '2026-09-25T16:40:00Z' }],
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

describe('fetchHistory', () => {
  it('asks for one app’s history and keeps each version and event it can place', async () => {
    const fetchImpl = vi.fn(async () =>
      ok({
        entries: [
          {
            kind: 'version',
            number: 2,
            commitSha: 'c09a4e1',
            submissionId: 'sub-2',
            sentAt: '2026-09-15T10:10:00Z',
            sentBy: 'kavya.n@bialairport.com',
            declaration: { commits: { shipping: 'c09a4e1' } },
            decision: { kind: 'rejected', by: 'admin@bial.com', at: '2026-09-16T04:30:00Z', note: 'Remove the passport field.' },
            attempts: [{ status: 'failed', startedAt: '2026-09-15T10:10:00Z', finishedAt: null, failureCode: 'build_failed' }],
            state: 'rejected',
            publishedAt: null,
            replacedBy: null,
            replacedAt: null,
          },
          { kind: 'event', action: 'disable', at: '2026-09-13T13:10:00Z', by: 'admin@bial.com', reenabledAt: '2026-09-14T03:32:00Z' },
          { kind: 'version', sentAt: '2026-09-01T00:00:00Z' },
          { kind: 'mystery' },
        ],
        live: { number: 1, commitSha: '2e77b10', since: '2026-09-05T09:32:00Z' },
        liveUrl: 'https://pub.example/app',
        truncated: true,
      }),
    )

    const history = await registry.fetchHistory('a/1', deps(fetchImpl))

    expect(fetchImpl.mock.calls[0][0]).toBe('/api/admin/apps/a%2F1/history')
    expect(history.entries.map((entry) => entry.kind)).toEqual(['version', 'event'])
    expect(history.entries[0].decision.note).toBe('Remove the passport field.')
    expect(history.entries[0].attempts[0].failureCode).toBe('build_failed')
    expect(history.entries[1].reenabledAt).toBe('2026-09-14T03:32:00Z')
    expect(history.live).toEqual({ number: 1, commitSha: '2e77b10', since: '2026-09-05T09:32:00Z' })
    expect(history.liveUrl).toBe('https://pub.example/app')
    expect(history.truncated).toBe(true)
  })

  it('reads an unknown state or decision as not recorded, never as something it is not', async () => {
    const fetchImpl = vi.fn(async () =>
      ok({ entries: [{ kind: 'version', number: 1, sentAt: '2026-09-01T00:00:00Z', state: 'teleported', decision: { kind: 'vibes' } }] }),
    )

    const history = await registry.fetchHistory('a1', deps(fetchImpl))

    expect(history.entries[0].state).toBe('not_recorded')
    expect(history.entries[0].decision.kind).toBe('not_recorded')
    expect(history.live).toBeNull()
    expect(history.truncated).toBe(false)
  })

  it('no longer exports the audit list it replaced', () => {
    expect(registry.fetchAudit).toBeUndefined()
    expect(typeof registry.fetchHistory).toBe('function')
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
