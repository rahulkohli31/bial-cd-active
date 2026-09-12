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
import { outcomeSummary } from './messageTypes'
import type { BuildOutcomeStatus, ChatMessage, MessagePart } from './messageTypes'

/** The in-memory header shape pages expect, normalized from the server's raw doc.
 *
 * `kind` is the WHOLE of what a chat is now — `plan` or `build`, fixed when the chat is created.
 * The header used to carry a second field beside it, a per-thread setting the citizen could move
 * mid-conversation; there is no such field on the wire any more, and nothing here should look for
 * one. A chat that needs to do the other thing is a different chat. */
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

/** The raw projection item shapes read here — one union discriminated on `type`, kept LOCAL
 * (not exported) since this is the server-projection wire shape, not the message-parts shape
 * (`messageTypes.ts`) it maps into. UNCHECKED (pre-migration behavior): asserted per
 * `item.type`, not validated.
 *
 * EVERY KIND THE SERVER SENDS HAS AN ARM NOW, and `turn_terminal`'s is the one that draws
 * conditionally rather than always. It is the durable record of HOW a turn ended, written so a
 * transcript rebuilt without the live stream can tell a finished turn from a running one; a
 * NON-COMPLETED one is now the reload half of the outcome sentence, and a completed one
 * still draws nothing at all — see the arm itself for why that asymmetry is the whole design and
 * not an omission. An unrecognised type pushes no message at all, so it costs an empty bubble
 * rather than rendering one. */
type RawProjectionItem = { type: string; seq: number } & Record<string, unknown>

/** One attachment on a projected `user_text` item, as the server sends it. */
interface ProjectedAttachment {
  attachmentId?: unknown
  kind?: unknown
  name?: unknown
  mediaType?: unknown
}

/**
 * The `file` parts for a projected user turn, rebuilt from what the server sent.
 *
 * VALIDATED RATHER THAN CAST. A projection item is `Record<string, unknown>`, so this is the
 * boundary where the shape stops being a promise: an entry with no id draws nothing, and a
 * missing `kind` falls back to `image`, which is the branch whose own fetch discovers the file
 * is gone and renders "attachment unavailable". That is the honest outcome for a reference
 * whose row was reclaimed with its conversation — the id outlives the row by design.
 */
function fileParts(item: RawProjectionItem): MessagePart[] {
  const raw = item.attachments
  if (!Array.isArray(raw)) return []
  const parts: MessagePart[] = []
  for (const entry of raw as ProjectedAttachment[]) {
    const attachmentId = typeof entry?.attachmentId === 'string' ? entry.attachmentId : ''
    if (!attachmentId) continue
    parts.push({
      type: 'file',
      kind: entry.kind === 'document' ? 'document' : 'image',
      attachmentId,
      name: typeof entry.name === 'string' ? entry.name : '',
      mediaType: typeof entry.mediaType === 'string' ? entry.mediaType : '',
    })
  }
  return parts
}

/**
 * What happens when the projection carries a type this client does not know.
 *
 * DELIBERATELY NOT A THROW, and deliberately not a rendered message either. A throw would take a
 * whole transcript down because the server shipped one new item kind ahead of the browser, which
 * is a routine deployment order. A rendered message would put platform-internal text in a
 * citizen's chat, which the split-audience rule forbids. So the item is surfaced to DEVELOPERS, loudly,
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
 * A stored banner kind → the build part's terminal.
 *
 * FOUR KINDS IN, THREE OUT, and the pairing is the point: `stopped` and `quota` both mean the
 * build ended without anything going wrong, so both land on `stopped` rather than on the
 * "everything finished" value. Anything unrecognised reads as `ended`, matching this module's
 * standing rule that a client behind its server degrades quietly rather than inventing a
 * failure — an unknown banner is a deployment order, not a broken build.
 *
 * TWO CALLERS, ONE VOCABULARY, and the name still says "banner" on purpose. `TurnTerminalItem`
 * does not own a second enum: its `terminal` field is `_banner_kind`'s output, by the server's
 * own design ("`terminal` REUSES `_banner_kind`'s vocabulary rather than inventing a parallel
 * one"). So the banner arm and the turn-terminal arm below read the same four words through
 * this one function — because the moment they each grow their own, a stop means one thing on a
 * legacy build session and another on a turn, for one fact.
 */
function bannerStatus(banner: unknown): BuildOutcomeStatus {
  if (banner === 'failed') return 'failed'
  if (banner === 'stopped' || banner === 'quota') return 'stopped'
  return 'ended'
}

/**
 * Server projection items → the in-memory message shape the pages render
 * ({id, role, parts, seq}). The reload read returns DISPLAY ITEMS derived
 * server-side from the native transcript, not raw message docs.
 *
 * Every projection item type is rendered, including the turn terminal's arm, which draws
 * conditionally rather than always:
 *   - `user_text` / `assistant_text` — plain chat bubbles;
 *   - `banner` — the build outcome (stored sentence + the `type:'build'` part the
 *     builder page's renderer draws);
 *   - `step` — a stored friendly agent step (hidden steps skipped, as the live feed does);
 *   - `build_in_progress` — the durable anchor for a build with no recorded outcome;
 *   - `plan_options` — the Build it / Keep refining card, carried with its STORED
 *     resolution state so live and reload agree;
 *   - `turn_terminal` — the durable record of HOW a turn ended, rendered as the outcome
 *     sentence when the ending needs explaining and silent when it does not (see its arm).
 *
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
   * A reply is prose and steps interleaved, matching the LIVE path: `streamingParts` returns
   * ONE ordered `MessagePart[]` per turn, coalescing adjacent tool parts into an activity group.
   *
   * This path used to push a SEPARATE message per item, so a reload turned one reply into
   * seven or fourteen assistant messages — 41 copy buttons where there should be 8, none
   * copying what the citizen actually read. Live and reload must render identically.
   *
   * Only `assistant_text` and `step` accumulate — exactly the set `streamingParts` carries.
   * Banners, offers and the in-progress marker stay their own messages on both paths.
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
  // THE KEY CARRIES THE ITEM'S POSITION, not just its seq. One `messages` row can project
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
        // FILES FIRST, THEN THE PROSE — the same order `buildUserParts` writes when the message
        // is first composed, so a reloaded turn renders identically to the one the citizen
        // watched send. Rebuilding these is the second half of the reload path: the server has always
        // sent the attachment identities and this path threw them away, so every chip vanished
        // on refresh for every format, and with it the citizen's only sight of the files still
        // riding on every turn.
        parts: [...fileParts(item), { type: 'text', text: item.text as string }],
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
            // THE RELOAD HALF OF THE BANNER-VOCABULARY FIX. The projection's banner vocabulary is
            // four-valued — `completed` / `failed` / `stopped` / `quota`, the four kinds
            // `projection.py::_banner_kind` returns — and this mapping used to throw two of those
            // away, landing `stopped` and `quota` on `ended`. That put a part claiming the build
            // ended normally directly beside the stored sentence "You stopped this build before it
            // finished." A quota stop is a stop for the same reason the live path treats it as
            // one: nothing broke, the day ran out.
            status: bannerStatus(item.banner),
            reason: item.banner as string,
            previewUrl: (item.previewUrl as string | null) ?? null,
          },
        ],
        seq: item.seq,
      })
    }
    else if (item.type === 'plan_options') {
      // The Build it / Keep refining card: carried as its own part so the surface
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
      // A stored friendly agent step — the reload half of the build narrative.
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
      // when there is one, and states the durable truth when there is not.
      messages.push({
        id: `srv_${item.seq}_g_${index}`,
        role: 'assistant',
        parts: [{ type: 'build_in_progress', sessionId: item.sessionId as string }],
        seq: item.seq,
      })
    } else if (item.type === 'turn_terminal') {
      // THE RELOAD HALF OF THE OUTCOME SENTENCE.
      //
      // A stopped turn used to say NOTHING after a refresh. Live, the surface draws a sentence
      // the moment the turn ends (`announceTerminal` → `showBuildOutcome`); this row is the only
      // durable record of that ending — `finish_turn_sandbox` deliberately writes no
      // build-outcome part for a turn, "so writing a build-outcome part as well would render the
      // same ending twice" — and it drew nothing at all. So a citizen who came back to a build
      // they had stopped found a transcript that simply trailed off, with no account of why.
      //
      // THE SENTENCE COMES FROM `outcomeSummary`, THE SAME FUNCTION THE LIVE PATH CALLS, and
      // that shared call is the entire point rather than a convenience. Two authors for one
      // sentence is a documented failure of this codebase — fixing one emitter only changed WHEN
      // the wrong text appeared. The server's own contract says the same thing from its end: the
      // terminal is "derived from the same mapping of the same stored meta".
      //
      // A COMPLETED TERMINAL STILL DRAWS NOTHING, and the narrowing stops exactly there. Every
      // turn writes one of these rows — `_write_turn_terminal` runs for BOTH kinds,
      // unconditionally — so rendering the completed ones would stamp "Build finished." after
      // every single exchange in every chat, including a Plan conversation that never built
      // anything. The endings worth a sentence are the ones a citizen cannot otherwise explain.
      // An unrecognised terminal is silent for the same reason an unknown banner reads as
      // `ended`: a client behind its server says less, never something invented.
      const terminal = item.terminal
      if (terminal === 'failed' || terminal === 'stopped' || terminal === 'quota') {
        seal()
        messages.push({
          id: `srv_${item.seq}_t_${index}`,
          role: 'assistant',
          parts: [
            {
              type: 'text',
              // THE REASON IS THE STORED TOKEN, not the banner kind — this is what the server
              // had been withholding until it started exposing `reason` beside `terminal`.
              // `outcomeSummary` reads it FIRST (see its docblock), so `quota_exceeded` on a
              // `failed` terminal renders "you reached your daily limit" here exactly as it
              // does live, instead of the generic failure the terminal alone would produce.
              text: outcomeSummary({
                status: bannerStatus(terminal),
                reason: typeof item.reason === 'string' ? item.reason : null,
              }),
            },
          ],
          seq: item.seq,
        })
      }
      // NO `build` PART BESIDE THE TEXT, unlike the banner arm above, and the omission is
      // deliberate. A `build` part draws no element of its own; what it does is claim "an app
      // was built in this chat" to the preview pane (`transcriptHasBuildOutcome`) and carry a
      // `snapshotCommitted` warning. A turn terminal knows neither — it has no preview URL and
      // no snapshot verdict — so synthesising one would answer the pane's question with a guess,
      // on every stopped Plan turn as well. The reload fix above supplies only the sentence, never
      // a build part.
    } else {
      // THE LOUD FALLBACK ARM. Until this existed the chain simply ended, so an item type
      // this client did not recognise vanished with no error, no warning and no trace — on the
      // one path a reloaded transcript is rebuilt from. That is the four-edit change no compiler
      // enforces, on the path that is load-bearing for BOTH kinds of chat.
      //
      // A KNOWN TYPE THAT DRAWS NOTHING NOW HAS ITS OWN ARM rather than a membership set: the
      // completed `turn_terminal` above falls out of that arm having rendered nothing, and never
      // reaches here. "We decided this draws nothing" and "we have never heard of this" are
      // different facts, and only the second is a bug — the arm the item lands in is what says
      // which, and a type with no arm at all is the definition of the second.
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
 * `messages` is derived from the server-side projection (one read rebuilds the
 * chat). `activeTurn` is `{turnId, lastSeq}` while a turn is running server-side and
 * null otherwise — `ConversationSurface` re-subscribes to it on adopt, which is the other half
 * of the same guarantee: a reload mid-reply keeps streaming instead of freezing.
 */
export interface ActiveTurn {
  turnId: string
  lastSeq: number
}

export type ConversationWithMessages = ConversationHeader & {
  messages: ChatMessage[]
  activeTurn: ActiveTurn | null
  /**
   * HOW FULL THIS CHAT IS — the provider's raw prompt count for the largest turn it has served
   * here, which is the very number `enforce_context_limit` refuses on. It rides the read a
   * reopened chat already makes, so the "getting long" line is right from first paint.
   *
   * `null` MEANS UNMEASURED, NOT EMPTY. A chat with no served turn has no measurement, and the
   * meter stays silent rather than assuming either: a guessed number reads as a measurement,
   * and the platform stopped deriving this one for exactly that reason.
   */
  contextTokens: number | null
}

export async function getConversation(id: string, deps: AuthFetchDeps = {}): Promise<ConversationWithMessages | null> {
  const res = await authFetch(`/api/conversations/${encodeURIComponent(id)}`, {}, deps)
  if (res.status === 404) return null
  if (!res.ok) throw await readApiError(res, 'Failed to load conversation')
  // UNCHECKED (matches pre-migration behavior): the shape is asserted, not validated.
  const data = (await res.json()) as { conversation: unknown; projection?: RawProjectionItem[]; activeTurn?: ActiveTurn | null; contextTokens?: number | null }
  return {
    ...(normalizeHeader(data.conversation) as ConversationHeader),
    messages: messagesFromProjection(data.projection),
    activeTurn: data.activeTurn ?? null,
    // NARROWED, not coerced: a server that has not learned this field yet, or one answering
    // `null` for an unmeasured chat, both mean "no measurement" — and neither may be read as
    // zero, which would be a claim that the chat is empty.
    contextTokens: typeof data.contextTokens === 'number' ? data.contextTokens : null,
  }
}

/* THE ROW-CREATE AND HEADER-PATCH WRAPPERS ARE GONE.
 *
 * A CHAT'S ROW IS NO LONGER CREATED BY A ROUND TRIP OF ITS OWN. `POST /conversations` used to
 * commit the row before the first turn went out, checking only project ownership — so a first
 * message the workspace then refused left a real, empty chat behind. The row's parentage now
 * rides the turn itself (`startTurn`'s `create` block, `turnStreamApi.ts`): written inside the
 * turn's own transaction, after every side-effect-free refusal, so a refusal rolls it back.
 * `PATCH /conversations/{id}` lost its client the same way — the header is written by the turn.
 *
 * NOTHING RENDERS A LIST OF CHATS, the other half: no recents rail, no chat list inside a chat.
 * So there is no row to summarise, date, or delete — the SERVER routes are untouched, these are
 * clients with no caller, not capabilities the backend lost. Cleanup, if ever needed, is a
 * scheduled job rather than a control. */

// Client-minted ids + timestamps (Decision 3): ids are no longer guessable `chat_<timestamp>`.
//
// UUIDv7, NOT `crypto.randomUUID()` (that mints a v4). The id minted here IS the primary key —
// the create route builds `Conversation(id=body.id, …)`, overriding the server's own UUIDv7
// column default — so a v4 here is a v4 in Postgres. A random v4 lands at an arbitrary point in
// the btree and splits pages; a v7 sorts by mint time, keeping index locality. Forward-only:
// rows already carrying a v4 stay valid.
//
// No npm uuid dependency — 16 random bytes from `crypto.getRandomValues` with the first six
// overwritten by the 48-bit BIG-ENDIAN Unix-ms timestamp. Big-endian is the whole trick: MSB
// first is what makes the string sort chronologically — little-endian would still pass every
// version-nibble assertion while silently losing the only reason v7 exists.
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
 * Create the conversation row, BEFORE its first file is uploaded and before its first turn.
 *
 * ★ THIS CLIENT CAME BACK ON PURPOSE, AND IT COSTS SOMETHING. It was retired when a chat's
 * parentage moved onto its first turn, and that move bought a real guarantee: a first message
 * refused by the workspace gate left NOTHING behind, because the row was flushed inside the turn's
 * own transaction and rolled back with it. An upload now names the conversation it belongs to, so
 * the row has to exist a round trip earlier than the send — and two orderings cannot both be true.
 * The trade is recorded rather than hidden: a refused first send leaves an empty chat.
 *
 * NO TITLE IS PASSED, deliberately. `deriveTitle` runs on the draft at SEND time, which is one
 * round trip later than this call, and stamping refused text into a row nobody can delete would
 * be worse than leaving it unnamed. The first message that actually lands titles the chat.
 *
 * IDEMPOTENT PER OWNER: re-posting the same id with the same parentage answers 200 with the
 * existing header, which is what makes a retry after a refused first send work — the leftover chat
 * must not refuse its own second attempt.
 */
export async function createConversation(
  { id, projectId, kind }: { id: string; projectId: string; kind: string },
  deps: AuthFetchDeps = {},
): Promise<ConversationHeader | null> {
  const res = await authFetch(
    '/api/conversations',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, projectId, kind }),
    },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'The chat could not be started. Try again.')
  const data = (await res.json()) as { conversation?: unknown }
  return normalizeHeader(data.conversation)
}

/**
 * Build an async READ store for one conversation `kind` (plan | build), preserving the names
 * `builderHistory` re-exports. `newConversation` stays SYNCHRONOUS — it mints a UUID with no
 * network, and minting an id is still all this store does about creation.
 *
 * STILL NO CREATE MEMBER, though the reason changed: `createConversation` came back above, but
 * it belongs to the SEND path, which is the only place that knows a chat is about to receive its
 * first file. A create member here would invite creating a row at the moment one is listed or
 * navigated to, which is exactly the empty-chat problem the send path is careful to bound.
 */
export interface ConversationStore {
  loadHistory: (deps?: AuthFetchDeps) => Promise<(ConversationHeader | null)[]>
  newConversation: () => string
  getConversation: (id: string, deps?: AuthFetchDeps) => Promise<ConversationWithMessages | null>
}

export function createConversationStore(kind: string): ConversationStore {
  return {
    loadHistory: (deps) => listConversations(kind, deps),
    newConversation: () => newId(),
    getConversation: (id, deps) => getConversation(id, deps),
  }
}
