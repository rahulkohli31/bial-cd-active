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

import { authFetch } from './api'
import { asCompileState } from './compileState'
import type { CompileState } from './compileState'
import { readApiError } from './apiError'

// ---------------------------------------------------------------------------------------
// Frame types (mirror backend `conversations/schemas.py`; camelCase on the wire)
// ---------------------------------------------------------------------------------------

/**
 * A step, and DELIBERATELY WITHOUT A `detail`. `StepDetail` carried the tool's own arguments
 * and result, and it is gone from the server (`services/messages/projection.py`, which now
 * pins its emitted field set in a test) because nothing rendered it: it was a platform
 * internal that crossed to the browser and sat there, one refactor away from an expander.
 * Re-adding the field here would rebuild the client half of that seam and give the next
 * contributor something to wire up — which is why this comment names it rather than leaving
 * a silent omission. The egress rule it belongs to is C7 §3.0.
 */
export interface StepItem {
  type: 'step'
  seq: number
  tool: string
  label: string
  state: 'ok' | 'failed' | 'pending'
  hidden: boolean
}

/** Projection items ride the snapshot verbatim (U6 shapes); the hook re-exposes them. */
export interface ProjectionItem {
  type: string
  seq: number
  [key: string]: unknown
}

/** The Build it / Keep refining card state — identical live and on reload.
 *
 * `build_failed` is GONE from the state set, not merely unused. Every case it carried is a
 * typed HTTP status the Build-it call already raises on, so a card that renders it would be a
 * second, staler account of a failure the caller has already been told about in full. */
export interface PlanOptionsItem {
  type: 'plan_options'
  seq: number
  toolCallId: string
  state: 'pending' | 'refine' | 'build'
}

/** One block of the turn's prose, at the position it took. */
export interface TurnTextPart {
  type: 'text'
  text: string
}

/** One of the turn's steps, at the position it took. `toolCallId` is the SAME key the live
 *  `StepFrame` carries, so a step that resolves after the snapshot replaces the one the
 *  snapshot delivered rather than stacking a second copy beside it. */
export interface TurnStepPart {
  type: 'step'
  toolCallId: string
  item: StepItem
}

/** What the turn has produced so far, prose and steps INTERLEAVED in emission order.
 *
 *  The snapshot used to carry a flat `textSoFar` string beside an unordered step list, which
 *  cannot express a turn that wrote, acted and wrote again — the two shapes agreed only while
 *  a turn was guaranteed at most one block of text, always last. That guarantee came from
 *  prose beside a tool call being thrown away; with it gone, a citizen who reloads mid-turn
 *  would otherwise read the same turn in a different order from one who never left. */
export type TurnPart = TurnTextPart | TurnStepPart

export interface SnapshotFrame {
  type: 'snapshot'
  seq: number
  turnId: string | null
  turnStatus: 'idle' | 'running' | 'completed' | 'failed' | 'stopped'
  items: ProjectionItem[]
  parts: TurnPart[]
  /** Was the model mid-thought when this snapshot was taken? Same catch-up reasoning as the
   *  fields below — a client that reattaches while it is thinking would otherwise sit on a
   *  still screen until the next frame changed something. */
  working: boolean
  /** WHY a failed turn failed. The in-band `error` frame lives only in the ring, so a
   *  subscriber that arrives after the failure reads the reason from here or not at all. */
  errorMessage: string | null
  /** The Write turn's newest workspace/preview facts, for the same catch-up reason: a
   *  `preview` frame that fired before this tab connected is gone from the ring, so a
   *  mid-build reconnect recovers from here instead of a second REST round-trip. */
  workspaceState?: 'preparing' | 'ready' | 'unavailable' | null
  previewUrl?: string | null
  previewState?: 'ready' | 'reconnecting' | null
  /** R17/R18. Compile frames are emitted ON CHANGE, so a tab that reloads while the app is
   *  sitting broken would learn nothing until the next change — and would show an uncovered
   *  error screen until then. This is what makes a refresh mid-build land covered. */
  compileState?: CompileState | null
}

export interface TextDeltaFrame {
  type: 'text_delta'
  seq: number
  text: string
  /** This slice OPENS a block rather than continuing the current one — the live half of the
   *  boundary the reload projection gets for free from one stored text part per item. Without
   *  it a client could only concatenate, and a turn that wrote, acted and wrote again would
   *  render as one paragraph under all of its steps live, and as two paragraphs around them
   *  after a reload. */
  newBlock: boolean
}

/** The model is REASONING — and this is the whole of what reasoning becomes.
 *
 *  A BOOLEAN, NEVER THE TEXT. Reasoning blocks are stored server-side so the provider can be
 *  given them back on the next turn, and they are never projected, never framed and never sent
 *  here. What the citizen reads is one status line saying the agent is working.
 *
 *  Edge-triggered: the server emits it when the flag CHANGES, not per reasoning delta. */
export interface WorkingFrame {
  type: 'working'
  seq: number
  working: boolean
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
  /** What is happening RIGHT NOW, narrated — replaced by whatever the next phase says. */
  message?: string | null
  /** U2 — something the platform needs the citizen to SEE, as distinct from `message`.
   *  A statement about the app (it was reset and is being put back, it was reset and cannot be,
   *  we could not check it) rather than about the phase, so it outlives the phase that carried
   *  it and belongs in the banner slot rather than in the build bubble. Sharing one field made
   *  every ordinary turn post "Getting your workspace ready…" above the composer. */
  notice?: string | null
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
 *  an error would tell the user their build died on its way to succeeding.
 *
 *  ONE AUDIENCE NOW. This frame used to carry the model's half beside the citizen's —
 *  `title`, the compiler's own first meaningful line, and `cleanedStack`, the de-noised log —
 *  described as safe to transmit but not a product surface. That distinction is not one a wire
 *  format can hold, and the sentence "safe to render verbatim" that preceded it is what once
 *  put a stack trace under a file-path title in a citizen's chat. The server stopped sending
 *  both; this stopped parsing them, because a parser for a field nothing sends is a field one
 *  refactor away from being rendered.
 *
 *  `userMessage` / `userAction` are what crosses, and both always arrive non-empty: the server
 *  derives them from the error class when its producer supplies none. */
export interface DiagnosticFrame {
  type: 'diagnostic'
  seq: number
  source: 'tsc' | 'next_build' | 'server' | 'client'
  userMessage: string
  userAction: string
}

/** The daily cap, hit mid-turn. Structured so the client can format the numbers itself. */
export interface QuotaFrame {
  type: 'quota'
  seq: number
  limit: number
  used: number
  resetsAt: string
}

/** What the app's dev server is compiling right now (R17/R18) — the preview pane covers its
 *  frame while this is `building` or `failed`, and uncovers on `clean`. Emitted ON CHANGE, not
 *  per poll. `unknown` is a real value the pane must HOLD its current cover on: it means the
 *  platform could not tell, which after a container image predating the signal is the normal
 *  reading for the whole existing fleet. */
export interface CompileFrame {
  type: 'compile'
  seq: number
  state: CompileState
}

export interface UnknownFrame {
  type: string
  seq: number
  [key: string]: unknown
}

export type KnownTurnFrame =
  | SnapshotFrame
  | TextDeltaFrame
  | WorkingFrame
  | StepFrame
  | PlanOptionsFrame
  | TurnErrorFrame
  | TurnEndedFrame
  | WorkspaceFrame
  | PreviewFrame
  | DiagnosticFrame
  | QuotaFrame
  | CompileFrame

export type TurnFrame = KnownTurnFrame | UnknownFrame

// A new frame type needs BOTH this set and a `KnownTurnFrame` member: listed in only one, it
// still parses — as `UnknownFrame` — so the forward-compat escape hatch silently swallows our
// own frame instead of the foreign one it exists for. Mirrors the server's `_KNOWN_FRAME_TAGS`.
const KNOWN_FRAME_TYPES = new Set([
  'snapshot',
  'text_delta',
  'working',
  'step',
  'plan_options',
  'error',
  'turn_ended',
  'workspace',
  'preview',
  'diagnostic',
  'quota',
  'compile',
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

/** Exported so conversationApi.ts's reload path can narrow a stored `step` item the
 * same way the live path does, instead of a raw `as unknown as StepItem` cast — see
 * PR #93 review finding 9. */
export function toStepItem(value: unknown): StepItem | null {
  if (!isRecord(value)) return null
  const state = value.state
  return {
    type: 'step',
    seq: typeof value.seq === 'number' ? value.seq : 0,
    tool: asString(value.tool),
    label: asString(value.label),
    // Fail SAFE, not silent: an unrecognized state renders as still-running rather than
    // claiming a success the server never reported.
    state: state === 'ok' || state === 'failed' ? state : 'pending',
    // F3/U3: `hidden` is a RENDER hint that must survive the parse — dropping it makes the
    // consumer's `!step.hidden` filter a no-op on `undefined`.
    hidden: value.hidden === true,
  }
}

/** Exported so conversationApi.ts's reload path can narrow a stored `plan_options`
 * item the same way the live path does, instead of a raw `as unknown as
 * PlanOptionsItem` cast — see PR #93 review finding 9. */
export function toPlanOptionsItem(value: unknown): PlanOptionsItem | null {
  if (!isRecord(value)) return null
  const toolCallId = value.toolCallId
  // The card IS its tool-call id — the resolve endpoint is addressed by it, so a card
  // without one is an unclickable ghost. Drop the frame rather than render a dead button.
  if (typeof toolCallId !== 'string' || toolCallId === '') return null
  const state = value.state
  return {
    type: 'plan_options',
    seq: typeof value.seq === 'number' ? value.seq : 0,
    toolCallId,
    state: state === 'refine' || state === 'build' ? state : 'pending',
  }
}

/** A snapshot's ordered parts, narrowed field by field like every other wire shape here.
 *
 *  A part that is neither a text block nor a resolvable step is DROPPED rather than guessed
 *  at: the list IS the turn's order, and an entry that renders nothing is better than one
 *  that renders a placeholder in a real position. */
function toTurnParts(value: unknown): TurnPart[] {
  if (!Array.isArray(value)) return []
  const parts: TurnPart[] = []
  for (const entry of value) {
    if (!isRecord(entry)) continue
    if (entry.type === 'text') {
      parts.push({ type: 'text', text: asString(entry.text) })
    } else if (entry.type === 'step') {
      const item = toStepItem(entry.item)
      const toolCallId = entry.toolCallId
      if (item === null || typeof toolCallId !== 'string' || toolCallId === '') continue
      parts.push({ type: 'step', toolCallId, item })
    }
  }
  return parts
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
        parts: toTurnParts(parsed.parts),
        working: parsed.working === true,
        errorMessage: typeof parsed.errorMessage === 'string' ? parsed.errorMessage : null,
        workspaceState: asWorkspaceState(parsed.workspaceState),
        previewUrl: typeof parsed.previewUrl === 'string' ? parsed.previewUrl : null,
        previewState: asPreviewState(parsed.previewState),
        // `null` when the server said nothing, `'unknown'` when it said it could not tell.
        // Both HOLD the pane's cover; only a stated value moves it.
        compileState: parsed.compileState == null ? null : asCompileState(parsed.compileState),
      }
    }
    case 'working':
      return { type: 'working', seq, working: parsed.working === true }
    case 'text_delta':
      // A delta with no text is nothing to append — but it is not corruption either; the
      // empty string is the honest reading.
      //
      // `newBlock` fails CLOSED to false: a missing flag continues the block already open,
      // which at worst runs two paragraphs together. Defaulting it true would split a single
      // paragraph at every delta boundary, one block per token.
      return { type: 'text_delta', seq, text: asString(parsed.text), newBlock: parsed.newBlock === true }
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
        notice: typeof parsed.notice === 'string' ? parsed.notice : null,
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
        // Empty from an older server that predates the pair — NOT a parse failure. The feed
        // owns the fallback sentence + action for exactly this case, so an empty string here
        // degrades to product copy rather than to a blank error row.
        userMessage: asString(parsed.userMessage),
        userAction: asString(parsed.userAction),
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
    case 'compile':
      // A state string this client does not recognise narrows to `unknown`, which HOLDS the
      // cover. The container and this bundle ship separately and can be a release apart in
      // either direction, so an unheard-of value is a real state — never a reason to uncover.
      return { type: 'compile', seq, state: asCompileState(parsed.state) }
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
 * chat transport: start, stop, Build-it, plan-resolve and the SSE reader all died where
 * every other call in the app quietly recovered (N11).
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

/**
 * THE PARENTAGE OF A CHAT THAT DOES NOT EXIST YET (R-18, plan 006 U13).
 *
 * Sent only with a chat's FIRST message. Until this existed, the row was created by a separate
 * `POST /conversations` a round trip earlier — and that call's only workspace awareness was a
 * project-ownership check, so a message the workspace then refused left a real, titled, empty
 * conversation in the project's list, named after the text that had just been refused.
 *
 * Carrying it here lets the server check first and create second, inside one transaction, so a
 * refusal rolls the row back with it.
 */
export interface NewConversationParentage {
  projectId: string
  kind: 'plan' | 'build'
  title?: string
  context?: unknown
}

export async function startTurn(
  conversationId: string,
  message: StartTurnMessage,
  deps: AuthFetchDeps = {},
  create?: NewConversationParentage,
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
        // OMITTED, not `undefined`, for every turn after the first: the server treats a `create`
        // block on an existing conversation as a retry and ignores it, but sending one where none
        // is meant would make the wire say something the client does not intend.
        ...(create ? { create } : {}),
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

export interface BuildFromPlanOutcome {
  /** `stale_plan` is gone with the pin that produced it: the plan the build runs on IS the plan
   *  the card offered, carried in the offer itself, so there is no second copy to drift from. */
  outcome: 'started' | 'already_started'
  /** The BUILD chat — a different conversation from the one the card was clicked in. Echoed
   *  back rather than assumed, because `already_started` answers with the chat that already
   *  exists, which is the same id on a double-press and the thing to navigate to either way. */
  chatId: string
  turnId?: string | null
}

/**
 * Build-it: the HANDOFF. The plan chat is left exactly as it stands and a NEW build chat is
 * created, seeded with the plan and started, in one call.
 *
 * THE ID IS MINTED BY THE CALLER, and that is what makes a double-press safe. Two presses send
 * the same `chatId`, so the second finds the first's conversation already there and answers
 * `already_started` naming it — where a server-minted id would have produced two build chats
 * for one plan and left the citizen looking at the empty one.
 *
 * A TYPED failure, because the caller has to tell these apart and each has a different remedy:
 * 409 `already_building_here` (this user's one workspace is committed to another chat — wait or
 * go there), 503 `workspace_unavailable` (nothing to build in — not the citizen's fault and not
 * their fix), 429 (the daily cap), 400 (the offer carried no usable plan). A bare `Error`
 * collapses four remedies into one sentence, and the sentence is wrong for three of them.
 */
export async function buildFromPlan(
  conversationId: string,
  toolCallId: string,
  chatId: string,
  deps: AuthFetchDeps = {}
): Promise<BuildFromPlanOutcome> {
  const resp = await authFetch(
    `/api/conversations/${conversationId}/plan-options/${toolCallId}/build`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chatId }),
    },
    deps
  )
  if (!resp.ok) throw await readApiError(resp, 'Could not start the build')
  return (await resp.json()) as BuildFromPlanOutcome
}

export async function resolvePlanOptions(
  conversationId: string,
  toolCallId: string,
  deps: AuthFetchDeps = {}
): Promise<{ state: 'refine' | 'build'; alreadyResolved: boolean }> {
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
  return (await resp.json()) as { state: 'refine' | 'build'; alreadyResolved: boolean }
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
  //
  // THE WATCHDOG COVERS THIS AWAIT TOO (#137). `raceAgainst` guards `reader.read()`, which
  // only begins once response HEADERS have arrived — so a server that accepted the socket
  // and then went quiet left this promise PENDING FOREVER. The caller's `endGenerating`
  // sits after the await, so `generatingChatId` never cleared and the composer animated
  // "Setting up your sandbox… running Nm Ns", with a live Stop button, on a turn the server
  // had already failed in under a second. The 60s stall window
  // could not save it: it was never armed. Every outcome of a subscribe must be reachable
  // in bounded time, headers or no headers.
  //
  // The fetch rides an INTERNAL controller chained to the caller's signal, because a stall
  // has to cancel the hung request itself and aborting the caller's controller would cancel
  // intent that is not ours to cancel (the same controller governs the resume-once retry).
  // The relay is `once` and the controller is per-call, so it dies with this invocation —
  // unlike the per-iteration listeners in `raceAgainst`, this one is registered a single
  // time and needs no teardown.
  const requestAbort = new AbortController()
  if (signal.aborted) return 'aborted'
  signal.addEventListener('abort', () => requestAbort.abort(), { once: true })

  let resp: Response
  try {
    const settled = await raceAgainst(
      authFetch(
        `/api/conversations/${conversationId}/events${query}`,
        { signal: requestAbort.signal },
        options.deps ?? {}
      ),
      signal,
      stallMs
    )
    if (settled === 'abort') return 'aborted'
    if (settled === 'stall') {
      requestAbort.abort() // never leave a hung socket behind us
      return 'stalled'
    }
    resp = settled
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
      const winner = await raceAgainst(reader.read(), signal, stallMs)
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
 * One awaited step raced against the stall watchdog and the caller's abort — with the
 * timer and listener torn down whichever way the race settles (no per-iteration leaks).
 *
 * Generic over the work because BOTH halves of a subscribe need the same bound: the
 * request that produces the response, and each `reader.read()` that drains it. Two
 * watchdogs would be two answers to "how long may this hang", free to drift — and the
 * half that had none is exactly where #137 lived. `T` is a `Response` or a read result,
 * never a string, so the `'stall' | 'abort'` sentinels stay unambiguous.
 */
async function raceAgainst<T>(
  work: Promise<T>,
  signal: AbortSignal,
  stallMs: number
): Promise<T | 'stall' | 'abort'> {
  if (signal.aborted) return 'abort'
  let timer: ReturnType<typeof setTimeout> | undefined
  let onAbort: (() => void) | undefined
  try {
    return await Promise.race([
      work,
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
