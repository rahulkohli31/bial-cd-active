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
import { authFetch } from './api'
import type { AuthFetchDeps } from './api'
import { readApiError } from './apiError'
import { toPlanOptionsItem, toStepItem } from './turnStreamApi'
import type { ChatMessage, MessagePart } from './messageTypes'

/** The in-memory header shape pages expect, normalized from the server's raw doc.
 *
 * `kind` is the WHOLE of what a chat is now — `plan` or `build`, fixed when the chat is created.
 * The header used to carry a second field beside it, a per-thread setting the citizen could move
 * mid-conversation; there is no such field on the wire any more, and nothing here should look for
 * one. A chat that needs to do the other thing is a different chat. */
/**
 * The chat-row shape a project's rail renders — a deliberate narrowing of `ConversationHeader`,
 * not a second spelling of it. It lives HERE, beside the rows it is narrowed from, because its
 * owner is the API boundary that parses them: defining it on the component that renders it made
 * the page importing its own parsed shape back out of a leaf.
 */
export interface ChatSummary {
  id: string
  kind: string
  title: string
  updatedAt: string
}

export interface ConversationHeader {
  id: string
  kind: string
  projectId: string
  title: string
  createdAt: string
  updatedAt: string
  context?: unknown
}

/**
 * Server header doc → the in-memory header shape pages expect.
 *
 * `projectId` is load-bearing, not decoration: it is how `ChatRoute` resolves a
 * chat's breadcrumb and how a chat re-parents its writes after a cold open. Drop
 * it here and `conversation.projectId` is silently `undefined` everywhere.
 */
function normalizeHeader(doc: unknown): ConversationHeader | null {
  if (!doc) return null
  // UNCHECKED (matches pre-migration behavior): the server doc's shape is
  // asserted, not validated.
  const d = doc as {
    _id: string
    kind: string
    projectId: string
    title?: string
    createdAt: string
    updatedAt: string
    context?: unknown
  }
  return {
    id: d._id,
    kind: d.kind,
    projectId: d.projectId,
    title: d.title || '',
    createdAt: d.createdAt,
    updatedAt: d.updatedAt,
    ...(d.context !== undefined ? { context: d.context } : {}),
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
/** The raw projection item shapes read here — one union, discriminated on
 * `type` like everything else, but kept LOCAL (not exported) since this is the
 * server-projection wire shape, not the message-parts shape (`messageTypes.ts`)
 * it gets mapped into. UNCHECKED (matches pre-migration behavior): asserted per
 * `item.type`, not validated.
 *
 * THE SERVER SENDS ONE MORE KIND THAN THIS MAPS, and the fall-through is deliberate rather
 * than an oversight. `turn_terminal` is a durable record of HOW a turn ended, written so a
 * transcript rebuilt without the live stream can tell a finished turn from a running one; it
 * carries no prose and draws nothing, and what a surface makes of it — a group that stops
 * spinning, a control that stops being offered — is a design decision no unit has taken yet.
 * An unrecognised type pushes no message at all, so it costs an empty bubble rather than
 * rendering one. */
type RawProjectionItem = { type: string; seq: number } & Record<string, unknown>

/**
 * Projection types that are KNOWN and deliberately render nothing.
 *
 * The distinction this set draws is the whole point of the fallback arm below. `turn_terminal` is
 * a durable record of HOW a turn ended, carrying no prose and drawing no element — its silence is
 * a decision. An item type nobody has ever heard of is not a decision; it is a client that has
 * fallen behind its server, and the two must not look the same from here.
 */
const KNOWN_UNRENDERED = new Set(['turn_terminal'])

/**
 * What happens when the projection carries a type this client does not know.
 *
 * DELIBERATELY NOT A THROW, and deliberately not a rendered message either. A throw would take a
 * whole transcript down because the server shipped one new item kind ahead of the browser, which
 * is a routine deployment order. A rendered message would put platform-internal text in a
 * citizen's chat, which is exactly what R36 forbids. So the item is surfaced to DEVELOPERS, loudly,
 * and the transcript renders everything it does understand.
 */
export function reportUnknownProjectionItem(item: RawProjectionItem): void {
  console.error(
    `[conversationApi] Unknown projection item type "${item.type}" at seq ${item.seq} — dropped. ` +
      `A new server projection arm needs a matching arm here, or this content is invisible to ` +
      `every reader of a reloaded transcript.`,
    item,
  )
}

/**
 * @param onUnknown Injected so a test can assert the surfaced item rather than scrape the console.
 *   A parameter with a default rather than module state: every existing call site is unchanged and
 *   two tests running in parallel cannot see each other's handler.
 */
export function messagesFromProjection(
  projection: RawProjectionItem[] | undefined,
  onUnknown: (item: RawProjectionItem) => void = reportUnknownProjectionItem,
): ChatMessage[] {
  const messages: ChatMessage[] = []

  /**
   * THE ASSISTANT MESSAGE CURRENTLY BEING FILLED — one per reply, not one per item.
   *
   * A citizen asks once and is answered once, and a reply is prose and steps interleaved. The
   * LIVE path has always built it that way: `streamingParts` returns ONE ordered `MessagePart[]`
   * for the whole turn, and the library coalesces adjacent tool parts into an activity group,
   * so a paragraph written between two runs of steps seals one group and opens the next.
   *
   * This path used to push a SEPARATE message per projection item, so the same reply came back
   * from a reload as seven, or fourteen, assistant messages. Everything hung off a message then
   * multiplied with them — most visibly the copy control, which sits on each one: a real
   * transcript offered 41 copy buttons where it should offer 8, none of which copied the reply
   * a citizen had actually read, only the fragment beside it. Live and reload are supposed to
   * render identically (R72/AE43); they did not, and the reload half was wrong.
   *
   * Only `assistant_text` and `step` accumulate, which is exactly the set `streamingParts`
   * carries. Banners, offers and the in-progress marker stay their own messages on both paths.
   */
  let open: (ChatMessage & { parts: MessagePart[] }) | null = null

  /** End the current reply. The next assistant item starts a new one. */
  const seal = (): void => {
    open = null
  }

  /** Add a part to the reply in progress, or start a reply with it. */
  const addToReply = (part: MessagePart, id: string, seq: number): void => {
    if (open) {
      open.parts.push(part)
      return
    }
    open = { id, role: 'assistant', parts: [part], seq }
    messages.push(open)
  }
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
      seal()
      messages.push({
        id: `srv_${item.seq}_u_${index}`,
        role: 'user',
        parts: [{ type: 'text', text: item.text as string }],
        seq: item.seq,
      })
    } else if (item.type === 'assistant_text') {
      addToReply({ type: 'text', text: item.text as string }, `srv_${item.seq}_a_${index}`, item.seq)
    } else if (item.type === 'banner') {
      seal()
      // The builder outcome bubble: the stored sentence + the build part the page's
      // existing renderer reads (status derives from the banner kind).
      messages.push({
        id: `srv_${item.seq}_b_${index}`,
        role: 'assistant',
        parts: [
          { type: 'text', text: item.text as string },
          {
            type: 'build',
            sessionId: item.sessionId as string,
            status: item.banner === 'failed' ? 'failed' : 'ended',
            reason: item.banner as string,
            previewUrl: (item.previewUrl as string | null) ?? null,
          },
        ],
        seq: item.seq,
      })
    }
    else if (item.type === 'plan_options') {
      // The Build it / Keep refining card (U11/U13): carried as its own part so the surface
      // renders the offer with the STORED resolution state — live and reload agree.
      // Narrowed via toPlanOptionsItem — the same function the live path uses
      // (turnStreamApi.ts) — rather than a raw `as unknown as PlanOptionsItem` cast, so
      // a malformed stored item is DROPPED here exactly as it is live, not handed to the
      // renderer with garbage fields. The concrete drop case: a stored item with no
      // toolCallId (or an empty one) — toPlanOptionsItem returns null for it ("the card
      // IS its tool-call id... drop the frame rather than render a dead button"), and no
      // message is pushed for it, same as the live path's `if (item === null) return null`.
      const planItem = toPlanOptionsItem(item)
      if (planItem !== null) {
        seal()
        messages.push({
          id: `srv_${item.seq}_p_${index}`,
          role: 'assistant',
          parts: [{ type: 'plan_options', item: planItem }],
          seq: item.seq,
        })
      }
    } else if (item.type === 'step') {
      // A stored friendly agent step (U6/U15) — the reload half of the build narrative.
      // Hidden steps stay out of the transcript, same rule as the live feed — checked on the
      // RAW item.hidden, which is a structural filter (reload drops hidden steps before they
      // become a message at all; live forwards them and the surface filters them out of the
      // parts it paints).
      //
      // WHAT `hidden` MEANS IS NARROWER THAN IT WAS. It used to mark reads as well, which is
      // why a build's activity opened on a write with no account of what the agent had looked
      // at to get there. Reads are drawn now; the flag is down to plumbing — a write to a
      // configuration file, a housekeeping shell command — and never covers a step that
      // FAILED, whatever class it belongs to. The server decides all of that; this filter is
      // unchanged and deliberately holds no opinion of its own.
      if (!item.hidden) {
        // Narrowed via toStepItem — the same function the live path uses — rather than a
        // raw cast. Only returns null if the value isn't a record at all, which can't
        // happen here (RawProjectionItem already is one); kept for parity with the live
        // path's guard (turnStreamApi.ts: "a step frame IS its item; without one there is
        // nothing to show"), not because this branch can realistically trigger it today.
        const stepItem = toStepItem(item)
        if (stepItem !== null) {
          addToReply({ type: 'step', step: stepItem }, `srv_${item.seq}_s_${index}`, item.seq)
        }
      }
    } else if (item.type === 'build_in_progress') {
      seal()
      // A build began and no outcome closed it — the page reattaches to the live session
      // when there is one, and states the durable truth when there is not (U15).
      messages.push({
        id: `srv_${item.seq}_g_${index}`,
        role: 'assistant',
        parts: [{ type: 'build_in_progress', sessionId: item.sessionId as string }],
        seq: item.seq,
      })
    } else if (!KNOWN_UNRENDERED.has(item.type)) {
      // THE LOUD FALLBACK ARM (L4). Until this existed the chain simply ended, so an item type
      // this client did not recognise vanished with no error, no warning and no trace — on the
      // one path a reloaded transcript is rebuilt from. That is the four-edit change no compiler
      // enforces, on the path this plan makes load-bearing for BOTH kinds of chat.
      //
      // A known-and-deliberately-silent type (`turn_terminal`) takes neither branch and stays
      // silent, because "we decided this draws nothing" and "we have never heard of this" are
      // different facts and only the second is a bug.
      onUnknown(item)
    }
  }
  return messages
}

/** List the caller's conversation headers of `kind`, newest-first. */
export async function listConversations(kind?: string, deps: AuthFetchDeps = {}): Promise<(ConversationHeader | null)[]> {
  const qs = kind ? `?kind=${encodeURIComponent(kind)}` : ''
  const res = await authFetch(`/api/conversations${qs}`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load conversations')
  // UNCHECKED (matches pre-migration behavior): the shape is asserted, not validated.
  const data = (await res.json()) as { conversations?: unknown[] }
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
export async function listProjectConversations(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<(ConversationHeader | null)[]> {
  const res = await authFetch(`/api/conversations?projectId=${encodeURIComponent(projectId)}`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load conversations')
  // UNCHECKED (matches pre-migration behavior): the shape is asserted, not validated.
  const data = (await res.json()) as { conversations?: unknown[] }
  return (data.conversations || []).map(normalizeHeader)
}

/**
 * Header + display projection for one conversation; null if not found (404).
 * U7: `messages` is derived from the server-side projection (one read rebuilds the
 * chat — R8). `activeTurn` is `{turnId, lastSeq}` while a turn is running server-side and
 * null otherwise — `ConversationSurface` re-subscribes to it on adopt, which is the other half
 * of R8: a reload mid-reply keeps streaming instead of freezing.
 */
export interface ActiveTurn {
  turnId: string
  lastSeq: number
}

export type ConversationWithMessages = ConversationHeader & {
  messages: ChatMessage[]
  activeTurn: ActiveTurn | null
}

export async function getConversation(id: string, deps: AuthFetchDeps = {}): Promise<ConversationWithMessages | null> {
  const res = await authFetch(`/api/conversations/${encodeURIComponent(id)}`, {}, deps)
  if (res.status === 404) return null
  if (!res.ok) throw await readApiError(res, 'Failed to load conversation')
  // UNCHECKED (matches pre-migration behavior): the shape is asserted, not validated.
  const data = (await res.json()) as { conversation: unknown; projection?: RawProjectionItem[]; activeTurn?: ActiveTurn | null }
  return {
    ...(normalizeHeader(data.conversation) as ConversationHeader),
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
export interface CreateConversationArgs {
  projectId: string
  /** REQUIRED, and the server has no default: what a chat is has to be decided by whoever opens
   *  it, because it can never be changed afterwards. */
  kind: string
  title?: string
  context?: unknown
}

export async function createConversation(
  id: string,
  { projectId, kind, title, context }: CreateConversationArgs,
  deps: AuthFetchDeps = {},
): Promise<ConversationHeader | null> {
  const body: { id: string; projectId: string; kind: string; title?: string; context?: unknown } = {
    id,
    projectId,
    kind,
  }
  if (title !== undefined) body.title = title
  if (context !== undefined) body.context = context
  const res = await authFetch(
    '/api/conversations',
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to create the conversation')
  // UNCHECKED (matches pre-migration behavior): the shape is asserted, not validated.
  const data = (await res.json()) as { conversation: unknown }
  return normalizeHeader(data.conversation)
}

/** Patch a header: any of `{ title, context, code }`. `code` is the builder snapshot.
 * Return typed `unknown` — no real caller reads the resolved value (verified: only
 * this file's own test calls patchConversation; it isn't wired into any live page). */
export async function patchConversation(id: string, patch: Record<string, unknown>, deps: AuthFetchDeps = {}): Promise<unknown> {
  const res = await authFetch(
    `/api/conversations/${encodeURIComponent(id)}`,
    { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to update conversation')
  return res.json()
}

/* THE DELETE WRAPPER IS GONE (plan 002, U3). Its one caller was the project rail's list of past
   conversations, and the ruling of 2026-09-02 removed the list: nothing points back to a chat,
   running or finished, so nothing offers to delete one either. The SERVER route is untouched —
   this is a client with no caller, not a capability the backend has lost — and chats, their plans
   and their uploaded files all stay. Cleanup, if the client ever asks for it, is a scheduled job.
   Recorded here rather than removed silently, because the next person reaching for a delete needs
   to know it was a decision. */

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
/** A canonical lowercase UUIDv7 (8-4-4-4-12). */
export function uuidv7(): string {
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
export function deriveTitle(text: string): string {
  const t = (text || '').trim()
  return t.slice(0, 40) + (t.length > 40 ? '…' : '')
}

/**
 * Build an async store for one conversation `kind` (plan | build),
 * preserving the names the pages import from chatHistory/builderHistory.
 * `newConversation` stays SYNCHRONOUS — it mints a UUID with no network; U7 moves
 * row creation to an explicit `createConversation` call the send path makes BEFORE
 * the first turn (the legacy appears-on-first-append upsert died with the message
 * API — the server persists turns itself now).
 */
export interface ConversationStore {
  loadHistory: (deps?: AuthFetchDeps) => Promise<(ConversationHeader | null)[]>
  newConversation: () => string
  getConversation: (id: string, deps?: AuthFetchDeps) => Promise<ConversationWithMessages | null>
  createConversation: (
    id: string,
    header?: Partial<Omit<CreateConversationArgs, 'kind'>>,
    deps?: AuthFetchDeps,
  ) => Promise<ConversationHeader | null>
}

export function createConversationStore(kind: string): ConversationStore {
  return {
    loadHistory: (deps) => listConversations(kind, deps),
    newConversation: () => newId(),
    getConversation: (id, deps) => getConversation(id, deps),
    // UNCHECKED (matches pre-migration behavior): `header` is asserted to carry
    // whatever CreateConversationArgs still needs (projectId) once merged with `kind`.
    createConversation: (id, header = {}, deps) => createConversation(id, { kind, ...header } as CreateConversationArgs, deps),
  }
}
