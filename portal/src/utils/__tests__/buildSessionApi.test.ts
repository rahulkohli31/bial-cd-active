import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  start,
  relaunchPreview,
  stop,
  getStatus,
  forceEnd,
  buildSessionClient,
  BuildSessionAlreadyActiveError,
  asReclaimBlocked,
  releaseProject,
  fetchPreviewState,
  handOverWorkspace,
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

// The frozen `buildSessionClient` member set — the portal's mirror of the backend's
// `test_abstractmethod_set_equals_the_c2_contract` (`test_base.py`). A drifted mock bag is
// what this guards: the client interface trimmed to five members here, but one call site
// (`BuilderPage-memo.test.jsx`) went on mocking `acquireLock` / `renewLock` / `releaseLock` /
// `heartbeat` anyway, because nothing forced its stale keys to be read against the real
// surface. This test fails LOUDLY the moment `buildSessionClient` gains or loses a member,
// so the next removal cannot leave the same kind of residue behind unnoticed.
const _CLIENT_MEMBERS = new Set(['start', 'relaunchPreview', 'stop', 'getStatus', 'forceEnd'])

describe('buildSessionApi — buildSessionClient member set (inertness guard)', () => {
  it('exposes exactly the five surviving C3 client operations', () => {
    expect(new Set(Object.keys(buildSessionClient))).toEqual(_CLIENT_MEMBERS)
  })
})

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
  it('attaches X-CSRF-Token on every mutating POST (start / stop / forceEnd)', async () => {
    const stopImpl = jsonFetch(200, { sessionId: 's', status: 'ended' })
    await stop('s', {}, { fetchImpl: stopImpl })
    expect(headerOf(stopImpl, 'X-CSRF-Token')).toBe(CSRF)

    // `acquireLock` / `releaseLock` are gone (U28) — `forceEnd` is the one lock op left.
    const cases: Array<[(id: string, deps: { fetchImpl: FetchImpl }) => Promise<unknown>, unknown]> = [
      [forceEnd, { sessionId: 's', status: 'ended' }],
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

  // Re-anchored onto `forceEnd` (U28): `acquireLock` / `releaseLock`, which this assertion
  // used to run against, are gone — nothing called them. `forceEnd` is the surviving bodyless
  // lock POST, and the "lock ops take no request body" contract still holds for it.
  it('lock ops take NO request body (C3 §3) — forceEnd sends neither body nor Content-Type', async () => {
    const impl = jsonFetch(200, { sessionId: 's', status: 'ended' })
    await forceEnd('s', { fetchImpl: impl })
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

  // The `renew` 409 and `heartbeat` 404 tests lived here and are gone with their functions. Both
  // proved a real contract, and both proved it about code no caller could reach: U13 deleted the
  // keep-alive loop that was the only consumer, so the assertions had been describing an unused
  // module surface ever since. The backend routes keep their own tests.

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
    expect(asReclaimBlocked(err)).toEqual({
      projectId: 'p-a',
      projectName: 'Lost & Found',
      dirty: true,
      building: false,
      // ABSENT READS AS FALSE (plan 002, U9). An older backend that does not send the field
      // cannot have an agent to report, and defaulting the other way would tell every citizen
      // their other project is busy.
      agentWorking: false,
    })
  })

  it('★ carries `agentWorking` — the WIDE fact, separate from `building`', () => {
    // `building` marks only turns whose toolset can WRITE, and the server records why widening
    // it was wrong: it put a stop button and a hammer icon in front of someone who had only
    // asked a question. This is the wide answer, for a different sentence the dialog needs to be
    // able to say over a workspace it has just reported as holding nothing to lose.
    const err = {
      code: 'sandbox_reclaim_blocked',
      details: { projectId: 'p-a', projectName: 'A', dirty: false, building: false, agentWorking: true },
    }
    const blocked = asReclaimBlocked(err)
    expect(blocked?.agentWorking).toBe(true)
    expect(blocked?.building).toBe(false)
    expect(blocked?.dirty).toBe(false)
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
      agentWorking: false,
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
      agentWorking: false,
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

/**
 * `fetchPreviewState` — the hand-written mirror of the server's preview-state shape.
 *
 * This module narrows the wire by hand, so a field the server sends and this parser does not
 * read is discarded SILENTLY — no type error, no failing test, just a feature that never
 * works. Both facts pinned below were exactly that: `starting` is a server state the narrower
 * had to stop swallowing, and `occupyingProjectId` is the only thing the "another project
 * holds your workspace" remedy has to navigate with.
 */
/** A clock that jumps rather than waits: the CEILING's behaviour is the thing under test, and a
 *  test that genuinely waited two minutes for it is a test nobody runs. */
function fastClock() {
  let t = 0
  return {
    now: () => t,
    sleep: async (ms: number) => {
      t += ms
    },
  }
}

describe('handOverWorkspace — the stop → save → release ordering (#83)', () => {
  /** Records the path of every call in order, and answers each of the three routes plausibly. */
  function recordingFetch(stopState = 'stopped') {
    const seen: string[] = []
    const fetchImpl = vi.fn<FetchImpl>(async (url: string) => {
      seen.push(new URL(url, 'http://x').pathname)
      if (url.endsWith('/save')) return res(200, { appId: 'a-1', headSha: 'deadbeef' })
      if (url.endsWith('/release')) return res(200, { released: true })
      // THREE NAMED STATES, never a boolean (plan 002, U9) — see `StopState`. The old wire said
      // `{stopped: true}` unconditionally, which is exactly the confusion this replaced.
      return res(200, { state: stopState })
    })
    return { seen, fetchImpl }
  }

  it('stops FIRST, then saves, then releases — save and release both refuse while a session is live', async () => {
    const { seen, fetchImpl } = recordingFetch()
    await handOverWorkspace('p-1', true, { fetchImpl })
    expect(seen).toEqual([
      '/api/build-sessions/projects/p-1/stop-active-build',
      '/api/build-sessions/projects/p-1/save',
      '/api/build-sessions/projects/p-1/release',
    ])
  })

  it('SKIPS the save on Leave without saving, and still stops and releases', async () => {
    const { seen, fetchImpl } = recordingFetch()
    await handOverWorkspace('p-1', false, { fetchImpl })
    expect(seen).toEqual([
      '/api/build-sessions/projects/p-1/stop-active-build',
      '/api/build-sessions/projects/p-1/release',
    ])
  })

  it('does NOT release when the save fails — that is the data loss the whole flow exists to prevent', async () => {
    const seen: string[] = []
    const fetchImpl = vi.fn<FetchImpl>(async (url: string) => {
      seen.push(new URL(url, 'http://x').pathname)
      if (url.endsWith('/save')) return res(500, { error: { message: 'disk full' } })
      return res(200, { state: 'stopped' })
    })
    await expect(handOverWorkspace('p-1', true, { fetchImpl })).rejects.toBeInstanceOf(ApiError)
    expect(seen.some((p) => p.endsWith('/release'))).toBe(false)
  })

  it('★ WAITS for the stop to genuinely settle, and reports how far it got', async () => {
    // THE ASK RETURNS IMMEDIATELY NOW, usually `still_running` because the unwind has barely
    // begun. Proceeding on that would take a container out from under a task still writing to
    // it — so the hand-over polls the state read, which is the source of truth, and only the
    // two SETTLED answers let it carry on.
    const seen: string[] = []
    let asked = 0
    const fetchImpl = vi.fn<FetchImpl>(async (url: string) => {
      const path = new URL(url, 'http://x').pathname
      seen.push(path)
      if (path.endsWith('/stop-active-build')) return res(200, { state: 'still_running' })
      if (path.endsWith('/stop-state')) {
        asked += 1
        return res(200, { state: asked >= 3 ? 'stopped' : 'still_running' })
      }
      return res(200, { released: true })
    })

    await handOverWorkspace('p-1', false, { fetchImpl })

    expect(asked).toBe(3)
    // …and the release comes only AFTER the state said so.
    expect(seen.lastIndexOf('/api/build-sessions/projects/p-1/stop-state')).toBeLessThan(
      seen.indexOf('/api/build-sessions/projects/p-1/release'),
    )
  })

  it('★ a stop that never settles does NOT release, and says nothing has changed', async () => {
    // THE DIFFERENCE BETWEEN A CLEAN STOP AND A TIMEOUT, which this repo has shipped confused
    // before. Mutation receipt: treat `still_running` as settled and the release fires.
    const seen: string[] = []
    const fetchImpl = vi.fn<FetchImpl>(async (url: string) => {
      seen.push(new URL(url, 'http://x').pathname)
      return res(200, { state: 'still_running' })
    })

    const err = await handOverWorkspace('p-1', false, { fetchImpl }, undefined, fastClock()).catch(
      (e: unknown) => e,
    )

    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toMatch(/nothing has changed/i)
    expect(seen.some((path) => path.endsWith('/release'))).toBe(false)
    expect(seen.some((path) => path.endsWith('/save'))).toBe(false)
  })

  it('★ "nothing was running" is a SUCCESS to proceed on, not a miss', async () => {
    const { seen, fetchImpl } = recordingFetch('nothing_was_running')
    await handOverWorkspace('p-1', false, { fetchImpl })
    expect(seen.some((path) => path.endsWith('/release'))).toBe(true)
    // …and it did not need to poll at all: the ask already answered.
    expect(seen.some((path) => path.endsWith('/stop-state'))).toBe(false)
  })

  it('★ a body it cannot read is "still running" — the only safe default', async () => {
    // Reading an unparseable answer as settled would let it take somebody's container.
    const fetchImpl = vi.fn<FetchImpl>(async (url: string) =>
      url.endsWith('/release') ? res(200, { released: true }) : res(200, { stopped: true }),
    )
    await expect(
      handOverWorkspace('p-1', false, { fetchImpl }, undefined, fastClock()),
    ).rejects.toBeInstanceOf(ApiError)
  })

  it('narrates each step, in the order it performs them', async () => {
    const { fetchImpl } = recordingFetch()
    const steps: string[] = []
    await handOverWorkspace('p-1', true, { fetchImpl }, (step) => steps.push(step))
    expect(steps).toEqual(['stopping', 'saving', 'releasing'])
  })

  /**
   * THE POLL'S OWN FAILURE MODES (review #38, review #74).
   *
   * `awaitStopSettled` retries a read that failed and abandons one that hangs, and both are
   * documented as load-bearing for the dialog not hanging forever — but every fetch above
   * resolves, so deleting either left the suite green. What it must NOT retry is an answer a
   * repeat cannot change: a hundred 401s in two minutes is a hundred token refreshes ending in a
   * sentence about the other project's turn that has nothing to do with what failed.
   */
  /** Answers the three routes, with the stop-state read scripted per attempt. */
  function pollingFetch(read: (attempt: number, opts?: RequestInit) => Promise<Response>) {
    const seen: string[] = []
    let attempt = 0
    const fetchImpl = vi.fn<FetchImpl>(async (url: string, opts?: RequestInit) => {
      const path = new URL(url, 'http://x').pathname
      seen.push(path)
      if (path.endsWith('/stop-active-build')) return res(200, { state: 'still_running' })
      if (path.endsWith('/stop-state')) {
        attempt += 1
        return read(attempt, opts)
      }
      return res(200, { released: true })
    })
    const reads = () => seen.filter((path) => path.endsWith('/stop-state')).length
    return { seen, fetchImpl, reads }
  }

  it('★ a read that DROPPED is asked again — losing the network says nothing about their turn', async () => {
    const { seen, fetchImpl, reads } = pollingFetch(async (attempt) => {
      if (attempt === 1) throw new TypeError('Failed to fetch')
      return res(200, { state: 'stopped' })
    })

    await handOverWorkspace('p-1', false, { fetchImpl }, undefined, fastClock())

    expect(reads()).toBe(2)
    // …and the hand-over went through on the read that landed.
    expect(seen.some((path) => path.endsWith('/release'))).toBe(true)
  })

  it('★ a read that HANGS is abandoned on its own deadline, and the next one settles it', async () => {
    // `authFetch` sets no timeout: a connection that opens and then stalls never settles, the
    // loop never re-evaluates, and the two-minute ceiling silently becomes forever — under a
    // modal holding Escape. The per-read AbortController is what bounds it, and only a read that
    // actually hangs can prove the timer is still wired to it.
    vi.useFakeTimers()
    try {
      const { fetchImpl, reads, seen } = pollingFetch(async (attempt, opts) => {
        if (attempt > 1) return res(200, { state: 'stopped' })
        return new Promise<Response>((_resolve, reject) => {
          opts?.signal?.addEventListener('abort', () =>
            reject(new DOMException('The operation was aborted.', 'AbortError')),
          )
        })
      })

      const settled = handOverWorkspace('p-1', false, { fetchImpl }, undefined, fastClock())
      // Past the per-read deadline, which is a REAL timer rather than the injected clock's.
      await vi.advanceTimersByTimeAsync(20_000)
      await settled

      expect(reads()).toBe(2)
      expect(seen.some((path) => path.endsWith('/release'))).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ a session that expired mid-hand-over says so AT ONCE, rather than polling for two minutes', async () => {
    // Every retry of a 401 re-attempts a token refresh, so the old behaviour was ~100 reads and
    // ~100 refreshes across two minutes, ending in the ceiling's sentence about the other app
    // still saving its work — a sentence about somebody else's app, for an expired session.
    const { fetchImpl, reads, seen } = pollingFetch(async () =>
      res(401, { error: { message: 'Not authenticated' } }),
    )

    const err = await handOverWorkspace('p-1', false, { fetchImpl }, undefined, fastClock()).catch(
      (e: unknown) => e,
    )

    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(401)
    expect((err as ApiError).message).not.toMatch(/still saving its work/i)
    expect(reads()).toBe(1)
    // Nothing was taken from the other project on the way out.
    expect(seen.some((path) => path.endsWith('/release'))).toBe(false)
  })

  it('a 5xx is still a blip, not a verdict — the stop is running behind it', async () => {
    // The narrow list is 401/403/404. A server that fell over mid-stop may well answer the next
    // read, and the stop it was asked for is still unwinding server-side.
    const { fetchImpl, reads, seen } = pollingFetch(async (attempt) =>
      attempt === 1 ? res(503, { error: { message: 'unavailable' } }) : res(200, { state: 'stopped' }),
    )

    await handOverWorkspace('p-1', false, { fetchImpl }, undefined, fastClock())

    expect(reads()).toBe(2)
    expect(seen.some((path) => path.endsWith('/release'))).toBe(true)
  })

  it('★ at the ceiling it says the other app is still saving, not that anything failed', async () => {
    // THE OWNER'S DECISION ON REVIEW #45, pinned as copy. The server's stop budget is now a little
    // over eight minutes — its derivation covers the snapshot a build's unwind writes — while the
    // browser's wait deliberately stays at two, because nobody should be held in front of a modal
    // for eight. That makes arriving here ORDINARY rather than alarming: the likeliest cause is a
    // large app being packed away exactly as it should be, and the stop is still running behind
    // the sentence. So the one line the citizen reads must not read as a failure in the other
    // project, and it must point at the remedy, which is only to ask again.
    const { fetchImpl, reads } = pollingFetch(async () => res(200, { state: 'still_running' }))

    const err = await handOverWorkspace('p-1', false, { fetchImpl }, undefined, fastClock()).catch(
      (e: unknown) => e,
    )

    expect((err as ApiError).message).toBe(
      'The other app is still saving its work. Nothing has changed — give it a moment and try again.',
    )
    // NOT A WORD OF BLAME, and not one word a citizen could not act on.
    expect((err as ApiError).message).not.toMatch(
      /fail|could not|did not|timed out|container|sandbox|server|api/i,
    )
    // AND IT IS THE TWO-MINUTE WAIT THAT PRODUCED IT: a 120s ceiling at one read every 1.2s. This
    // is the assertion that holds the ceiling where the owner put it — raising it to the server's
    // budget would make this 409 reads, and the citizen would sit in front of the modal for them.
    expect(reads()).toBe(100)
  })

  it('carries a reclaim refusal out to the caller rather than swallowing it', async () => {
    // The dialog is the only thing mounted that can report a failed hand-over.
    const fetchImpl = jsonFetch(409, {
      error: {
        message: '“Lost & Found” is still open and has unsaved changes.',
        code: 'sandbox_reclaim_blocked',
        projectId: 'p-a',
        projectName: 'Lost & Found',
        dirty: true,
      },
    })
    const err = await handOverWorkspace('p-1', false, { fetchImpl }).catch((e: unknown) => e)
    expect(asReclaimBlocked(err)?.projectName).toBe('Lost & Found')
  })
})

describe('fetchPreviewState — the wire mirror (C3 §8.3)', () => {
  const previewFetch = (body: unknown) =>
    ({ fetchImpl: async () => res(200, body) })

  it('narrows `starting` to itself, not to `unknown` (AE54a)', async () => {
    // The one that degrades silently: a closed tuple without `starting` resolves it through
    // the fallback to `unknown`, so the pane says "we could not check" and offers a retry for
    // a start that is actively under way.
    const state = await fetchPreviewState('p1', previewFetch({ state: 'starting', alive: false }))
    expect(state.state).toBe('starting')
  })

  it('parses BOTH halves of the slot_taken attribution', async () => {
    const state = await fetchPreviewState(
      'p1',
      previewFetch({
        state: 'slot_taken',
        alive: false,
        occupyingProjectName: 'Car pool apps',
        occupyingProjectId: 'proj-9',
      }),
    )
    expect(state.occupyingProjectName).toBe('Car pool apps')
    expect(state.occupyingProjectId).toBe('proj-9')
  })

  it('leaves a withheld attribution null on BOTH fields rather than inventing one', async () => {
    // The server withholds the whole attribution when it cannot map the live container to a
    // project this user owns — naming the wrong project is worse than naming none, and that
    // now applies to the id as much as to the name.
    const state = await fetchPreviewState('p1', previewFetch({ state: 'slot_taken', alive: false }))
    expect(state.occupyingProjectName).toBeNull()
    expect(state.occupyingProjectId).toBeNull()
  })

  it('does not fill in the half that is missing when only one arrives', async () => {
    const nameOnly = await fetchPreviewState(
      'p1',
      previewFetch({ state: 'slot_taken', alive: false, occupyingProjectName: 'Roster' }),
    )
    expect(nameOnly.occupyingProjectName).toBe('Roster')
    expect(nameOnly.occupyingProjectId).toBeNull()

    const idOnly = await fetchPreviewState(
      'p1',
      previewFetch({ state: 'slot_taken', alive: false, occupyingProjectId: 'proj-9' }),
    )
    expect(idOnly.occupyingProjectName).toBeNull()
    expect(idOnly.occupyingProjectId).toBe('proj-9')
  })

  it('refuses a non-string id rather than coercing it into a route', async () => {
    // A number or an object here becomes a URL segment the go-to action navigates into and
    // 404s on. Same discipline the name beside it already follows.
    const state = await fetchPreviewState(
      'p1',
      previewFetch({ state: 'slot_taken', alive: false, occupyingProjectId: 42 }),
    )
    expect(state.occupyingProjectId).toBeNull()
  })

  it('keeps the deploy-outliving fallback: an unrecognised state is unknown, never gone', async () => {
    const dead = await fetchPreviewState('p1', previewFetch({ state: 'teleporting', alive: false }))
    expect(dead.state).toBe('unknown')

    const live = await fetchPreviewState('p1', previewFetch({ state: 'teleporting', alive: true }))
    expect(live.state).toBe('alive')
  })

  it('returns the all-null unknown shape for an unreadable body, id included', async () => {
    const state = await fetchPreviewState('p1', previewFetch(null))
    expect(state).toEqual({
      state: 'unknown',
      alive: false,
      previewUrl: null,
      occupyingProjectName: null,
      occupyingProjectId: null,
      restorable: null,
    })
  })
})
