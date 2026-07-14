import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  start,
  stop,
  getStatus,
  acquireLock,
  renewLock,
  releaseLock,
  forceEnd,
  heartbeat,
  BuildSessionAlreadyActiveError,
} from '../buildSessionApi'
import { ApiError } from '../apiError'

/**
 * A fake `Response` for the injected `fetchImpl`. `json()` is re-callable (unlike a
 * real body) and `clone()` returns itself, so `authFetch`'s 403 suspension probe can
 * read the clone while the client still reads the original.
 */
function res(status: number, body: unknown): Response {
  const ok = status >= 200 && status < 300
  const response = { ok, status, json: async () => body, clone: () => response }
  return response as unknown as Response
}

type FetchImpl = (url: string, opts?: RequestInit) => Promise<Response>

/** A typed `fetchImpl` mock so `.mock.calls[n][1]` narrows to `RequestInit`. */
function jsonFetch(status: number, body: unknown) {
  return vi.fn<FetchImpl>(async () => res(status, body))
}

const CSRF = 'csrf-tok-123'

beforeEach(() => {
  document.cookie = `csrf=${CSRF}`
})

function optsOf(m: ReturnType<typeof jsonFetch>, call = 0): RequestInit {
  return m.mock.calls[call][1] ?? {}
}

function headerOf(m: ReturnType<typeof jsonFetch>, name: string, call = 0): string | undefined {
  return (optsOf(m, call).headers as Record<string, string> | undefined)?.[name]
}

describe('buildSessionApi — control operations (C3 §2)', () => {
  it('start: 201 maps {sessionId, projectId, appId, status:provisioning, previewUrl:null, createdAt} (C3 §2.1)', async () => {
    const fetchImpl = jsonFetch(201, {
      sessionId: 's1',
      projectId: 'p1',
      appId: 'a1',
      status: 'provisioning',
      previewUrl: null,
      createdAt: '2026-07-14T00:00:00Z',
    })
    const out = await start({ projectId: 'p1', prompt: 'build a CRUD app' }, { fetchImpl })

    expect(out).toEqual({
      sessionId: 's1',
      projectId: 'p1',
      appId: 'a1',
      status: 'provisioning',
      previewUrl: null,
      createdAt: '2026-07-14T00:00:00Z',
    })
    // The mutating POST carries the CSRF header (C3 §3, KTD-2) and the JSON body.
    expect(headerOf(fetchImpl, 'X-CSRF-Token')).toBe(CSRF)
    expect(JSON.parse(optsOf(fetchImpl).body as string)).toEqual({ projectId: 'p1', prompt: 'build a CRUD app' })
    expect(optsOf(fetchImpl).method).toBe('POST')
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/build-sessions')
  })

  it('start: a 409 build_session_already_active surfaces the existing sessionId as a typed error (C3 §2.1/§6)', async () => {
    const fetchImpl = jsonFetch(409, { error: { code: 'build_session_already_active', message: 'You already have a build running.' }, sessionId: 'existing-9' })
    const err = await start({ projectId: 'p1', prompt: 'x' }, { fetchImpl }).catch((e: unknown) => e)

    expect(err).toBeInstanceOf(BuildSessionAlreadyActiveError)
    expect(err).toBeInstanceOf(ApiError)
    const active = err as BuildSessionAlreadyActiveError
    expect(active.status).toBe(409)
    expect(active.code).toBe('build_session_already_active')
    expect(active.existingSessionId).toBe('existing-9')
  })

  it('start: reads the existing sessionId whether it sits at top-level or under error{}', async () => {
    const fetchImpl = jsonFetch(409, { error: { code: 'build_session_already_active', message: 'busy', sessionId: 'nested-42' } })
    const err = await start({ projectId: 'p1', prompt: 'x' }, { fetchImpl }).catch((e: unknown) => e)
    expect((err as BuildSessionAlreadyActiveError).existingSessionId).toBe('nested-42')
  })

  it('getStatus: previewUrl is null before ready and the stable URL once ready; lastSeq present after the first envelope (C3 §2.3)', async () => {
    const before = jsonFetch(200, { sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'building', previewUrl: null, lastSeq: 3, createdAt: 'c', updatedAt: 'u' })
    const b = await getStatus('s1', { fetchImpl: before })
    expect(b.previewUrl).toBeNull()
    expect(b.status).toBe('building')
    expect(b.lastSeq).toBe(3)

    const READY_URL = 'https://app.example.azurecontainerapps.io/'
    const after = jsonFetch(200, { sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'ready', previewUrl: READY_URL, lastSeq: 7, createdAt: 'c', updatedAt: 'u' })
    const a = await getStatus('s1', { fetchImpl: after })
    expect(a.previewUrl).toBe(READY_URL)
    expect(a.status).toBe('ready')

    // A safe GET carries NO CSRF token and no method override (C3 §3).
    expect(headerOf(after, 'X-CSRF-Token')).toBeUndefined()
    expect(optsOf(after).method).toBeUndefined()
  })
})

describe('buildSessionApi — CSRF discipline (C3 §3, KTD-2)', () => {
  it('attaches X-CSRF-Token on every mutating POST (start / stop / all lock ops / heartbeat)', async () => {
    const stopImpl = jsonFetch(200, { sessionId: 's', status: 'ended' })
    await stop('s', {}, { fetchImpl: stopImpl })
    expect(headerOf(stopImpl, 'X-CSRF-Token')).toBe(CSRF)

    const lockBody = { sessionId: 's', held: true, ownerUserId: 'u', ttlSeconds: 900, expiresAt: 'e' }
    const cases: Array<[(id: string, deps: { fetchImpl: FetchImpl }) => Promise<unknown>, unknown]> = [
      [acquireLock, lockBody],
      [renewLock, lockBody],
      [releaseLock, { sessionId: 's', released: true }],
      [forceEnd, { sessionId: 's', status: 'ended' }],
      [heartbeat, { sessionId: 's', alive: true, cadenceSeconds: 30, heartbeatExpiresAt: 'e' }],
    ]
    for (const [fn, body] of cases) {
      const impl = jsonFetch(200, body)
      await fn('s', { fetchImpl: impl })
      expect(headerOf(impl, 'X-CSRF-Token')).toBe(CSRF)
      expect(optsOf(impl).method).toBe('POST')
    }
  })

  it('omits the CSRF header when no csrf cookie is readable (parity with auth.js)', async () => {
    document.cookie = 'csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
    const impl = jsonFetch(200, { sessionId: 's', status: 'ended' })
    await stop('s', {}, { fetchImpl: impl })
    expect(headerOf(impl, 'X-CSRF-Token')).toBeUndefined()
  })
})

describe('buildSessionApi — lock ops + fail-closed errors (C3 §3)', () => {
  it('stop: sends {reason} when supplied, and a valid empty StopBuildRequest {} otherwise', async () => {
    const withReason = jsonFetch(200, { sessionId: 's', status: 'ended' })
    await stop('s', { reason: 'user cancelled' }, { fetchImpl: withReason })
    expect(JSON.parse(optsOf(withReason).body as string)).toEqual({ reason: 'user cancelled' })

    // A bare stop still carries a body — {} is a complete StopBuildRequest (reason
    // defaults to None), so it always satisfies the body model (C3 §2.2). The lock
    // ops, by contrast, take NO body (asserted below via the absent Content-Type).
    const noReason = jsonFetch(200, { sessionId: 's', status: 'ended' })
    await stop('s', {}, { fetchImpl: noReason })
    expect(JSON.parse(optsOf(noReason).body as string)).toEqual({})
  })

  it('lock ops take NO request body (C3 §3) — release sends neither body nor Content-Type', async () => {
    const impl = jsonFetch(200, { sessionId: 's', released: true })
    await releaseLock('s', { fetchImpl: impl })
    expect(optsOf(impl).body).toBeUndefined()
    expect(headerOf(impl, 'Content-Type')).toBeUndefined()
  })

  it('forceEnd: a 403 build_session_forbidden is surfaced fail-closed, not swallowed (C3 §3.4)', async () => {
    const fetchImpl = jsonFetch(403, { error: { code: 'build_session_forbidden', message: 'Not the owner.' } })
    const err = await forceEnd('s', { fetchImpl }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(403)
    expect((err as ApiError).code).toBe('build_session_forbidden')
  })

  it('renew: a 409 build_session_lock_lost is surfaced as a typed ApiError, NOT the already-active subclass (C3 §3.2)', async () => {
    const fetchImpl = jsonFetch(409, { error: { code: 'build_session_lock_lost', message: 'Lock lost.' } })
    const err = await renewLock('s', { fetchImpl }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(409)
    expect((err as ApiError).code).toBe('build_session_lock_lost')
    expect(err).not.toBeInstanceOf(BuildSessionAlreadyActiveError)
  })

  it('heartbeat: a 404 (session not owned) is surfaced, not swallowed (C3 §3.5)', async () => {
    const fetchImpl = jsonFetch(404, { detail: 'Not found' })
    const err = await heartbeat('s', { fetchImpl }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(404)
  })

  it('a malformed success body fails at the boundary rather than corrupting state downstream', async () => {
    const fetchImpl = jsonFetch(200, { status: 'ready' }) // no sessionId
    const err = await getStatus('s', { fetchImpl }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(500)
  })
})
