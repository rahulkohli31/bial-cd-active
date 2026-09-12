/**
 * WHY THIS EXISTS: the shared `parts[]` message-content model every producer/consumer of a
 * chat message (`conversationApi`'s reload projection, the live turn stream, `MessageContent`'s
 * render, `attachmentStore`'s transforms) agrees on. Derived from the real construction/
 * consumption sites, not invented:
 *   - `TextPart`/`FilePart` come verbatim from the JSDoc contract atop `attachmentStore.ts`
 *     (owns the parts<->wire transform; converted since this file was written, confirming
 *     these shapes — one revision, `FilePartOffice` gained `truncationNote`).
 *   - `PlanOptionsPart`/`StepPart` wrap the already-typed `PlanOptionsItem`/`StepItem` from
 *     `turnStreamApi.ts`; both are built only by `messagesFromProjection` (reload path).
 *   - `BuildInProgressPart` likewise comes from `messagesFromProjection`.
 *
 * PRE-EXISTING INCONSISTENCY, STILL NOT FIXED: the persisted/reload `build` part (the
 * `banner` branch of `messagesFromProjection`) and the live `build` part carry different
 * field sets under the same `type:'build'` discriminant — both trace to the deleted builder
 * page, and the divergence outlived it. No consumer has ever distinguished them (every field
 * is read via optional access regardless of producer), so it was never a runtime bug.
 * `BuildPartPersisted`/`BuildPartLive` stay two distinct named types, unioned rather than
 * collapsed into one everything-optional shape, so the divergence stays legible.
 *
 * IT ALSO CARRIES ONE PIECE OF RUNTIME CODE, for the same reason the shapes are here:
 * `outcomeSummary` turns a build part's own fields into the sentence a citizen reads, and BOTH
 * producers of that part need it — a util cannot import a component, so a leaf both already
 * depend on is the only place one copy of that sentence can live.
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

/** Image/PDF bytes living in the object store.
 *
 * `key` and `size` are OPTIONAL because a part can be rebuilt from the conversation
 * projection on reload, which ships neither: the blob key is an internal storage
 * detail the browser has no business holding, and the byte size is not needed to draw a chip.
 * Both are carried by the composer's own upload path and neither is ever read — `key` is
 * written at `attachmentStore.ts` and never consulted, `size` is read nowhere at all — so
 * typing them as required would force a reload to invent values that look like data. */
export interface FilePartImageOrDocument {
  type: 'file'
  kind: 'image' | 'document'
  attachmentId: string
  key?: string
  name: string
  mediaType: string
  size?: number
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
   * `truncated` is true. */
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

/**
 * HOW A BUILD ENDED — three terminals, not two.
 *
 * `'stopped'` is a first-class outcome and NOT a flavour of failure. A citizen's own Stop, a
 * force-end, an idle teardown and a spent daily limit all end a build with nothing wrong, and
 * folding them into `'failed'` is exactly what once announced a deliberate Stop as "The build
 * failed: stopped_by_user" — while the activity pill beside it correctly read "stopped before it
 * finished". One fact, two states, one screen.
 *
 * BOTH producers carry it, deliberately. The live turn terminal (`ConversationSurface`'s
 * `announceTerminal`) and the stored banner (`conversationApi`'s `messagesFromProjection`) each
 * used to collapse a stop into a different lie — `failed` live, `ended` on reload — so widening
 * one alone would only move the contradiction to whichever path the citizen took.
 */
export type BuildOutcomeStatus = 'ended' | 'failed' | 'stopped'

/**
 * REASON → THE SENTENCE A CITIZEN READS. The one table; there is no second copy of it.
 *
 * IT LIVES IN THIS MODULE, not on the surface that renders it, because BOTH paths need it and
 * only a leaf can serve both: the live terminal is drawn by `ConversationSurface` and the reloaded
 * one is projected by `conversationApi`, and a util cannot import a component without a cycle.
 * "Two authors for one sentence" is the documented failure this arrangement exists to prevent —
 * fixing one emitter alone only ever changed WHEN the wrong text appeared, never whether it did.
 *
 * IT MIRRORS `backend/src/services/build_sessions/outcome.py::_summary` — its four reasons, same
 * wording — because that emitter writes the durable row for legacy build sessions while this one
 * renders the turn terminal, and a transcript must not say different things about the same build
 * depending on when you looked at it. The turn engine's own endings (`request_limit`,
 * `wall_clock_deadline_exceeded`, `model_unavailable`) have no legacy row and are listed here only.
 *
 * PLUS ONE ARM THE SERVER TABLE LACKS: `workspace_restored`. It is raised at
 * `engine.py`'s `_WriteEndedError("workspace_restored", …)` — a turn that ends because the
 * citizen's workspace
 * had to be put back from the last saved copy, which is a SUCCESSFUL restore and not a broken
 * build, once wrongly announced as "The build failed: workspace_restored".
 */
export const OUTCOME_COPY: Readonly<Record<string, string | undefined>> = {
  quota_exceeded: 'The build stopped: you reached your daily limit.',
  stopped_by_user: 'You stopped this build before it finished.',
  force_ended: 'This build was force-stopped before it finished, and its work was discarded.',
  idle_teardown: 'This build was stopped because it sat idle.',
  workspace_restored:
    'This build stopped so your workspace could be put back from the last saved copy. Send your message again once your workspace is back.',
  // THE TURN ENGINE'S OWN BOUNDED ENDINGS (2026-09-11). `request_limit` and
  // `wall_clock_deadline_exceeded` end through `_bounded_run_ending`, whose banner tells the
  // citizen their app is working — a transcript row reading "The build failed." over that banner
  // was the contradiction a citizen photographed. One sentence for both, because which bound
  // fired is not something they can act on differently. `model_unavailable` is the named ending
  // for a model service that stayed down past every retry (`engine.py::_end_model_unavailable`).
  request_limit: 'This build stopped after doing as much as it does in one go.',
  wall_clock_deadline_exceeded: 'This build stopped after doing as much as it does in one go.',
  model_unavailable:
    'The assistant could not get an answer from its service, so this build stopped.',
}

/**
 * The one-line summary carried beside a build part. It is the message's TEXT, so it is both what
 * a plain reader sees and what the model is shown as history on the next turn — which is why it
 * states the outcome plainly rather than decoratively.
 *
 * THE REASON IS CONSULTED BEFORE THE STATUS, and that is the one ordering difference from
 * `outcome.py::_summary` — do not "restore" it to match. On the SERVER, `_terminal_status` maps a
 * Stop, a force-end and an idle reap all onto ENDED, so a FAILED status there really does mean
 * something broke and can be answered first. On the turn stream it does not: `_WriteEndedError`
 * finishes as `failed` for every named graceful end there is, quota and workspace-restore
 * included. Answering the status first is exactly what printed "The build failed: quota_exceeded"
 * at someone who had merely used up their day.
 *
 * AN UNKNOWN REASON IS NEVER INTERPOLATED. Every `reason` that reaches here is a machine token —
 * a `_WriteEndedError` reason or a session end reason, `self_heal_budget_exhausted`,
 * `wall_clock_deadline_exceeded`, `sandbox_unavailable` and the rest — and not one of them is
 * prose. So an unlisted reason gets the neutral fallback, and the human-readable detail arrives on
 * its own `error` frame (the engine emits one beside every named end), written for a citizen
 * rather than for a log. Printing the token is the defect; the fallback is the fix.
 */
export function outcomeSummary({
  status,
  reason,
}: {
  status: BuildOutcomeStatus
  reason: string | null
}): string {
  const named = reason ? OUTCOME_COPY[reason] : undefined
  if (named) return named
  if (status === 'failed') return 'The build failed.'
  if (status === 'stopped') return 'This build was stopped before it finished.'
  return NEUTRAL_BUILD_SUMMARY
}

/**
 * The sentence for an ending that needs no sentence — a build that simply finished.
 *
 * EXPORTED SO THE TWO EMITTERS CAN AGREE. It is not copy anybody should read: the assistant has
 * just written its own account of what it built, and stamping a second bubble underneath saying
 * "Build finished." adds nothing, arrives after EVERY turn in a Build chat (every turn writes a
 * terminal, including one that changed a label), and — because it is its own message — carries a
 * second copy button directly under the first.
 */
export const NEUTRAL_BUILD_SUMMARY = 'Build finished.'

/**
 * Is this ending worth a sentence of its own?
 *
 * DERIVED FROM `outcomeSummary` RATHER THAN RE-DECIDED, and that is the whole design. The reload
 * path in `conversationApi.ts` already withheld the neutral ending — its docblock spells out why,
 * at length — while the LIVE path in `ConversationSurface.tsx` emitted it unconditionally. So the
 * same build read one way as it happened and another way after a refresh, and the bubble a citizen
 * reported was the live one. Two authors for one rule is a documented failure of this codebase;
 * asking the copy function itself means a new named reason becomes announceable in both places at
 * once, with nothing to keep in step.
 */
export function outcomeWorthAnnouncing(outcome: {
  status: BuildOutcomeStatus
  reason: string | null
}): boolean {
  return outcomeSummary(outcome) !== NEUTRAL_BUILD_SUMMARY
}

/** The persisted/reload `build` part (`conversationApi.ts`'s `banner` projection
 * item) — the builder outcome bubble read back after a page reload. */
export interface BuildPartPersisted {
  type: 'build'
  sessionId: string
  status: BuildOutcomeStatus
  reason: string | null
  previewUrl: string | null
}

/** The live `build` part — rendered the moment a build turn ends, before any reload. Two call
 * sites feed this: the legacy session-based path (carries `sessionId`) and the current
 * turn-stream "Build it" path (carries `turnId`); both otherwise produce the same fields. */
export interface BuildPartLive {
  type: 'build'
  status: BuildOutcomeStatus
  previewUrl: string | null
  endedAt: string
  snapshotCommitted: boolean | null
  reason: string | null
  sessionId?: string
  turnId?: string
}

export type BuildPart = BuildPartPersisted | BuildPartLive

/** The Build it / Keep refining card, carried with its STORED resolution state
 * (`conversationApi.ts` only — reload path). */
export interface PlanOptionsPart {
  type: 'plan_options'
  item: PlanOptionsItem
}

/** A stored friendly agent step — the reload half of the build narrative
 * (`conversationApi.ts` only — reload path; hidden steps are filtered before
 * this part is ever constructed). */
export interface StepPart {
  type: 'step'
  step: StepItem
}

/**
 * The agent is REASONING; carries no text, by construction — STRUCTURAL, not a promise:
 * reasoning is too technical for readers, so the transcript shows only THAT the agent works.
 * The server enforces this too (never projected here); this shape is the second wall, with
 * no field for the text to land in by accident. Synthesised only by the LIVE surface, at the
 * TAIL of a streaming message while `working` is true (see `streamingParts` on the index-0
 * jump) — no reload counterpart, since a finished turn isn't thinking.
 */
export interface ReasoningPart {
  type: 'reasoning'
}

/** A build began and no outcome closed it yet — the durable anchor
 * (`conversationApi.ts` only — reload path). */
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
