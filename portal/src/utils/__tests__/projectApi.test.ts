import { describe, it, expect, vi } from 'vitest'
import {
  listProjects,
  getProject,
  createProject,
  patchProject,
  deleteProject,
  generateDescription,
} from '../projectApi'
import { ApiError } from '../apiError'

// A real WHATWG Response so `res.ok` / `res.status` / `res.json()` behave exactly
// as production fetch would — no hand-rolled stub, no casts.
const jsonResponse = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

// A fetch mock typed to fetch's own signature, so `.mock.calls[i][1]` is a RequestInit.
const fetchReturning = (status: number, body: unknown) =>
  vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => jsonResponse(status, body))

// authFetch deps injection — no real token/network (mirrors conversationApi.test.js).
// getToken returns null: the cookie-session model carries no client bearer token.
const deps = (fetchImpl: typeof fetch) => ({ fetchImpl, getToken: () => null, refresh: async () => false })

const parseBody = (init: RequestInit | undefined): unknown => JSON.parse(String(init?.body))

const sampleProject = {
  id: 'p1',
  name: 'VIP Movement',
  description: null,
  appId: null,
  appStatus: null,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
}

describe('listProjects', () => {
  it('GETs /api/projects with NO query string when no args are passed', async () => {
    const fetchImpl = fetchReturning(200, { items: [sampleProject], page: 1, pageSize: 25, total: 1, totalPages: 1 })
    const page = await listProjects({}, deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/projects')
    // N7: the wire sample carries no `hasRelaunchableSnapshot` — the list never computes one —
    // and the narrower defaults it to `null`, the "cannot say" answer that withholds the claim.
    expect(page).toEqual({
      items: [{ ...sampleProject, hasRelaunchableSnapshot: null, isServing: false }],
      page: 1,
      pageSize: 25,
      total: 1,
      totalPages: 1,
    })
  })

  it('URL-encodes page, limit, and q into the query string', async () => {
    const fetchImpl = fetchReturning(200, { items: [], page: 1, pageSize: 25, total: 0, totalPages: 0 })
    await listProjects({ page: 3, limit: 50, q: 'vip movement' }, deps(fetchImpl))
    const url = String(fetchImpl.mock.calls[0][0])
    expect(url.startsWith('/api/projects?')).toBe(true)
    expect(url).toContain('page=3')
    expect(url).toContain('limit=50')
    expect(url).toContain('q=vip+movement')
  })

  it('normalizes an empty envelope to a safe page shape', async () => {
    const fetchImpl = fetchReturning(200, {})
    const page = await listProjects({}, deps(fetchImpl))
    expect(page).toEqual({ items: [], page: 1, pageSize: 25, total: 0, totalPages: 0 })
  })
})

describe('getProject', () => {
  it('returns the narrowed project', async () => {
    const fetchImpl = fetchReturning(200, { ...sampleProject, appId: 'a1', appStatus: 'approved' })
    const project = await getProject('p1', deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/projects/p1')
    expect(project.appId).toBe('a1')
    expect(project.appStatus).toBe('approved')
  })

  it('throws an ApiError carrying status 404 and the server message on not-found', async () => {
    const fetchImpl = fetchReturning(404, { error: { message: 'Project not found.' } })
    const err = await getProject('missing', deps(fetchImpl)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(404)
    expect((err as ApiError).message).toBe('Project not found.')
  })
})

describe('createProject', () => {
  it('POSTs a body with name and NO description key when description is omitted', async () => {
    const fetchImpl = fetchReturning(201, { ...sampleProject })
    await createProject({ name: 'VIP Movement' }, deps(fetchImpl))
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/projects')
    expect(init?.method).toBe('POST')
    const body = parseBody(init)
    expect(body).toEqual({ name: 'VIP Movement' })
    expect(Object.prototype.hasOwnProperty.call(body, 'description')).toBe(false)
  })

  it('surfaces the FastAPI 422 field message, not the generic fallback', async () => {
    const fetchImpl = fetchReturning(422, {
      detail: [{ type: 'string_too_long', loc: ['body', 'name'], msg: 'String should have at most 120 characters' }],
    })
    const longName = 'x'.repeat(130)
    const err = await createProject({ name: longName }, deps(fetchImpl)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(422)
    expect((err as ApiError).message).toContain('at most 120 characters')
    expect((err as ApiError).message).not.toContain('Failed to create project')
  })
})

describe('patchProject', () => {
  it('serializes description:null exactly (clear the field)', async () => {
    const fetchImpl = fetchReturning(200, { ...sampleProject })
    await patchProject('p1', { description: null }, deps(fetchImpl))
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/projects/p1')
    expect(init?.method).toBe('PATCH')
    expect(String(init?.body)).toBe('{"description":null}')
  })

  it('sends only name when only name is patched (no description key)', async () => {
    const fetchImpl = fetchReturning(200, { ...sampleProject, name: 'x' })
    await patchProject('p1', { name: 'x' }, deps(fetchImpl))
    expect(String(fetchImpl.mock.calls[0][1]?.body)).toBe('{"name":"x"}')
  })

  it('preserves an empty-string description (server maps "" -> null)', async () => {
    const fetchImpl = fetchReturning(200, { ...sampleProject })
    await patchProject('p1', { description: '' }, deps(fetchImpl))
    expect(String(fetchImpl.mock.calls[0][1]?.body)).toBe('{"description":""}')
  })
})

describe('deleteProject', () => {
  it('DELETEs, carries the reason in the body, and returns {ok:true}', async () => {
    const fetchImpl = fetchReturning(200, { ok: true })

    const result = await deleteProject('p1', 'Superseded by the new gate pass tool', deps(fetchImpl))

    const init = fetchImpl.mock.calls[0][1]
    expect(init?.method).toBe('DELETE')
    // The server requires it (#158 §13.2) and refuses independently of the dialog, so the
    // client sending it is not optional — a DELETE with no body is a 422.
    expect(JSON.parse(String(init?.body))).toEqual({
      remark: 'Superseded by the new gate pass tool',
    })
    expect(result).toEqual({ ok: true })
  })
})

describe('generateDescription', () => {
  it('POSTs an empty body to the :generate endpoint', async () => {
    const fetchImpl = fetchReturning(200, { ...sampleProject, description: 'Auto text', appStatus: 'approved' })
    const project = await generateDescription('p1', deps(fetchImpl))
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/projects/p1/description:generate')
    expect(init?.method).toBe('POST')
    expect(init?.body).toBeUndefined()
    expect(project.description).toBe('Auto text')
  })

  it('throws ApiError status 409 when there is no code to describe yet', async () => {
    const fetchImpl = fetchReturning(409, { error: { message: 'No app code yet.' } })
    const err = await generateDescription('p1', deps(fetchImpl)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(409)
  })

  it('exposes the daily-limit code on the 429 envelope', async () => {
    const fetchImpl = fetchReturning(429, {
      error: { message: 'Daily token limit reached.', code: 'daily_token_limit_exceeded' },
    })
    const err = await generateDescription('p1', deps(fetchImpl)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).code).toBe('daily_token_limit_exceeded')
  })

  it('throws ApiError status 503 when generation is not configured', async () => {
    const fetchImpl = fetchReturning(503, { error: { message: 'Description generation is unavailable.' } })
    const err = await generateDescription('p1', deps(fetchImpl)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(503)
  })
})

describe('response narrowing (parse, do not validate)', () => {
  it('throws rather than coerce a project with no id into id: ""', async () => {
    const fetchImpl = fetchReturning(200, { name: 'No id here' })
    const err = await getProject('p1', deps(fetchImpl)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toMatch(/could not read/i)
  })

  it('throws when a page carries an unreadable project', async () => {
    const fetchImpl = fetchReturning(200, { items: [sampleProject, { name: 'broken' }], page: 1, pageSize: 25, total: 2, totalPages: 1 })
    await expect(listProjects({}, deps(fetchImpl))).rejects.toBeInstanceOf(ApiError)
  })

  it('tolerates absent description/appId/appStatus — each has a defined "none yet" meaning', async () => {
    const fetchImpl = fetchReturning(200, { id: 'p1', name: 'Bare' })
    const project = await getProject('p1', deps(fetchImpl))
    expect(project).toMatchObject({ id: 'p1', name: 'Bare', description: null, appId: null, appStatus: null })
  })

  it('maps an unknown appStatus to null rather than trusting it', async () => {
    const fetchImpl = fetchReturning(200, { ...sampleProject, appStatus: 'exploded' })
    expect((await getProject('p1', deps(fetchImpl))).appStatus).toBeNull()
  })
})
