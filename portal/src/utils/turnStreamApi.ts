/**
 * The U10 turn transport client: start/stop turns and read the per-conversation SSE
 * event stream with a PROPER carry buffer (the streamed-reply learning — a frame split
 * across TCP chunks must reassemble; `useClaudeAPI`'s reader lacked one and is retired
 * with the relay in U13).
 *
 * Wire contract (server: `api/v1/conversations/turns.py`): frames are
 * `id: {seq}\ndata: {json}\n\n`, `: ping` comments ride between complete frames, and
 * `data: [DONE]` closes the transport. The union discriminates on `type`; UNKNOWN types
 * are surfaced to the caller but never throw — streams stay forward-extensible.
 */

import { authFetch } from './api.js'
import { readApiError } from './apiError'

// ---------------------------------------------------------------------------------------
// Frame types (mirror backend `conversations/schemas.py`; camelCase on the wire)
// ---------------------------------------------------------------------------------------

export interface StepDetail {
  args?: string | null
  result?: string | null
}

export interface StepItem {
  type: 'step'
  seq: number
  mode: string
  tool: string
  label: string
  state: 'ok' | 'failed' | 'pending'
  hidden: boolean
  detail: StepDetail
}

/** Projection items ride the snapshot verbatim (U6 shapes); the hook re-exposes them. */
export interface ProjectionItem {
  type: string
  seq: number
  [key: string]: unknown
}

/** The Build it / Keep refining card state (U11) — identical live and on reload. */
export interface PlanOptionsItem {
  type: 'plan_options'
  seq: number
  mode: string
  toolCallId: string
  state: 'pending' | 'refine' | 'build' | 'build_failed'
  reason?: string | null
}

export interface SnapshotFrame {
  type: 'snapshot'
  seq: number
  turnId: string | null
  turnStatus: 'idle' | 'running' | 'completed' | 'failed' | 'stopped'
  items: ProjectionItem[]
  textSoFar: string
  steps: StepItem[]
  /** WHY a failed turn failed. The in-band `error` frame lives only in the ring, so a
   *  subscriber that arrives after the failure reads the reason from here or not at all. */
  errorMessage: string | null
  /** The Write turn's newest workspace/preview facts, for the same catch-up reason: a
   *  `preview` frame that fired before this tab connected is gone from the ring, so a
   *  mid-build reconnect recovers from here instead of a second REST round-trip. */
  workspaceState?: 'preparing' | 'ready' | 'unavailable' | null
  previewUrl?: string | null
  previewState?: 'ready' | 'reconnecting' | null
}

export interface TextDeltaFrame {
  type: 'text_delta'
  seq: number
  text: string
}

export interface StepFrame {
  type: 'step'
  seq: number
  toolCallId: string
  phase: 'started' | 'finished'
  item: StepItem
}

export interface PlanOptionsFrame {
  type: 'plan_options'
  seq: number
  item: PlanOptionsItem
}

export interface TurnErrorFrame {
  type: 'error'
  seq: number
  message: string
}

export interface TurnEndedFrame {
  type: 'turn_ended'
  seq: number
  turnId: string
  status: 'completed' | 'failed' | 'stopped'
  /** WHY a non-completed turn stopped: `quota_exceeded` | `self_heal_budget_exhausted` |
   *  `sandbox_gone` | `wall_clock_deadline_exceeded` | `request_limit` | `stopped_by_user`. */
  reason?: string | null
  previewUrl?: string | null
  /** TRI-STATE, and the distinction matters: `null` means UNKNOWN (a chat turn, or a
   *  terminal that never reached the save), `false` means the save ran and did not land.
   *  Collapsing the two tells a citizen their work is gone when it is very likely on disk. */
  snapshotCommitted?: boolean | null
}

/** The turn's sandbox lifecycle. `preparing` is a 30-60s cold provision or restore — it is a
 *  frame rather than a blocked POST precisely so the wait can be narrated. */
export interface WorkspaceFrame {
  type: 'workspace'
  seq: number
  state: 'preparing' | 'ready' | 'unavailable'
  message?: string | null
}

/** The live preview. A NEW url remounts the iframe; `reconnecting` says the dev process died
 *  and a re-frame is coming — a fact only the server can report (`/dev/status` is guarded). */
export interface PreviewFrame {
  type: 'preview'
  seq: number
  state: 'ready' | 'reconnecting'
  previewUrl?: string | null
}

/** An in-narrative build diagnostic. NOT a failure: a repair run follows, and rendering it as
 *  an error would tell the user their build died on its way to succeeding. */
export interface DiagnosticFrame {
  type: 'diagnostic'
  seq: number
  source: 'tsc' | 'next_build' | 'server' | 'client'
  title: string
  cleanedStack: string
}

/** The daily cap, hit mid-turn. Structured so the client can format the numbers itself. */
export interface QuotaFrame {
  type: 'quota'
  seq: number
  limit: number
  used: number
  resetsAt: string
}

export interface UnknownFrame {
  type: string
  seq: number
  [key: string]: unknown
}

export type KnownTurnFrame =
  | SnapshotFrame
  | TextDeltaFrame
  | StepFrame
  | PlanOptionsFrame
  | TurnErrorFrame
  | TurnEndedFrame
  | WorkspaceFrame
  | PreviewFrame
  | DiagnosticFrame
  | QuotaFrame

export type TurnFrame = KnownTurnFrame | UnknownFrame

// A new frame type needs BOTH this set and a `KnownTurnFrame` member: listed in only one, it
// still parses — as `UnknownFrame` — so the forward-compat escape hatch silently swallows our
// own frame instead of the foreign one it exists for. Mirrors the server's `_KNOWN_FRAME_TAGS`.
const KNOWN_FRAME_TYPES = new Set([
  'snapshot',
  'text_delta',
  'step',
  'plan_options',
  'error',
  'turn_ended',
  'workspace',
  'preview',
  'diagnostic',
  'quota',
])

export function isKnownFrame(frame: TurnFrame): frame is KnownTurnFrame {
  return KNOWN_FRAME_TYPES.has(frame.type)
}

// ---------------------------------------------------------------------------------------
// Timing contract (cross-repo inequality, test-pinned on BOTH sides)
// ---------------------------------------------------------------------------------------

/**
 * The reader's stall window. The server emits `: ping` keepalives every 15s
 * (`turns.py KEEPALIVE_SECONDS`) — 60s gives a 4x margin, so a couple of delayed pings
 * can never false-trip the watchdog while a genuinely dead socket is detected within a
 * minute. Pinned by test here and in `test_turn_stream.py` server-side.
 */
export const TURN_STREAM_STALL_TIMEOUT_MS = 60_000

// ---------------------------------------------------------------------------------------
// The carry-buffered SSE parse (pure — the unit under test)
// ---------------------------------------------------------------------------------------

export interface ParsedChunk {
  frames: TurnFrame[]
  /** The unterminated remainder — carry it into the next chunk (NEVER discard). */
  rest: string
  sawDone: boolean
}

/**
 * Split accumulated SSE text into complete frames + the carry remainder. A block without
 * its terminating blank line stays in `rest` untouched — a frame torn across chunks
 * reassembles on the next call. Malformed JSON in a data line throws (a KNOWN-shape
 * corruption must never be silently dropped); unknown frame `type`s parse fine and are
 * left to the caller.
 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function toStepDetail(value: unknown): StepDetail {
  const doc = isRecord(value) ? value : {}
  return {
    args: typeof doc.args === 'string' ? doc.args : null,
    result: typeof doc.result === 'string' ? doc.result : null,
  }
}

function toStepItem(value: unknown): StepItem | null {
  if (!isRecord(value)) return null
  const state = value.state
  return {
    type: 'step',
    seq: typeof value.seq === 'number' ? value.seq : 0,
    mode: asString(value.mode),
    tool: asString(value.tool),
    label: asString(value.label),
    // Fail SAFE, not silent: an unrecognized state renders as still-running rather than
    // claiming a success the server never reported.
    state: state === 'ok' || state === 'failed' ? state : 'pending',
    // F3/U3: `hidden` is a RENDER hint that must survive the parse — dropping it makes the
    // consumer's `!step.hidden` filter a no-op on `undefined`.
    hidden: value.hidden === true,
    detail: toStepDetail(value.detail),
  }
}

function toPlanOptionsItem(value: unknown): PlanOptionsItem | null {
  if (!isRecord(value)) return null
  const toolCallId = value.toolCallId
  // The card IS its tool-call id — the resolve endpoint is addressed by it, so a card
  // without one is an unclickable ghost. Drop the frame rather than render a dead button.
  if (typeof toolCallId !== 'string' || toolCallId === '') return null
  const state = value.state
  return {
    type: 'plan_options',
    seq: typeof value.seq === 'number' ? value.seq : 0,
    mode: asString(value.mode),
    toolCallId,
    state:
      state === 'refine' || state === 'build' || state === 'build_failed' ? state : 'pending',
    reason: typeof value.reason === 'string' ? value.reason : null,
  }
}

function toProjectionItems(value: unknown): ProjectionItem[] {
  if (!Array.isArray(value)) return []
  const items: ProjectionItem[] = []
  for (const entry of value) {
    if (!isRecord(entry) || typeof entry.type !== 'string') continue
    items.push({ ...entry, type: entry.type, seq: typeof entry.seq === 'number' ? entry.seq : 0 })
  }
  return items
}

/**
 * Parse-don't-validate at the wire boundary (the `buildSessionEvents.ts::toProgressEnvelope`
 * precedent). Every KNOWN frame type is narrowed field by field before its literal object is
 * built — a blanket `as TurnFrame` cast made the whole union a promise the wire never kept, so
 * a `step` frame missing `item` reached consumers as `undefined` and threw at render time,
 * inside a stream reader, where the failure reads as a dropped connection.
 *
 * Unknown `type`s keep the spread and are surfaced verbatim: streams stay forward-extensible
 * (an added server frame must never break an older client). Returning null drops the frame.
 */
function asWorkspaceState(value: unknown): 'preparing' | 'ready' | 'unavailable' | null {
  return value === 'preparing' || value === 'ready' || value === 'unavailable' ? value : null
}

function asPreviewState(value: unknown): 'ready' | 'reconnecting' | null {
  return value === 'ready' || value === 'reconnecting' ? value : null
}

function toTurnFrame(parsed: unknown): TurnFrame | null {
  if (!isRecord(parsed) || typeof parsed.type !== 'string') return null
  const seq = typeof parsed.seq === 'number' ? parsed.seq : 0

  switch (parsed.type) {
    case 'snapshot': {
      const status = parsed.turnStatus
      return {
        type: 'snapshot',
        seq,
        turnId: typeof parsed.turnId === 'string' ? parsed.turnId : null,
        turnStatus:
          status === 'running' ||
          status === 'completed' ||
          status === 'failed' ||
          status === 'stopped'
            ? status
            : 'idle',
        items: toProjectionItems(parsed.items),
        textSoFar: asString(parsed.textSoFar),
        steps: Array.isArray(parsed.steps)
          ? parsed.steps.map(toStepItem).filter((step): step is StepItem => step !== null)
          : [],
        errorMessage: typeof parsed.errorMessage === 'string' ? parsed.errorMessage : null,
        workspaceState: asWorkspaceState(parsed.workspaceState),
        previewUrl: typeof parsed.previewUrl === 'string' ? parsed.previewUrl : null,
        previewState: asPreviewState(parsed.previewState),
      }
    }
    case 'text_delta':
      // A delta with no text is nothing to append — but it is not corruption either; the
      // empty string is the honest reading.
      return { type: 'text_delta', seq, text: asString(parsed.text) }
    case 'step': {
      const item = toStepItem(parsed.item)
      if (item === null) return null // a step frame IS its item; without one there is nothing to show
      const phase = parsed.phase
      return {
        type: 'step',
        seq,
        toolCallId: asString(parsed.toolCallId),
        phase: phase === 'finished' ? 'finished' : 'started',
        item,
      }
    }
    case 'plan_options': {
      const item = toPlanOptionsItem(parsed.item)
      if (item === null) return null
      return { type: 'plan_options', seq, item }
    }
    case 'error':
      return { type: 'error', seq, message: asString(parsed.message) }
    case 'turn_ended': {
      const status = parsed.status
      return {
        type: 'turn_ended',
        seq,
        turnId: asString(parsed.turnId),
        // Fail closed: an unrecognized terminal status is `failed`, never lost — the
        // terminal is what tells the consumer the turn is over at all.
        status: status === 'completed' || status === 'stopped' ? status : 'failed',
        reason: typeof parsed.reason === 'string' ? parsed.reason : null,
        previewUrl: typeof parsed.previewUrl === 'string' ? parsed.previewUrl : null,
        // `undefined` is NOT coerced to false: the tri-state is the point (see the type).
        snapshotCommitted:
          typeof parsed.snapshotCommitted === 'boolean' ? parsed.snapshotCommitted : null,
      }
    }
    case 'workspace': {
      const state = asWorkspaceState(parsed.state)
      // A workspace frame IS its state; an unrecognized one says nothing the phase machine
      // can act on, so drop it rather than invent `preparing` and hang the UI there.
      if (state === null) return null
      return {
        type: 'workspace',
        seq,
        state,
        message: typeof parsed.message === 'string' ? parsed.message : null,
      }
    }
    case 'preview': {
      const state = asPreviewState(parsed.state)
      if (state === null) return null
      return {
        type: 'preview',
        seq,
        state,
        previewUrl: typeof parsed.previewUrl === 'string' ? parsed.previewUrl : null,
      }
    }
    case 'diagnostic': {
      const source = parsed.source
      return {
        type: 'diagnostic',
        seq,
        source:
          source === 'tsc' || source === 'next_build' || source === 'server' || source === 'client'
            ? source
            : 'server',
        title: asString(parsed.title),
        cleanedStack: asString(parsed.cleanedStack),
      }
    }
    case 'quota':
      return {
        type: 'quota',
        seq,
        limit: typeof parsed.limit === 'number' ? parsed.limit : 0,
        used: typeof parsed.used === 'number' ? parsed.used : 0,
        resetsAt: asString(parsed.resetsAt),
      }
    default:
      return { ...parsed, type: parsed.type, seq }
  }
}

export function parseSseText(buffer: string): ParsedChunk {
  const frames: TurnFrame[] = []
  let sawDone = false
  const blocks = buffer.split('\n\n')
  const rest = blocks.pop() ?? '' // the unterminated tail (or '' when buffer ended clean)
  for (const block of blocks) {
    for (const line of block.split('\n')) {
      if (line.startsWith(':')) continue // keepalive comment
      if (!line.startsWith('data: ')) continue // id: lines ride with their data line
      const payload = line.slice('data: '.length)
      if (payload === '[DONE]') {
        sawDone = true
        continue
      }
      const frame = toTurnFrame(JSON.parse(payload))
      if (frame !== null) frames.push(frame)
    }
  }
  return { frames, rest, sawDone }
}

// ---------------------------------------------------------------------------------------
// HTTP calls
// ---------------------------------------------------------------------------------------

/**
 * The one transport every turn call rides (U1 / KTD-9).
 *
 * This module used to call raw `fetch` at six sites and hand-roll its own `credentials`
 * and CSRF header — a second, weaker copy of what `authFetch` already owns, and one that
 * had no 401 → refresh → retry at all. An expired session therefore killed the entire
 * chat transport: start, stop, mode-switch, Build-it, plan-resolve and the SSE reader all
 * died where every other call in the app quietly recovered (N11).
 *
 * One shared expression, N readers — the rule from the daily-token-double-count learning.
 * `authFetch` owns the refresh retry, the per-attempt CSRF token, the suspension gate and
 * `credentials: 'include'`; nothing here recomputes any of them. Tests inject the raw
 * `fetchImpl` through `deps`, so they exercise that real behaviour rather than bypass it.
 */
export type AuthFetchDeps = NonNullable<Parameters<typeof authFetch>[2]>

export interface StartTurnMessage {
  text: string
  attachmentTexts?: string[]
  attachmentIds?: string[]
}

export async function startTurn(
  conversationId: string,
  message: StartTurnMessage,
  deps: AuthFetchDeps = {}
): Promise<{ turnId: string }> {
  const resp = await authFetch(
    `/api/conversations/${conversationId}/turns`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: {
          text: message.text,
          attachmentTexts: message.attachmentTexts ?? [],
          attachmentIds: message.attachmentIds ?? [],
        },
      }),
    },
    deps
  )
  if (!resp.ok) {
    const body = (await resp.json().catch(() => null)) as {
      error?: { message?: string; code?: string } & Record<string, unknown>
    } | null
    const detail = body?.error?.message ?? `turn start failed (${resp.status})`
    throw new TurnStartError(resp.status, detail, body?.error?.code ?? null, body?.error ?? null)
  }
  return (await resp.json()) as { turnId: string }
}

export class TurnStartError extends Error {
  readonly status: number
  /** The backend's error code, so a caller can tell the refusals apart. A 409 is
   *  `build_session_already_active` (nothing the user can do but wait) OR
   *  `sandbox_reclaim_blocked` (another project holds the workspace and CAN be released) —
   *  dropping the code made those two indistinguishable and both read as "try again later". */
  readonly code: string | null
  /** The whole `error` object, for codes that carry more than a message (see `ApiError.details`). */
  readonly details: Record<string, unknown> | null

  constructor(
    status: number,
    message: string,
    code: string | null = null,
    details: Record<string, unknown> | null = null,
  ) {
    super(message)
    this.status = status
    this.code = code
    this.details = details
  }
}

export type ConversationMode = 'ask' | 'plan' | 'write'

export interface BuildFromPlanOutcome {
  /** `build_failed` is gone: every case it carried is now a typed HTTP status `authFetch`
   *  already raises on — 429 for the daily cap, 409 for a busy workspace, 503 for an
   *  unconfigured engine. `turnId` replaces `sessionId` because a build IS a turn now. */
  outcome: 'started' | 'already_started' | 'stale_plan'
  turnId?: string | null
  appId?: string | null
  planHeadSha?: string | null
  currentHeadSha?: string | null
}

/** Build-it (U5): flip + record + START A TURN, one endpoint. */
export async function buildFromPlan(
  conversationId: string,
  toolCallId: string,
  options: { force?: boolean } = {},
  deps: AuthFetchDeps = {}
): Promise<BuildFromPlanOutcome> {
  const resp = await authFetch(
    `/api/conversations/${conversationId}/plan-options/${toolCallId}/build`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force: options.force ?? false }),
    },
    deps
  )
  if (!resp.ok) {
    const body = (await resp.json().catch(() => null)) as { error?: { message?: string } } | null
    throw new Error(body?.error?.message ?? `build transition failed (${resp.status})`)
  }
  return (await resp.json()) as BuildFromPlanOutcome
}

/** The atomic mode switch (U13) — returns the SERVER's confirmed mode. */
export async function switchMode(
  conversationId: string,
  mode: ConversationMode,
  deps: AuthFetchDeps = {}
): Promise<ConversationMode> {
  const resp = await authFetch(
    `/api/conversations/${conversationId}/mode`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    },
    deps
  )
  // A TYPED failure, because the caller has to tell 409 (a turn is still running — the one
  // case "finish the current step" is true of) from 401 (the session lapsed) from everything
  // else. A bare `Error` forced BuilderPage to map all three onto one string, and the string
  // it picked was false for two of them (N12).
  if (!resp.ok) throw await readApiError(resp, 'Could not switch modes')
  const parsed = (await resp.json()) as { mode: ConversationMode }
  return parsed.mode
}

export async function resolvePlanOptions(
  conversationId: string,
  toolCallId: string,
  deps: AuthFetchDeps = {}
): Promise<{ state: 'refine' | 'build' | 'build_failed'; alreadyResolved: boolean }> {
  const resp = await authFetch(
    `/api/conversations/${conversationId}/plan-options/${toolCallId}/resolve`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ choice: 'refine' }),
    },
    deps
  )
  if (!resp.ok) throw new Error(`plan options resolve failed (${resp.status})`)
  return (await resp.json()) as { state: 'refine' | 'build' | 'build_failed'; alreadyResolved: boolean }
}

export async function stopTurn(
  conversationId: string,
  turnId: string,
  deps: AuthFetchDeps = {}
): Promise<'stopping' | 'already_settled'> {
  const resp = await authFetch(
    `/api/conversations/${conversationId}/turns/${turnId}/stop`,
    { method: 'POST' },
    deps
  )
  if (!resp.ok) throw new Error(`stop failed (${resp.status})`)
  const body = (await resp.json()) as { status: 'stopping' | 'already_settled' }
  return body.status
}

// ---------------------------------------------------------------------------------------
// The stream reader
// ---------------------------------------------------------------------------------------

/** Why the read loop ended — the four distinct outcomes (streamed-reply learning). */
export type StreamOutcome = 'completed' | 'aborted' | 'stalled' | 'truncated'

export interface ReadStreamOptions {
  conversationId: string
  /** Resume continuity: the last applied frame seq + its turn (omit for snapshot-first). */
  cursor?: number
  turnId?: string
  signal: AbortSignal
  onFrame: (frame: TurnFrame) => void
  deps?: AuthFetchDeps
  stallTimeoutMs?: number
}

/**
 * Read the conversation's event stream to its end. Resolves with the transport outcome:
 * `completed` = the server closed after `[DONE]` (the semantic result rides the frames);
 * `aborted` = the caller's signal fired; `stalled` = the watchdog tripped (no bytes for
 * the stall window); `truncated` = the socket closed with no `[DONE]` — the caller
 * decides whether to resubscribe with its cursor.
 */
export async function readTurnStream(options: ReadStreamOptions): Promise<StreamOutcome> {
  const { conversationId, cursor, turnId, signal, onFrame } = options
  const stallMs = options.stallTimeoutMs ?? TURN_STREAM_STALL_TIMEOUT_MS

  const params = new URLSearchParams()
  if (cursor && cursor > 0) params.set('cursor', String(cursor))
  if (turnId) params.set('turn', turnId)
  const query = params.size > 0 ? `?${params.toString()}` : ''

  // Only the REQUEST rides the wrapper: authFetch owns admission (401 → refresh → retry,
  // the suspension gate, the session cookie) and hands back a Response whose body is a
  // fresh stream. The reader, the carry buffer and the abort race below stay ours.
  let resp: Response
  try {
    resp = await authFetch(
      `/api/conversations/${conversationId}/events${query}`,
      { signal },
      options.deps ?? {}
    )
  } catch (err) {
    if (signal.aborted) return 'aborted'
    throw err
  }
  if (!resp.ok || resp.body === null) throw new Error(`event stream failed (${resp.status})`)

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let carry = ''
  try {
    for (;;) {
      const winner = await raceReadAgainst(reader.read(), signal, stallMs)
      if (winner === 'stall' || winner === 'abort') {
        await reader.cancel().catch(() => undefined)
        return winner === 'stall' ? 'stalled' : 'aborted'
      }
      const { done, value } = winner
      if (done) return 'truncated' // closed without [DONE]
      carry += decoder.decode(value, { stream: true })
      const { frames, rest, sawDone } = parseSseText(carry)
      carry = rest
      for (const frame of frames) onFrame(frame)
      if (sawDone) return 'completed'
    }
  } catch (err) {
    if (signal.aborted) return 'aborted'
    throw err
  } finally {
    reader.releaseLock()
  }
}

/**
 * One read attempt raced against the stall watchdog and the caller's abort — with the
 * timer and listener torn down whichever way the race settles (no per-iteration leaks).
 */
async function raceReadAgainst(
  read: Promise<ReadableStreamReadResult<Uint8Array>>,
  signal: AbortSignal,
  stallMs: number
): Promise<ReadableStreamReadResult<Uint8Array> | 'stall' | 'abort'> {
  if (signal.aborted) return 'abort'
  let timer: ReturnType<typeof setTimeout> | undefined
  let onAbort: (() => void) | undefined
  try {
    return await Promise.race([
      read,
      new Promise<'stall' | 'abort'>((resolve) => {
        timer = setTimeout(() => resolve('stall'), stallMs)
        onAbort = () => resolve('abort')
        signal.addEventListener('abort', onAbort, { once: true })
      }),
    ])
  } finally {
    if (timer !== undefined) clearTimeout(timer)
    if (onAbort !== undefined) signal.removeEventListener('abort', onAbort)
  }
}
