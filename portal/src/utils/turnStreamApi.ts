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

import { getCsrfToken } from './auth'

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
}

export interface UnknownFrame {
  type: string
  seq: number
  [key: string]: unknown
}

export type TurnFrame =
  | SnapshotFrame
  | TextDeltaFrame
  | StepFrame
  | PlanOptionsFrame
  | TurnErrorFrame
  | TurnEndedFrame
  | UnknownFrame

const KNOWN_FRAME_TYPES = new Set(['snapshot', 'text_delta', 'step', 'plan_options', 'error', 'turn_ended'])

export function isKnownFrame(
  frame: TurnFrame
): frame is
  | SnapshotFrame
  | TextDeltaFrame
  | StepFrame
  | PlanOptionsFrame
  | TurnErrorFrame
  | TurnEndedFrame {
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

/** Parse-don't-validate at the wire boundary (buildSessionEvents precedent): a frame
 * needs a string `type`; a missing/odd `seq` reads as 0 (unknown frames may omit it). */
function toTurnFrame(parsed: unknown): TurnFrame | null {
  if (!isRecord(parsed) || typeof parsed.type !== 'string') return null
  const seq = typeof parsed.seq === 'number' ? parsed.seq : 0
  return { ...parsed, type: parsed.type, seq } as TurnFrame
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

function csrfHeaders(): Record<string, string> {
  const csrf = getCsrfToken()
  return csrf ? { 'X-CSRF-Token': csrf } : {}
}

export interface StartTurnMessage {
  text: string
  attachmentTexts?: string[]
  attachmentIds?: string[]
}

export async function startTurn(
  conversationId: string,
  message: StartTurnMessage,
  fetchFn: typeof fetch = fetch
): Promise<{ turnId: string }> {
  const resp = await fetchFn(`/api/v1/conversations/${conversationId}/turns`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders() },
    body: JSON.stringify({
      message: {
        text: message.text,
        attachmentTexts: message.attachmentTexts ?? [],
        attachmentIds: message.attachmentIds ?? [],
      },
    }),
  })
  if (!resp.ok) {
    const body = (await resp.json().catch(() => null)) as { error?: { message?: string } } | null
    const detail = body?.error?.message ?? `turn start failed (${resp.status})`
    throw new TurnStartError(resp.status, detail)
  }
  return (await resp.json()) as { turnId: string }
}

export class TurnStartError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function resolvePlanOptions(
  conversationId: string,
  toolCallId: string,
  fetchFn: typeof fetch = fetch
): Promise<{ state: 'refine' | 'build' | 'build_failed'; alreadyResolved: boolean }> {
  const resp = await fetchFn(
    `/api/v1/conversations/${conversationId}/plan-options/${toolCallId}/resolve`,
    {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...csrfHeaders() },
      body: JSON.stringify({ choice: 'refine' }),
    }
  )
  if (!resp.ok) throw new Error(`plan options resolve failed (${resp.status})`)
  return (await resp.json()) as { state: 'refine' | 'build' | 'build_failed'; alreadyResolved: boolean }
}

export async function stopTurn(
  conversationId: string,
  turnId: string,
  fetchFn: typeof fetch = fetch
): Promise<'stopping' | 'already_settled'> {
  const resp = await fetchFn(`/api/v1/conversations/${conversationId}/turns/${turnId}/stop`, {
    method: 'POST',
    credentials: 'include',
    headers: csrfHeaders(),
  })
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
  fetchFn?: typeof fetch
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
  const fetchFn = options.fetchFn ?? fetch
  const stallMs = options.stallTimeoutMs ?? TURN_STREAM_STALL_TIMEOUT_MS

  const params = new URLSearchParams()
  if (cursor && cursor > 0) params.set('cursor', String(cursor))
  if (turnId) params.set('turn', turnId)
  const query = params.size > 0 ? `?${params.toString()}` : ''

  let resp: Response
  try {
    resp = await fetchFn(`/api/v1/conversations/${conversationId}/events${query}`, {
      credentials: 'include',
      signal,
    })
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
