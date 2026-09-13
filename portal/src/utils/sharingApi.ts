/**
 * Typed client for project sharing (#198) — colleague search, share/unshare, and the two
 * lists a share produces (a project's own share panel, and "Shared with me"). Mirrors
 * `projectApi.ts`'s shape: `fn(args, deps = {})`, camelCase wire, `ApiError` via
 * `readApiError`, response bodies narrowed from `unknown` — never cast, never `any`.
 */
import { ApiError, isRecord, optionalString, readApiError } from './apiError'
import { authFetch } from './api'
import type { AuthFetchDeps } from './api'

const JSON_HEADERS = { 'Content-Type': 'application/json' }

/** One colleague search hit — display name AND the email local part only, never a full
 *  address (`GET /colleagues` never returns one, by the same rule `MarketplacePage`'s
 *  "Built by X" follows: enough to tell two colleagues apart, not a directory). */
export interface Colleague {
  id: string
  displayName: string | null
  emailLocalPart: string
}

function toColleague(value: unknown): Colleague | null {
  if (!isRecord(value) || typeof value.id !== 'string' || value.id === '') return null
  return {
    id: value.id,
    displayName: optionalString(value.displayName),
    emailLocalPart: typeof value.emailLocalPart === 'string' ? value.emailLocalPart : '',
  }
}

/**
 * Find a colleague to share a project with. The server refuses below 3 characters (422) and
 * rate-limits at 30/minute (429) — both are ordinary, expected answers here, not failures the
 * caller logs; see `SharePanel.tsx` for how each is turned into a state rather than an error
 * banner. Returns at most 10 matches.
 */
export async function searchColleagues(q: string, deps: AuthFetchDeps = {}): Promise<Colleague[]> {
  const res = await authFetch(`/api/projects/colleagues?q=${encodeURIComponent(q)}`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to search colleagues')
  const body: unknown = await res.json()
  const doc = isRecord(body) ? body : {}
  return Array.isArray(doc.colleagues)
    ? doc.colleagues.flatMap((row) => {
        const colleague = toColleague(row)
        return colleague === null ? [] : [colleague]
      })
    : []
}

/** One row of a project's OWN share panel — who it is shared with, and when. */
export interface ProjectShare {
  id: string
  sharedWithUserId: string
  sharedWithDisplayName: string | null
  sharedWithEmailLocalPart: string
  createdAt: string
}

function toProjectShare(value: unknown): ProjectShare | null {
  if (!isRecord(value) || typeof value.id !== 'string' || value.id === '') return null
  return {
    id: value.id,
    sharedWithUserId: typeof value.sharedWithUserId === 'string' ? value.sharedWithUserId : '',
    sharedWithDisplayName: optionalString(value.sharedWithDisplayName),
    sharedWithEmailLocalPart:
      typeof value.sharedWithEmailLocalPart === 'string' ? value.sharedWithEmailLocalPart : '',
    createdAt: typeof value.createdAt === 'string' ? value.createdAt : '',
  }
}

/** Who a project is currently shared with. Owner-only — the same 404 an owner-scoped
 *  endpoint gives a non-owner everywhere else in this portal. */
export async function listProjectShares(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<ProjectShare[]> {
  const res = await authFetch(`/api/projects/${encodeURIComponent(projectId)}/shares`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load this project’s shares')
  const body: unknown = await res.json()
  const doc = isRecord(body) ? body : {}
  return Array.isArray(doc.shares)
    ? doc.shares.flatMap((row) => {
        const share = toProjectShare(row)
        return share === null ? [] : [share]
      })
    : []
}

/** Share a project with a colleague. Idempotent — sharing with the same colleague twice
 *  returns the same row rather than erroring. */
export async function shareProject(
  projectId: string,
  sharedWithUserId: string,
  deps: AuthFetchDeps = {},
): Promise<ProjectShare> {
  const res = await authFetch(
    `/api/projects/${encodeURIComponent(projectId)}:share`,
    { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ sharedWithUserId }) },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to share this project')
  const share = toProjectShare(await res.json())
  if (share === null) throw new ApiError('The server returned a share we could not read.', 500)
  return share
}

/** Revoke a colleague's access. `{ok: true}` whether or not the share still existed — a
 *  double-click or a retry, not an error. */
export async function unshareProject(
  projectId: string,
  sharedWithUserId: string,
  deps: AuthFetchDeps = {},
): Promise<void> {
  const res = await authFetch(
    `/api/projects/${encodeURIComponent(projectId)}:unshare`,
    { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ sharedWithUserId }) },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to revoke this share')
}

/** One row of "Shared with me" — the project, who shared it, and when. No email: the
 *  stricter attribution rule `MarketplaceEntry.builderDisplayName` already established for
 *  showing one citizen's identity to another (display name only). */
export interface SharedProject {
  projectId: string
  projectName: string
  projectDescription: string | null
  sharedByDisplayName: string | null
  sharedAt: string
}

function toSharedProject(value: unknown): SharedProject | null {
  if (!isRecord(value) || typeof value.projectId !== 'string' || value.projectId === '') return null
  return {
    projectId: value.projectId,
    projectName: typeof value.projectName === 'string' ? value.projectName : '',
    projectDescription: optionalString(value.projectDescription),
    sharedByDisplayName: optionalString(value.sharedByDisplayName),
    sharedAt: typeof value.sharedAt === 'string' ? value.sharedAt : '',
  }
}

/** One keyset page of `SharedProject` rows — the shape `useKeysetList` expects. */
export interface SharedProjectsPage {
  items: SharedProject[]
  nextCursor: string | null
  hasMore: boolean
}

/** Projects a colleague has shared with the caller, newest grant first. Keyset-paginated —
 *  a list every sharer writes into, unlike the caller's own numbered-offset project list. */
export async function listSharedWithMe(
  args: { cursor?: string | null; limit?: number } = {},
  deps: AuthFetchDeps = {},
): Promise<SharedProjectsPage> {
  const params = new URLSearchParams()
  if (args.cursor) params.set('cursor', args.cursor)
  if (args.limit !== undefined) params.set('limit', String(args.limit))
  const qs = params.toString()
  const res = await authFetch(`/api/projects/shared${qs ? `?${qs}` : ''}`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load projects shared with you')
  const body: unknown = await res.json()
  const doc = isRecord(body) ? body : {}
  return {
    items: Array.isArray(doc.items)
      ? doc.items.flatMap((row) => {
          const entry = toSharedProject(row)
          return entry === null ? [] : [entry]
        })
      : [],
    nextCursor: optionalString(doc.nextCursor),
    hasMore: doc.hasMore === true,
  }
}
