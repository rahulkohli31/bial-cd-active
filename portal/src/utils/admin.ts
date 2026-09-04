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
 * the one real caller (UsersLimitsPanel.tsx) always sends all three, each a
 * number to set or null to reset to the default. */
interface UserLimitsPatch {
  dailyTokenLimit: number | null
  contextSoftLimit: number | null
  contextHardLimit: number | null
}

/** Mirrors the backend's `LimitFields` (`backend/src/api/v1/admin/schemas.py`) —
 * the shared shape for a row's `limits` (raw override, any/all fields may be
 * null) and `effectiveLimits` (resolved, always fully populated in practice,
 * though the schema itself doesn't distinguish that from `limits` at the type
 * level). */
export interface LimitFields {
  dailyTokenLimit: number | null
  contextSoftLimit: number | null
  contextHardLimit: number | null
}

/** Mirrors the backend's `UserLimitsOut` (`backend/src/api/v1/admin/schemas.py`,
 * `CamelModel`-based — camelCase on the wire). `role` is a real two-value enum
 * (`backend/src/services/rbac/roles.py`'s `Role(StrEnum)`), not an open string. */
export interface UserLimitsOut {
  userId: string
  email: string
  displayName: string | null
  role: 'citizen' | 'super_admin'
  suspendedAt: string | null
  usageToday: number
  limits: LimitFields
  effectiveLimits: LimitFields
}

/** The roster-page envelope `fetchUsers` resolves to. Mirrors the backend's
 * `UsersResponse`. `defaults` is `Partial<LimitFields>` (not `LimitFields`)
 * because a real caller can get an envelope without it — the fallback below is
 * `{}` rather than a fabricated all-null object, so a types-only diff should
 * widen the type to match the real fallback rather than reshape the fallback to
 * match a narrower type. (Independently, release's own admin.js fallback
 * already reads `data.defaults || {}` too.) */
interface UsersPage {
  defaults: Partial<LimitFields>
  users: UserLimitsOut[]
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
 * `cursor` is `string | null` (not just `string`), matching `useKeysetList`'s real
 * `KeysetFetchArgs.cursor: string | null` contract — passed straight through rather
 * than forcing an unnecessary conversion at the call site.
 *
 * `signal` (optional) rides straight through to the underlying `fetch()` call via
 * `authFetch`'s spread `opts` — lets a caller with an unmount-scoped AbortController
 * (UsersLimitsPanel's background bulk-load chain) actually cancel an in-flight
 * request instead of just discarding its eventual response.
 */
export async function fetchUsers(
  { cursor, limit, q, signal }: { cursor?: string | null; limit?: number; q?: string; signal?: AbortSignal } = {},
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

/** Mirrors the backend's `FeedbackItem` (`backend/src/api/v1/admin/schemas.py`,
 * `CamelModel`-based — camelCase on the wire). `page` is non-nullable in both the
 * DB column (`nullable=False, server_default=""`) and the schema: it's always a
 * string, possibly empty, never null. `userId` is part of the real row but has no
 * reader today (FeedbackPanel.jsx only ever displayed email/message/page/createdAt). */
export interface FeedbackItem {
  userId: string
  email: string
  message: string
  page: string
  createdAt: string
}

/** GET the collected feedback (newest first, capped) plus the true total. */
export async function fetchFeedback(deps: AuthFetchDeps = {}): Promise<{ feedback: FeedbackItem[]; total: number }> {
  const res = await authFetch('/api/admin/feedback', {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load feedback')
  // UNCHECKED (matches pre-migration behavior): the shape is asserted, not validated.
  const body: unknown = await res.json()
  const data = body as { feedback?: FeedbackItem[]; total?: number }
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
export async function updateUserLimits(
  userId: string,
  patch: UserLimitsPatch,
  deps: AuthFetchDeps = {},
): Promise<{ userId: string; limits: LimitFields; effectiveLimits: LimitFields }> {
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
  return body as { userId: string; limits: LimitFields; effectiveLimits: LimitFields }
}

/**
 * POST to set the SAME daily token limit for many users in one request — the admin
 * "Global Limits" bulk apply. `userIds` omitted/undefined targets EVERY user
 * system-wide; a non-empty array targets exactly those users. Unlike
 * `updateUserLimits`, there is no "reset to default" here — bulk always sets an
 * exact value — and it never touches the per-conversation context limits.
 *
 * `confirmAll` is sent `true` whenever `userIds` is omitted — the backend requires it
 * as an explicit opt-in for the "every user, system-wide" scope, since field-absence
 * alone would otherwise be the most destructive input for an irreversible fleet-wide
 * mutation. There's no separate confirmation step here because this function IS the
 * confirmed action: the caller's own confirm step already gated the click that reaches
 * this call.
 *
 * Returns `{updatedCount}`.
 */
export async function bulkUpdateUserLimits(
  dailyTokenLimit: number,
  userIds: string[] | undefined,
  deps: AuthFetchDeps = {},
): Promise<{ updatedCount: number }> {
  const res = await authFetch(
    '/api/admin/users/limits/bulk',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        dailyTokenLimit,
        userIds: userIds ?? null,
        confirmAll: userIds === undefined,
      }),
    },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to apply the bulk limit')
  // UNCHECKED (matches pre-migration behavior): the shape is asserted, not validated.
  const body: unknown = await res.json()
  return body as { updatedCount: number }
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


// --- deleted projects (#176) ----------------------------------------------------

/** One deletion, as the admin console reads it.
 *
 *  `deletedBy` and `deletedByName` are NOT interchangeable. The first is the account that
 *  acted; the second is a readable label for it. Both are stamped server-side from the
 *  session — the name was briefly client-supplied, which let a browser signed in as one
 *  person file a deletion under another person's name — so they cannot disagree, but only
 *  `deletedBy` is an identity. */
export interface DeletedProjectRow {
  id: string
  projectId: string
  projectName: string
  ownerId: string
  ownerEmail: string
  deletedBy: string
  deletedByName: string
  deletedAt: string
  remark: string
  chatsDeleted: number
  hadApp: boolean
  hadDatabase: boolean
}

export interface DeletedProjectsPage {
  deletions: DeletedProjectRow[]
  nextCursor: string | null
  hasMore: boolean
}

/**
 * Search the deletions log: a keyset page, newest first, optionally filtered by `q` (project
 * name, owner email, who deleted it, or the reason itself) and by a `deletedFrom`/`deletedTo`
 * range over when the deletion happened.
 *
 * A POST, THOUGH IT READS, and the method is the security property rather than an ergonomic
 * one. The route commits an audit row on every call — its own comment says "A READ THAT
 * WRITES" — but the backend's cross-origin guard fires only on POST/PUT/PATCH/DELETE,
 * precisely because a GET is not supposed to mutate. As a GET, an audited admin-only endpoint
 * sat outside that guard with a `SameSite=Lax` session cookie, and generated apps are served
 * same-site: app code written by a model from a citizen's prompt could drive a super-admin's
 * session into writing audit rows under their identity. POST puts it back inside the guard and
 * picks up the `X-CSRF-Token` `authFetch` attaches to every non-GET.
 *
 * It also keeps the search term out of the URL — a query string is logged verbatim by uvicorn's
 * access log and by the gateway's `requestUri`, two audiences wider than the super-admins this
 * screen is gated to, with a retention this repo does not control.
 *
 * KEYSET, unlike `/api/projects`, and the difference is deliberate rather than an
 * inconsistency: that list needs a `total` for `Showing 1-8 of 12`, and this one does not.
 * The table is append-only with a time-sortable key, so the cursor is just the last row's id.
 */
export async function fetchDeletedProjects(
  {
    cursor,
    limit,
    q,
    deletedFrom,
    deletedTo,
    signal,
  }: {
    cursor?: string | null
    limit?: number
    q?: string
    deletedFrom?: string | null
    deletedTo?: string | null
    signal?: AbortSignal
  } = {},
  deps: AuthFetchDeps = {},
): Promise<DeletedProjectsPage> {
  const res = await authFetch(
    '/api/admin/deleted-projects/search',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // Empty values are sent as null rather than omitted: the server reads absent and blank
      // identically, and one shape on the wire keeps the request easy to read in a har file.
      body: JSON.stringify({
        cursor: cursor || null,
        limit: limit ?? null,
        q: q || null,
        deletedFrom: deletedFrom || null,
        deletedTo: deletedTo || null,
      }),
      signal,
    },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to load deletions')
  const body: unknown = await res.json()
  const data = body as Partial<DeletedProjectsPage>
  return {
    deletions: data.deletions || [],
    nextCursor: data.nextCursor ?? null,
    hasMore: data.hasMore ?? false,
  }
}

/** One recorded read of the deletions log, as the "who has read this" strip shows it. */
export interface DeletionsAuditEvent {
  id: string
  action: string
  username: string | null
  createdAt: string
  detail: { filtered?: boolean; count?: number; cursor?: string | null } | null
  count: number | null
}

/**
 * Who has read the deletions log, newest first.
 *
 * The reason cross-owner reading is defensible on this screen is that reading is itself
 * recorded — and until this existed nothing could retrieve the record. The other audit surface
 * matches on an app id, and a search of the deletions log has no app, so those rows sat where
 * no reader could ever find them: the same write-only shape #176 was raised to close, one layer
 * up, in the table whose entire job is accountability.
 *
 * A plain GET, unlike its sibling above, because this one genuinely writes nothing.
 */
export async function fetchDeletionsAudit(
  { signal }: { signal?: AbortSignal } = {},
  deps: AuthFetchDeps = {},
): Promise<DeletionsAuditEvent[]> {
  const res = await authFetch('/api/admin/deleted-projects/audit', { signal }, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load the read log')
  const body: unknown = await res.json()
  return (body as { events?: DeletionsAuditEvent[] }).events || []
}
