/**
 * App Registry data access — thin wrappers over the registry endpoints, all via
 * authFetch (Bearer + refresh-and-retry). Two groups:
 *  - ADMIN (/api/admin/apps/*, admin-gated server-side): list / approve / reject /
 *    patch / disable / enable / two-step clear-data / delete / audit.
 *  - OWNER (/api/apps/:appId/{provision,submit}, behind requireAuth): used by the
 *    builder to provision a draft and submit a build for deployment.
 * Each throws an Error with a user-ready message on failure.
 */
import { authFetch } from './api.js'
import { compileJsx } from './compileJsx.js'

async function asJson(res, fallback) {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error?.message || `${fallback} (${res.status}).`)
  }
  return res.json()
}

const jsonOpts = (method, body) => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
})

// ── Admin ──────────────────────────────────────────────────────────────────

/** List registry apps, optionally filtered by status. */
export async function listApps(status, deps = {}) {
  const q = status ? `?status=${encodeURIComponent(status)}` : ''
  const data = await asJson(await authFetch(`/api/admin/apps${q}`, {}, deps), 'Failed to load apps')
  return data.apps || []
}

/** Approve a pending app (pre-compiles + snapshots server-side). */
export async function approveApp(appId, deps = {}) {
  return asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/approve`, jsonOpts('POST'), deps), 'Failed to approve')
}

/** Reject a pending app with an optional note. */
export async function rejectApp(appId, note, deps = {}) {
  return asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/reject`, jsonOpts('POST', { note }), deps), 'Failed to reject')
}

/** Patch name / loginRequired (a loginRequired flip is audited server-side). */
export async function patchApp(appId, patch, deps = {}) {
  return asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}`, jsonOpts('PATCH', patch), deps), 'Failed to update app')
}

/** Disable (kill-switch) an approved app. */
export async function disableApp(appId, deps = {}) {
  return asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/disable`, jsonOpts('POST'), deps), 'Failed to disable')
}

/** Re-enable a disabled app. */
export async function enableApp(appId, deps = {}) {
  return asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/enable`, jsonOpts('POST'), deps), 'Failed to enable')
}

/** Clear-data step 1: preflight returning { dataCount, dataBytes, confirmToken }. */
export async function dataSummary(appId, deps = {}) {
  return asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/data-summary`, {}, deps), 'Failed to read data summary')
}

/** Clear-data step 2: the destructive op, gated on the single-use confirm token. */
export async function clearData(appId, confirmToken, createdInDraftOnly, deps = {}) {
  return asJson(
    await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/clear-data`, jsonOpts('POST', { confirmToken, createdInDraftOnly }), deps),
    'Failed to clear data',
  )
}

/** Hard-delete an app (audited, data purged, registry doc removed). */
export async function deleteApp(appId, deps = {}) {
  return asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}`, { method: 'DELETE' }, deps), 'Failed to delete app')
}

/** Recompute file counters from ready metadata + sweep stale pending uploads (audited file:gc). */
export async function recomputeFiles(appId, deps = {}) {
  return asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/recompute-files`, jsonOpts('POST'), deps), 'Failed to recompute file counters')
}

/** The app's audit trail (data mutations + admin actions), newest-first. */
export async function fetchAudit(appId, deps = {}) {
  const data = await asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/audit`, {}, deps), 'Failed to load audit')
  return data.events || []
}

// ── Owner (builder) ──────────────────────────────────────────────────────────

/**
 * Provision (idempotent) the build's registry draft; returns { appId, appKey, loginRequired, status }.
 * FastAPI keys the draft on the builder conversation itself: POST /v1/apps/provision (no id in the
 * path) with { conversationId } — the appId IS that conversation's uuid. The `/api`→`/v1` proxy
 * rewrite turns `/api/apps/provision` into `/v1/apps/provision`.
 */
export async function provisionApp(appId, deps = {}) {
  return asJson(await authFetch(`/api/apps/provision`, jsonOpts('POST', { conversationId: appId }), deps), 'Failed to provision app')
}

/**
 * Submit the current build for deployment; moves the app to pending. The server no longer runs Babel
 * (R19), so we compile the build's JSX in-browser here — the same transform `/preview` uses — and
 * POST the { source, compiled, entry } artifact the runner serves verbatim. `entry` matches the
 * snapshot the builder stores ('PreviewApp'). A compile error throws (surfaced as a toast), never a
 * silent drop. Path `/api/apps/<id>/submit` rewrites to `/v1/apps/<id>/submit`.
 */
export async function submitApp(appId, source, deps = {}) {
  const compiled = await compileJsx(source)
  const artifact = { source, compiled, entry: 'PreviewApp' }
  return asJson(await authFetch(`/api/apps/${encodeURIComponent(appId)}/submit`, jsonOpts('POST', artifact), deps), 'Failed to submit app')
}

/** Owner read of the deploy status (no provision); { status:null } if not provisioned yet. */
export async function getAppStatus(appId, deps = {}) {
  return asJson(await authFetch(`/api/apps/${encodeURIComponent(appId)}/status`, {}, deps), 'Failed to read status')
}
