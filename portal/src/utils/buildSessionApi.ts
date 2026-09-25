/**
 * Typed client for the build-session control API (`/api/build-sessions*`), mirroring
 * `projectApi.ts`: every call is `fn(args, deps = {})`, forwards `deps` to `authFetch` (cookie
 * session + one 401-refresh retry); the edge rewrites `/api/*` → `/v1/*`.
 *
 * Wire format is camelCase; bodies are untrusted `unknown`, narrowed with `toX()` guards — never
 * cast, never `any`. Every non-2xx becomes an `ApiError`, so callers branch on `.status`/`.code`.
 *
 * CSRF: `relaunchPreview` (and the project-scoped save / release / stop-active calls below)
 * are mutating POSTs and carry the signed double-submit token (`X-CSRF-Token`,
 * reusing `auth.ts` `getCsrfToken()`); the GETs are safe methods and carry NO token. This is
 * net-new: no prior business route in the portal enforces CSRF.
 */
import { ApiError, extractApiCode, extractApiMessage, isRecord, readApiError } from './apiError'
import { authFetch } from './api'
import { asCompileState } from './compileState'
import type { CompileState } from './compileState'
import { getCsrfToken } from './auth'
import type { RelaunchPreviewRequest, SharedPreviewResponse } from './buildSessionTypes'

/**
 * The dep bundle `authFetch` accepts, injectable so tests need no real network.
 * Derived straight from `authFetch`'s own JSDoc typedef so it can never drift
 * (identical to `projectApi.ts`'s `AuthFetchDeps`).
 */
export type AuthFetchDeps = NonNullable<Parameters<typeof authFetch>[2]>

// NO KEEP-ALIVE CLIENT HERE, and none may be added back: the `renew` / `heartbeat` routes one
// would call are retired, so it would be a browser timer POSTing at a 404. The lock and
// heartbeat TTLs are the backend schema's and are not mirrored here.

const BASE = '/api/build-sessions'
const JSON_HEADERS = { 'Content-Type': 'application/json' }

/**
 * Thrown by `relaunchPreview` on a `409 build_session_already_active` — a typed discriminator,
 * because the `409` alone is not a self-describing one.
 *
 * Its one live handler is `StartAppControl`, which reports "a build is already running in this
 * project" through the workspace state.
 */
export class BuildSessionAlreadyActiveError extends ApiError {
  constructor(message: string) {
    super(message, 409, 'build_session_already_active')
    this.name = 'BuildSessionAlreadyActiveError'
  }
}

// ─── request plumbing ────────────────────────────────────────────────────────

/** The double-submit CSRF header for a mutating POST, or `{}` when no csrf cookie is readable (parity with `auth.ts`). */
function csrfHeaders(): Record<string, string> {
  const csrf = getCsrfToken()
  return csrf ? { 'X-CSRF-Token': csrf } : {}
}

/**
 * A plain GET with the same error handling. Separate from `postJson` rather than a flag on it,
 * because a GET carries no CSRF header and no body — and a helper that took "is this a mutation"
 * as an argument would be one edit away from sending one that did.
 */
async function getJson(
  url: string,
  fallback: string,
  deps: AuthFetchDeps,
  signal?: AbortSignal,
): Promise<unknown> {
  const res = await authFetch(url, signal ? { signal } : {}, deps)
  if (!res.ok) {
    const errBody: unknown = await res.json().catch(() => null)
    throw new ApiError(extractApiMessage(errBody, res.status, fallback), res.status, extractApiCode(errBody))
  }
  return res.json().catch(() => null)
}

/**
 * A mutating POST with CSRF. `body === undefined` sends no JSON body — the project-scoped
 * commands (`saveProject` / `releaseProject` / `stopActiveBuild`) name the target in the path
 * and carry nothing else. A non-2xx becomes an `ApiError`, EXCEPT a
 * `409 build_session_already_active` which becomes the typed
 * `BuildSessionAlreadyActiveError`.
 */
async function postJson(url: string, body: unknown, fallback: string, deps: AuthFetchDeps): Promise<unknown> {
  const hasBody = body !== undefined
  const res = await authFetch(
    url,
    {
      method: 'POST',
      headers: { ...(hasBody ? JSON_HEADERS : {}), ...csrfHeaders() },
      ...(hasBody ? { body: JSON.stringify(body) } : {}),
    },
    deps,
  )
  if (!res.ok) {
    const errBody: unknown = await res.json().catch(() => null)
    const code = extractApiCode(errBody)
    const message = extractApiMessage(errBody, res.status, fallback)
    if (res.status === 409 && code === 'build_session_already_active') {
      throw new BuildSessionAlreadyActiveError(message)
    }
    // CARRY THE WHOLE ERROR OBJECT. This built its own ApiError and dropped everything but
    // the message and code, so `sandbox_reclaim_blocked` arrived with no projectId — and
    // `asReclaimBlocked` returned null, so Relaunch rendered the refusal as red text in the
    // preview pane instead of the dialog that offers to save the other project.
    const details = isRecord(errBody) && isRecord(errBody.error) ? errBody.error : null
    throw new ApiError(message, res.status, code, details)
  }
  return res.json()
}

// ─── control operations ─────────────────────────────────────────────

// `start` IS GONE: nothing here provisions a session — a build happens inside the turn's own
// transaction. The ROUTE is untouched; deleting a browser client says nothing about it.

/**
 * `relaunch` — start a project's saved app. Resolves once the server has ADMITTED the start (202);
 * the app comes up afterwards, and the preview-state poll is what reports it — `starting`, then
 * `alive` with the address to frame. Nothing in the answer is read, so nothing is parsed.
 * A mutating POST (carries CSRF). `postJson` already turns a `409 build_session_already_active` into
 * `BuildSessionAlreadyActiveError` (a build is running); 404 = nothing to relaunch, 503 =
 * transient/retryable.
 */
export async function relaunchPreview(
  args: RelaunchPreviewRequest,
  deps: AuthFetchDeps = {},
): Promise<void> {
  await postJson(`${BASE}/relaunch`, { projectId: args.projectId }, 'Failed to relaunch the preview', deps)
}

// ─── lock operations — THERE ARE NONE LEFT ─────────────────────────────────
//
// `acquireLock` and `releaseLock` are gone: nothing called them — the portal's blind
// keep-alive loop that was their only caller was itself deleted, same as `renewLock` and
// `heartbeat` before them (see the note above).
//
// `forceEnd` is gone too, and so is the ROUTE it spoke to; the session-scoped `stop` that
// replaced it in this comment has since been retired the same way, route and all. What a live
// build offers now is the turn's own stop and, project-scoped, `stopActiveBuild`.

// --- the save model ---------------------------------------------------------

export interface SaveResult {
  appId: string
  headSha: string | null
}

export interface SaveState {
  appId: string | null
  /** TRI-STATE, and the null is load-bearing: `null` means UNKNOWN — no live workspace to
   *  compare, or a bundle the server could not read. Rendering it as clean would tell the
   *  user their work is safe when nothing actually checked. */
  dirty: boolean | null
  containerHead: string | null
  savedHead: string | null
}

/** Two readings that say the same thing. Every field is a primitive, so this is exact rather
 *  than an approximation — and it exists so a poll that keeps reporting the same answer stops
 *  handing consumers a new object to re-render for.
 *
 *  EVERY FIELD MEANS EVERY FIELD, AND OMITTING ONE IS NOT A MISSED OPTIMISATION — IT DISCARDS
 *  THE NEW VALUE. The caller keeps the PREVIOUS object whenever this answers "same"
 *  (`useWorkspaceState`: `sameSaveState(prev, state) ? prev : state`), so a field this cannot
 *  see never reaches the screen at all: the reading that changed is thrown away and the rail
 *  goes on saying the sentence that belonged to the old one. */
export const sameSaveState = (a: SaveState | null, b: SaveState | null): boolean =>
  a === b ||
  (a !== null &&
    b !== null &&
    a.appId === b.appId &&
    a.dirty === b.dirty &&
    a.containerHead === b.containerHead &&
    a.savedHead === b.savedHead)

/** Push the project's current tree to durable storage. THE USER'S CLICK — the one write of the
 *  bundle anybody asks for. A 409 means the workspace is no longer running, and is surfaced, never
 *  swallowed: a Save that reports success having stored nothing is the worst outcome here. */
export async function saveProject(projectId: string, deps: AuthFetchDeps = {}): Promise<SaveResult> {
  const body = await postJson(
    `${BASE}/projects/${encodeURIComponent(projectId)}/save`,
    undefined,
    'Could not save your work',
    deps,
  )
  if (!isRecord(body)) throw new ApiError('The server returned a save we could not read.', 500)
  return {
    appId: typeof body.appId === 'string' ? body.appId : projectId,
    headSha: typeof body.headSha === 'string' ? body.headSha : null,
  }
}

/** The workspace is one-per-user, so opening a second project needs the first to give up its
 *  container. THE ONLY ROUTE THAT DESTROYS ONE ON PURPOSE — the start path used to do this
 *  silently, inside the request for a different project, with the user's unsaved work in it.
 *  `released: false` is a success: the workspace was already gone, which is what was asked. */
export async function releaseProject(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<boolean> {
  const body = await postJson(
    `${BASE}/projects/${encodeURIComponent(projectId)}/release`,
    undefined,
    'Could not close the other workspace',
    deps,
  )
  return isRecord(body) && body.released === true
}

function toSharedPreviewResponse(value: unknown): SharedPreviewResponse {
  if (!isRecord(value)) throw new ApiError('The server returned a preview we could not read.', 500)
  return {
    appId: typeof value.appId === 'string' ? value.appId : '',
    previewUrl: typeof value.previewUrl === 'string' ? value.previewUrl : '',
    ready: value.ready === true,
    snapshotTakenAt: typeof value.snapshotTakenAt === 'string' ? value.snapshotTakenAt : null,
  }
}

/**
 * Open a project a colleague shared with you (#198). Attaches to an already-live view if one
 * is up (a reopened tab, a second click); otherwise restores one from the owner's latest
 * SAVED snapshot.
 *
 * IT DOES TAKE THE CALLER'S OWN ONE-PER-USER SLOT — the same slot a build occupies — via the
 * identical `_holding_user_lock` skeleton `relaunchPreview` runs under. A prior docstring here
 * said the opposite ("registers no build session... nothing here occupies the caller's own
 * slot"), which was true of the build-SESSION bookkeeping and false of the thing that actually
 * matters to a caller: whether pressing this can conflict with something else. It can — a
 * `409 sandbox_reclaim_blocked` here means exactly what it means on a relaunch, and the caller
 * has to handle it the same way (see `asReclaimBlocked` / `ReclaimBlocked.isSharedView`).
 *
 * NOT "READ-ONLY" EITHER, for the same reason the product copy already gets
 * right: the recipient can create, update and delete the owner's records through the app's own
 * UI. "Can use", never "view only" — these two functions open the door, nothing more.
 */
export async function launchSharedPreview(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<SharedPreviewResponse> {
  const body = await postJson(
    `${BASE}/projects/${encodeURIComponent(projectId)}/shared-launch`,
    undefined,
    'Could not open this shared application',
    deps,
  )
  return toSharedPreviewResponse(body)
}

/**
 * Re-restore a shared project from whatever is CURRENTLY saved (#198) — unlike Launch, never
 * attaches to an already-live view even when one is up, since the owner may have saved
 * something newer since it came up. `snapshotTakenAt` on the response is how the caller
 * learns whether anything actually moved.
 */
export async function refreshSharedPreview(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<SharedPreviewResponse> {
  const body = await postJson(
    `${BASE}/projects/${encodeURIComponent(projectId)}/shared-refresh`,
    undefined,
    'Could not refresh this shared application',
    deps,
  )
  return toSharedPreviewResponse(body)
}

/**
 * WHAT A STOP ACHIEVED — THREE NAMED STATES, never a boolean.
 *
 * The boolean this replaced hardcoded success on both branches, so a stop that had NOT
 * finished — the one answer a caller must never act on — read identically to one that had.
 * Folding a timeout into `false` ("nothing was running") would take a container out from
 * under a task still writing to it.
 *
 *   `stopped`              proceed — something ran and has finished unwinding.
 *   `nothing_was_running`  proceed — there was nothing to stop.
 *   `still_running`        DO NOT PROCEED — the wait expired, or something still holds the app.
 */
export type StopState = 'stopped' | 'nothing_was_running' | 'still_running'

/** The two states a hand-over may act on. Named rather than inlined, because "which of these
 *  means go" is the whole decision and it should have one place to be read. */
export function stopSettled(state: StopState): boolean {
  return state === 'stopped' || state === 'nothing_was_running'
}

function readStopState(body: unknown): StopState {
  const state = isRecord(body) ? body.state : undefined
  if (state === 'stopped' || state === 'nothing_was_running' || state === 'still_running') {
    return state
  }
  // AN UNREADABLE ANSWER IS "STILL RUNNING", which is the only safe default: it refuses to
  // proceed. Reading it as settled would let an unparseable body take somebody's container.
  return 'still_running'
}

/**
 * ASK for the stop. Returns immediately with the state at the instant the ask landed — usually
 * `still_running`, because the unwind has barely begun.
 *
 * NOTHING HOLDS A REQUEST OPEN FOR THE LENGTH OF A STOP any more: the old shape's budget had
 * to sit under the request timeout of the gateway in front of the service, a number recorded
 * nowhere in this repo and owned by the client's network.
 */
export async function stopActiveBuild(projectId: string, deps: AuthFetchDeps = {}): Promise<StopState> {
  return readStopState(
    await postJson(
      `${BASE}/projects/${encodeURIComponent(projectId)}/stop-active-build`,
      undefined,
      'Could not stop the build in the other application',
      deps,
    ),
  )
}

/** READ the real state, from the source of truth rather than from elapsed time. */
export async function readStopStateOf(
  projectId: string,
  deps: AuthFetchDeps = {},
  signal?: AbortSignal,
): Promise<StopState> {
  return readStopState(
    await getJson(
      `${BASE}/projects/${encodeURIComponent(projectId)}/stop-state`,
      'Could not check on the other application',
      deps,
      signal,
    ),
  )
}

/**
 * HOW OFTEN TO ASK, AND FOR HOW LONG. Ordinary tuning; neither number constrains anything —
 * the read is cheap, nothing is held open, and the ceiling only decides when the dialog stops
 * saying "closing…" and starts saying it could not.
 *
 * DELIBERATELY BELOW THE SERVER'S OWN STOP BUDGET (~8 minutes: four bounded two-minute steps,
 * including the unwind's snapshot write). Nobody should sit in front of a modal for eight
 * minutes to learn whether they may switch projects, so the browser stops WAITING first, at two.
 *
 * EXPIRING HERE IS NOT A VERDICT. The stop runs as a detached server-side task; the state read
 * is the authority and is idempotent; the hand-over proceeds only on a settled answer. Reaching
 * this ceiling ends the WAIT, not the stop — a second press picks it up wherever it got to, and
 * nothing here may say it failed (the likely cause is a large app still being packed up, per
 * `handOverWorkspace`'s sentence).
 */
export const STOP_POLL_MS = 1200
export const STOP_CEILING_MS = 120_000
/**
 * HOW LONG ONE READ MAY HANG BEFORE IT IS ABANDONED — a REAL timer, not the injected clock's.
 *
 * The ceiling below is checked BETWEEN iterations, so it can only fire if each iteration
 * returns. `authFetch` sets no timeout of its own: a stalled connection never settles, the
 * `while` never re-evaluates, and the two-minute ceiling silently becomes forever, with the
 * dialog stuck holding Escape and its overlay click disabled. So every read is abandoned on
 * its own deadline and retried — the same treatment a dropped connection already gets.
 *
 * `setTimeout`, not `clock.sleep`: the injected clock's `sleep` resolves instantly for tests,
 * so racing a read against it would abandon every read in every test. This bound is about a
 * socket, not about pacing.
 */
const STOP_READ_TIMEOUT_MS = 15_000

/**
 * Wait for a stop to genuinely finish, narrating while it does.
 *
 * IT POLLS THE STATE RATHER THAN WATCHING A CLOCK — a container declared dead when merely slow
 * has destroyed unsaved work in this repo before, because a timeout was read as a verdict.
 *
 * A DROPPED CONNECTION IS NOT A VERDICT EITHER: a failed read is retried until the ceiling
 * rather than treated as "still running forever", because the stop itself runs server-side and
 * the state read is idempotent — the caller can simply ask again.
 *
 * BUT A DECIDED ANSWER IS NOT A BLIP, AND RETRYING ONE IS ITS OWN DEFECT. A session that expired
 * mid-hand-over answers 401 to every read; `authFetch` retries a refresh on each one, so the
 * dialog once sat on "Closing the other app…" for two minutes issuing a hundred refresh attempts
 * — traffic that trips reuse-detection — before answering with the ceiling's own sentence, which
 * had nothing to do with what actually failed. 403 (and a 404 from a project deleted in another
 * tab) behave the same way: the poll cannot change those answers, so they travel straight to the
 * dialog, which says the true thing immediately. Everything else — an abort, a dropped socket, a
 * 5xx — is still a blip.
 */
export interface StopWaitClock {
  now: () => number
  sleep: (ms: number) => Promise<void>
}

/** The real one. Injectable, because a test that actually waited two minutes for the ceiling
 *  would be a test nobody runs — and the ceiling's behaviour is exactly what must be proven. */
export const REAL_CLOCK: StopWaitClock = {
  now: () => Date.now(),
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
}

/**
 * The statuses a repeat of the same read can only answer the same way: the session is gone, this
 * user may not ask, or there is no such project. Deliberately NOT 5xx — a server that fell over
 * mid-stop may well be up on the next tick, and the stop is still running behind it.
 */
const DECIDED_STATUSES: ReadonlySet<number> = new Set([401, 403, 404])

const isDecided = (err: unknown): boolean => err instanceof ApiError && DECIDED_STATUSES.has(err.status)

export async function awaitStopSettled(
  projectId: string,
  deps: AuthFetchDeps = {},
  clock: StopWaitClock = REAL_CLOCK,
): Promise<StopState> {
  const { now, sleep } = clock
  const deadline = now() + STOP_CEILING_MS
  let last: StopState = 'still_running'
  while (now() < deadline) {
    const abandon = new AbortController()
    const bell = setTimeout(() => abandon.abort(), STOP_READ_TIMEOUT_MS)
    try {
      last = await readStopStateOf(projectId, deps, abandon.signal)
      if (stopSettled(last)) return last
    } catch (err) {
      if (isDecided(err)) throw err
      // Retried below. See the docblock: a read that failed — or was abandoned — decided nothing.
    } finally {
      clearTimeout(bell)
    }
    await sleep(STOP_POLL_MS)
  }
  return last
}

/** Hand the workspace over: STOP, then optionally SAVE, then RELEASE — what the refusal
 *  dialog's buttons do to the server, in the one order that works.
 *
 *  TAKES THE WHOLE `ReclaimBlocked`, NOT A BARE PROJECT ID (#198) — the one thing that makes
 *  the branch below impossible to drop at a call site again. Four surfaces (`StartAppControl`,
 *  `ProjectWorkspace`, `ConversationSurface`, and this page itself) each used to call this with
 *  `blocked.projectId` alone, and none of them learned that a shared occupant's `projectId`
 *  names its OWNER, never the caller — so `stopActiveBuild`'s `owned_project_or_404` 404'd
 *  every one of them. Passing the whole object means the discriminant travels with the id it
 *  qualifies, in one place, rather than needing to be re-remembered at every call site.
 *
 *  A SHARED OCCUPANT NEVER REACHES `stopActiveBuild`/`saveProject`/`releaseProject` AT ALL —
 *  none of the three would even resolve the right project for it. `giveUpSharedView` is the
 *  entire remedy: no id, no ownership check, reaping under the caller's own registry key.
 *  `save` is accepted but ignored on this arm — a shared view's `dirty` is always `false`, so
 *  `ReclaimWorkspaceDialog`'s own `copyFor` never even renders a Save button for it.
 *
 *  THE ORDER ON THE ORDINARY ARM IS THE DESIGN: save and release BOTH refuse while an agent is
 *  writing, so saving first would simply fail (or, past that guard, bundle a tree caught
 *  mid-edit) — stopping settles the turn first, and only then is there a coherent tree to save.
 *
 *  THE STOP IS UNCONDITIONAL, not gated on `ReclaimBlocked.building` (true only for a Write
 *  turn). Every mode pins the container and `release` refuses for all of them, so gating the
 *  stop on the Write-only flag would leave Ask/Plan turns stuck in the dead end this flow
 *  removes. Stopping when nothing is running is free.
 *
 *  IT WAITS FOR THE STOP TO GENUINELY FINISH: the ask returns immediately, the state read is
 *  the authority, and this proceeds only on a settled answer — a timeout reports still
 *  running and the transfer DOES NOT PROCEED (shipped confused before, without this).
 *
 *  REJECTS RATHER THAN SWALLOWS: a failed save must not be followed by a release, so the
 *  rejection travels back to the only thing still mounted that can report it. */
export async function handOverWorkspace(
  blocked: ReclaimBlocked,
  save: boolean,
  deps: AuthFetchDeps = {},
  narrate: (step: HandoverStep) => void = () => {},
  clock: StopWaitClock = REAL_CLOCK,
): Promise<void> {
  if (blocked.isSharedView) {
    // NARRATE AFTER, NOT BEFORE. A caller that reads its OWN narration callback as a record of
    // how far the hand-over got (`SharedProjectPage.tsx`, which infers "was anything stopped?"
    // from the last step it observed) must see NO step at all when this throws — nothing here
    // ever stops anything, on either outcome, so a step recorded before the call would make a
    // rejection look exactly like a successful stop of the OWNER's app.
    await giveUpSharedView(deps)
    narrate('releasing')
    return
  }
  const projectId = blocked.projectId
  narrate('stopping')
  const asked = await stopActiveBuild(projectId, deps)
  const settled = stopSettled(asked) ? asked : await awaitStopSettled(projectId, deps, clock)
  if (!stopSettled(settled)) {
    // NOT AN ERROR OF OURS, AND NOT A REASON TO TAKE THE CONTAINER. Everything the citizen has is
    // still where it was; what failed is the wait, and asking again is the remedy.
    throw new ApiError(
      // THE TRUE THING AT TWO MINUTES, which is not "that project failed". The
      // browser's ceiling is under the server's on purpose — see `STOP_CEILING_MS` — so arriving
      // here almost always means the other app is still putting its work away, and the wait is
      // what ran out rather than the stop. Nothing has been taken from either project, and asking
      // again resumes the same stop instead of starting a second one.
      'The other app is still saving its work. Nothing has changed — give it a moment and try again.',
      409,
      'stop_did_not_settle',
    )
  }
  if (save) {
    narrate('saving')
    await saveProject(projectId, deps)
  }
  narrate('releasing')
  await releaseProject(projectId, deps)
}

/**
 * What a hand-over is doing right now, so the dialog can say it rather than spin.
 *
 * STOPS AT `starting` DELIBERATELY. An `opening` member once existed for the chat being
 * opened, but nothing could ever produce it: the retry's last act is a navigate, which
 * unmounts the dialog-publishing surface in the same commit that would carry the step. The
 * destination narrates its own wait, so an unreachable member was dead code with copy attached.
 */
export type HandoverStep = 'stopping' | 'saving' | 'releasing' | 'starting'

/** The project standing in the way, read off a `sandbox_reclaim_blocked` 409. */
export interface ReclaimBlocked {
  projectId: string
  projectName: string
  /** TRI-STATE like `SaveState.dirty`: `true` = known unsaved work, `null` = the server reached
   *  the workspace but could not ask it. Both block; only the copy differs, because promising
   *  "nothing to lose" when nobody could check is the one wrong answer available here.
   *  Always `null` when `building` — see below. */
  dirty: boolean | null
  /** An agent is WRITING in that project right now, so this is a different choice with a
   *  different cost: resolving it stops work in progress, not just a container.
   *
   *  `dirty` is null here because the server deliberately did not ask — a `git status` taken
   *  mid-write describes an instant nobody cares about — so the dialog must not say "unsaved
   *  changes". And Save/Release both refuse until the build stops, which is why this variant
   *  runs `stopActiveBuild` first instead of offering them directly. */
  building: boolean
  /**
   * AN AGENT IS MID-TURN IN THERE, OF ANY KIND — deliberately wider than `building`, and a
   * separate field. `building` marks only write-capable turns; widening it would show a stop
   * button and hammer icon to someone who only asked a question, short-circuiting the escape
   * hatch that lets a pristine container be reclaimed without asking. This field carries the
   * wider sentence instead — "their agent is still working" — over a workspace already
   * reported as holding nothing to lose.
   *
   * Absent reads as false: an older backend that omits it cannot have an agent to report, and
   * defaulting the other way would falsely mark every citizen's other project busy.
   */
  agentWorking: boolean
  /**
   * WHICH REMEDY ACTUALLY WORKS (#198). `projectId`/`projectName` above name a project the
   * CALLER OWNS when this is an ordinary build occupant — `stopActiveBuild`/`release` both
   * gate on `owned_project_or_404`, which that caller satisfies. When `isSharedView` is true,
   * the occupant is a colleague's shared view and `projectId` names its OWNER instead, whom a
   * recipient never owns — those same two routes would 404 them out of their own slot. Route
   * to `giveUpSharedView` instead, which needs no project id at all.
   *
   * Absent reads as false: an older backend that omits it never produced a shared occupant.
   */
  isSharedView: boolean
}

/** Narrow a thrown error to the refusal, or `null` for anything else.
 *
 *  Branch on the CODE, never the 409 alone: the same status also carries
 *  `build_session_already_active`, which has no remedy — treating the two alike would offer a
 *  Save button for a build that is simply still running.
 *
 *  STRUCTURAL, not `instanceof`: the refusal arrives as two different error types (`ApiError`
 *  from `relaunchPreview`, `TurnStartError` from `startTurn`) sharing the same `{code, details}`
 *  shape, so keying on the shape works on whichever path the user took. */
export function asReclaimBlocked(err: unknown): ReclaimBlocked | null {
  if (!isRecord(err) || err.code !== 'sandbox_reclaim_blocked') return null
  const d = err.details
  if (!isRecord(d) || typeof d.projectId !== 'string' || typeof d.projectName !== 'string') {
    return null
  }
  return {
    projectId: d.projectId,
    projectName: d.projectName,
    dirty: typeof d.dirty === 'boolean' ? d.dirty : null,
    // Absent reads as false — an older backend that does not send the field cannot have a
    // build to report, and defaulting the other way would show the stop dialog for a project
    // nobody is building.
    building: d.building === true,
    agentWorking: d.agentWorking === true,
    isSharedView: d.isSharedView === true,
  }
}

/**
 * Give up whatever colleague's shared view currently holds the caller's OWN slot (#198,
 * requirement 24's self-service exit). No `project_id`, because a `ReclaimBlocked` naming a
 * shared occupant carries its OWNER's project — never the recipient's — so neither
 * `stopActiveBuild` nor `release` can be reached with an id that passes `owned_project_or_404`;
 * this is the one door a recipient can always use, regardless of whether they have ever built
 * anything of their own. `released: false` is a success — nothing was there to give up, or what
 * was there was the caller's own build sandbox instead (not this function's job).
 */
export async function giveUpSharedView(deps: AuthFetchDeps = {}): Promise<boolean> {
  const body = await postJson(
    `${BASE}/shared-view/release`,
    undefined,
    'Could not close that shared app',
    deps,
  )
  return isRecord(body) && body.released === true
}

/** What is (or is not) serving a project's preview right now.
 *
 *  THREE STATES, and a read that could not decide is none of them — it throws:
 *
 *   - `alive`    — a container is serving this project; `previewUrl` is framable.
 *   - `starting` — a build, relaunch, or turn sandbox-start is IN FLIGHT, or a container is up
 *                  and has not served yet. Grouped with `alive` as "just a wait", never as a
 *                  "gone" state inviting a remedy.
 *   - `asleep`   — nothing serving it and nothing starting: never built, put away, or another
 *                  of this user's projects holds the workspace. `restorable` says whether there
 *                  is work to bring back. NOT a failure — never styled as one. */
export const PREVIEW_LIFE_STATES = ['alive', 'asleep', 'starting'] as const
export type PreviewLifeState = (typeof PREVIEW_LIFE_STATES)[number]

export interface PreviewState {
  state: PreviewLifeState
  /** Strictly `state === 'alive'`. Kept because the server keeps it; branch on `state`. */
  alive: boolean
  previewUrl: string | null
  /** TRI-STATE, exactly like `SaveState.dirty`: `true` = the server could restore this app
   *  from the recovery copy or the saved bundle, `false` = confirmed it could not, `null` =
   *  NO CLAIM, so the UI promises nothing and keeps whatever it already knew. Two ways to
   *  reach that null and they mean the same thing to us: the object store was unreachable, or
   *  `state` is `alive` or `starting` and the poll did not ask (the answer could not change the
   *  screen and is not worth a Blob round trip every 45 seconds). This is why `hasSavedBuild`
   *  reads it with `??` and not `||`. */
  restorable: boolean | null
  /** `starting` only: the ISO-8601 instant THIS project's wait began, so an elapsed figure is
   *  the real wait rather than the life of the current page. `null` is NO CLAIM — a surface
   *  counting from its own mount is what the pane did before this field existed, and it stays
   *  the fallback. The server answers it to the second, so the same wait reads as the same
   *  instant on every poll and this can be compared like any other field. */
  startingSince: string | null
}

/** Two readings that say the same thing — see `sameSaveState`. `alive` is omitted deliberately:
 *  its own docblock above pins it to `state === 'alive'`, so comparing it could only ever agree
 *  with the comparison of `state` that is already here. */
export const samePreviewState = (a: PreviewState | null, b: PreviewState | null): boolean =>
  a === b ||
  (a !== null &&
    b !== null &&
    a.state === b.state &&
    a.previewUrl === b.previewUrl &&
    a.restorable === b.restorable &&
    a.startingSince === b.startingSince)

const UNREADABLE_PREVIEW = 'The server returned a preview state we could not read.'

function asPreviewLifeState(value: unknown, alive: boolean): PreviewLifeState {
  // An unrecognised (or absent) state falls back to what `alive` can prove and NO further: a
  // live container is `alive`, and anything else is a read that decided nothing — never a
  // confident "gone". The fallback exists for a tab that outlives a deploy, not as a normal path.
  const known = PREVIEW_LIFE_STATES.find((s) => s === value)
  if (known !== undefined) return known
  if (alive) return 'alive'
  throw new ApiError(UNREADABLE_PREVIEW, 500)
}

/** Is the preview this tab is framing still real — and if not, why?
 *
 *  A reclaimed preview is visually IDENTICAL to a working one — the last render stays on
 *  screen and a cross-origin pane cannot read a status code. Once a build ends there is no
 *  SSE and no timer left, and the teardown happens inside another project's request, so
 *  nothing can be pushed here — the tab has to ask.
 *
 *  THROWS ON ANYTHING THAT IS NOT AN ANSWER: a non-2xx (the server's 503 for a coordination
 *  store it could not read among them), a body that is not an object, or a state this client
 *  does not know. Both polls read a throw as a check that decided nothing, which is the only
 *  honest reading of any of the three.
 *
 *  Cheap by contract: one Redis hash read, two rows, at most two object-store HEADs, no
 *  container call — unlike `fetchSaveState`, which runs two `git` execs per call. */
export async function fetchPreviewState(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<PreviewState> {
  const res = await authFetch(
    `${BASE}/projects/${encodeURIComponent(projectId)}/preview-state`,
    {},
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Could not check the preview')
  const body: unknown = await res.json().catch(() => null)
  if (!isRecord(body)) throw new ApiError(UNREADABLE_PREVIEW, 500)
  const alive = body.alive === true
  return {
    state: asPreviewLifeState(body.state, alive),
    alive,
    previewUrl: typeof body.previewUrl === 'string' ? body.previewUrl : null,
    // Anything that is not literally a boolean stays UNKNOWN — the same rule `dirty` follows,
    // and for the same reason: coercing here is how a missing field becomes a false promise.
    restorable: typeof body.restorable === 'boolean' ? body.restorable : null,
    // Same discipline again: a non-string is NO CLAIM, and so is a string no clock can read.
    // Dating a wait from garbage would put the lying counter back wearing a server's face.
    startingSince:
      typeof body.startingSince === 'string' && !Number.isNaN(Date.parse(body.startingSince))
        ? body.startingSince
        : null,
  }
}

/**
 * What is the app compiling right now — for a tab with NO LIVE TURN.
 *
 * A turn's `compile` frame stops at the terminal, so a tab reloaded after a red turn has
 * nothing covering a broken preview and reads the framework's error screen under a
 * live-preview label; this call is the producer that outlives the turn.
 *
 * Its own call, not a field on `preview-state` (whose budget is frozen at no container call).
 * Unreadable answers `unknown` — the pane HOLDS its cover, never `clean` — and this never
 * throws: a signal about an app that may already be broken must not become a second failure.
 */
export async function fetchCompileState(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<CompileState> {
  try {
    const res = await authFetch(
      `${BASE}/projects/${encodeURIComponent(projectId)}/compile-state`,
      {},
      deps,
    )
    if (!res.ok) return 'unknown'
    const body: unknown = await res.json().catch(() => null)
    return asCompileState(isRecord(body) ? body.state : null)
  } catch {
    return 'unknown'
  }
}

/**
 * Is the app this tab is framing still the citizen's app?
 *
 * THE TURN MAY NEVER COME: every other integrity check runs at the start of a turn, which
 * catches a reversion between messages but nothing for someone reading, in another tab, or at
 * lunch — a completion claim stays displayed for as long as the page stays open.
 *
 * ONLY A POSITIVE `reverted` MEANS ANYTHING — the server's own boolean, not derived here. Four
 * states can come back and only one may retract a claim; `state !== 'intact'` would also
 * retract on the two that mean "we could not tell", which this verdict exists to prevent.
 *
 * NEVER THROWS, answering `false` on anything unreadable — this runs on a background timer
 * beside a preview the citizen is looking at, and a probe that failed the page would be a
 * second failure caused by the check for the first one.
 */
export async function checkWorkspace(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<boolean> {
  try {
    const res = await authFetch(
      `${BASE}/projects/${encodeURIComponent(projectId)}/workspace-check`,
      { method: 'POST' },
      deps,
    )
    if (!res.ok) return false
    const body: unknown = await res.json().catch(() => null)
    return isRecord(body) && body.reverted === true
  } catch {
    return false
  }
}

/** Untrusted body → `SaveState`. Every field is whitelisted one at a time and narrowed the same
 *  way: anything that is not literally the expected type falls back to `null` rather than being
 *  coerced — a fabricated value here would claim a state nobody actually checked. Shared by
 *  `fetchSaveState` and `discardUnsavedChanges`, which read the identical shape. */
function toSaveState(body: unknown): SaveState {
  if (!isRecord(body)) throw new ApiError('The server returned a save state we could not read.', 500)
  return {
    appId: typeof body.appId === 'string' ? body.appId : null,
    dirty: typeof body.dirty === 'boolean' ? body.dirty : null,
    containerHead: typeof body.containerHead === 'string' ? body.containerHead : null,
    savedHead: typeof body.savedHead === 'string' ? body.savedHead : null,
  }
}

/** Is there unsaved work? Compared by COMMIT server-side, so it survives a reload and a
 *  second tab — neither of which a local dirty flag would. */
export async function fetchSaveState(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<SaveState> {
  const res = await authFetch(
    `${BASE}/projects/${encodeURIComponent(projectId)}/save-state`,
    {},
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Could not check for unsaved work')
  return toSaveState(await res.json().catch(() => null))
}

export interface DiscardNotice {
  seq: number
  savedAt: string | null
}

export interface DiscardResult {
  saveState: SaveState
  notice: DiscardNotice | null
}

/** A notice that is not a record, or carries no numeric `seq`, is not a notice. */
function toDiscardNotice(value: unknown): DiscardNotice | null {
  if (!isRecord(value) || typeof value.seq !== 'number') return null
  return { seq: value.seq, savedAt: typeof value.savedAt === 'string' ? value.savedAt : null }
}

/** Put the app back to the version its owner last saved. `conversationId` names the chat the
 *  press came from, so that chat's own transcript gets a `notice` back without a reload — `null`
 *  outside a chat sends an empty body rather than a null field. */
export async function discardUnsavedChanges(
  projectId: string,
  conversationId: string | null,
  deps: AuthFetchDeps = {},
): Promise<DiscardResult> {
  const body = await postJson(
    `${BASE}/projects/${encodeURIComponent(projectId)}/discard`,
    conversationId !== null ? { conversationId } : {},
    'Could not discard your changes',
    deps,
  )
  return {
    saveState: toSaveState(body),
    notice: isRecord(body) ? toDiscardNotice(body.notice) : null,
  }
}

/**
 * WHETHER A SCREEN IS ON SCREEN, which is the whole of what the renewal below sends.
 *
 * A word rather than a number of seconds: the server maps it to a budget, and a client that
 * named its own would be a client that could ask a container to live longer than the platform's
 * ceiling allows.
 */
export type SurfacePresence = 'visible' | 'hidden'

/**
 * The three things a renewal can mean, mirroring the server's closed set exactly. Nothing is
 * rendered from any of them — see `renewPresence` for why a failure here says nothing.
 */
export type RenewalOutcome = 'renewed' | 'not_this_container' | 'nothing_running'

/** What a renewal answered. */
export interface Renewal {
  outcome: RenewalOutcome
}

/**
 * Tell the platform a screen that can frame this project is still open, so its container stays.
 *
 * PRESENCE IS THE SIGNAL, AND SILENCE IS DEPARTURE. Nothing is sent when somebody leaves:
 * navigating away, closing the tab, sleeping the machine and losing the network all simply stop
 * the renewals. That is the whole mechanism — there is no departure message that can fail to
 * arrive, and no handler on an unload path to get wrong.
 *
 * IT NEVER THROWS AND IT NEVER REPORTS. A renewal that could not be made says nothing about the
 * container: 401, 403 and 503 are facts about the request, not about the app, and a screen that
 * painted "your workspace is going away" on one would be over-claiming from an outage. A lease
 * that genuinely lapsed reaches the citizen through `fetchPreviewState`, which is the one read
 * allowed to say a preview is gone. The outcome is returned for callers that re-arm their poll
 * on it, and `null` means the ask itself did not complete.
 */
export async function renewPresence(
  projectId: string,
  presence: SurfacePresence,
  deps: AuthFetchDeps = {},
): Promise<Renewal | null> {
  try {
    const res = await authFetch(
      `${BASE}/projects/${encodeURIComponent(projectId)}/renew`,
      {
        method: 'POST',
        headers: { ...JSON_HEADERS, ...csrfHeaders() },
        body: JSON.stringify({ presence }),
      },
      deps,
    )
    if (!res.ok) return null
    const body: unknown = await res.json().catch(() => null)
    if (!isRecord(body)) return null
    const outcome = body.outcome
    if (outcome !== 'renewed' && outcome !== 'not_this_container' && outcome !== 'nothing_running') {
      return null
    }
    return { outcome }
  } catch {
    return null
  }
}

