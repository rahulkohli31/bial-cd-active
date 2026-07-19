/**
 * App Registry data access — ADMIN-side thin wrappers over the registry endpoints
 * (/api/admin/apps/*, admin-gated server-side), all via authFetch (Bearer +
 * refresh-and-retry): list / approve / reject / patch / disable / enable /
 * bundle download / mark-deployed / two-step clear-data / delete / audit.
 * Each throws an Error with a user-ready message on failure.
 *
 * The OWNER group (provision/submit/status/source) is RETIRED: the open-sandbox
 * submit flow lives in the typed `approvalApi.ts` (no client compile, no
 * client-supplied artifact), and provisioning happens inside the build session.
 */
import { authFetch } from './api.js'
import { readApiError } from './apiError'

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

/** Approve a pending app, pinning EXACTLY the reviewed submission (D5): the server
 * refuses (409) when the app was re-submitted since the admin reviewed it. */
export async function approveApp(appId, submissionId, deps = {}) {
  return asJson(
    await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/approve`, jsonOpts('POST', { submissionId }), deps),
    'Failed to approve',
  )
}

/** Mint a short-TTL signed download URL for the submission under review (audited server-side). */
export async function bundleDownloadUrl(appId, deps = {}) {
  return asJson(
    await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/bundle-url`, {}, deps),
    'Failed to mint the bundle download',
  )
}

/** Record that the go-live runbook was run for the approved submission, optionally
 * recording WHERE the app now lives (R5) — the URL the owner's Live link points at.
 * Omitting `deployedUrl` keeps whatever address is already recorded (the server treats
 * an absent field as "leave it alone"), which is the routine re-deploy case. The https
 * check lives server-side: a bad URL comes back as a 422 whose message the caller shows. */
export async function markDeployed(appId, deployedUrl, deps = {}) {
  return asJson(
    await authFetch(
      `/api/admin/apps/${encodeURIComponent(appId)}/mark-deployed`,
      jsonOpts('POST', deployedUrl ? { deployedUrl } : {}),
      deps,
    ),
    'Failed to mark deployed',
  )
}

/** Reject a pending app with an optional note. */
export async function rejectApp(appId, note, deps = {}) {
  return asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/reject`, jsonOpts('POST', { note }), deps), 'Failed to reject')
}

/** Patch the loginRequired gate (audited server-side). The app name is project-sourced (#48). */
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
