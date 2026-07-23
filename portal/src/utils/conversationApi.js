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
    mode: doc.mode, // the server-owned sticky chat mode (U4)
    title: doc.title || '',
    createdAt: doc.createdAt,
    updatedAt: doc.updatedAt,
    ...(doc.context !== undefined ? { context: doc.context } : {}),
  }
}

/**
 * Server projection items → the in-memory message shape the pages render
 * ({id, role, parts, seq}). U7: the reload read returns DISPLAY ITEMS derived
 * server-side from the native transcript, not raw message docs.
 *
 * Rendered today: user/assistant text bubbles and build banners (text + the
 * `type:'build'` part the builder page already knows how to draw). Step,
 * build-in-progress, and plan-options items are skipped here.
 * TODO(U15): render friendly step items + in-progress anchors in the chat.
 */
export function messagesFromProjection(projection) {
  const messages = []
  for (const item of projection || []) {
    if (item.type === 'user_text') {
      messages.push({
        id: `srv_${item.seq}_u`,
        role: 'user',
        parts: [{ type: 'text', text: item.text }],
        seq: item.seq,
      })
    } else if (item.type === 'assistant_text') {
      messages.push({
        id: `srv_${item.seq}_a`,
        role: 'assistant',
        parts: [{ type: 'text', text: item.text }],
        seq: item.seq,
      })
    } else if (item.type === 'banner') {
      // The builder outcome bubble: the stored sentence + the build part the page's
      // existing renderer reads (status derives from the banner kind).
      messages.push({
        id: `srv_${item.seq}_b`,
        role: 'assistant',
        parts: [
          { type: 'text', text: item.text },
          {
            type: 'build',
            sessionId: item.sessionId,
            status: item.banner === 'failed' ? 'failed' : 'ended',
            reason: item.banner,
            previewUrl: item.previewUrl ?? null,
          },
        ],
        seq: item.seq,
      })
    }
    else if (item.type === 'plan_options') {
      // The Build it / Keep refining card (U11/U13): carried as its own part so the page
      // renders PlanOptionsCard with the STORED resolution state — live and reload agree.
      messages.push({
        id: `srv_${item.seq}_p`,
        role: 'assistant',
        parts: [{ type: 'plan_options', item }],
        seq: item.seq,
      })
    } else if (item.type === 'step') {
      // A stored friendly agent step (U6/U15) — the reload half of the build narrative.
      // Hidden steps (reads) stay out of the transcript, same rule as the live feed.
      if (!item.hidden) {
        messages.push({
          id: `srv_${item.seq}_s`,
          role: 'assistant',
          parts: [{ type: 'step', step: item }],
          seq: item.seq,
        })
      }
    } else if (item.type === 'build_in_progress') {
      // A build began and no outcome closed it — the page reattaches to the live session
      // when there is one, and states the durable truth when there is not (U15).
      messages.push({
        id: `srv_${item.seq}_g`,
        role: 'assistant',
        parts: [{ type: 'build_in_progress', sessionId: item.sessionId }],
        seq: item.seq,
      })
    }
  }
  return messages
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

/**
 * Header + display projection for one conversation; null if not found (404).
 * U7: `messages` is derived from the server-side projection (one read rebuilds the
 * chat — R8); `activeTurn` is the U10 seam (always null until the turn engine lands).
 */
export async function getConversation(id, deps = {}) {
  const res = await authFetch(`/api/conversations/${encodeURIComponent(id)}`, {}, deps)
  if (res.status === 404) return null
  if (!res.ok) throw await readApiError(res, 'Failed to load conversation')
  const data = await res.json()
  return {
    ...normalizeHeader(data.conversation),
    messages: messagesFromProjection(data.projection),
    activeTurn: data.activeTurn ?? null,
  }
}

/**
 * Create the conversation row BEFORE its first turn (U7). The id is still client-minted
 * (`newConversation()`); the server 404s a chat turn whose conversation does not exist, so
 * every send path creates-or-confirms first. Idempotent per owner: a re-POST of the same
 * mint answers 200 with the existing header.
 */
export async function createConversation(id, { projectId, kind, title, context, mode }, deps = {}) {
  const body = { id, projectId, kind }
  if (title !== undefined) body.title = title
  if (context !== undefined) body.context = context
  if (mode !== undefined) body.mode = mode // the starting chat mode (U13); server defaults 'plan'
  const res = await authFetch(
    '/api/conversations',
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to create the conversation')
  const data = await res.json()
  return normalizeHeader(data.conversation)
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

/** Derive a conversation title from its first message text (≤40 chars + ellipsis). */
export function deriveTitle(text) {
  const t = (text || '').trim()
  return t.slice(0, 40) + (t.length > 40 ? '…' : '')
}

/**
 * Build an async store for one conversation `kind` (planning | builder),
 * preserving the names the pages import from chatHistory/builderHistory.
 * `newConversation` stays SYNCHRONOUS — it mints a UUID with no network; U7 moves
 * row creation to an explicit `createConversation` call the send path makes BEFORE
 * the first turn (the legacy appears-on-first-append upsert died with the message
 * API — the server persists turns itself now).
 */
export function createConversationStore(kind) {
  return {
    loadHistory: (deps) => listConversations(kind, deps),
    newConversation: () => newId(),
    getConversation: (id, deps) => getConversation(id, deps),
    deleteConversation: (id, deps) => deleteConversation(id, deps),
    createConversation: (id, header = {}, deps) => createConversation(id, { kind, ...header }, deps),
  }
}
