/**
 * The shared `parts[]` message-content model — the one thing every producer and
 * consumer of a chat message (the `conversationApi` reload projection, the live
 * turn stream, `MessageContent`'s render, `attachmentStore`'s transforms) agrees
 * on the shape of. This did NOT exist as a type anywhere before this file — it is derived
 * from the real construction/consumption sites, not invented:
 *
 *   - `TextPart`/`FilePart` (all three `file` sub-shapes) come verbatim from the
 *     JSDoc contract at the top of `attachmentStore.js`, the module that owns
 *     the parts<->wire transform.
 *   - `PlanOptionsPart`/`StepPart` wrap the already-typed `PlanOptionsItem`/
 *     `StepItem` from `turnStreamApi.ts` rather than re-declaring them — both
 *     are constructed ONLY by `conversationApi.js`'s `messagesFromProjection`
 *     (the reload path); the live path never builds these two part kinds
 *     directly.
 *   - `BuildInProgressPart` likewise comes from `messagesFromProjection`.
 *   - `BuildPart` is the one case with a real pre-existing inconsistency
 *     between producers (see below) — this file makes it visible rather than
 *     papering over it.
 *
 * PRE-EXISTING INCONSISTENCY, STILL NOT FIXED: the persisted/reload `build` part
 * (`conversationApi`'s `messagesFromProjection`, the `banner` branch) and the live
 * `build` part carry different field sets under the same `type:'build'`
 * discriminant. Both producers named here originally lived on the deleted builder
 * page; the divergence outlived it, which is why the two named types below are
 * still worth keeping apart. No consumer has ever distinguished them — every field
 * is read via plain optional access regardless of producer, which is why this was
 * never a runtime bug.
 * `BuildPartPersisted` and `BuildPartLive` are kept as two distinct named
 * types, unioned, rather than collapsed into one everything-optional shape, so
 * the divergence stays legible to a future reader.
 *
 * UPDATE: `attachmentStore.ts` has since converted — its real construction
 * sites confirmed this file's shapes, with one revision: `FilePartOffice`
 * gained `truncationNote` (was missing when this file was first written).
 */
import type { PlanOptionsItem, StepItem } from './turnStreamApi'

/** Prose part — optionally an inline csv/txt attachment (content lives in `text`,
 * shown as a chip, re-inlined every turn). */
export interface TextPart {
  type: 'text'
  text: string
  attachment?: {
    attachmentId: string
    name: string
    mediaType: string
    size: number
  }
}

/** Image/PDF bytes living in the object store. */
export interface FilePartImageOrDocument {
  type: 'file'
  kind: 'image' | 'document'
  attachmentId: string
  key: string
  name: string
  mediaType: string
  size: number
}

/** A HYBRID: original .docx/.xlsx bytes live in the object store (chip
 * re-downloads them) but are NEVER sent to the model — the server-extracted
 * Markdown (`text`) is sent as a sticky text block instead. */
export interface FilePartOffice {
  type: 'file'
  kind: 'office'
  format: 'word' | 'excel'
  attachmentId: string
  key: string
  name: string
  mediaType: string
  size: number
  text: string
  truncated: boolean
  /** Human-readable truncation detail for the chip tooltip; only set when
   * `truncated` is true. Added converting attachmentStore.ts — flagged as
   * missing when this file was first written (Step 1), before
   * attachmentStore.js's real construction site (`buildUserParts`) was
   * traced. */
  truncationNote?: string
}

/** A .pptx: original bytes live in the object store (chip re-downloads them);
 * the model sees a sticky vision `document` block referencing the INTERNAL
 * converted PDF by `pdfFileId` (never the .pptx, never base64) — the PDF is
 * invisible to the user, only the .pptx is ever surfaced. */
export interface FilePartDeck {
  type: 'file'
  kind: 'deck'
  attachmentId: string
  key: string
  name: string
  mediaType: string
  size: number
  pdfFileId: string
  pageCount: number
}

export type FilePart = FilePartImageOrDocument | FilePartOffice | FilePartDeck

/** The persisted/reload `build` part (`conversationApi.js`'s `banner` projection
 * item) — the builder outcome bubble read back after a page reload. */
export interface BuildPartPersisted {
  type: 'build'
  sessionId: string
  status: 'ended' | 'failed'
  reason: string | null
  previewUrl: string | null
}

/** The live `build` part — rendered the moment a build turn ends, before any reload. Two call sites feed this: the C7
 * session-based path (carries `sessionId`) and the current turn-stream "Build
 * it" path (carries `turnId`); both otherwise produce the same fields. */
export interface BuildPartLive {
  type: 'build'
  status: 'ended' | 'failed'
  previewUrl: string | null
  endedAt: string
  snapshotCommitted: boolean | null
  reason: string | null
  sessionId?: string
  turnId?: string
}

export type BuildPart = BuildPartPersisted | BuildPartLive

/** The Build it / Keep refining card, carried with its STORED resolution state
 * (`conversationApi.js` only — reload path). */
export interface PlanOptionsPart {
  type: 'plan_options'
  item: PlanOptionsItem
}

/** A stored friendly agent step — the reload half of the build narrative
 * (`conversationApi.js` only — reload path; hidden steps are filtered before
 * this part is ever constructed). */
export interface StepPart {
  type: 'step'
  step: StepItem
}

/**
 * The agent is REASONING — and this part carries no text, by construction.
 *
 * THE STATUS-ONLY GUARANTEE IS STRUCTURAL, NOT A PROMISE. Reasoning text is technical and far
 * too much for the people who read this, so the decision is that the transcript shows THAT the
 * agent is working and never what it is working through. The server enforces the same rule at
 * the other end — reasoning is stored for the provider's next turn and is never projected,
 * never framed and never sent here — and this shape is the second wall: there is no field for
 * reasoning text to arrive in, so a later change cannot start carrying it by accident.
 *
 * THE ONLY PRODUCER IS THE LIVE SURFACE, which synthesises one at the TAIL of the streaming
 * message while the turn's `working` flag is true — the model is thinking at the end of what it
 * has written so far, and `streamingParts` records at length why pinning it to index 0 made the
 * turn jump down the screen. It has no reload counterpart on purpose: a finished turn is not
 * thinking, and a status line about a moment that has passed is noise in a transcript somebody
 * is reading tomorrow.
 */
export interface ReasoningPart {
  type: 'reasoning'
}

/** A build began and no outcome closed it yet — the durable anchor
 * (`conversationApi.js` only — reload path). */
export interface BuildInProgressPart {
  type: 'build_in_progress'
  sessionId: string
}

export type MessagePart =
  | TextPart
  | FilePart
  | BuildPart
  | PlanOptionsPart
  | StepPart
  | ReasoningPart
  | BuildInProgressPart

/** The in-memory message shape the conversation surface renders
 * (`{id, role, parts, seq}`, per `conversationApi`'s own doc comment).
 * `seq`/`createdAt` are absent on the ephemeral local welcome message. */
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  parts: MessagePart[]
  seq?: number
  createdAt?: string
  ephemeral?: boolean
}
