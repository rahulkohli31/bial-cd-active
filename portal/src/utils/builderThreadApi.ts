/**
 * The canonical build thread of a project (003-U1).
 *
 * A project has ONE build conversation, and this resolves it — creating it on first open. It
 * exists as a server round-trip rather than a client-side "list and pick the newest" because
 * picking client-side races: two tabs opening a fresh project would each mint a chat, and under
 * newest-wins the loser's empty row would silently become the project's thread. The server
 * serializes the get-or-create behind an advisory lock.
 *
 * Mutating (it may create the row), so it carries the signed double-submit CSRF token — the same
 * gate the build-session POSTs use (`buildSessionApi.ts`). Typed-client house style of
 * `projectApi.ts`: `fn(args, deps = {})`, responses narrowed at the boundary, never cast.
 */
import { ApiError, isRecord, readApiError } from './apiError'
import { authFetch } from './api.js'
import { getCsrfToken } from './auth.js'

export type AuthFetchDeps = NonNullable<Parameters<typeof authFetch>[2]>

/** The project's canonical builder conversation header. */
export interface BuilderThread {
  id: string
  projectId: string
  kind: string
  title: string
  createdAt: string
  updatedAt: string
}

/**
 * A thread with no id is not a thread — fail at the boundary rather than let the page navigate
 * to `/chat/undefined` (mirrors `projectApi.toProject` / `buildSessionApi.requireSessionId`).
 */
function toBuilderThread(value: unknown): BuilderThread {
  if (!isRecord(value)) throw new ApiError('The server returned a thread we could not read.', 500)
  const conv = value.conversation
  if (!isRecord(conv) || typeof conv._id !== 'string' || conv._id === '') {
    throw new ApiError('The server returned a thread we could not read.', 500)
  }
  if (typeof conv.projectId !== 'string' || conv.projectId === '') {
    throw new ApiError('The server returned a thread we could not read.', 500)
  }
  return {
    // The wire keeps the Mongo-style `_id`; pages use `.id` (the `conversationApi` convention).
    id: conv._id,
    projectId: conv.projectId,
    kind: typeof conv.kind === 'string' ? conv.kind : '',
    title: typeof conv.title === 'string' ? conv.title : '',
    createdAt: typeof conv.createdAt === 'string' ? conv.createdAt : '',
    updatedAt: typeof conv.updatedAt === 'string' ? conv.updatedAt : '',
  }
}

/**
 * Resolve (or create) the project's ONE build thread.
 *
 * A foreign or missing project is a non-leaking 404 (`ApiError`), so callers branch on
 * `.status` rather than re-parsing the envelope.
 */
export async function resolveBuilderThread(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<BuilderThread> {
  const csrf = getCsrfToken()
  const res = await authFetch(
    '/api/conversations/builder-thread',
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
      },
      body: JSON.stringify({ projectId }),
    },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Could not open this project’s build chat')
  return toBuilderThread(await res.json())
}
