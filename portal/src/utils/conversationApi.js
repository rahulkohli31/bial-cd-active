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
 * All six projection item types are rendered (U15 closed the last gaps):
 *   - `user_text` / `assistant_text` — plain chat bubbles;
 *   - `banner` — the build outcome (its stored sentence + the `type:'build'` part the
 *     builder page's existing renderer draws);
 *   - `step` — a stored friendly agent step, the reload half of the build narrative
 *     (hidden steps are skipped, the same rule the live feed applies);
 *   - `build_in_progress` — the durable anchor for a build with no recorded outcome;
 *   - `plan_options` — the Build it / Keep refining card, carried with its STORED
 *     resolution state so live and reload agree.
 */
export function messagesFromProjection(projection) {
  const messages = []
  // THE KEY CARRIES THE ITEM'S POSITION, not just its seq (N3). One `messages` row can project
  // SEVERAL items — an assistant turn with two text parts, a row that yields both a step and a
  // banner — and every one of them inherits that row's seq. Keyed `srv_{seq}_{kind}` alone,
  // those collide, and React is explicit that duplicate keys "may cause children to be
  // duplicated and/or omitted": this is latent message-list corruption, not a console warning.
  //
  // The SOURCE index is the ordinal, not the output index: hidden steps are skipped below, so an
  // output-derived ordinal would shift every later key the moment a step's `hidden` flipped.
  // The transcript is append-only and ordered by seq, so a source index is stable across
  // re-renders and across refetches that append.
  for (const [index, item] of (projection || []).entries()) {
    if (item.type === 'user_text') {
      messages.push({
        id: `srv_${item.seq}_u_${index}`,
        role: 'user',
        parts: [{ type: 'text', text: item.text }],
        seq: item.seq,
      })
    } else if (item.type === 'assistant_text') {
      messages.push({
        id: `srv_${item.seq}_a_${index}`,
        role: 'assistant',
        parts: [{ type: 'text', text: item.text }],
        seq: item.seq,
      })
    } else if (item.type === 'banner') {
      // The builder outcome bubble: the stored sentence + the build part the page's
      // existing renderer reads (status derives from the banner kind).
      messages.push({
        id: `srv_${item.seq}_b_${index}`,
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
        id: `srv_${item.seq}_p_${index}`,
        role: 'assistant',
        parts: [{ type: 'plan_options', item }],
        seq: item.seq,
      })
    } else if (item.type === 'step') {
      // A stored friendly agent step (U6/U15) — the reload half of the build narrative.
      // Hidden steps (reads) stay out of the transcript, same rule as the live feed.
      if (!item.hidden) {
        messages.push({
          id: `srv_${item.seq}_s_${index}`,
          role: 'assistant',
          parts: [{ type: 'step', step: item }],
          seq: item.seq,
        })
      }
    } else if (item.type === 'build_in_progress') {
      // A build began and no outcome closed it — the page reattaches to the live session
      // when there is one, and states the durable truth when there is not (U15).
      messages.push({
        id: `srv_${item.seq}_g_${index}`,
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
 * chat — R8). `activeTurn` is `{turnId, lastSeq}` while a turn is running server-side and
 * null otherwise — BuilderPage re-subscribes to it on adopt, which is the other half of
 * R8: a reload mid-reply keeps streaming instead of freezing.
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

// Client-minted ids + timestamps (Decision 3): ids are no longer guessable `chat_<timestamp>`.
//
// UUIDv7, NOT `crypto.randomUUID()` — that mints a v4, and ADR-0006 mandates v7 for DB primary
// keys. The id minted here IS the primary key: the create route builds `Conversation(id=body.id,
// …)`, which OVERRIDES the server's own UUIDv7 column default, so a v4 here is a v4 in Postgres.
// A random v4 lands at an arbitrary point in the btree and splits pages; a v7 sorts by mint time,
// so inserts stay at the index's right edge and keep their locality.
//
// Forward-only: rows already carrying a v4 are still valid ids and keep loading untouched.
//
// No npm uuid dependency — 16 random bytes from `crypto.getRandomValues` with the first six
// overwritten by the 48-bit BIG-ENDIAN Unix-ms timestamp. Big-endian is the entire trick:
// most-significant byte first is what makes the canonical string sort chronologically. Emit the
// bytes little-endian and every version-nibble assertion still passes while the sortability —
// the only reason v7 exists — is silently gone.
/** @returns {string} a canonical lowercase UUIDv7 (8-4-4-4-12). */
export function uuidv7() {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  const ms = Date.now()
  // bytes[0..5] = ms as 48 bits, most significant first.
  for (let i = 0; i < 6; i += 1) bytes[i] = Math.floor(ms / 2 ** (8 * (5 - i))) & 0xff
  bytes[6] = (bytes[6] & 0x0f) | 0x70 // version nibble → 7
  bytes[8] = (bytes[8] & 0x3f) | 0x80 // RFC-4122 variant → 10xx (renders as 8 | 9 | a | b)
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

// The store's mint IS the shared mint — one implementation, nothing to drift.
const newId = uuidv7

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
