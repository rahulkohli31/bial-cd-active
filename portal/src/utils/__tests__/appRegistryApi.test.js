/**
 * The owner-facing half of the app registry, where the appId-vs-conversationId split
 * lives. Two contracts pinned here:
 *
 *  - `provisionApp` takes BOTH ids and returns a FRESH appId. It used to take the
 *    conversation id positionally and claim the appId "IS that conversation's uuid".
 *  - `getAppStatus` maps 404 → `{status: null}`. The backend used to say "not provisioned"
 *    with `200 {status:null}`; it now says it with a 404, and the shared asJson helper
 *    throws on any non-2xx.
 */
import { describe, it, expect, vi } from 'vitest'
import { provisionApp, getAppStatus } from '../appRegistryApi.js'
import { ApiError } from '../apiError'

const deps = (fetchImpl) => ({ fetchImpl, getToken: () => null, refresh: vi.fn() })

// authFetch peeks a 403's body through res.clone(), so a faked Response must be cloneable.
const res = (init) => ({ ...init, clone: () => res(init) })
const ok = (json) => res({ ok: true, status: 200, json: async () => json })
const fail = (status, json) => res({ ok: false, status, json: async () => json })

describe('provisionApp', () => {
  it('POSTs both conversationId and projectId', async () => {
    const fetchImpl = vi.fn(async () => ok({ appId: 'app-1', appKey: 'k', status: 'draft', loginRequired: false }))
    const reg = await provisionApp({ conversationId: 'conv-1', projectId: 'proj-1' }, deps(fetchImpl))

    const [url, opts] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/apps/provision')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ conversationId: 'conv-1', projectId: 'proj-1' })
    expect(reg.appId).toBe('app-1')
  })

  it('returns a FRESH appId — never the conversation id it was given', async () => {
    const fetchImpl = vi.fn(async () => ok({ appId: 'a-fresh-uuid', appKey: 'k', status: 'draft', loginRequired: false }))
    const reg = await provisionApp({ conversationId: 'conv-1', projectId: 'proj-1' }, deps(fetchImpl))
    expect(reg.appId).not.toBe('conv-1')
    expect(reg.appId).toBe('a-fresh-uuid')
  })

  it('is idempotent per project — a second call returns the SAME appId', async () => {
    const fetchImpl = vi.fn(async () => ok({ appId: 'app-1', appKey: 'k', status: 'draft', loginRequired: false }))
    const first = await provisionApp({ conversationId: 'conv-1', projectId: 'proj-1' }, deps(fetchImpl))
    const second = await provisionApp({ conversationId: 'conv-2', projectId: 'proj-1' }, deps(fetchImpl))
    expect(second.appId).toBe(first.appId) // "more chats, one app"
  })

  it('throws ApiError 404 when the project is not owned', async () => {
    const fetchImpl = vi.fn(async () => fail(404, { error: { message: 'Project not found.' } }))
    const err = await provisionApp({ conversationId: 'c', projectId: 'p' }, deps(fetchImpl)).catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(404)
    expect(err.message).toBe('Project not found.')
  })

  it('throws ApiError 409 when the project’s app belongs to another user', async () => {
    const fetchImpl = vi.fn(async () => fail(409, { error: { message: 'App owned by another user.' } }))
    const err = await provisionApp({ conversationId: 'c', projectId: 'p' }, deps(fetchImpl)).catch((e) => e)
    expect(err.status).toBe(409)
  })

  it('throws ApiError 422 with the Pydantic field message when projectId is missing', async () => {
    const fetchImpl = vi.fn(async () => fail(422, { detail: [{ msg: 'Input should be a valid UUID' }] }))
    const err = await provisionApp({ conversationId: 'c', projectId: undefined }, deps(fetchImpl)).catch((e) => e)
    expect(err.status).toBe(422)
    expect(err.message).toContain('valid UUID')
  })
})

describe('getAppStatus', () => {
  it('returns the status envelope for a provisioned app', async () => {
    const fetchImpl = vi.fn(async () => ok({ appId: 'app-1', status: 'draft', appKey: 'k', loginRequired: false, rejectionNote: null }))
    const s = await getAppStatus('app-1', deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/apps/app-1/status')
    expect(s.status).toBe('draft')
  })

  it('resolves 404 to { status: null } — "not provisioned" is a normal answer, not an error', async () => {
    const fetchImpl = vi.fn(async () => fail(404, { error: { message: 'App not found.' } }))
    await expect(getAppStatus('app-1', deps(fetchImpl))).resolves.toEqual({ status: null })
  })

  it('still throws on 401 and on 500 — those are real failures', async () => {
    const unauthorized = vi.fn(async () => fail(401, { detail: 'Not authenticated' }))
    await expect(getAppStatus('app-1', deps(unauthorized))).rejects.toBeInstanceOf(ApiError)

    const boom = vi.fn(async () => fail(500, { detail: 'Internal Server Error' }))
    await expect(getAppStatus('app-1', deps(boom))).rejects.toBeInstanceOf(ApiError)
  })
})
