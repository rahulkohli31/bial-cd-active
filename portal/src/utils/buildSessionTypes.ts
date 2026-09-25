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

/** `POST …/relaunch` body — start a project's saved app. The 202 it earns carries nothing a client
 *  reads: the preview-state poll reports the start. */
export interface RelaunchPreviewRequest {
  projectId: string
}

/**
 * `POST …/projects/{id}/shared-launch` and `.../shared-refresh` → 200 (#198), for a project a
 * colleague shares with the viewer — "Can use", never "view only": the viewer can create, update
 * and delete the owner's records through the app's own UI. Answered once the view is up: the
 * viewer has no preview-state to poll, so this is where the address and `ready` arrive.
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
