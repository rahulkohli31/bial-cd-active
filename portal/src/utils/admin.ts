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
import { authFetch } from './api'
import type { AuthFetchDeps } from './api'
import { readApiError } from './apiError'

/** Mirrors the backend's `LimitFields` (`backend/src/api/v1/admin/schemas.py`) —
 * the one real caller (UsersLimitsPanel.jsx) always sends all three, each a
 * number to set or null to reset to the default. */
interface UserLimitsPatch {
  dailyTokenLimit: number | null
  contextSoftLimit: number | null
  contextHardLimit: number | null
}

/** The roster-page envelope `fetchUsers` resolves to. `defaults`/`users` stay
 * `unknown` — the real per-row/defaults shape belongs with UsersLimitsPanel.jsx's
 * own conversion, the only place that reads their fields today. */
interface UsersPage {
  defaults: unknown
  users: unknown[]
  nextCursor: string | null
  hasMore: boolean
}

/**
 * GET a keyset page of the roster (newest-first), each row carrying raw overrides,
 * effective limits, `usageToday`, `role`, and the `suspendedAt` marker — plus the
 * standard-plan `defaults` and the `{nextCursor, hasMore}` cursor envelope.
 *
 * The page is filtered server-side by `q` (email / display-name substring). Today's
 * call used to send NO params, so once the backend paginated at 25 a larger roster
 * was silently truncated with nothing thrown; sending the cursor/limit/q closes that.
 *
 * `signal` (optional) rides straight through to the underlying `fetch()` call via
 * `authFetch`'s spread `opts` — lets a caller with an unmount-scoped AbortController
 * (UsersLimitsPanel's background bulk-load chain) actually cancel an in-flight
 * request instead of just discarding its eventual response.
 */
export async function fetchUsers(
  { cursor, limit, q, signal }: { cursor?: string; limit?: number; q?: string; signal?: AbortSignal } = {},
  deps: AuthFetchDeps = {},
): Promise<UsersPage> {
  const params = new URLSearchParams()
  if (cursor) params.set('cursor', cursor)
  if (limit != null) params.set('limit', String(limit))
  if (q) params.set('q', q)
  const query = params.toString()
  const res = await authFetch(`/api/admin/users${query ? `?${query}` : ''}`, { signal }, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load users')
  // UNCHECKED (matches pre-migration behavior): the shape is asserted, not validated.
  const body: unknown = await res.json()
  const data = body as Partial<UsersPage>
  return {
    defaults: data.defaults || {},
    users: data.users || [],
    nextCursor: data.nextCursor ?? null,
    hasMore: data.hasMore ?? false,
  }
}

/** GET the collected feedback (newest first, capped) plus the true total. `feedback`
 * stays `unknown` — the real per-row shape belongs with FeedbackPanel.jsx's own
 * conversion, the only place that reads its fields today. */
export async function fetchFeedback(deps: AuthFetchDeps = {}): Promise<{ feedback: unknown[]; total: number }> {
  const res = await authFetch('/api/admin/feedback', {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load feedback')
  // UNCHECKED (matches pre-migration behavior): the shape is asserted, not validated.
  const body: unknown = await res.json()
  const data = body as { feedback?: unknown[]; total?: number }
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
 *
 * `limits`/`effectiveLimits` stay `unknown` — the one caller (UsersLimitsPanel.jsx)
 * merges them wholesale into local state without reading their inner fields.
 */
export async function updateUserLimits(
  userId: string,
  patch: UserLimitsPatch,
  deps: AuthFetchDeps = {},
): Promise<{ userId: string; limits: unknown; effectiveLimits: unknown }> {
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
  // UNCHECKED (matches pre-migration behavior): the shape is asserted, not validated.
  const body: unknown = await res.json()
  return body as { userId: string; limits: unknown; effectiveLimits: unknown }
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
export async function deactivateUser(userId: string, deps: AuthFetchDeps = {}): Promise<{ userId: string; suspendedAt: string | null }> {
  const res = await authFetch(
    `/api/admin/users/${encodeURIComponent(userId)}/deactivate`,
    { method: 'POST' },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to deactivate user')
  // UNCHECKED (matches pre-migration behavior): the shape is asserted, not validated.
  const body: unknown = await res.json()
  return body as { userId: string; suspendedAt: string | null }
}

/**
 * POST to restore a suspended user (R12). Returns `{userId, suspendedAt: null}`.
 *   409 → user is not suspended;
 *   404 → no such user.
 */
export async function reactivateUser(userId: string, deps: AuthFetchDeps = {}): Promise<{ userId: string; suspendedAt: string | null }> {
  const res = await authFetch(
    `/api/admin/users/${encodeURIComponent(userId)}/reactivate`,
    { method: 'POST' },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to reactivate user')
  // UNCHECKED (matches pre-migration behavior): the shape is asserted, not validated.
  const body: unknown = await res.json()
  return body as { userId: string; suspendedAt: string | null }
}

/**
 * POST to zero out a user's TODAY-only token usage, so they don't have to wait for
 * the IST midnight rollover. Returns `{userId, usageToday: 0}`. Idempotent — no 409,
 * resetting an already-zero day is a harmless no-op. 404 → no such user.
 */
export async function resetUserUsage(userId: string, deps: AuthFetchDeps = {}): Promise<{ userId: string; usageToday: number }> {
  const res = await authFetch(
    `/api/admin/users/${encodeURIComponent(userId)}/reset-usage`,
    { method: 'POST' },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to reset usage')
  // UNCHECKED (matches pre-migration behavior): the shape is asserted, not validated.
  const body: unknown = await res.json()
  return body as { userId: string; usageToday: number }
}
