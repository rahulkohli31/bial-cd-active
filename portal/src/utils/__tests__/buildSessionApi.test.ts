import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  start,
  relaunchPreview,
  stop,
  getStatus,
  acquireLock,
  renewLock,
  releaseLock,
  forceEnd,
  heartbeat,
  BuildSessionAlreadyActiveError,
  asReclaimBlocked,
  releaseProject,
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

  it('start: sends conversationId when supplied, so the server can ground the build in the thread\'s attachments (R3)', async () => {
    const fetchImpl = jsonFetch(201, { sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning', previewUrl: null, createdAt: '2026-07-14T00:00:00Z' })

    await start({ projectId: 'p1', prompt: 'build a dashboard', conversationId: 'c9' }, { fetchImpl })

    expect(JSON.parse(optsOf(fetchImpl).body as string)).toEqual({ projectId: 'p1', prompt: 'build a dashboard', conversationId: 'c9' })
  })

  it('start: OMITS conversationId entirely when absent — the field is an additive amendment to the frozen C3 body (R3)', async () => {
    const fetchImpl = jsonFetch(201, { sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning', previewUrl: null, createdAt: '2026-07-14T00:00:00Z' })

    await start({ projectId: 'p1', prompt: 'x' }, { fetchImpl })

    const body = JSON.parse(optsOf(fetchImpl).body as string)
    expect('conversationId' in body).toBe(false) // not even as an explicit null
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

  it('relaunchPreview: 200 maps {appId, previewUrl, status} — no sessionId/createdAt on this shape (Decision 6, #43)', async () => {
    const READY_URL = 'https://app.example.azurecontainerapps.io/'
    const fetchImpl = jsonFetch(200, { appId: 'a1', previewUrl: READY_URL, status: 'ready' })
    const out = await relaunchPreview({ projectId: 'p1' }, { fetchImpl })

    // Two absent fields, two DIFFERENT defaults, and the asymmetry is the point.
    // `restoredFromFailedBuild` absent reads FALSE — the label is an aid, not a gate, so silence
    // claims nothing. `ready` absent reads TRUE — every server predating that field only ever
    // replied once the app was serving, so defaulting it false would paint a permanent
    // "not ready yet" over correct responses.
    expect(out).toEqual({
      appId: 'a1',
      previewUrl: READY_URL,
      status: 'ready',
      restoredFromFailedBuild: false,
      ready: true,
    })
    // A mutating POST: carries the CSRF header (KTD-2) and the projectId body to the relaunch route.
    expect(headerOf(fetchImpl, 'X-CSRF-Token')).toBe(CSRF)
    expect(JSON.parse(optsOf(fetchImpl).body as string)).toEqual({ projectId: 'p1' })
    expect(optsOf(fetchImpl).method).toBe('POST')
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/build-sessions/relaunch')
  })

  it('relaunchPreview: carries `ready: false` through — a framable URL that is not serving yet (R6/SL-20)', async () => {
    // The attach arm now hands back the live container's URL even when the app has not answered
    // within its readiness budget, because the alternative — condemning the container — rolled a
    // citizen back to their last save. The pane needs to be able to tell those two apart, so a
    // `false` here must survive decoding rather than being flattened by the absent-reads-true
    // default that protects older servers.
    const URL_NOT_SERVING = 'https://app.example.azurecontainerapps.io/'
    const fetchImpl = jsonFetch(200, {
      appId: 'a1',
      previewUrl: URL_NOT_SERVING,
      status: 'provisioning',
      restoredFromFailedBuild: false,
      ready: false,
    })

    const out = await relaunchPreview({ projectId: 'p1' }, { fetchImpl })

    expect(out.ready).toBe(false)
    expect(out.previewUrl).toBe(URL_NOT_SERVING) // still framable — that is the whole point
    expect(out.status).toBe('provisioning') // …and `status` does not claim READY over it
  })

  it('relaunchPreview: the wire restoredFromFailedBuild=true survives the mapping (U6/F1)', async () => {
    const fetchImpl = jsonFetch(200, {
      appId: 'a1', previewUrl: 'https://x.example/', status: 'ready', restoredFromFailedBuild: true,
    })
    const out = await relaunchPreview({ projectId: 'p1' }, { fetchImpl })
    expect(out.restoredFromFailedBuild).toBe(true)
  })

  it('relaunchPreview: a malformed success body fails at the boundary (parity with start)', async () => {
    // A non-object body trips the mapper's isRecord guard.
    const nonObject = jsonFetch(200, 'not a preview')
    const err = await relaunchPreview({ projectId: 'p1' }, { fetchImpl: nonObject }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(500)

    // A status outside the known lifecycle fails closed rather than rendering an undefined state.
    const unknownStatus = jsonFetch(200, { appId: 'a1', previewUrl: 'u', status: 'warp-speed' })
    const statusErr = await relaunchPreview({ projectId: 'p1' }, { fetchImpl: unknownStatus }).catch((e: unknown) => e)
    expect(statusErr).toBeInstanceOf(ApiError)
    expect((statusErr as ApiError).status).toBe(500)
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

  it('a session body with NO projectId fails at the boundary — it drives the 409 reattach/block routing (finding #31)', async () => {
    // getStatus: the projectId comparison is the reattach-vs-block gate.
    const statusImpl = jsonFetch(200, { sessionId: 's1', appId: 'a1', status: 'ready', previewUrl: null, lastSeq: 1, createdAt: 'c', updatedAt: 'u' })
    const statusErr = await getStatus('s1', { fetchImpl: statusImpl }).catch((e: unknown) => e)
    expect(statusErr).toBeInstanceOf(ApiError)
    expect((statusErr as ApiError).status).toBe(500)

    // start: the created session's projectId anchors the same routing on later turns.
    const startImpl = jsonFetch(201, { sessionId: 's1', appId: 'a1', status: 'provisioning', previewUrl: null, createdAt: 'c' })
    const startErr = await start({ projectId: 'p1', prompt: 'x' }, { fetchImpl: startImpl }).catch((e: unknown) => e)
    expect(startErr).toBeInstanceOf(ApiError)
    expect((startErr as ApiError).status).toBe(500)
  })
})

describe('asReclaimBlocked (#83)', () => {
  it('reads the occupying project off the 409', () => {
    const err = { code: 'sandbox_reclaim_blocked', details: { projectId: 'p-a', projectName: 'Lost & Found', dirty: true } }
    expect(asReclaimBlocked(err)).toEqual({ projectId: 'p-a', projectName: 'Lost & Found', dirty: true, building: false })
  })

  it('keeps dirty TRI-STATE — a non-boolean is unknown, never clean', () => {
    const err = { code: 'sandbox_reclaim_blocked', details: { projectId: 'p-a', projectName: 'A', dirty: null } }
    expect(asReclaimBlocked(err)?.dirty).toBeNull()
  })

  it('ignores the OTHER 409 — a running build has no remedy the user can act on', () => {
    // Branching on the status alone would offer a Save button for a build that is simply
    // still going, which is not a choice the user has.
    const err = { code: 'build_session_already_active', details: { sessionId: 's-1' } }
    expect(asReclaimBlocked(err)).toBeNull()
  })

  it('is STRUCTURAL, so it works on both error types the refusal arrives as', () => {
    // ApiError from relaunchPreview, TurnStartError from startTurn — same {code, details}.
    class TurnStartErrorLike extends Error {
      code = 'sandbox_reclaim_blocked'
      details = { projectId: 'p-b', projectName: 'Roster', dirty: false }
    }
    expect(asReclaimBlocked(new TurnStartErrorLike())?.projectId).toBe('p-b')
  })

  it('declines a malformed body rather than rendering an unnamed project', () => {
    expect(asReclaimBlocked({ code: 'sandbox_reclaim_blocked', details: { projectId: 'p-a' } })).toBeNull()
    expect(asReclaimBlocked(null)).toBeNull()
    expect(asReclaimBlocked(new Error('boom'))).toBeNull()
  })

  // THE WIRE, END TO END — the tests above hand-build `{code, details}` and so would all
  // still pass if `postJson` stopped carrying `details` at all, which is exactly the
  // regression this feature already shipped once: the relaunch button rendered the raw error
  // text instead of the dialog, because `readApiError` populated `details` and `postJson`
  // dropped it on the floor. Drive the real 409 envelope through the real client instead.
  //
  // The body below is `reclaim_blocked_response`'s output verbatim (`live_build.py`) — a flat
  // `error` object, NOT a nested `details` key. If the backend ever reshapes it, this goes red
  // on the same commit rather than in someone's browser.
  const WIRE_409 = {
    error: {
      message: '“Lost & Found” is still open and has unsaved changes.',
      code: 'sandbox_reclaim_blocked',
      projectId: 'p-a',
      projectName: 'Lost & Found',
      dirty: true,
    },
  }

  it('survives the round trip through postJson: relaunchPreview 409 → ReclaimBlocked', async () => {
    const fetchImpl = jsonFetch(409, WIRE_409)
    const err = await relaunchPreview({ projectId: 'p-b' }, { fetchImpl }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(asReclaimBlocked(err)).toEqual({
      projectId: 'p-a',
      projectName: 'Lost & Found',
      dirty: true,
      building: false,
    })
  })

  it('survives the round trip through postJson: releaseProject 409', async () => {
    // The release route can 409 too — a build genuinely running for this user — and the same
    // envelope has to reach the dialog rather than the raw message.
    const fetchImpl = jsonFetch(409, WIRE_409)
    const err = await releaseProject('p-b', { fetchImpl }).catch((e: unknown) => e)
    expect(asReclaimBlocked(err)?.projectName).toBe('Lost & Found')
  })

  it('carries dirty=null through the wire as unknown, not clean', async () => {
    const fetchImpl = jsonFetch(409, {
      error: { ...WIRE_409.error, dirty: null, message: '“A” is still open and may have unsaved changes.' },
    })
    const err = await relaunchPreview({ projectId: 'p-b' }, { fetchImpl }).catch((e: unknown) => e)
    expect(asReclaimBlocked(err)?.dirty).toBeNull()
  })
})

describe('asReclaimBlocked — a project that is still being built', () => {
  it('carries `building` off the wire, so the client can offer Stop instead of Save', async () => {
    // The refusal a mid-build switch produces. `dirty` is null and that is NOT "could not
    // tell": the server deliberately does not probe a tree the agent is writing to.
    const fetchImpl = jsonFetch(409, {
      error: {
        message: '“Lost & Found” is still being built.',
        code: 'sandbox_reclaim_blocked',
        projectId: 'p-a',
        projectName: 'Lost & Found',
        dirty: null,
        building: true,
      },
    })
    const err = await relaunchPreview({ projectId: 'p-b' }, { fetchImpl }).catch((e: unknown) => e)
    expect(asReclaimBlocked(err)).toEqual({
      projectId: 'p-a',
      projectName: 'Lost & Found',
      dirty: null,
      building: true,
    })
  })

  it('defaults `building` to FALSE when absent, never true', () => {
    // Erring the other way would show the stop-the-build dialog for a project nobody is
    // building — offering to kill work that does not exist.
    const err = { code: 'sandbox_reclaim_blocked', details: { projectId: 'p-a', projectName: 'A', dirty: true } }
    expect(asReclaimBlocked(err)?.building).toBe(false)
  })

  it('treats a non-boolean `building` as false rather than truthy', () => {
    const err = {
      code: 'sandbox_reclaim_blocked',
      details: { projectId: 'p-a', projectName: 'A', dirty: true, building: 'yes' },
    }
    expect(asReclaimBlocked(err)?.building).toBe(false)
  })
})
