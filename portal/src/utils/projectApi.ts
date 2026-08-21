/**
 * Typed client for the projects domain (`/api/projects*`), mirroring the
 * deps-injection shape of `conversationApi.js`: every call is
 * `fn(args, deps = {})` and forwards `deps` to `authFetch` (Bearer + one
 * 401-refresh retry, cookie session). The edge rewrites `/api/*` to `/v1/*`.
 *
 * Wire format is camelCase (the backend serializes `by_alias=True`). Response
 * bodies are untrusted network input: they arrive as `unknown` and are narrowed
 * with type guards — never cast, never `any` (`.claude/rules/fail-first-typescript.md`).
 * Every non-2xx becomes an `ApiError` via `readApiError`, so callers branch on
 * `.status` / `.code` (409 / 429 / 503) instead of re-parsing three envelopes.
 */
import { ApiError, isRecord, readApiError } from './apiError'
import { authFetch } from './api'

/** The lifecycle of a project's one app, as surfaced by `AppRegistryPanel`/`SubmitControl`. */
export type AppStatus = 'draft' | 'pending' | 'approved' | 'rejected' | 'disabled'

/**
 * Which lineage the app's current submission entered the approve queue through, mirroring
 * the backend's `ApprovalRoute` enum (`db/models/app_registry.py`). `null` is a real value
 * — never submitted, or a row that predates the publish flow.
 *
 * It lives HERE, beside `AppStatus`, because it is the same kind of thing: registry
 * vocabulary two clients read and neither owns. The citizen's deploy client and the
 * admin's registry client each need it, they were written independently and each declared
 * its own copy, and two hand-maintained mirrors of one server enum only agree until the
 * server grows a third value. What they do NOT share is what to do with an unrecognised
 * one — see each client's own narrower, which disagree on purpose.
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
   * Does this project have a snapshot a Relaunch could actually restore (N7)?
   * `true` = yes, `false` = confirmed no, `null` = the server could not reach the object
   * store, so it declines to claim anything — the UI must not read `null` as either answer.
   * Only the single-project GET computes it; the list leaves it `null` and no caller reads it.
   */
  hasRelaunchableSnapshot: boolean | null
  createdAt: string
  updatedAt: string
}

/** One keyset page of projects, newest-first. */
export interface ProjectsPage {
  items: Project[]
  nextCursor: string | null
  hasMore: boolean
}

export interface ListProjectsArgs {
  cursor?: string | null
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

/**
 * Narrow one untrusted `ProjectResponse` into the typed `Project` shape.
 *
 * A project with no `id` is not a project — coercing it to `''` would hand the UI
 * a card that links to `/projects/` and a delete that targets nothing. Fail here,
 * at the boundary, rather than let an empty string corrupt state downstream
 * (`.claude/rules/fail-first.md`). The other fields tolerate absence because each
 * has a defined meaning: a missing `description`/`appId` IS null ("none yet"), and
 * `appStatus` outside the known union is unknown, not fatal.
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
    createdAt: asString(value.createdAt),
    updatedAt: asString(value.updatedAt),
  }
}

/** Narrow the `{items, nextCursor, hasMore}` keyset envelope. */
function toProjectsPage(value: unknown): ProjectsPage {
  const doc = isRecord(value) ? value : {}
  return {
    items: Array.isArray(doc.items) ? doc.items.map(toProject) : [],
    nextCursor: asStringOrNull(doc.nextCursor),
    hasMore: doc.hasMore === true,
  }
}

const JSON_HEADERS = { 'Content-Type': 'application/json' }

/** One page of the caller's projects, newest-first. Only the args the caller passed hit the query string. */
export async function listProjects(args: ListProjectsArgs = {}, deps: AuthFetchDeps = {}): Promise<ProjectsPage> {
  const params = new URLSearchParams()
  if (args.cursor) params.set('cursor', args.cursor)
  if (args.limit !== undefined) params.set('limit', String(args.limit))
  if (args.q) params.set('q', args.q)
  const qs = params.toString()
  const res = await authFetch(`/api/projects${qs ? `?${qs}` : ''}`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load projects')
  return toProjectsPage(await res.json())
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

/** Delete a project; the server cascades its chats, app, and blobs. */
export async function deleteProject(id: string, deps: AuthFetchDeps = {}): Promise<DeleteResult> {
  const res = await authFetch(`/api/projects/${encodeURIComponent(id)}`, { method: 'DELETE' }, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to delete project')
  const data: unknown = await res.json().catch(() => null)
  return { ok: isRecord(data) && data.ok === true }
}

/**
 * Regenerate the project's stored description from its app code (empty body).
 * Distinct failures the caller branches on: 409 (no code yet), 429 (daily limit,
 * `code === 'daily_token_limit_exceeded'`), 500 (generation failed), 503 (not configured).
 */
export async function generateDescription(id: string, deps: AuthFetchDeps = {}): Promise<Project> {
  const res = await authFetch(
    `/api/projects/${encodeURIComponent(id)}/description:generate`,
    { method: 'POST' },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to generate description')
  return toProject(await res.json())
}
