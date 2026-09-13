/**
 * Typed client for the projects domain (`/api/projects*`), mirroring the
 * deps-injection shape of `conversationApi.ts`: every call is
 * `fn(args, deps = {})` and forwards `deps` to `authFetch` (Bearer + one
 * 401-refresh retry, cookie session). The edge rewrites `/api/*` to `/v1/*`.
 *
 * Wire format is camelCase (the backend serializes `by_alias=True`). Response
 * bodies are untrusted network input: they arrive as `unknown` and are narrowed
 * with type guards — never cast, never `any`.
 * Every non-2xx becomes an `ApiError` via `readApiError`, so callers branch on
 * `.status` / `.code` (409 / 429 / 503) instead of re-parsing three envelopes.
 */
import { ApiError, isRecord, readApiError } from './apiError'
import { authFetch } from './api'
import { toEntry, type MarketplaceEntry } from './marketplaceApi'

/** The lifecycle of a project's one app, as surfaced by `AppRegistryPanel` on the admin
 *  side. The citizen side no longer reads it: publishing is one chip reading one
 *  server-computed state, and this raw status is one of the parts it stopped recombining. */
export type AppStatus = 'draft' | 'pending' | 'approved' | 'rejected' | 'disabled'

/**
 * Which lineage the app's current submission entered the approve queue through, mirroring
 * the backend's `ApprovalRoute` enum (`db/models/app_registry.py`). `null` is real — never
 * submitted, or a row predating the publish flow. Lives here, beside `AppStatus`: both the
 * deploy and admin registry clients hand-mirror this enum independently, and agree only
 * until it grows a third value — what they do NOT share is handling an unrecognised one,
 * which each client's own narrower disagrees on deliberately.
 */
export type ApprovalRoute = 'runbook' | 'self_publish'

/** A project: the container that owns one app, its description, and its chats. */
export interface Project {
  id: string
  name: string
  description: string | null
  /** Read-only discovery of the project's one app (LEFT JOIN); `null` = no app yet. */
  appId: string | null
  appStatus: AppStatus | null
  /**
   * Does this project have a snapshot a Relaunch could actually restore?
   * `true` = yes, `false` = confirmed no, `null` = the server could not reach the object
   * store, so it declines to claim anything — the UI must not read `null` as either answer.
   * Only the single-project GET computes it; the list leaves it `null` and no caller reads it.
   */
  hasRelaunchableSnapshot: boolean | null
  /**
   * Whether the OWNER has ever saved a version — narrower than `hasRelaunchableSnapshot`,
   * which also counts an autosave/recovery copy (#198). The restricted shared workspace
   * restores ONLY from the saved bundle, never a recovery copy, so this is the one field it
   * can trust to decide whether Launch would actually work. `null` for an owner's own view
   * (irrelevant there) and for a shared view with no app at all; `false` means Launch should
   * be shown disabled, with the reason, rather than attempted into a known failure.
   */
  hasSavedSnapshot: boolean | null
  /**
   * Is the app SERVING right now? Not derivable from `appStatus`: APPROVED means an
   * administrator said yes, but one-click deploy never writes `status`, so a live app can
   * stay `draft`. Server-computed from deployment history; `false` for no app.
   * NAMED `isServing`, not the obvious retired predicate name (`jsx-deploy-retirement.test.ts`
   * guards its return) — this field is that predicate's opposite, the server's own answer.
   * (Even this note avoids spelling the old name.)
   */
  isServing: boolean
  createdAt: string
  updatedAt: string
  /**
   * Whether the caller owns this project or is viewing it because a colleague shared it with
   * them (#198). `'owner'` for every project fetched before this field existed — the historic
   * contract, and the only reading that keeps every existing owner-oriented screen unchanged
   * for a caller who is, in fact, the owner. `ProjectPage` reads this to route a shared
   * viewer to the restricted workspace instead of the full build/save/publish one.
   */
  access: 'owner' | 'shared'
}

/**
 * One NUMBERED page of projects, newest-first. Was `{items, nextCursor, hasMore}` — a
 * forward-only "Load more" — until numbered pages and a rows-per-page selector replaced
 * it; `Showing 1-8 of 12` and `Page 1 of 2` both need a `total`, which the keyset envelope
 * deliberately did not carry. `total` is counted AFTER the search is applied, so it
 * describes the rows it sits under.
 */
export interface ProjectsPage {
  items: Project[]
  page: number
  pageSize: number
  total: number
  totalPages: number
}

/**
 * The three numbers above the project list.
 *
 * A dedicated route rather than a count derived from the list: the page holds 8 of 12 rows,
 * so none of these is computable client-side, and polling the list for three integers would
 * pay for row projection and joins it does not need.
 */
export interface ProjectCounts {
  /** Apps SERVING right now — "live = deployed / published, with a url". Not `approved`. */
  inProduction: number
  totalApplications: number
  inPipeline: number
}

export interface ListProjectsArgs {
  /** 1-based. The server 422s outside 1..100000 rather than overflowing its OFFSET. */
  page?: number
  limit?: number
  q?: string
}

export interface CreateProjectArgs {
  name: string
  description?: string
}

/**
 * A project patch that distinguishes *absent* from *null*: omit `description`
 * to leave it unchanged, pass `null` to clear it. `name` can never be `null` —
 * the server 400s "name cannot be cleared", so it is unrepresentable here.
 */
export interface ProjectPatch {
  name?: string
  description?: string | null
}

export interface DeleteResult {
  ok: boolean
}

/**
 * The dep bundle `authFetch` accepts, injectable so tests need no real network.
 * Derived straight from `authFetch`'s own JSDoc typedef so it can never drift
 * from what that seam expects.
 */
export type AuthFetchDeps = NonNullable<Parameters<typeof authFetch>[2]>

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asStringOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function asAppStatus(value: unknown): AppStatus | null {
  return value === 'draft' ||
    value === 'pending' ||
    value === 'approved' ||
    value === 'rejected' ||
    value === 'disabled'
    ? value
    : null
}

/** Anything but the literal `'shared'` reads as `'owner'` — the historic contract for every
 *  server response that predates this field, and the fail-safe direction: a caller must
 *  never be shown fewer build/save/publish controls than they actually own. */
function asAccess(value: unknown): 'owner' | 'shared' {
  return value === 'shared' ? 'shared' : 'owner'
}

/**
 * Narrow one untrusted `ProjectResponse` into the typed `Project` shape.
 *
 * A project with no `id` is not a project — coercing it to `''` would hand the UI a card
 * that links to `/projects/` and a delete that targets nothing, so this fails at the
 * boundary instead. Other fields tolerate absence: a missing `description`/`appId` IS
 * null ("none yet"), and `appStatus` outside the known union is unknown, not fatal.
 */
function toProject(value: unknown): Project {
  if (!isRecord(value) || typeof value.id !== 'string' || value.id === '') {
    throw new ApiError('The server returned a project we could not read.', 500)
  }
  return {
    id: value.id,
    name: asString(value.name),
    description: asStringOrNull(value.description),
    appId: asStringOrNull(value.appId),
    appStatus: asAppStatus(value.appStatus),
    // Anything that is not a literal boolean — absent, null, or a shape we do not recognize —
    // is the "cannot say" answer. That is the fail-safe direction: it withholds the claim.
    hasRelaunchableSnapshot: typeof value.hasRelaunchableSnapshot === 'boolean' ? value.hasRelaunchableSnapshot : null,
    hasSavedSnapshot: typeof value.hasSavedSnapshot === 'boolean' ? value.hasSavedSnapshot : null,
    // Absent or non-boolean means NOT live: the badge claims something, so an unknown
    // must never render as a claim.
    isServing: value.isServing === true,
    createdAt: asString(value.createdAt),
    updatedAt: asString(value.updatedAt),
    access: asAccess(value.access),
  }
}

/** Narrow the `{items, page, pageSize, total, totalPages}` offset envelope. */
function toProjectsPage(value: unknown): ProjectsPage {
  const doc = isRecord(value) ? value : {}
  return {
    items: Array.isArray(doc.items) ? doc.items.map(toProject) : [],
    page: asPositiveInt(doc.page, 1),
    pageSize: asPositiveInt(doc.pageSize, DEFAULT_PAGE_SIZE),
    // Defaulting to 0 rather than to `items.length`: an absent total is unknown, and
    // guessing it from the page would render a confident "of 8" that is simply wrong.
    total: asPositiveInt(doc.total, 0),
    totalPages: asPositiveInt(doc.totalPages, 0),
  }
}

/** The server's page size when the caller does not choose one (`DEFAULT_PAGE_SIZE`). */
export const DEFAULT_PAGE_SIZE = 25

function asPositiveInt(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : fallback
}

const JSON_HEADERS = { 'Content-Type': 'application/json' }

/** One page of the caller's projects, newest-first. Only the args the caller passed hit the query string. */
export async function listProjects(args: ListProjectsArgs = {}, deps: AuthFetchDeps = {}): Promise<ProjectsPage> {
  const params = new URLSearchParams()
  if (args.page !== undefined) params.set('page', String(args.page))
  if (args.limit !== undefined) params.set('limit', String(args.limit))
  if (args.q) params.set('q', args.q)
  const qs = params.toString()
  const res = await authFetch(`/api/projects${qs ? `?${qs}` : ''}`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load projects')
  return toProjectsPage(await res.json())
}

/** The three summary numbers for the landing screen.
 *
 *  Each field falls back to 0 only when the wire value is not a number. That is a narrowing
 *  decision, not a guess at the data: the caller renders a skeleton while `counts` is null
 *  and never treats a fetch failure as "you have nothing".
 */
export async function listProjectCounts(deps: AuthFetchDeps = {}): Promise<ProjectCounts> {
  const res = await authFetch('/api/projects/counts', {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load project counts')
  const body: unknown = await res.json()
  const doc = isRecord(body) ? body : {}
  return {
    inProduction: asPositiveInt(doc.inProduction, 0),
    totalApplications: asPositiveInt(doc.totalApplications, 0),
    inPipeline: asPositiveInt(doc.inPipeline, 0),
  }
}

/** One project by id. */
export async function getProject(id: string, deps: AuthFetchDeps = {}): Promise<Project> {
  const res = await authFetch(`/api/projects/${encodeURIComponent(id)}`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load project')
  return toProject(await res.json())
}

/** Create a project. `description` is only sent when the caller supplied it. */
export async function createProject(args: CreateProjectArgs, deps: AuthFetchDeps = {}): Promise<Project> {
  const body: { name: string; description?: string } = { name: args.name }
  if (args.description !== undefined) body.description = args.description
  const res = await authFetch(
    '/api/projects',
    { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to create project')
  return toProject(await res.json())
}

/** What the citizen did once shown possible duplicates (#191 R39) — a closed set the
 *  server also validates (`ProjectDuplicateResolution`), so a typo here 422s rather than
 *  silently logging an event nothing downstream recognises. */
export type DuplicateCheckResolution = 'opened_existing' | 'created_anyway'

/**
 * Search the live marketplace for apps that look like `description`, BEFORE a project is
 * created (#191 R31) — called from `ProjectCreateModal`'s submit path, ahead of
 * `createProject` itself. At most three matches, already confidence-gated server-side
 * (R34); an empty array is the ordinary, day-one case, not a signal anything went wrong.
 */
export async function checkDuplicateProjects(
  description: string,
  deps: AuthFetchDeps = {},
): Promise<MarketplaceEntry[]> {
  const res = await authFetch(
    '/api/projects:check-duplicates',
    { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ description }) },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to check for duplicate apps')
  const body: unknown = await res.json()
  const doc = isRecord(body) ? body : {}
  // Same drop-unparseable-rows tolerance as `marketplaceApi.ts::toPage` — reusing its own
  // `toEntry` rather than a second, possibly-drifting parser for the identical wire shape.
  return Array.isArray(doc.matches)
    ? doc.matches.flatMap((row) => {
        const entry = toEntry(row)
        return entry === null ? [] : [entry]
      })
    : []
}

/**
 * Record what the citizen did once shown possible duplicates (#191 R39). The caller treats
 * this as fire-and-forget: a failure here is a lost analytics event, never a reason to
 * interrupt someone who is opening an existing app or creating their own anyway.
 */
export async function reportDuplicateCheckResolution(
  resolution: DuplicateCheckResolution,
  deps: AuthFetchDeps = {},
): Promise<void> {
  const res = await authFetch(
    '/api/projects:duplicate-check-resolved',
    { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ resolution }) },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to record duplicate-check resolution')
}

/**
 * Patch a project. Serializes ONLY the keys the caller actually passed (via `in`,
 * not truthiness) so `description: null` (clear) and `description: ''` (server maps
 * to null) both survive, while an omitted key leaves the field unchanged.
 */
export async function patchProject(id: string, patch: ProjectPatch, deps: AuthFetchDeps = {}): Promise<Project> {
  const body: ProjectPatch = {}
  if ('name' in patch) body.name = patch.name
  if ('description' in patch) body.description = patch.description
  const res = await authFetch(
    `/api/projects/${encodeURIComponent(id)}`,
    { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(body) },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to update project')
  return toProject(await res.json())
}

/**
 * Delete a project, with the reason the server requires; it cascades chats, app, database
 * and blobs, none of which comes back. THE REASON IS ALL THE CLIENT SENDS — WHO deleted it
 * is stamped server-side from the session, deliberately not a field here: a name this code
 * set could disagree with the account that acted, and an administrator reads that field to
 * answer exactly that. A body on a DELETE is unusual (RFC 9110 gives it no semantics) but
 * used here since a 50-word reason doesn't belong in a query string, and `fetch` sends it.
 */
export async function deleteProject(
  id: string,
  remark: string,
  deps: AuthFetchDeps = {},
): Promise<DeleteResult> {
  const res = await authFetch(
    `/api/projects/${encodeURIComponent(id)}`,
    { method: 'DELETE', headers: JSON_HEADERS, body: JSON.stringify({ remark }) },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to delete project')
  const data: unknown = await res.json().catch(() => null)
  return { ok: isRecord(data) && data.ok === true }
}
