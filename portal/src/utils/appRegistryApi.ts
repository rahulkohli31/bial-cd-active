/**
 * App Registry data access — ADMIN-side thin wrappers over the registry endpoints
 * (/api/admin/apps/*, admin-gated server-side), all via authFetch (Bearer +
 * refresh-and-retry): list / approve / reject / patch / disable / enable /
 * bundle download / mark-deployed / delete / audit.
 * Each throws an Error with a user-ready message on failure.
 *
 * The OWNER group (provision/submit/status/source) is RETIRED: the open-sandbox
 * submit flow lives in the typed `approvalApi.ts` (no client compile, no
 * client-supplied artifact), and provisioning happens inside the build session.
 */
import { authFetch } from './api.js'
import { readApiError } from './apiError'

type AuthFetchDeps = NonNullable<Parameters<typeof authFetch>[2]>

/**
 * Throws an ApiError carrying `.status`, so callers branch on 404 (project deleted) and
 * 409 (app owned by another user) instead of string-matching a message.
 *
 * `T` is an ASSERTION by the CALLER, not validation performed here — this parses the
 * body to `unknown` and hands it back typed as whatever `T` the call site named,
 * unchecked. Every one of this file's nine call sites is trusting the server's shape
 * as-is, matching pre-migration behavior exactly (this function did zero validation
 * before either). To make it real validation instead of an assertion, each call site
 * would need its own `toX`-style narrowing function (`projectApi.ts`'s `toProject`/
 * `toProjectsPage` pattern: `isRecord` + per-field guards, throwing or defaulting
 * per-field as appropriate) — not done here, types-only diff.
 */
async function asJson<T>(res: Response, fallback: string): Promise<T> {
  if (!res.ok) throw await readApiError(res, fallback)
  const body: unknown = await res.json()
  return body as T
}

const jsonOpts = (method: string, body?: unknown) => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
})

// ── Admin ──────────────────────────────────────────────────────────────────

/**
 * List registry apps, optionally filtered by status.
 *
 * Typed `unknown[]` on purpose: the real per-app shape (`RegistryApp` — appId, name,
 * ownerUsername, loginRequired, submissionId, deployedUrl, …) lives with
 * `AppRegistryPanel.jsx`'s own conversion (Step 4), which is the only place that
 * actually reads these fields today. Deriving it here would be guessing at a shape
 * this file itself never touches.
 */
export async function listApps(status?: string, deps: AuthFetchDeps = {}): Promise<unknown[]> {
  const q = status ? `?status=${encodeURIComponent(status)}` : ''
  const data = await asJson<{ apps?: unknown[] }>(await authFetch(`/api/admin/apps${q}`, {}, deps), 'Failed to load apps')
  return data.apps || []
}

/** Approve a pending app, pinning EXACTLY the reviewed submission (D5): the server
 * refuses (409) when the app was re-submitted since the admin reviewed it. */
export async function approveApp(appId: string, submissionId: string, deps: AuthFetchDeps = {}): Promise<unknown> {
  return asJson(
    await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/approve`, jsonOpts('POST', { submissionId }), deps),
    'Failed to approve',
  )
}

/** Mint a short-TTL signed download URL for the submission under review (audited
 * server-side). Typed concretely — the one caller (AppRegistryPanel.jsx) reads
 * `.url` directly. */
export async function bundleDownloadUrl(appId: string, deps: AuthFetchDeps = {}): Promise<{ url: string }> {
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
export async function markDeployed(appId: string, deployedUrl?: string, deps: AuthFetchDeps = {}): Promise<unknown> {
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
export async function rejectApp(appId: string, note: string | undefined, deps: AuthFetchDeps = {}): Promise<unknown> {
  return asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/reject`, jsonOpts('POST', { note }), deps), 'Failed to reject')
}

/** Patch the loginRequired gate (audited server-side). The app name is project-sourced (#48). */
export async function patchApp(appId: string, patch: Record<string, unknown>, deps: AuthFetchDeps = {}): Promise<unknown> {
  return asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}`, jsonOpts('PATCH', patch), deps), 'Failed to update app')
}

/** Disable (kill-switch) an approved app. */
export async function disableApp(appId: string, deps: AuthFetchDeps = {}): Promise<unknown> {
  return asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/disable`, jsonOpts('POST'), deps), 'Failed to disable')
}

/** Re-enable a disabled app. */
export async function enableApp(appId: string, deps: AuthFetchDeps = {}): Promise<unknown> {
  return asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/enable`, jsonOpts('POST'), deps), 'Failed to enable')
}

/** Hard-delete an app (audited; blobs swept, registry row and app database removed). */
export async function deleteApp(appId: string, deps: AuthFetchDeps = {}): Promise<unknown> {
  return asJson(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}`, { method: 'DELETE' }, deps), 'Failed to delete app')
}

/**
 * The app's audit trail (data mutations + admin actions), newest-first.
 *
 * Typed `unknown[]` on purpose — same reasoning as `listApps`: the real per-event
 * shape lives with `AppRegistryPanel.jsx`'s own conversion (Step 4).
 */
export async function fetchAudit(appId: string, deps: AuthFetchDeps = {}): Promise<unknown[]> {
  const data = await asJson<{ events?: unknown[] }>(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/audit`, {}, deps), 'Failed to load audit')
  return data.events || []
}
