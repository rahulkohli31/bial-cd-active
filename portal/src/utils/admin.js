/**
 * Admin-console data access for per-user usage limits. Thin wrappers over the
 * /api/admin endpoints (admin-only, gated server-side). Each throws an Error
 * with a user-ready message on failure so the AdminPage can surface it.
 */
import { authFetch } from './api.js'

/** GET the user list with raw overrides + effective limits, plus the standard-plan defaults. */
export async function fetchUsers(deps = {}) {
  const res = await authFetch('/api/admin/users', {}, deps)
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error?.message || `Failed to load users (${res.status}).`)
  }
  const data = await res.json()
  return { users: data.users || [], defaults: data.defaults || {} }
}

/** GET the collected feedback (newest first, capped) plus the true total. */
export async function fetchFeedback(deps = {}) {
  const res = await authFetch('/api/admin/feedback', {}, deps)
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error?.message || `Failed to load feedback (${res.status}).`)
  }
  const data = await res.json()
  return { feedback: data.feedback || [], total: data.total ?? 0 }
}

/**
 * PATCH a user's limit overrides. `userId` is the user's UUID (row.user_id) —
 * the FastAPI route keys on the uuid path param, not the username. `patch`
 * carries any of dailyTokenLimit / contextSoftLimit / contextHardLimit — a
 * number to set, or null to reset that field to the default. Returns the user's
 * new effective limits.
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
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error?.message || `Failed to update limits (${res.status}).`)
  }
  return res.json()
}
