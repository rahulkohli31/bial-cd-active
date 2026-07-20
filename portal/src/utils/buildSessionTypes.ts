/**
 * The wire shapes of the build-session control surface (C3) and the progress
 * envelope the SSE feed carries (C7). Mostly types — imported with `import type` —
 * plus two tiny PURE helpers derived from these shapes (`isActiveBuildStatus`, a
 * predicate over the status union, and `formatDailyLimitMessage`, the shared quota
 * copy) so every surface shares one definition instead of re-deriving it.
 *
 * TWO DELIBERATELY DIFFERENT CASINGS, frozen by the contracts:
 *
 *   - **C3 REST bodies are camelCase** (`CamelModel`: the backend serializes
 *     `by_alias=True`). So Python `session_id` ⇄ JSON `sessionId`, etc. The client
 *     narrows these camelCase fields (`buildSessionApi.ts`).
 *   - **The C7 progress envelope stays snake_case** — field names AND `type`
 *     literals (`preview_ready`, `cleaned_stack`, `resets_at`). It is a streaming
 *     frame shape (kin to the chat relay's `{"delta":{"text":…}}`); U8 renders it
 *     WITHOUT the camelCase alias generator so the keys are byte-stable
 *     BRAIN-emit → SESSION-API-relay → portal-consume (C3 §naming, C7 §wire-casing).
 *     Do NOT run a camel alias over the envelope (KTD-5).
 *
 * Bodies arrive as `unknown` (untrusted network input) and are narrowed with type
 * guards at the boundary — never cast, never `any` (`.claude/rules/fail-first-typescript.md`).
 */
import { assertNever } from './assertNever'

// ─── C3: the control-plane status enum (camelCase surface) ───────────────────

/**
 * The five members of the build-session lifecycle (C3 §1). Wire value == the
 * lowercase member name. `provisioning → building → ready` is the forward path;
 * `ended` (graceful: stop / idle / quota) and `failed` (unrecoverable / escalated)
 * are the two DISTINCT absorbing terminals — a quota breach resolves to `ended`,
 * never `failed` (C7 §8).
 */
export type BuildSessionStatus = 'provisioning' | 'building' | 'ready' | 'ended' | 'failed'

/** The three non-terminal (in-progress) lifecycle states — a build is "active" while in any of them. */
export function isActiveBuildStatus(status: BuildSessionStatus | null): boolean {
  return status === 'provisioning' || status === 'building' || status === 'ready'
}

// ─── C3: control operations — start / stop / status (§2) ─────────────────────

/** `POST /v1/build-sessions` body. `projectId` is REQUIRED — project-first, no lazy Default (never reintroduce). */
export interface StartBuildRequest {
  projectId: string
  prompt: string
  /**
   * OPTIONAL (C3 §2.1) — the thread whose attachments ground this build. When present the
   * server reads that conversation's persisted file parts and materializes them into the build
   * agent's prompt (images/PDF as vision content, office/csv as extracted text); when absent the
   * build is text-only. The parts are already persisted by the time `start` fires (the composer
   * appends the user turn BEFORE starting), so the bytes never travel on this request.
   *
   * The conversation must belong to `projectId` and to the caller — otherwise the server returns
   * a non-leaking 404. An attachment the server cannot use fails the start with a 422 naming the
   * file, rather than silently building without it.
   */
  conversationId?: string
}

/** `POST /v1/build-sessions` → 201. `previewUrl` is always null here (the dev server is not up yet). */
export interface StartBuildResponse {
  sessionId: string
  projectId: string
  appId: string
  status: BuildSessionStatus
  previewUrl: string | null
  createdAt: string
}

/** `POST …/relaunch` body — restore a project's saved app into a fresh, ready sandbox (#43). */
export interface RelaunchPreviewRequest {
  projectId: string
}

/**
 * `POST …/relaunch` → 200 (#43). NO `sessionId`/`createdAt`: relaunch registers no build session
 * (Decision 6 — it must not occupy the build slot), so there is nothing to poll or stop. It
 * returns a live `previewUrl` synchronously (the server blocked on `wait_ready` before replying).
 */
export interface RelaunchPreviewResponse {
  appId: string
  previewUrl: string
  status: BuildSessionStatus
  /**
   * U6's "last saved version" signal: the project's NEWEST recorded build outcome was FAILED, so
   * the restored snapshot is the last SAVED state — not that build's intent. The preview pane
   * surfaces this so the user isn't silently shown older code as an unqualified "ready".
   */
  restoredFromFailedBuild: boolean
}

/**
 * Why a relaunch failed, discriminated for the U6 response matrix. A 409 (a build is running) is
 * deliberately NOT a member: it already has a first-class state — `blocked` (with the existing
 * session id + its own banner/force-end affordance) — and re-encoding it here would give one
 * failure two competing renderings.
 *
 *   - `not_found`   → 404: the project has no saved build — hide the Relaunch affordance.
 *   - `unavailable` → 503: transient (snapshot/sandbox) — retry copy, affordance restored.
 *   - `failed`      → anything else — "relaunch failed", affordance restored.
 */
export type RelaunchErrorKind = 'not_found' | 'unavailable' | 'failed'

export interface RelaunchError {
  kind: RelaunchErrorKind
  /** User-facing copy — the server's own message where it sent one (it is the approved wording). */
  message: string
}

/** Whether the Relaunch affordance comes back after this failure (only a definite 404 hides it). */
export function relaunchRetryable(kind: RelaunchErrorKind): boolean {
  switch (kind) {
    case 'not_found':
      return false
    case 'unavailable':
      return true
    case 'failed':
      return true
    default:
      return assertNever(kind)
  }
}

/** `POST …/stop` body — an optional free-text reason for the audit / activity feed. */
export interface StopBuildRequest {
  reason?: string | null
}

/** `POST …/stop` → 200. `status` is `ended` after a graceful stop (idempotent). */
export interface StopBuildResponse {
  sessionId: string
  status: BuildSessionStatus
}

/**
 * `GET /v1/build-sessions/{id}` → 200. The poll surface and the source of the
 * framable `previewUrl`. `previewUrl` is null until `ready`, then STABLE. `lastSeq`
 * is the highest C7 `seq` emitted so far (the reconnect cursor), or null before the
 * first envelope. On connect/reattach the owning hook seeds preview continuity from
 * here, so a `preview_ready` that fired before the client connected still frames the
 * app (C3 §2.3, KTD-1).
 */
export interface BuildSessionStatusResponse {
  sessionId: string
  projectId: string
  appId: string
  status: BuildSessionStatus
  previewUrl: string | null
  lastSeq: number | null
  createdAt: string
  updatedAt: string
}

// ─── C3: lock operations — acquire / renew / release / force-end / heartbeat (§3) ─

/** `…/lock/acquire` and `…/lock/renew` → 200. */
export interface LockStateResponse {
  sessionId: string
  held: boolean
  ownerUserId: string
  ttlSeconds: number
  expiresAt: string
}

/** `…/lock/release` → 200. Idempotent — true if the lock was held-and-released or already free. */
export interface LockReleaseResponse {
  sessionId: string
  released: boolean
}

/** `…/lock/force-end` → 200. The owner-only kill switch; `status` is `ended`. */
export interface ForceEndResponse {
  sessionId: string
  status: BuildSessionStatus
}

/** `…/heartbeat` → 200. The portal's liveness ping. */
export interface HeartbeatResponse {
  sessionId: string
  alive: boolean
  cadenceSeconds: number
  heartbeatExpiresAt: string
}

// ─── C7: the tagged-union progress envelope (snake_case surface) ─────────────

/**
 * Where a self-heal-relevant error came from (C7 §3). `client` is RESERVED for the
 * Wave-1 browser client-error arm and is not emitted in Stage 0 — it stays in the
 * union so the type is future-proof, but nothing produces it yet.
 */
export type ErrorSource = 'tsc' | 'next_build' | 'server' | 'client'

/** The shared error sub-shape used by `error`, `escalation.last_error`, and `BuildResult.error` (snake_case, frozen). */
export interface BuildError {
  source: ErrorSource
  title: string
  cleaned_stack: string
}

/** `step` — a high-level phase marker driving the feed's spinner → check/cross (C7 §3.1). */
export interface StepEvent {
  type: 'step'
  seq: number
  name: string
  label: string
  state: 'started' | 'ok' | 'failed'
}

/** `log` — one LF-normalized build/install/dev-server line (C7 §3.2). */
export interface LogEvent {
  type: 'log'
  seq: number
  source: string
  stream: 'stdout' | 'stderr'
  text: string
}

/** `error` — the structured, self-heal-relevant error BRAIN reacts to (C7 §3.3). */
export interface ErrorEvent {
  type: 'error'
  seq: number
  source: ErrorSource
  title: string
  cleaned_stack: string
}

/** `preview_ready` — the dev server is live and framable. Flips status → `ready` and triggers the iframe (re)load (C7 §3.4). */
export interface PreviewReadyEvent {
  type: 'preview_ready'
  seq: number
  preview_url: string
}

/** `escalation` — the self-heal loop gave up; informational, the terminal boundary is the following `ended` (C7 §3.5). */
export interface EscalationEvent {
  type: 'escalation'
  seq: number
  reason: string
  detail: string
  last_error: BuildError | null
}

/** `quota_exceeded` — the per-user daily token cap was hit; BRAIN then GRACEFULLY ends (C7 §3.6, §8). */
export interface QuotaExceededEvent {
  type: 'quota_exceeded'
  seq: number
  limit: number
  used: number
  resets_at: string
}

/** The daily-limit copy shared by the activity-feed row and the session-controls banner ("resets at midnight IST"). */
export function formatDailyLimitMessage(limit: number, used: number): string {
  const cap = limit > 0 ? `${limit.toLocaleString('en-US')} tokens` : 'daily token limit'
  const spent = used > 0 ? ` (used ${used.toLocaleString('en-US')})` : ''
  return `You've hit your daily limit of ${cap}${spent}. It resets at midnight IST.`
}

/**
 * `ended` — the terminal envelope. `status` is narrowed to the two absorbing members
 * (`ended` graceful | `failed` unrecoverable). `reason` is display copy (C7 §3.7 names
 * six values), typed loosely as `string` so a new BRAIN reason renders as text rather
 * than breaking the build — the control decisions ride `status`, never `reason`.
 */
export interface EndedEvent {
  type: 'ended'
  seq: number
  status: 'ended' | 'failed'
  preview_url: string | null
  snapshot_committed: boolean
  reason: string
}

/**
 * The full 7-member C7 union (discriminated on `type`). A `switch` over `.type`
 * ending in `assertNever` is a compile error until every arm is handled.
 */
export type ProgressEnvelope =
  | StepEvent
  | LogEvent
  | ErrorEvent
  | PreviewReadyEvent
  | EscalationEvent
  | QuotaExceededEvent
  | EndedEvent

/**
 * The 6-member SUBSET the activity feed renders (U3). `preview_ready` is excluded:
 * the owning hook (U4) routes it to preview status only (frame reload), never to a
 * feed row — so the feed's `switch` + `assertNever` is over SIX members, and an
 * `assertNever` over the full 7-member union would (correctly) fail to compile
 * without a `preview_ready` arm (C7 §3.4).
 */
export type FeedEnvelope = Exclude<ProgressEnvelope, PreviewReadyEvent>
