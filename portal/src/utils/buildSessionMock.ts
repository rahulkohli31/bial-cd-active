/**
 * A contract-faithful C3/C7 mock: the cockpit is built and tested against it because
 * SESSION-API (the real C3 producer) is a sibling Wave-1 track whose endpoints do not
 * exist yet, and this track merges AFTER them (origin §6). The real client is the
 * wired default (`buildSessionClient`); this mock is a test/dev module. When the real
 * endpoints land, the cockpit points at them with no rework.
 *
 * Three things live here:
 *   1. `FakeEventSource` — a hand-driven `EventSourceLike` (no `EventSource` exists in
 *      jsdom, and none was mocked before — net-new). Tests emit frames / trigger the
 *      error arm on it directly.
 *   2. Scripted C7 envelope sequences (happy / quota / escalation) + a factory that
 *      auto-plays one over a `FakeEventSource` (dev harness + integration-style tests).
 *   3. A canned `BuildSessionClient` (+ a stateful coordinated mock) for the control
 *      operations.
 *
 * The envelope keys stay snake_case (C7) and REST bodies camelCase (C3) — the mock is
 * only faithful if its wire shapes match the contracts byte-for-byte (KTD-5).
 */
import { isRecord } from './apiError'
import { BuildSessionAlreadyActiveError } from './buildSessionApi'
import type { BuildSessionClient } from './buildSessionApi'
import type { EventSourceFactory, EventSourceLike } from './buildSessionEvents'
import type {
  BuildSessionStatusResponse,
  ForceEndResponse,
  HeartbeatResponse,
  LockReleaseResponse,
  LockStateResponse,
  ProgressEnvelope,
  StartBuildResponse,
  StopBuildResponse,
} from './buildSessionTypes'
import { HEARTBEAT_CADENCE_SECONDS, LOCK_TTL_SECONDS } from './buildSessionApi'

const READY_STATE = { CONNECTING: 0, OPEN: 1, CLOSED: 2 } as const

/** A concrete sandbox FQDN the mock frames — the un-prefixed `next dev` root (C2/C8 §5). */
export const MOCK_PREVIEW_URL = 'https://app-mock-session.example.azurecontainerapps.io/'
export const MOCK_SESSION_ID = 'mock-session-0000'
export const MOCK_APP_ID = 'mock-app-0000'

// ─── FakeEventSource — a hand-driven EventSourceLike ─────────────────────────

/**
 * A test/dev double for a native `EventSource`. Starts CONNECTING; a test drives it
 * with `open()` / `emit()` / `emitEnvelope()` and the two error arms
 * (`failNeverOpened()` = admission failure, `dropAfterOpen()` = transient drop). The
 * consumer's `close()` sets CLOSED, and `closeCalls` records how many times it fired
 * (the terminal-close assertion).
 */
export class FakeEventSource implements EventSourceLike {
  readyState: number = READY_STATE.CONNECTING
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  onopen: ((ev: Event) => void) | null = null
  readonly url: string
  closeCalls = 0

  constructor(url: string) {
    this.url = url
  }

  /** Simulate the connection opening (first byte). */
  open(): void {
    this.readyState = READY_STATE.OPEN
    this.onopen?.(new Event('open'))
  }

  /** Push one raw `data:` payload (a JSON envelope string, or the `[DONE]` sentinel). */
  emit(data: string): void {
    this.onmessage?.(new MessageEvent('message', { data }))
  }

  /** Push one typed envelope, JSON-encoded exactly as the wire would carry it. */
  emitEnvelope(env: ProgressEnvelope): void {
    this.emit(JSON.stringify(env))
  }

  /** The never-opened admission failure (401/404, C3 §4.1): CLOSED + `error`, no retry. */
  failNeverOpened(): void {
    this.readyState = READY_STATE.CLOSED
    this.onerror?.(new Event('error'))
  }

  /** A transient drop after a prior open: CONNECTING + `error` (the browser would auto-retry). */
  dropAfterOpen(): void {
    this.readyState = READY_STATE.CONNECTING
    this.onerror?.(new Event('error'))
  }

  close(): void {
    this.closeCalls += 1
    this.readyState = READY_STATE.CLOSED
  }
}

// ─── scripted C7 envelope sequences (gap-free `seq`, snake_case) ─────────────

/** The happy path: scaffold → install → dev_start → preview_ready → ended(completed). */
export function happyBuildScript(previewUrl: string = MOCK_PREVIEW_URL): ProgressEnvelope[] {
  return [
    { type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding the app…', state: 'started' },
    { type: 'step', seq: 2, name: 'scaffold', label: 'Scaffolding the app…', state: 'ok' },
    { type: 'step', seq: 3, name: 'install_deps', label: 'Installing dependencies…', state: 'started' },
    { type: 'log', seq: 4, source: 'exec', stream: 'stdout', text: 'added 312 packages in 6s' },
    { type: 'step', seq: 5, name: 'install_deps', label: 'Installing dependencies…', state: 'ok' },
    { type: 'step', seq: 6, name: 'dev_start', label: 'Starting the dev server…', state: 'started' },
    { type: 'preview_ready', seq: 7, preview_url: previewUrl },
    { type: 'step', seq: 8, name: 'dev_start', label: 'Starting the dev server…', state: 'ok' },
    { type: 'ended', seq: 9, status: 'ended', preview_url: previewUrl, snapshot_committed: true, reason: 'completed' },
  ]
}

/**
 * The C7 §8 graceful-quota terminal: a model step, then `quota_exceeded`, then an
 * `ended` with `status: 'ended'` / `reason: 'quota_exceeded'` — DISTINCT from the
 * `escalation → ended{status:'failed'}` fork (this must resolve ENDED, not FAILED).
 */
export function quotaBuildScript(resetsAt: string): ProgressEnvelope[] {
  return [
    { type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding the app…', state: 'started' },
    { type: 'step', seq: 2, name: 'self_heal', label: 'Repairing a type error…', state: 'started' },
    { type: 'quota_exceeded', seq: 3, limit: 1_000_000, used: 1_000_000, resets_at: resetsAt },
    { type: 'ended', seq: 4, status: 'ended', preview_url: null, snapshot_committed: true, reason: 'quota_exceeded' },
  ]
}

/** The unrecoverable fork: `escalation` then `ended{status:'failed'}` — proves ENDED and FAILED are distinct. */
export function escalationBuildScript(): ProgressEnvelope[] {
  return [
    { type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding the app…', state: 'started' },
    { type: 'error', seq: 2, source: 'tsc', title: 'Type error in app/page.tsx', cleaned_stack: "app/page.tsx(12,5): Type 'string' is not assignable to type 'number'." },
    { type: 'escalation', seq: 3, reason: 'self_heal_budget_exhausted', detail: 'Could not repair the build after several attempts.', last_error: { source: 'tsc', title: 'Type error in app/page.tsx', cleaned_stack: 'app/page.tsx(12,5): …' } },
    { type: 'ended', seq: 4, status: 'failed', preview_url: null, snapshot_committed: false, reason: 'escalated' },
  ]
}

/**
 * A factory that auto-plays a script over a `FakeEventSource`, one frame per
 * `stepMs`, then closes with a `[DONE]` sentinel — for the dev harness and
 * integration-style tests (drive it with fake timers). For unit tests, prefer a bare
 * `FakeEventSource` and emit frames by hand.
 */
export function createScriptedEventSourceFactory(
  script: ProgressEnvelope[],
  opts: { stepMs?: number } = {},
): EventSourceFactory {
  const stepMs = opts.stepMs ?? 400
  return (url: string): EventSourceLike => {
    const source = new FakeEventSource(url)
    // Kick off asynchronously so the consumer has assigned its handlers first.
    setTimeout(() => {
      source.open()
      script.forEach((env, i) => {
        setTimeout(() => source.emit(JSON.stringify(env)), stepMs * (i + 1))
      })
      setTimeout(() => source.emit('[DONE]'), stepMs * (script.length + 1))
    }, 0)
    return source
  }
}

/** A factory whose stream never opens — the admission-failure (401/404) arm (C3 §4.1). */
export function createAdmissionFailureFactory(): EventSourceFactory {
  return (url: string): EventSourceLike => {
    const source = new FakeEventSource(url)
    setTimeout(() => source.failNeverOpened(), 0)
    return source
  }
}

// ─── canned C3 control client ─────────────────────────────────────────────────

function nowIso(): string {
  return new Date().toISOString()
}

function laterIso(seconds: number): string {
  return new Date(Date.now() + seconds * 1000).toISOString()
}

export interface MockClientOptions {
  sessionId?: string
  projectId?: string
  appId?: string
  /** When set, `getStatus` reports this url and status `ready`; else null / `provisioning`. */
  previewUrl?: string | null
  /** When true, `start` rejects with `BuildSessionAlreadyActiveError` (the 409 already-active path). */
  alreadyActive?: boolean
  ownerUserId?: string
}

/**
 * A canned `BuildSessionClient` — every op resolves the contract-shaped happy
 * response (or, with `alreadyActive`, `start` rejects 409). Stateless: pair it with a
 * `FakeEventSource` / scripted factory for the feed side.
 */
export function createMockBuildSessionClient(opts: MockClientOptions = {}): BuildSessionClient {
  const sessionId = opts.sessionId ?? MOCK_SESSION_ID
  const projectId = opts.projectId ?? 'mock-project-0000'
  const appId = opts.appId ?? MOCK_APP_ID
  const ownerUserId = opts.ownerUserId ?? 'mock-user-0000'
  const previewUrl = opts.previewUrl ?? null

  const lockState = (): LockStateResponse => ({ sessionId, held: true, ownerUserId, ttlSeconds: LOCK_TTL_SECONDS, expiresAt: laterIso(LOCK_TTL_SECONDS) })

  return {
    start: async (): Promise<StartBuildResponse> => {
      if (opts.alreadyActive) throw new BuildSessionAlreadyActiveError('You already have a build running.', sessionId)
      return { sessionId, projectId, appId, status: 'provisioning', previewUrl: null, createdAt: nowIso() }
    },
    stop: async (): Promise<StopBuildResponse> => ({ sessionId, status: 'ended' }),
    getStatus: async (): Promise<BuildSessionStatusResponse> => ({
      sessionId,
      projectId,
      appId,
      status: previewUrl ? 'ready' : 'provisioning',
      previewUrl,
      lastSeq: null,
      createdAt: nowIso(),
      updatedAt: nowIso(),
    }),
    acquireLock: async (): Promise<LockStateResponse> => lockState(),
    renewLock: async (): Promise<LockStateResponse> => lockState(),
    releaseLock: async (): Promise<LockReleaseResponse> => ({ sessionId, released: true }),
    forceEnd: async (): Promise<ForceEndResponse> => ({ sessionId, status: 'ended' }),
    heartbeat: async (): Promise<HeartbeatResponse> => ({ sessionId, alive: true, cadenceSeconds: HEARTBEAT_CADENCE_SECONDS, heartbeatExpiresAt: laterIso(HEARTBEAT_CADENCE_SECONDS * 3) }),
  }
}

/**
 * A stateful, coordinated mock for a manual dev harness: `start` seeds a session,
 * the feed plays the happy script, and `getStatus` reflects the framed `previewUrl`
 * once `preview_ready` has played. Returns the `client` + `eventSourceFactory` that
 * U4's hook accepts.
 */
export function createMockBuildSession(opts: { previewUrl?: string; stepMs?: number } = {}): {
  client: BuildSessionClient
  eventSourceFactory: EventSourceFactory
} {
  const previewUrl = opts.previewUrl ?? MOCK_PREVIEW_URL
  const state = { previewUrl: null as string | null, status: 'provisioning' as BuildSessionStatusResponse['status'] }

  const script = happyBuildScript(previewUrl)
  const baseFactory = createScriptedEventSourceFactory(script, { stepMs: opts.stepMs })
  const eventSourceFactory: EventSourceFactory = (url) => {
    const source = baseFactory(url)
    const originalOnmessage = { current: source.onmessage }
    // Reflect preview readiness into the shared state so getStatus agrees with the feed.
    Object.defineProperty(source, 'onmessage', {
      get: () => originalOnmessage.current,
      set: (fn: ((ev: MessageEvent) => void) | null) => {
        originalOnmessage.current = fn
          ? (ev: MessageEvent) => {
              try {
                const parsed: unknown = JSON.parse(String(ev.data))
                if (isRecord(parsed) && parsed.type === 'preview_ready') {
                  state.previewUrl = previewUrl
                  state.status = 'ready'
                }
              } catch {
                // non-JSON (e.g. [DONE]) — ignore for state tracking
              }
              fn(ev)
            }
          : null
      },
    })
    return source
  }

  const client: BuildSessionClient = {
    ...createMockBuildSessionClient({ previewUrl }),
    getStatus: async (): Promise<BuildSessionStatusResponse> => ({
      sessionId: MOCK_SESSION_ID,
      projectId: 'mock-project-0000',
      appId: MOCK_APP_ID,
      status: state.status,
      previewUrl: state.previewUrl,
      lastSeq: null,
      createdAt: nowIso(),
      updatedAt: nowIso(),
    }),
  }

  return { client, eventSourceFactory }
}
