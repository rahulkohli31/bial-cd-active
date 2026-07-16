/**
 * Admin-console data access for the super-admin user roster + per-user limits.
 * Thin wrappers over the /api/admin endpoints (super-admin-only, gated
 * server-side). Every non-2xx throws an `ApiError` (via `readApiError`) carrying
 * the backend's own message AND its `.status`/`.code`, so `UsersLimitsPanel` can
 * both show the message and branch on 403 / 409 / 404 without re-parsing the body.
 *
 * The control-plane emits three error envelopes (`{error:{message}}`,
 * `{detail:"…"}`, `{detail:[…]}`). The old `body.error?.message || fallback` read
 * was blind to the `{detail}` shape — which is exactly how the 403 super-admin
 * gate and 500 arrive — so those messages were invisible. `readApiError` reads all
 * three (see ./apiError).
 */
import { authFetch } from './api.js'
import { readApiError } from './apiError'

/**
 * GET a keyset page of the roster (newest-first), each row carrying raw overrides,
 * effective limits, `usageToday`, `role`, and the `suspendedAt` marker — plus the
 * standard-plan `defaults` and the `{nextCursor, hasMore}` cursor envelope.
 *
 * The page is filtered server-side by `q` (email / display-name substring). Today's
 * call used to send NO params, so once the backend paginated at 25 a larger roster
 * was silently truncated with nothing thrown; sending the cursor/limit/q closes that.
 */
export async function fetchUsers({ cursor, limit, q, status } = {}, deps = {}) {
  const params = new URLSearchParams()
  if (cursor) params.set('cursor', cursor)
  if (limit != null) params.set('limit', String(limit))
  if (q) params.set('q', q)
  if (status) params.set('status', status)
  const query = params.toString()
  const res = await authFetch(`/api/admin/users${query ? `?${query}` : ''}`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load users')
  const data = await res.json()
  return {
    defaults: data.defaults || {},
    users: data.users || [],
    nextCursor: data.nextCursor ?? null,
    hasMore: data.hasMore ?? false,
  }
}

/** GET the collected feedback (newest first, capped) plus the true total. */
export async function fetchFeedback(deps = {}) {
  const res = await authFetch('/api/admin/feedback', {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load feedback')
  const data = await res.json()
  return { feedback: data.feedback || [], total: data.total ?? 0 }
}

/**
 * PATCH a user's limit overrides. `userId` is the user's UUID (row.userId) — the
 * FastAPI route keys on the uuid path param. `patch` carries any of dailyTokenLimit /
 * contextSoftLimit / contextHardLimit — a number to set, or null to reset that field
 * to the default. Returns `{userId, limits, effectiveLimits}` (the new state).
 *
 * Propagation note (see docs/solutions/.../per-user-limits-daily-vs-context-…): a
 * dailyTokenLimit change lands on the user's NEXT request (live server read); the
 * context limits ride the cached profile and only take effect after the user reloads.
 */
export async function updateUserLimits(userId, patch, deps = {}) {
  const res = await authFetch(
    `/api/admin/users/${encodeURIComponent(userId)}/limits`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to update limits')
  return res.json()
}

/**
 * POST to suspend a user (R10). Returns `{userId, suspendedAt}`. The server bumps
 * the user's token_version and kills every live session/refresh/runner token, so the
 * suspension is immediate. Distinct failure paths the panel branches on:
 *   403 → target is a super-admin (never suspendable — and since the caller is one,
 *         this also covers self-suspension);
 *   409 → already suspended (another admin got there first);
 *   404 → no such user.
 */
export async function deactivateUser(userId, deps = {}) {
  const res = await authFetch(
    `/api/admin/users/${encodeURIComponent(userId)}/deactivate`,
    { method: 'POST' },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to deactivate user')
  return res.json()
}

/**
 * POST to restore a suspended user (R12). Returns `{userId, suspendedAt: null}`.
 *   409 → user is not suspended;
 *   404 → no such user.
 */
export async function reactivateUser(userId, deps = {}) {
  const res = await authFetch(
    `/api/admin/users/${encodeURIComponent(userId)}/reactivate`,
    { method: 'POST' },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to reactivate user')
  return res.json()
}

/**
 * POST to approve a pending (never-approved) user. Returns `{userId, approvedAt}`.
 * Unlike deactivate/reactivate, this never touches sessions — a pending user has no
 * live session to revoke, and approval isn't a security-sensitive act to force-recheck.
 *   409 → already approved (another admin got there first);
 *   404 → no such user.
 */
export async function approveUser(userId, deps = {}) {
  const res = await authFetch(
    `/api/admin/users/${encodeURIComponent(userId)}/approve`,
    { method: 'POST' },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to approve user')
  return res.json()
}
