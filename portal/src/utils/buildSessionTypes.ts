/**
 * Wire shapes for the build-session control surface and the SSE progress envelope, plus two
 * pure helpers (`isActiveBuildStatus`, `formatDailyLimitMessage`) shared by every surface.
 *
 * TWO DELIBERATELY DIFFERENT CASINGS: REST bodies are camelCase (backend serializes by alias);
 * the progress envelope stays snake_case — field names AND `type` literals (`preview_ready`,
 * `cleaned_stack`, `resets_at`) — with NO alias generator. Every guard below discriminates on
 * those exact literals, so an aliased envelope would go quiet, not error.
 *
 * Bodies arrive as `unknown`, narrowed with type guards at the boundary — never cast, never `any`.
 */

// ─── The control-plane status enum (camelCase surface) ───────────────────────

/**
 * The five members of the build-session lifecycle. Wire value == the
 * lowercase member name. `provisioning → building → ready` is the forward path;
 * `ended` (graceful: stop / idle / quota) and `failed` (unrecoverable / escalated)
 * are the two DISTINCT absorbing terminals — a quota breach resolves to `ended`,
 * never `failed`.
 */
export type BuildSessionStatus = 'provisioning' | 'building' | 'ready' | 'ended' | 'failed'

/** The three non-terminal (in-progress) lifecycle states — a build is "active" while in any of them. */
export function isActiveBuildStatus(status: BuildSessionStatus | null): boolean {
  return status === 'provisioning' || status === 'building' || status === 'ready'
}

// ─── Control operations — relaunch / stop / status ───────────────────────────
//
// `StartBuildRequest` / `StartBuildResponse` are GONE with the client `start` wrapper they
// typed, and nothing else read them. The ROUTE is untouched — deleting a browser client says
// nothing about it.

/** `POST …/relaunch` body — restore a project's saved app into a fresh, ready sandbox. */
export interface RelaunchPreviewRequest {
  projectId: string
}

/**
 * `POST …/relaunch` → 200. NO `sessionId`/`createdAt`: relaunch registers no build session
 * (it must not occupy the build slot), so there is nothing to poll or stop. It
 * returns a live `previewUrl` synchronously (the server blocked on `wait_ready` before replying).
 */
export interface RelaunchPreviewResponse {
  appId: string
  previewUrl: string
  status: BuildSessionStatus
  /**
   * The "last saved version" signal: the project's NEWEST recorded build outcome was FAILED, so
   * the restored snapshot is the last SAVED state — not that build's intent. The preview pane
   * surfaces this so the user isn't silently shown older code as an unqualified "ready".
   */
  restoredFromFailedBuild: boolean
  /**
   * Is the app actually SERVING `previewUrl` yet? False when the server attached to a live
   * container whose root route had not answered within its readiness budget. The URL is framable
   * either way — the pane keeps its labelled wait up until the framed document loads, exactly as
   * it does for a first build. Absent reads as `true` (the historic contract: relaunch only ever
   * replied once the dev server was up).
   */
  ready: boolean
}

/**
 * `POST …/projects/{id}/shared-launch` and `.../shared-refresh` → 200 (#198).
 * `RelaunchPreviewResponse`'s sibling for a project a colleague shares with the viewer — "Can
 * use", never "view only" (Key Decision 3): the viewer can create, update and delete the
 * owner's records through the app's own UI. No `status`/`restoredFromFailedBuild`: this view
 * registers no build session and has no build-outcome history of its own to qualify.
 */
export interface SharedPreviewResponse {
  appId: string
  previewUrl: string
  /** Is the app actually SERVING `previewUrl` yet? False only on a degraded attach — the
   *  container is alive, the app is just slow to answer. The URL is framable either way. */
  ready: boolean
  /** When the snapshot NOW BEING SERVED was saved — `null` only when the server could not
   *  ask the store for the timestamp; the restore itself already confirmed the snapshot
   *  exists. Refresh's whole point is moving this forward. */
  snapshotTakenAt: string | null
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
 * is the highest envelope `seq` emitted so far (the reconnect cursor), or null before the
 * first envelope. On connect/reattach the owning hook seeds preview continuity from
 * here, so a `preview_ready` that fired before the client connected still frames the
 * app.
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

// ─── Lock operations — the whole response surface is gone ───────────────────
//
// `LockStateResponse` / `LockReleaseResponse` / `HeartbeatResponse` typed `acquire` / `renew` /
// `release` / `heartbeat`, and nothing called those routes — the portal's keep-alive loop that
// was their only caller was itself deleted. `ForceEndResponse` was the last one standing, and
// it went with the `force-end` route itself, which had had no UI call site since the block
// banner's Force-end button was deleted with the banner.

// ─── The tagged-union progress envelope (snake_case surface) ─────────────────

/**
 * Where a self-heal-relevant error came from. `client` is the browser client-error
 * arm — LIVE, and rendered through the same split-audience path as every other class: its
 * report text stays agent-only, the citizen reads the platform's sentence for the class.
 */
export type ErrorSource = 'tsc' | 'next_build' | 'server' | 'client'

/** The shared error sub-shape used by `error`, `escalation.last_error`, and `BuildResult.error` (snake_case, frozen). */
export interface BuildError {
  source: ErrorSource
  title: string
  cleaned_stack: string
}

/** `step` — a high-level phase marker driving the feed's spinner → check/cross. */
export interface StepEvent {
  type: 'step'
  seq: number
  name: string
  label: string
  state: 'started' | 'ok' | 'failed'
  /**
   * Read-only and housekeeping steps are dropped from the VISIBLE feed. Optional so the
   * type stays back-compat with older emitters that predate this field (a missing value
   * reads as "not hidden").
   */
  hidden?: boolean
}

/** `error` — the structured, self-heal-relevant error the orchestrator reacts to. */
export interface ErrorEvent {
  type: 'error'
  seq: number
  source: ErrorSource
  title: string
  cleaned_stack: string
  /** True when this came off the turn stream as a `diagnostic` — the turn is NOT failing, a
   *  repair run follows — so it renders as a retry, never as the terminal red block. Absent
   *  on the legacy build-session feed, which keeps its historical red rendering. */
  recovering?: boolean
  /**
   * The CITIZEN-facing half of the split: `title`/`cleaned_stack` above are the model's (built
   * to be the compiler's own first line, naming a file and construct by design); these two are
   * what the feed renders instead — a plain sentence and something the reader can do.
   *
   * OPTIONAL: the legacy build-session feed emits neither, so a committed fallback pair renders
   * in its place (`DIAGNOSTIC_FALLBACK`) — an error status is never shown without an action.
   */
  user_message?: string
  user_action?: string
}

/** `preview_ready` — the dev server is live and framable. Flips status → `ready` and triggers the iframe (re)load. */
export interface PreviewReadyEvent {
  type: 'preview_ready'
  seq: number
  preview_url: string
}

/**
 * `preview_reconnecting` — the dev-server PROCESS crashed (port closed) after the preview was
 * framed. A status SIGNAL, not a feed row: the owning hook routes it to a distinct
 * `reconnecting` flag (never the "building" spinner), and a following `preview_ready` clears it.
 * Excluded from `FeedEnvelope` alongside `preview_ready`.
 */
export interface PreviewReconnectingEvent {
  type: 'preview_reconnecting'
  seq: number
}

/** `escalation` — the self-heal loop gave up; informational, the terminal boundary is the following `ended`. */
export interface EscalationEvent {
  type: 'escalation'
  seq: number
  reason: string
  detail: string
  last_error: BuildError | null
}

/** `quota_exceeded` — the per-user daily token cap was hit; the orchestrator then GRACEFULLY ends the build. */
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
 * `ended` — the terminal envelope. `status` narrows to the two absorbing members (`ended`
 * graceful | `failed` unrecoverable); `reason` is loosely-typed display copy so a new
 * orchestrator reason renders as text, not a crash. Control decisions ride `status` alone,
 * with ONE exception: `reason === 'completed'` marks the pardoned preview (server keeps its
 * container alive under an idle lease), which is what lets the pane keep framing it.
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
 * The full 7-member envelope union (discriminated on `type`). A `switch` over `.type`
 * ending in `assertNever` is a compile error until every arm is handled.
 *
 * `LogEvent` was retired: no production orchestrator path had ever emitted a `log` frame, so this
 * was a consumer arm the server could never actually feed. Removing it drops the count from 8 to 7.
 */
export type ProgressEnvelope =
  | StepEvent
  | ErrorEvent
  | PreviewReadyEvent
  | PreviewReconnectingEvent
  | EscalationEvent
  | QuotaExceededEvent
  | EndedEvent

/**
 * The 5-member SUBSET the activity feed renders. BOTH preview signals are excluded:
 * the owning hook routes `preview_ready` and `preview_reconnecting` to preview STATUS
 * only (frame reload / reconnecting flag), never to a feed row — so the feed's `switch` +
 * `assertNever` stays over FIVE members, and an `assertNever` over the full 7-member union would
 * (correctly) fail to compile without those two arms.
 */
export type FeedEnvelope = Exclude<ProgressEnvelope, PreviewReadyEvent | PreviewReconnectingEvent>
