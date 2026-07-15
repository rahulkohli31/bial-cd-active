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
import { readApiError } from './apiError'
import { compileJsx } from './compileJsx.js'

// Throws an ApiError carrying `.status`, so callers branch on 404 (project deleted) and
// 409 (app owned by another user) instead of string-matching a message.
async function asJson(res, fallback) {
  if (!res.ok) throw await readApiError(res, fallback)
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

/** The app's audit trail (data mutations + admin actions), newest-first. */
export async function fetchAudit(appId, deps = {}) {
  const data = await asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/audit`, {}, deps), 'Failed to load audit')
  return data.events || []
}

// ── Owner (builder) ──────────────────────────────────────────────────────────

/**
 * Provision (idempotent) the PROJECT's one app; returns { appId, appKey, loginRequired, status }.
 *
 * The returned `appId` is a FRESH uuid — it is NOT the conversation's id. Those were the
 * same value once, and a stale JSDoc here claimed so; passing a conversation id where an
 * appId belongs now points every data-service call at a store that does not exist.
 *
 * Idempotent PER PROJECT: a second builder chat in the same project provisions and gets
 * back the SAME appId. That is what makes "more chats, one app" true.
 *
 * 422 missing/non-uuid · 404 project not owned · 409 app owned by another user.
 */
export async function provisionApp({ conversationId, projectId }, deps = {}) {
  return asJson(
    await authFetch('/api/apps/provision', jsonOpts('POST', { conversationId, projectId }), deps),
    'Failed to provision app',
  )
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

/**
 * Owner read of the deploy status (never provisions).
 *
 * Resolves to `{ status: null }` when the app does not exist — a normal "not provisioned
 * yet" result, not an error. The backend used to say that with `200 {status:null}`; it now
 * says it with a **404**, and the shared `asJson` helper throws on any non-2xx. Mapping it
 * back to `{status: null}` here restores the semantic at the layer that owns the contract,
 * rather than leaving every caller to special-case a status code.
 *
 * A real 5xx/auth failure still rejects, so the caller can surface it.
 */
export async function getAppStatus(appId, deps = {}) {
  const res = await authFetch(`/api/apps/${encodeURIComponent(appId)}/status`, {}, deps)
  if (res.status === 404) return { status: null }
  return asJson(res, 'Failed to read status')
}

/**
 * Owner read of the project's ONE durable app code (KD-9), by appId; returns { source, entry }.
 *
 * This is what lets ANY builder chat in a project render the existing app. Code is stored
 * per-conversation, so a chat that did not build the app — a second chat, or a plain "hi"
 * turn — has no snapshot of its own and would otherwise show a blank canvas over a fully-built
 * app. `source` is `''` when the app has no code yet (LivePreview's empty state).
 *
 * A 404 (the app was deleted out from under an open chat, or a cross-user id) is masked to the
 * same empty result: a preview that can't load is never fatal. A real 5xx/auth failure still
 * rejects. Path `/api/apps/<id>/source` rewrites to `/v1/apps/<id>/source`.
 */
export async function getAppSource(appId, deps = {}) {
  const res = await authFetch(`/api/apps/${encodeURIComponent(appId)}/source`, {}, deps)
  if (res.status === 404) return { source: '', entry: 'PreviewApp' }
  return asJson(res, 'Failed to read app source')
}
