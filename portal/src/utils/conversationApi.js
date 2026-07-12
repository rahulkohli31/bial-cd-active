/**
 * Conversation/message data access over the server persistence routes (replaces
 * the retired localStorage stores). Server is the source of truth; identity comes
 * from the JWT, so these calls carry no username — the server scopes by token.
 *
 * Thin wrappers over /api/conversations via authFetch (Bearer + one 401-refresh
 * retry). Deps are injectable for tests (mirrors utils/admin.js). The neutral
 * `parts[]` message model is used throughout; `id` is normalized from the
 * server's `_id` so pages keep using `.id`.
 */
import { authFetch } from './api.js'
import { readApiError } from './apiError'

/**
 * Server header doc → the in-memory header shape pages expect.
 *
 * `projectId` is load-bearing, not decoration: it is how `ChatRoute` resolves a
 * chat's breadcrumb and how a chat re-parents its writes after a cold open. Drop
 * it here and `conversation.projectId` is silently `undefined` everywhere.
 */
function normalizeHeader(doc) {
  if (!doc) return null
  return {
    id: doc._id,
    kind: doc.kind,
    projectId: doc.projectId,
    title: doc.title || '',
    createdAt: doc.createdAt,
    updatedAt: doc.updatedAt,
    ...(doc.context !== undefined ? { context: doc.context } : {}),
    ...(doc.code !== undefined ? { code: doc.code } : {}),
  }
}

/** Server message doc → the in-memory message shape ({id, role, parts, seq}). */
function normalizeMessage(doc) {
  return { id: doc._id, role: doc.role, parts: doc.parts || [], seq: doc.seq, createdAt: doc.createdAt }
}

/** List the caller's conversation headers of `kind`, newest-first. */
export async function listConversations(kind, deps = {}) {
  const qs = kind ? `?kind=${encodeURIComponent(kind)}` : ''
  const res = await authFetch(`/api/conversations${qs}`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load conversations')
  const data = await res.json()
  return (data.conversations || []).map(normalizeHeader)
}

/**
 * The server's newest-first row cap for `GET /v1/conversations` (`_LIST_LIMIT`, no cursor).
 * A response of exactly this length means "AT LEAST this many", never "exactly this many" —
 * so anything that COUNTS these rows must not quote the cap as a total.
 */
export const CONVERSATION_LIST_CAP = 200

/**
 * Every conversation filed under one project, BOTH kinds, newest-first — what the
 * project home lists and what the delete dialog counts before naming the cascade.
 *
 * Deliberately NOT keyset-paginated, unlike `/api/projects`: this route caps at
 * CONVERSATION_LIST_CAP and offers no cursor. That is fine at pilot scale and is a
 * documented divergence, not an oversight — revisit when a project exceeds the cap.
 */
export async function listProjectConversations(projectId, deps = {}) {
  const res = await authFetch(`/api/conversations?projectId=${encodeURIComponent(projectId)}`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load conversations')
  const data = await res.json()
  return (data.conversations || []).map(normalizeHeader)
}

/** Header + ordered messages for one conversation; null if not found (404). */
export async function getConversation(id, deps = {}) {
  const res = await authFetch(`/api/conversations/${encodeURIComponent(id)}`, {}, deps)
  if (res.status === 404) return null
  if (!res.ok) throw await readApiError(res, 'Failed to load conversation')
  const data = await res.json()
  return { ...normalizeHeader(data.conversation), messages: (data.messages || []).map(normalizeMessage) }
}

/**
 * Persist one message AND upsert the header in a single call (so an assistant
 * turn never references a header-less conversation). `message` is
 * `{ _id, role, parts, seq, createdAt, schemaVersion }`; `header` is
 * `{ kind, projectId, title?, context?, createdAt? }` (owner is taken from the token).
 *
 * `header.projectId` is REQUIRED on the server's CREATE branch — absent → 400,
 * invalid → 400, not-owned → 404. The upsert branch never re-parents, so passing it
 * on later turns is harmless; callers pass it always and let the server ignore it.
 */
export async function appendMessage(id, message, header, deps = {}) {
  const res = await authFetch(
    `/api/conversations/${encodeURIComponent(id)}/messages`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message, header }) },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to save message')
  return res.json()
}

/** Patch a header: any of `{ title, context, code }`. `code` is the builder snapshot. */
export async function patchConversation(id, patch, deps = {}) {
  const res = await authFetch(
    `/api/conversations/${encodeURIComponent(id)}`,
    { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to update conversation')
  return res.json()
}

/** Delete a conversation (header + messages + its attachment objects, server-side). */
export async function deleteConversation(id, deps = {}) {
  const res = await authFetch(`/api/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }, deps)
  if (!res.ok && res.status !== 404) throw await readApiError(res, 'Failed to delete conversation')
  return true
}

// Client-minted ids + timestamps (Decision 3). crypto.randomUUID is available in
// modern browsers and jsdom; ids are no longer guessable `chat_<timestamp>`.
const newId = () => crypto.randomUUID()
const nowIso = () => new Date().toISOString()

/** Derive a conversation title from its first message text (≤40 chars + ellipsis). */
export function deriveTitle(text) {
  const t = (text || '').trim()
  return t.slice(0, 40) + (t.length > 40 ? '…' : '')
}

/**
 * Build an async store for one conversation `kind` (planning | builder),
 * preserving the names the pages import from chatHistory/builderHistory.
 * `newConversation` stays SYNCHRONOUS — it mints a UUID with no network; the
 * header is created server-side on the first `appendMessage` (idempotent upsert),
 * so the synchronous `navigate(/…/id)` send path is unchanged. `appendMessage`
 * mints the message `_id` + timestamp and forwards the page-supplied `seq`
 * (transcript index) and header patch.
 */
export function createConversationStore(kind) {
  return {
    loadHistory: (deps) => listConversations(kind, deps),
    newConversation: () => newId(),
    getConversation: (id, deps) => getConversation(id, deps),
    deleteConversation: (id, deps) => deleteConversation(id, deps),
    appendMessage: (id, message, header = {}, deps) =>
      appendMessage(
        id,
        { _id: newId(), role: message.role, parts: message.parts, seq: message.seq, schemaVersion: 1, createdAt: nowIso() },
        { kind, ...header },
        deps,
      ),
  }
}
