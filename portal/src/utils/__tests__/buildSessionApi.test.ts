import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  relaunchPreview,
  getStatus,
  buildSessionClient,
  BuildSessionAlreadyActiveError,
  asReclaimBlocked,
  releaseProject,
  fetchPreviewState,
  fetchSaveState,
  sameSaveState,
  handOverWorkspace,
  discardUnsavedChanges,
  STOP_CEILING_MS,
  STOP_POLL_MS,
} from '../buildSessionApi'
import type { ReclaimBlocked, SaveState } from '../buildSessionApi'
import { ApiError } from '../apiError'

/** A fake `Response`: `json()` is re-callable and `clone()` returns itself, so the
 *  403 suspension probe can read the clone while the client reads the original. */
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
// `test_abstractmethod_set_equals_the_pinned_contract`. A drifted mock bag is what this
// guards: one call site (`ConversationSurface-memo.test.jsx`) went on mocking `acquireLock` /
// `renewLock` / `releaseLock` / `heartbeat` after they were gone, unnoticed because nothing
// forced its stale keys to be read against the real surface. This test fails LOUDLY the
// moment `buildSessionClient` gains or loses a member, so the next removal cannot leave the
// same kind of residue behind unnoticed — it did its job again for `forceEnd`'s removal and
// again for the session-scoped `stop`'s, which is why the set is DOWN to two.
const _CLIENT_MEMBERS = new Set(['relaunchPreview', 'getStatus'])

describe('buildSessionApi — buildSessionClient member set (inertness guard)', () => {
  it('exposes exactly the two surviving client operations', () => {
    expect(new Set(Object.keys(buildSessionClient))).toEqual(_CLIENT_MEMBERS)
  })
})

describe('buildSessionApi — control operations', () => {
  // `start` is gone — a composer send is a TURN, so the wrapper had no caller — but the typed
  // 409 mapping these two cases pin lives in the shared `postJson`, not in `start` itself. They
  // are re-pointed onto `relaunchPreview`, now the only live caller that can raise this error.
  it('a 409 build_session_already_active surfaces the existing sessionId as a typed error', async () => {
    const fetchImpl = jsonFetch(409, { error: { code: 'build_session_already_active', message: 'You already have a build running.' }, sessionId: 'existing-9' })
    const err = await relaunchPreview({ projectId: 'p1' }, { fetchImpl }).catch((e: unknown) => e)

    expect(err).toBeInstanceOf(BuildSessionAlreadyActiveError)
    expect(err).toBeInstanceOf(ApiError)
    const active = err as BuildSessionAlreadyActiveError
    expect(active.status).toBe(409)
    expect(active.code).toBe('build_session_already_active')
    expect(active.existingSessionId).toBe('existing-9')
  })

  it('reads the existing sessionId whether it sits at top-level or under error{}', async () => {
    const fetchImpl = jsonFetch(409, { error: { code: 'build_session_already_active', message: 'busy', sessionId: 'nested-42' } })
    const err = await relaunchPreview({ projectId: 'p1' }, { fetchImpl }).catch((e: unknown) => e)
    expect((err as BuildSessionAlreadyActiveError).existingSessionId).toBe('nested-42')
  })

  it('relaunchPreview: 200 maps {appId, previewUrl, status} — no sessionId/createdAt on this shape', async () => {
    const READY_URL = 'https://app.example.azurecontainerapps.io/'
    const fetchImpl = jsonFetch(200, { appId: 'a1', previewUrl: READY_URL, status: 'ready' })
    const out = await relaunchPreview({ projectId: 'p1' }, { fetchImpl })

    // Two absent fields, two DIFFERENT defaults: `restoredFromFailedBuild` absent reads FALSE
    // (the label is an aid, not a gate — silence claims nothing), `ready` absent reads TRUE
    // (every server predating that field only replied once serving, so defaulting false would
    // paint a permanent "not ready yet" over correct responses).
    expect(out).toEqual({
      appId: 'a1',
      previewUrl: READY_URL,
      status: 'ready',
      restoredFromFailedBuild: false,
      ready: true,
    })
    expect(headerOf(fetchImpl, 'X-CSRF-Token')).toBe(CSRF)
    expect(JSON.parse(optsOf(fetchImpl).body as string)).toEqual({ projectId: 'p1' })
    expect(optsOf(fetchImpl).method).toBe('POST')
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/build-sessions/relaunch')
  })

  it('relaunchPreview: carries `ready: false` through — a framable URL that is not serving yet', async () => {
    // The attach arm hands back the live container's URL even when it hasn't answered within
    // its readiness budget — the alternative, condemning the container, rolled a citizen back
    // to their last save. So `false` here must survive decoding, not fall to the absent-reads-true
    // default meant for older servers.
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

  it('relaunchPreview: the wire restoredFromFailedBuild=true survives the mapping', async () => {
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

  it('getStatus: previewUrl is null before ready and the stable URL once ready; lastSeq present after the first envelope', async () => {
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

    expect(headerOf(after, 'X-CSRF-Token')).toBeUndefined()
    expect(optsOf(after).method).toBeUndefined()
  })
})

describe('buildSessionApi — CSRF discipline', () => {
  // RE-POINTED OFF THE DELETED LOCK OPS, and again off the session-scoped `stop`.
  // `relaunchPreview` is the one session-namespace mutating POST the portal still makes, and
  // the contract — every mutating POST carries the token — is unchanged.
  it('attaches X-CSRF-Token on the mutating POST (relaunchPreview)', async () => {
    const relaunchImpl = jsonFetch(200, { appId: 'a1', previewUrl: null, status: 'ready', ready: true, restoredFromFailedBuild: false })
    await relaunchPreview({ projectId: 'p1' }, { fetchImpl: relaunchImpl })
    expect(headerOf(relaunchImpl, 'X-CSRF-Token')).toBe(CSRF)
    expect(optsOf(relaunchImpl).method).toBe('POST')
  })

  it('omits the CSRF header when no csrf cookie is readable (parity with auth.js)', async () => {
    document.cookie = 'csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
    const impl = jsonFetch(200, { appId: 'a1', previewUrl: null, status: 'ready', ready: true, restoredFromFailedBuild: false })
    await relaunchPreview({ projectId: 'p1' }, { fetchImpl: impl })
    expect(headerOf(impl, 'X-CSRF-Token')).toBeUndefined()
  })
})

describe('buildSessionApi — lock ops + fail-closed errors', () => {
  // RE-POINTED OFF `forceEnd`, the same way it was once re-pointed onto it off
  // `acquireLock` / `releaseLock`. The CONTRACT is `postJson`'s `body === undefined` branch — send
  // no JSON body and therefore no Content-Type — and the kill switch was only ever its vehicle.
  // The branch is still live and still has three callers, all project-scoped commands that name
  // their target in the path and carry nothing else, so the assertion moves onto one of them
  // rather than dying with the route.
  it('a bodyless POST sends neither body nor Content-Type — releaseProject names its target in the path', async () => {
    const impl = jsonFetch(200, { released: true })
    await releaseProject('p-b', { fetchImpl: impl })
    expect(optsOf(impl).body).toBeUndefined()
    expect(headerOf(impl, 'Content-Type')).toBeUndefined()
    // LIVENESS: the call really went out as the mutating POST whose body we just asserted away.
    expect(optsOf(impl).method).toBe('POST')
    expect(headerOf(impl, 'X-CSRF-Token')).toBe(CSRF)
  })

  // `forceEnd`'s OWN test — the kill switch's `403 build_session_forbidden` surfacing fail-closed
  // — is gone rather than re-pointed, because it had no contract left to describe once the client
  // AND the route went in the same change: no other call can answer that code. The generic
  // non-2xx → `ApiError` mapping it rode is pinned by the 409 and boundary cases either side.

  // `renew`'s 409 and `heartbeat`'s 404 are not tested here — the keep-alive loop that called
  // them is gone. The backend routes keep their own tests.

  it('a malformed success body fails at the boundary rather than corrupting state downstream', async () => {
    const fetchImpl = jsonFetch(200, { status: 'ready' }) // no sessionId
    const err = await getStatus('s', { fetchImpl }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(500)
  })

  it('a session body with NO projectId fails at the boundary — it drives the 409 reattach/block routing', async () => {
    // getStatus is the ONE reader of this guard now: the projectId comparison is the
    // reattach-vs-block gate that `start`, gone with its wrapper, used to anchor.
    const statusImpl = jsonFetch(200, { sessionId: 's1', appId: 'a1', status: 'ready', previewUrl: null, lastSeq: 1, createdAt: 'c', updatedAt: 'u' })
    const statusErr = await getStatus('s1', { fetchImpl: statusImpl }).catch((e: unknown) => e)
    expect(statusErr).toBeInstanceOf(ApiError)
    expect((statusErr as ApiError).status).toBe(500)
  })
})

describe('asReclaimBlocked', () => {
  it('reads the occupying project off the 409', () => {
    const err = { code: 'sandbox_reclaim_blocked', details: { projectId: 'p-a', projectName: 'Lost & Found', dirty: true } }
    expect(asReclaimBlocked(err)).toEqual({
      projectId: 'p-a',
      projectName: 'Lost & Found',
      dirty: true,
      building: false,
      // ABSENT READS AS FALSE — an older backend with no such field has no agent to report,
      // and defaulting true would tell every citizen their other project is busy.
      agentWorking: false,
      isSharedView: false,
    })
  })

  it('★ carries `agentWorking` — the WIDE fact, separate from `building`', () => {
    // `building` marks only turns whose toolset can WRITE — widening it put a stop button and
    // a hammer icon in front of someone who had only asked a question. `agentWorking` is the
    // wide fact the dialog needs for a different sentence.
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

  it('★ carries `isSharedView` — the client\'s one signal to skip stopActiveBuild/release', () => {
    // A colleague's shared view names its OWNER in `projectId`/`projectName`, which the
    // recipient never owns — `stopActiveBuild`/`release` would 404 them on that id.
    // `isSharedView` is what routes the client to `giveUpSharedView` instead.
    const err = {
      code: 'sandbox_reclaim_blocked',
      details: { projectId: 'owner-p', projectName: 'Owner App', dirty: false, isSharedView: true },
    }
    expect(asReclaimBlocked(err)?.isSharedView).toBe(true)
  })

  it('isSharedView absent reads as false — an older backend never produced a shared occupant', () => {
    const err = { code: 'sandbox_reclaim_blocked', details: { projectId: 'p-a', projectName: 'A', dirty: false } }
    expect(asReclaimBlocked(err)?.isSharedView).toBe(false)
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

  // END TO END: the tests above hand-build `{code, details}` and would still pass if `postJson`
  // stopped carrying `details` at all — the exact regression that shipped once (the relaunch
  // button showed the raw error text instead of the dialog). This drives the real 409 through
  // the real client. The body is `reclaim_blocked_response`'s output verbatim (`live_build.py`),
  // a flat `error` object, NOT a nested `details` key — if the backend reshapes it, this goes
  // red on the same commit.
  const WIRE_409 = {
    error: {
      message: '“Lost & Found” is still open and has changes that are not saved yet.',
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
      isSharedView: false,
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
      error: { ...WIRE_409.error, dirty: null, message: '“A” is still open and may have changes that are not saved yet.' },
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
      isSharedView: false,
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
 * `fetchPreviewState` narrows the wire by hand, so a field the server sends that this parser
 * doesn't read is discarded SILENTLY — no type error, no failing test, just a dead feature.
 * `starting` and `occupyingProjectId`, pinned below, are exactly that risk.
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

describe('handOverWorkspace — the stop → save → release ordering', () => {
  // An ORDINARY (non-shared) occupant — `handOverWorkspace` now takes the whole
  // `ReclaimBlocked` rather than a bare project id, so every call below hands it this instead
  // of the string `'p-1'` it used to pass directly.
  const BLOCKED_P1: ReclaimBlocked = {
    projectId: 'p-1',
    projectName: 'P1',
    dirty: false,
    building: false,
    agentWorking: false,
    isSharedView: false,
  }

  function recordingFetch(stopState = 'stopped') {
    const seen: string[] = []
    const fetchImpl = vi.fn<FetchImpl>(async (url: string) => {
      seen.push(new URL(url, 'http://x').pathname)
      if (url.endsWith('/save')) return res(200, { appId: 'a-1', headSha: 'deadbeef' })
      if (url.endsWith('/release')) return res(200, { released: true })
      // THREE NAMED STATES, never a boolean — see `StopState`. The old wire said
      // `{stopped: true}` unconditionally, which is exactly the confusion this replaced.
      return res(200, { state: stopState })
    })
    return { seen, fetchImpl }
  }

  it('stops FIRST, then saves, then releases — save and release both refuse while a session is live', async () => {
    const { seen, fetchImpl } = recordingFetch()
    await handOverWorkspace(BLOCKED_P1, true, { fetchImpl })
    expect(seen).toEqual([
      '/api/build-sessions/projects/p-1/stop-active-build',
      '/api/build-sessions/projects/p-1/save',
      '/api/build-sessions/projects/p-1/release',
    ])
  })

  it('SKIPS the save on Leave without saving, and still stops and releases', async () => {
    const { seen, fetchImpl } = recordingFetch()
    await handOverWorkspace(BLOCKED_P1, false, { fetchImpl })
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
    await expect(handOverWorkspace(BLOCKED_P1, true, { fetchImpl })).rejects.toBeInstanceOf(ApiError)
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

    await handOverWorkspace(BLOCKED_P1, false, { fetchImpl })

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

    const err = await handOverWorkspace(BLOCKED_P1, false, { fetchImpl }, undefined, fastClock()).catch(
      (e: unknown) => e,
    )

    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toMatch(/nothing has changed/i)
    expect(seen.some((path) => path.endsWith('/release'))).toBe(false)
    expect(seen.some((path) => path.endsWith('/save'))).toBe(false)
  })

  it('★ "nothing was running" is a SUCCESS to proceed on, not a miss', async () => {
    const { seen, fetchImpl } = recordingFetch('nothing_was_running')
    await handOverWorkspace(BLOCKED_P1, false, { fetchImpl })
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
      handOverWorkspace(BLOCKED_P1, false, { fetchImpl }, undefined, fastClock()),
    ).rejects.toBeInstanceOf(ApiError)
  })

  it('narrates each step, in the order it performs them', async () => {
    const { fetchImpl } = recordingFetch()
    const steps: string[] = []
    await handOverWorkspace(BLOCKED_P1, true, { fetchImpl }, (step) => steps.push(step))
    expect(steps).toEqual(['stopping', 'saving', 'releasing'])
  })

  /**
   * THE POLL'S OWN FAILURE MODES.
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

    await handOverWorkspace(BLOCKED_P1, false, { fetchImpl }, undefined, fastClock())

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

      const settled = handOverWorkspace(BLOCKED_P1, false, { fetchImpl }, undefined, fastClock())
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

    const err = await handOverWorkspace(BLOCKED_P1, false, { fetchImpl }, undefined, fastClock()).catch(
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

    await handOverWorkspace(BLOCKED_P1, false, { fetchImpl }, undefined, fastClock())

    expect(reads()).toBe(2)
    expect(seen.some((path) => path.endsWith('/release'))).toBe(true)
  })

  it('★ at the ceiling it says the other app is still saving, not that anything failed', async () => {
    // PINNED AS COPY. The server's stop budget runs ~8 minutes; the browser's wait deliberately
    // stays at 2, so arriving here is ORDINARY, not alarming — likely a large app being packed
    // away as it should. The line must not read as a failure, and must point at the remedy.
    const { fetchImpl, reads } = pollingFetch(async () => res(200, { state: 'still_running' }))

    const err = await handOverWorkspace(BLOCKED_P1, false, { fetchImpl }, undefined, fastClock()).catch(
      (e: unknown) => e,
    )

    expect((err as ApiError).message).toBe(
      'The other app is still saving its work. Nothing has changed — give it a moment and try again.',
    )
    // NOT A WORD OF BLAME, and not one word a citizen could not act on.
    expect((err as ApiError).message).not.toMatch(
      /fail|could not|did not|timed out|container|sandbox|server|api/i,
    )
    // HOW LONG A CITIZEN IS HELD is fixed and worth a hard number: raising the ceiling to the
    // server's 8-minute budget is exactly the change that must not pass, so it fails by name.
    expect(STOP_CEILING_MS).toBe(2 * 60 * 1000)
    // HOW OFTEN IT ASKS is free to move (a traffic decision), so the count is derived —
    // `Math.ceil` because the loop checks the deadline BEFORE each read, so an unevenly
    // dividing ceiling still gets its last poll in.
    expect(reads()).toBe(Math.ceil(STOP_CEILING_MS / STOP_POLL_MS))
  })

  it('carries a reclaim refusal out to the caller rather than swallowing it', async () => {
    const fetchImpl = jsonFetch(409, {
      error: {
        message: '“Lost & Found” is still open and has changes that are not saved yet.',
        code: 'sandbox_reclaim_blocked',
        projectId: 'p-a',
        projectName: 'Lost & Found',
        dirty: true,
      },
    })
    const err = await handOverWorkspace(BLOCKED_P1, false, { fetchImpl }).catch((e: unknown) => e)
    expect(asReclaimBlocked(err)?.projectName).toBe('Lost & Found')
  })

  it('★ a SHARED occupant never touches stopActiveBuild/save/release — it goes through giveUpSharedView instead', async () => {
    // The regression this pins: `blocked.projectId` on a shared occupant names its OWNER, who
    // the caller never owns, so `stopActiveBuild`'s `owned_project_or_404` would 404 every one
    // of the four surfaces that used to call this with `blocked.projectId` alone.
    const seen: string[] = []
    const fetchImpl = vi.fn<FetchImpl>(async (url: string) => {
      seen.push(new URL(url, 'http://x').pathname)
      return res(200, { released: true })
    })
    const sharedBlocked: ReclaimBlocked = {
      projectId: 'owner-project-id',
      projectName: 'Owner App',
      dirty: false,
      building: false,
      agentWorking: false,
      isSharedView: true,
    }

    await handOverWorkspace(sharedBlocked, false, { fetchImpl })

    expect(seen).toEqual(['/api/build-sessions/shared-view/release'])
    expect(seen.some((p) => p.includes('owner-project-id'))).toBe(false)
  })

  it('★ save is accepted but ignored for a shared occupant — there is nothing of theirs to save', async () => {
    const seen: string[] = []
    const fetchImpl = vi.fn<FetchImpl>(async (url: string) => {
      seen.push(new URL(url, 'http://x').pathname)
      return res(200, { released: true })
    })
    const sharedBlocked: ReclaimBlocked = {
      projectId: 'owner-project-id',
      projectName: 'Owner App',
      dirty: false,
      building: false,
      agentWorking: false,
      isSharedView: true,
    }

    await handOverWorkspace(sharedBlocked, true, { fetchImpl }) // save=true

    expect(seen).toEqual(['/api/build-sessions/shared-view/release'])
    expect(seen.some((p) => p.endsWith('/save'))).toBe(false)
  })
})

describe('fetchPreviewState — the wire mirror', () => {
  const previewFetch = (body: unknown) =>
    ({ fetchImpl: async () => res(200, body) })

  it('narrows `starting` to itself, not to `unknown`', async () => {
    // Without `starting`, a closed tuple falls back to `unknown` — the pane would say "we
    // could not check" and offer a retry for a start actively under way.
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
    // The server withholds the whole attribution when it can't map the container to an owned
    // project — naming the wrong project is worse than naming none, id included now.
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

/**
 * ★ THE SAVE-STATE WIRE, FIELD BY FIELD.
 *
 * `savedHead` is what the rail's sentence and the Discard control both read, so a body the parse
 * mis-narrows is a screen making a claim about somebody's work that nobody checked.
 */
describe('fetchSaveState — the saved head, whitelisted and narrowed', () => {
  const saveFetch = (body: unknown) => ({ fetchImpl: async () => res(200, body) })

  it('★ keeps a string head exactly as it arrived', async () => {
    const state = await fetchSaveState(
      'p1',
      saveFetch({ appId: 'a1', dirty: true, containerHead: '059d936', savedHead: 'aaa1111' }),
    )
    expect(state.savedHead).toBe('aaa1111')
    // …and the rest of the reading is untouched — `dirty` STAYS TRUE beside it, because a saved
    // head older than the container's is exactly the state the indicator exists to report.
    expect(state.dirty).toBe(true)
    expect(state.containerHead).toBe('059d936')
  })

  it('★ reads an ABSENT field as nothing saved, rather than inventing one', async () => {
    // The direction this fact is allowed to fail in: a field the server did not send must never
    // arrive as a version somebody can be told they have.
    const state = await fetchSaveState('p1', saveFetch({ appId: 'a1', dirty: true }))
    expect(state.savedHead).toBeNull()
    // …and the reading is otherwise alive, so this is not a null from a body that failed to parse.
    expect(state.dirty).toBe(true)
  })

  it('refuses a non-string head instead of coercing it', async () => {
    const state = await fetchSaveState('p1', saveFetch({ appId: 'a1', dirty: true, savedHead: 1757500723 }))
    expect(state.savedHead).toBeNull()
    expect(state.dirty).toBe(true)
  })
})

describe('discardUnsavedChanges — the Discard button', () => {
  const DISCARDED = {
    appId: 'a1',
    dirty: false,
    containerHead: 'aaa',
    savedHead: 'aaa',
  }

  it('POSTs to the discard route with the CSRF header and the conversationId body', async () => {
    const fetchImpl = jsonFetch(200, { ...DISCARDED, notice: null })
    await discardUnsavedChanges('p1', 'conv-1', { fetchImpl })

    expect(fetchImpl.mock.calls[0][0]).toBe('/api/build-sessions/projects/p1/discard')
    expect(optsOf(fetchImpl).method).toBe('POST')
    expect(headerOf(fetchImpl, 'X-CSRF-Token')).toBe(CSRF)
    expect(JSON.parse(optsOf(fetchImpl).body as string)).toEqual({ conversationId: 'conv-1' })
  })

  it('sends an empty body when pressed outside a chat', async () => {
    const fetchImpl = jsonFetch(200, { ...DISCARDED, notice: null })
    await discardUnsavedChanges('p1', null, { fetchImpl })
    expect(JSON.parse(optsOf(fetchImpl).body as string)).toEqual({})
  })

  it('a 200 parses the save state and the notice', async () => {
    const fetchImpl = jsonFetch(200, {
      ...DISCARDED,
      notice: { seq: 7, savedAt: '2026-09-10T10:38:43Z' },
    })
    const out = await discardUnsavedChanges('p1', 'conv-1', { fetchImpl })
    expect(out.saveState).toEqual(DISCARDED)
    expect(out.notice).toEqual({ seq: 7, savedAt: '2026-09-10T10:38:43Z' })
  })

  it('a malformed notice parses to null', async () => {
    const notARecord = jsonFetch(200, { ...DISCARDED, notice: 'nope' })
    expect((await discardUnsavedChanges('p1', null, { fetchImpl: notARecord })).notice).toBeNull()

    const noSeq = jsonFetch(200, { ...DISCARDED, notice: { savedAt: '2026-09-10T10:38:43Z' } })
    expect((await discardUnsavedChanges('p1', null, { fetchImpl: noSeq })).notice).toBeNull()
  })

  it('a non-string savedAt on an otherwise valid notice reads as null', async () => {
    const fetchImpl = jsonFetch(200, { ...DISCARDED, notice: { seq: 3, savedAt: 12345 } })
    const out = await discardUnsavedChanges('p1', null, { fetchImpl })
    expect(out.notice).toEqual({ seq: 3, savedAt: null })
  })

  it('a 409 rejects with the server message', async () => {
    const fetchImpl = jsonFetch(409, {
      error: { message: 'There is no saved version to go back to yet.' },
    })
    await expect(discardUnsavedChanges('p1', null, { fetchImpl })).rejects.toThrow(
      'There is no saved version to go back to yet.',
    )
  })
})

describe('sameSaveState — what a poll is allowed to call "no change"', () => {
  const reading = (over: Partial<SaveState> = {}): SaveState => ({
    appId: 'a1',
    dirty: true,
    containerHead: '059d936',
    savedHead: null,
    ...over,
  })

  it('★ sees a change in `savedHead` and nothing else', () => {
    // THE MUTANT THAT MATTERS. `useWorkspaceState` keeps the PREVIOUS object whenever this says
    // "same", so a comparator blind to a field discards the reading that changed: the rail
    // freezes on the first poll's sentence, with no other test in the repo going red. Drop the
    // `a.savedHead === b.savedHead` conjunct and this is the assertion that catches it.
    expect(sameSaveState(reading(), reading({ savedHead: 'aaa1111' }))).toBe(false)
  })

  it('★ sees a change in `containerHead` too', () => {
    // The same mutant, one field along: the container's head is half of the comparison the dirty
    // indicator is, so a comparator blind to it would keep reporting the previous answer while
    // the workspace moved underneath it.
    expect(sameSaveState(reading(), reading({ containerHead: 'bbb2222' }))).toBe(false)
  })

  it('still calls two identical readings the same', () => {
    // The other half: this exists to stop a poll re-rendering on an unchanged answer, and a
    // comparator that answered `false` for everything would pass the tests above by doing nothing.
    expect(sameSaveState(reading({ savedHead: 'aaa1111' }), reading({ savedHead: 'aaa1111' }))).toBe(true)
  })
})
