/**
 * Wire shapes for the build-session control surface. REST bodies are camelCase (the backend
 * serializes by alias).
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

// ─── Control operations — relaunch / shared launch ───────────────────────────

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
 * use", never "view only": the viewer can create, update and delete the
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
