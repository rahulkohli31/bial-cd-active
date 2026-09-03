import { useState, useEffect, useRef, useCallback, useMemo, type FC } from 'react'
import { useNavigate, useLocation, useParams } from 'react-router-dom'
import Announcer, { useActivityAnnouncement } from './Announcer'
import ChatThread from './ChatThread'
import ChatRuntimeProvider from './runtime/ChatRuntimeProvider'
import Composer, { SendRefusal, type ComposerSubmission } from './Composer'
import type { BuildHandoff } from './OfferStrip'
import ScrollToLatest from './ScrollToLatest'
import SessionBanners from './SessionBanners'
import TurnBanner from './TurnBanner'
import { listProjectConversations } from '../../utils/conversationApi'
import type { ConversationHeader } from '../../utils/conversationApi'
import { ApiError } from '../../utils/apiError'
import { markAppVisible } from '../../utils/observe'
import { isConversationGone } from '../../utils/chatErrors'

import { resolvePreviewAddress } from '../../utils/previewAddress'
import { PREVIEW_PROBE_MS, SETTLED_GONE, resolveWorkspaceState } from '../workspace/workspaceState'
import type { StartOutcome } from '../workspace/workspaceState'
import {
  useAppPaneVisible,
  usePublishAddress,
  usePublishPaneView,
  usePublishReclaim,
  usePublishSave,
  usePublishSaveState,
  usePublishWorkspaceReport,
  useWorkspaceProject,
} from '../workspace/workspaceChannel'
import { notifyUsageChanged } from '../../utils/usage'
import { createBuildLock, openBuildLockChannel } from '../../utils/buildLock'
import type { BuildLock } from '../../utils/buildLock'
import { useDropTransientQuery } from '../../hooks/useDropTransientQuery'
import { useBuildSession } from '../../hooks/useBuildSession'
import type { UseBuildSessionDeps } from '../../hooks/useBuildSession'
import { isActiveBuildStatus } from '../../utils/buildSessionTypes'


import type { PendingAttachment } from '../../utils/attachmentInput'
import type { ChatKind } from '../../pages/ChatRoute'
import PlanChatWorkspaceLine from '../workspace/PlanChatWorkspaceLine'
import { startTurn, readTurnStream, buildFromPlan, stopTurn, TurnStartError } from '../../utils/turnStreamApi'
import { isKnownFrame } from '../../utils/turnStreamApi'
import type { CompileState } from '../../utils/compileState'
import { makeClientErrorRelay } from '../../utils/clientErrorRelay'
import type { TurnFrame, PlanOptionsItem, StepItem, DiagnosticFrame, StreamOutcome } from '../../utils/turnStreamApi'
import { contextState } from '../../utils/contextLimits'
import { atLimitSendState, narrativeEnvelopes, turnPhase } from '../../utils/turnNarrative'
import type { TurnNarrative } from '../../utils/turnNarrative'
import { fetchSaveState, saveProject, handOverWorkspace, asReclaimBlocked, fetchPreviewState, fetchCompileState, checkWorkspace } from '../../utils/buildSessionApi'
import type { HandoverStep, ReclaimBlocked, PreviewState } from '../../utils/buildSessionApi'
import { resolvePlanOptions } from '../../utils/turnStreamApi'
import { wireMessageFromParts, buildUserParts, partsToText, countAttachments, releaseUploadedAttachments } from '../../utils/attachmentStore'
import { validateConversationAttachmentCap } from '../../utils/attachmentInput'

import { loadBuilds, getBuild, deriveTitle } from '../../utils/builderHistory'
import type { ChatMessage, MessagePart, BuildPartLive } from '../../utils/messageTypes'

// The from-scratch greeting (ephemeral — never persisted, and never sent to the model: it is
// chrome, not a turn, and replaying it as history would have the model answering its own hello).
// #83's background cadence and U22/R16's terminal answers now live in
// `components/workspace/workspaceState.ts`, imported above. They moved when the project screen
// gained a read of its own (Plan F, U2): two surfaces polling the same endpoint on two private
// copies of "how often" and "when to stop" is the drift this file already has a scar from — the
// mint comment in `ProjectBuilder.tsx` records two sites keeping private copies of a one-liner
// and both going wrong together. A cadence that drifts is worse, because the symptom is a poll
// that stops on one surface and not the other, with nothing red anywhere.
//
// The reasoning is unchanged and lives beside the constants: the cadence is deliberately slow
// because focus and visibilitychange carry the real flow, and `unknown` is deliberately not
// terminal because it is the one answer that decided nothing.

const WELCOME_TEXT = "Hello! I'm Citizen Developer AI. Tell me what you'd like to build for BIAL operations."
const welcomeMessage = (): ChatMessage => ({ id: 'welcome', ephemeral: true, role: 'assistant', parts: [{ type: 'text', text: WELCOME_TEXT }], createdAt: new Date().toISOString() })

// U7: the whole system prompt is server-owned (`backend/src/services/agent/mode_prompts.py`,
// composed per turn from the conversation's kind). The thin client identity line this file
// used to hold moved there, and nothing about the prompt is decided in the browser.

// The three hardcoded "refinement chips" are GONE (user, 2026-07-30). They were a fixed list —
// dark mode, a real-time data table, a mobile layout — offered after every build regardless of
// what the app was, so a gate-cleaning log got advice about theming. A canned suggestion that
// cannot be about your app reads as filler, and it competed with the composer for the one
// decision the user was actually making. If per-app follow-ups are wanted later they have to
// come from the model, which is the only party that knows what was just built.

// The brief-card era is over (U11/U13): the plan streams as text, `present_plan_options` raises
// the offer, and its resolution state derives from the STORED record — never from fence-parsing
// the transcript. Plan D moved the offer from a card in the transcript to a strip on the composer.

// The LIVE half of a build turn is the TRANSCRIPT itself (Plan D U6): steps are parts, and the
// activity group draws them where they happened. It used to be the pinned `BuildProgress` bubble,
// which carried the headline, elapsed time, Stop and a Details expander; stop moved to the
// composer (U3) and the raw-output expander did not come across at all. The TERMINALS stay
// deliberately absent from the live surface: a finished build appends a real `build`-part message
// (003-U5) that says the same thing permanently — live narrative while it runs, a record after.

/**
 * The one-line summary persisted alongside a build part. It is the message's TEXT, so it is both
 * what a plain reader sees and what the model is shown as history on the next turn — which is why
 * it states the outcome plainly rather than decoratively.
 */
function outcomeSummary({ status, reason }: Pick<BuildPartLive, 'status' | 'reason'>) {
  if (status === 'failed') {
    return reason ? `The build failed: ${reason}` : 'The build failed.'
  }
  if (reason === 'quota_exceeded') return 'The build stopped: you reached your daily limit.'
  return 'Build finished.'
}

/**
 * THE CONVERSATION SURFACE — one surface, both kinds (Plan D U17, R72).
 *
 * ══ IT REPLACES TWO PAGES, AND IT CONSULTS NO KIND ══
 *
 * `BuilderPage.tsx` (2,665 lines) and `ChatPage.tsx` (1,026) are deleted. This file IS the former,
 * moved and re-rendered: the four stream consumers, the gate chain, the send discipline, the
 * save-state model and the preview/app-pane wiring came across unchanged, and only the render
 * layer is new. That is why the move is a `git mv` — the logic is auditable as a diff rather than
 * retyped, which on a page this size is the difference between a review and a hope.
 *
 * NOTHING BELOW ASKS WHAT KIND OF CHAT THIS IS. A Plan chat's transcript cannot show a build
 * because no build parts arrive in it, never because a renderer checked; what differs between the
 * kinds is the TOOLSET the server hands the model, which is decided when the conversation is
 * created and is not a thing a surface can branch on. `ChatPage`'s send path is not carried
 * forward: the builder's discipline — hold the draft until the server confirms, roll both bubbles
 * back on a refused turn — is the generalisation, and the planning page's blind restore is the
 * defect it fixes (R58).
 *
 * ══ THE ROUTING RULE (load-bearing — read before changing any send path) ══
 *
 * EVERY composer send is a TURN on this conversation (`startTurn` + the frame stream). A send
 * never starts a build; builds fire only from the offer strip's confirmation, first build and
 * iteration alike. The direct-fire send this surface once did is what made the agent silently
 * guess at a vague prompt.
 *
 * BUILD-IT IS A HANDOFF, NOT A FLIP. The offer strip's `Build this plan` creates a SECOND,
 * brand-new build chat seeded with the plan, and sends the citizen to it. The originating chat is
 * left exactly as it stands — no flip, no marker, nothing recorded in either direction. The turn
 * it starts is watched from the chat it actually runs in, on arrival, by the ordinary reattach
 * path — not from here.
 *
 * "Existing refine semantics" now means: a live LEGACY build session in this project is stopped
 * (`session.stop()`) before the handoff fires. `session.start()` is no longer called anywhere in
 * this file — the session half only reattaches (reload-mid-build) or stops.
 *
 * THREE DISTINCT IDENTITIES (unchanged from the single-file era, KTD-8):
 *   conversationId — the thread      (`/chat/{id}`, PATCH /conversations/{id})
 *   projectId      — the container   (breadcrumb; the C3 build session is project-scoped)
 *   build session  — the C3 session  (project/user-scoped, one-per-user)
 *
 * WHAT THE BUILD READS. The refined brief travels in the start body's `prompt` string; the
 * thread's `conversationId` rides along so the server can materialize the attachments it already
 * persisted (R3 / plan 002-U3) — it reads FILE PARTS from the thread, not the conversation as
 * context. (An earlier comment here claimed BRAIN reads project/conversation context server-side
 * per "C3 §2.1". It does not, and never did; the persist-before-start ordering below is what makes
 * the RELAY's project-context lookup and the attachment materialization work.)
 *
 * SESSION ↔ THREAD IDENTITY: the session is project-scoped. The thread that confirmed the brief
 * ORIGINATES the session; a confirm in another chat of the SAME project RE-ATTACHES the live
 * session (409 → getStatus → projectId-compare → resubscribe); a DIFFERENT project is BLOCKED
 * (the 409 is not self-describing — the projectId comparison is the gate, not the bare 409).
 * `sessionChatRef`/`sessionProjectRef` record the originating chat/project.
 *
 * @param {{
 *   chatId?: string, projectId?: string | null, projectName?: string | null,
 *   buildSessionDeps?: { client?: import('../../utils/buildSessionApi').BuildSessionClient,
 *                        eventSourceFactory?: import('../../utils/buildSessionEvents').EventSourceFactory },
 * }} [props]
 */
export interface ConversationSurfaceProps {
  chatId?: string
  projectId?: string | null
  projectName?: string | null
  /**
   * THE TITLE THIS SURFACE DERIVES, HANDED BACK UP (plan 002, U2).
   *
   * A chat's title is derived here, from its first message, at the moment the row is created — and
   * the route that publishes the toolbar row's heading learns it from a GET that already ran and
   * 404'd. Without this the row would say "New build chat" until a reload, while the board draws
   * the real title the instant the first message is sent.
   *
   * A CALLBACK RATHER THAN A SECOND PUBLISHER, so the heading keeps one author. This surface
   * informs; the route decides.
   */
  onTitleDerived?: (title: string) => void
  /** Which kind of conversation this is. Read for ONE thing: whether the app pane is seen. */
  kind?: ChatKind
  projectHasSavedBuild?: boolean | null
  buildSessionDeps?: UseBuildSessionDeps
}

/** A card's local overlay while the stored record catches up. `build_failed` is GONE from
 *  the card's state set: a build that could not start says so as a typed HTTP failure, in the
 *  error line under the card, and burning the card as well would tell the citizen the offer is
 *  spent when it is still pressable. */
type PlanOverrideValue = 'build' | 'refine'

/** One entry of the live turn's ordered content — see `TurnSink.parts`. */
type SinkPart = { kind: 'text'; text: string } | { kind: 'step'; toolCallId: string; step: StepItem }

/** The turn-frame reducer's mutable accumulator, carried back out to the caller once the
 * stream settles (`streamAssistant`/`reattachToTurn`/`fireRelayTurn`'s shared shape). */
interface TurnSink {
  /**
   * THE LIVE TURN, PROSE AND STEPS, IN THE ORDER IT PRODUCED THEM.
   *
   * This was a flat `text` string beside a step map, which could only ever draw every step and
   * then one block of text — while a reloaded transcript drew text and steps interleaved in
   * part order. The two agreed only because prose beside a tool call was thrown away, so a
   * turn could hold at most one text block and it was always last. With that gone, the live
   * path has to carry the order or the same turn reads differently after a refresh.
   *
   * The steps live here rather than only in `turnSteps` state because the TRANSCRIPT is what
   * renders them now: the activity group draws from message PARTS, and the frame handler is
   * created once per turn with an empty dependency list, so it cannot read a state value that
   * changes under it. `turnSteps` state is still set alongside, and that is not duplication
   * for its own sake — the narrative it feeds answers two different questions (what phase the
   * app pane is in, and whether today's budget is spent) that have nothing to do with what the
   * transcript draws.
   */
  parts: SinkPart[]
  /** Is the model REASONING right now (the server's `working` flag)?
   *
   *  IT IS NOT A PART, because the server never sends one and never will: reasoning text is
   *  stored for the provider's next turn and is never framed. The flag is turned INTO a
   *  content-free reasoning part at the head of the streaming message by `streamingParts`,
   *  because the library's status renderer is reached only when a message actually carries a
   *  part of that kind — a boolean riding the turn renders nothing at all on its own. */
  working: boolean
  terminal: 'completed' | 'failed' | 'stopped' | null
  reason: string | null
  snapshotCommitted: boolean | null
  /** WHICH turn this settled — needed after the stream ends, when `liveTurnIdRef` has already
   *  been cleared by the terminal frame. The outcome dedupes on it, so without it every build on
   *  one surface instance would dedupe on `undefined` and only the first would show. */
  turnId: string | null
}

/**
 * The part kinds `convertMessage` maps to no rendered element, restated here because this is the
 * one place that has to know a message made ONLY of them should not reach the thread at all.
 *
 * Kept as data rather than as a condition so the two files can be read against each other: if a
 * fourth kind stops rendering, it belongs in both, and a mismatch shows up as a stray action bar
 * rather than as a type error.
 */
const UNDRAWN_PARTS: ReadonlySet<MessagePart['type']> = new Set<MessagePart['type']>([
  'build',
  'build_in_progress',
  'plan_options',
])

/**
 * THE COMMITTED FALLBACK for a diagnostic that carries no citizen-facing sentence.
 *
 * RE-HOMED FROM `BuildProgress.tsx`'s `ERROR_FALLBACK_MESSAGE`, which U17 deleted. It is needed
 * because the legacy C7 feed carries no `user_message` at all and the turn stream may send a
 * diagnostic class the server has no sentence for — and a row with no label reads as the
 * unrecognised-step phrase, which would say "working on your app" over a problem.
 *
 * Its ACTION half does not come across. The old card printed "Try describing what you want again,
 * or ask for something simpler." beside the message, because the card was the whole account of a
 * failure; this is one row inside a group, and a next action on a run that is still repairing
 * itself would be advice to abandon something the platform is already fixing. The action belongs
 * to a terminal, and the terminal has its own prose.
 */
const DIAGNOSTIC_FALLBACK = 'We hit a problem finishing that change.'

/**
 * The durable truth for a build that began and whose outcome never landed.
 *
 * Carried over verbatim from the row it replaces. It is deliberately PAST TENSE and deliberately
 * says nothing about what to do next: the platform does not know whether that build's work
 * survived, and a next action invented here would be a guess presented as advice.
 */
const BUILD_WAS_RUNNING = 'A build was running here when this chat was last open.'

/** The key a diagnostic row takes in the turn's parts.
 *
 * A diagnostic is not a tool call, so it has no tool-call id of its own — but it IS drawn as a
 * failed row inside the activity group, so it needs a position and a stable key like any other.
 * The prefix is what lets a catch-up snapshot, which knows nothing about diagnostics, rebuild
 * the turn's order without discarding the ones this tab has already collected. */
const DIAGNOSTIC_KEY_PREFIX = 'diagnostic-'

/** A fresh accumulator. One helper, so a new field cannot be added to the type and forgotten at
 *  one of the two call sites that open a stream. */
function newSink(): TurnSink {
  return {
    parts: [],
    working: false,
    terminal: null,
    reason: null,
    snapshotCommitted: null,
    turnId: null,
  }
}

/**
 * The parts of the STREAMING assistant message, in the order the turn produced them.
 *
 * ORDER IS THE RENDER. `groupPartByType` coalesces ADJACENT tool-call parts into one activity
 * group, so a run of steps with nothing between them is one group and a paragraph written
 * between two steps SEALS the first group and opens a second — which is the canvas's own rule
 * and was unreachable while the live path could only draw every step and then one block of
 * text. This function is the whole of the live half of that; the reload path has always
 * produced part order.
 *
 * Hidden steps are dropped rather than positioned: a hidden step is plumbing the citizen never
 * reasons about — a write to a configuration file, a housekeeping shell command — and leaving a
 * gap where one was would break the adjacency the grouping reads. The flag no longer covers
 * reads, and never covers a step that failed; both of those are the server's call, and this
 * filter deliberately holds no opinion of its own.
 *
 * A NEW ARRAY EVERY TIME, deliberately. The runtime caches the converted message on OBJECT
 * IDENTITY (convertMessage trap 4), so a mutated-in-place part list is invisible and the UI
 * simply never re-renders — and this maps every entry to a fresh object for the same reason.
 */
function streamingParts(sink: TurnSink): MessagePart[] {
  const parts: MessagePart[] = []
  for (const part of sink.parts) {
    if (part.kind === 'text') {
      // An empty text part renders no element, so an in-flight turn with steps and no prose
      // yet is just its activity — which is exactly what should be on screen at that moment.
      parts.push({ type: 'text', text: part.text })
    } else if (!part.step.hidden) {
      parts.push({ type: 'step', step: part.step })
    }
  }
  // THE STATUS RIDES AT THE TAIL, and only while the model is actually thinking. It is
  // synthesised rather than received: the server sends a boolean, never a reasoning part, so
  // this is where the flag becomes something the thread can group and render. It carries no
  // text — the shape has no field for any — which is what makes "status only, never the
  // reasoning" structural rather than a promise.
  //
  // AT THE TAIL RATHER THAN THE HEAD, because `working` is not a turn-opening fact. It goes
  // true again on every reasoning burst, and with adaptive thinking on, a build that loops
  // through several tool calls thinks again between them — so pinning the row to index 0 put
  // "Working on your app" ABOVE paragraphs and steps the citizen had already read, and the
  // whole turn appeared to jump down the screen until the burst ended. ORDER IS THE RENDER,
  // and the model is thinking HERE, at the end of what it has written so far.
  //
  // At the start of a turn `sink.parts` is empty, so this is still the first thing on screen —
  // the case that mattered when the row was written is unchanged.
  if (sink.working) parts.push({ type: 'reasoning' })
  // THE STREAMING MESSAGE ALWAYS ENDS ON A TEXT PART, and the empty one is load-bearing twice
  // over. It was implicit while this function appended the whole reply as one trailing block;
  // once the parts became ordered it had to be said, because a turn that has only run steps so
  // far now genuinely produces a step-only message.
  //
  //  1. `hasUpcomingMessage` — the library appends an optimistic assistant message with an id we
  //     do not control the moment `isRunning` is true and the last message is not an assistant's
  //     (convertMessage trap 3), and a message whose parts all convert to nothing is what makes
  //     that reachable.
  //  2. The transcript's step-only rule — a message made ONLY of steps is a STORED row that the
  //     live message is re-telling, and it is dropped for the turn in flight. Without this the
  //     live message matched that rule against itself and vanished mid-build.
  //
  // It renders no element either way, so it costs nothing on screen, and it is only appended
  // when the newest part is not already text — a turn that has just written keeps its own block.
  if (parts[parts.length - 1]?.type !== 'text') parts.push({ type: 'text', text: '' })
  return parts
}

/** Append `text` to the block already open, or open a new one.
 *
 * A delta that arrives when the newest part is a STEP opens a block whatever the frame says:
 * appending to a sealed block would move that prose back above the step it was written after,
 * silently reordering the turn. */
function appendText(sink: TurnSink, text: string, newBlock: boolean): void {
  const newest = sink.parts[sink.parts.length - 1]
  if (!newBlock && newest?.kind === 'text') {
    newest.text += text
    return
  }
  sink.parts.push({ kind: 'text', text })
}

/** Record a step at its position, or replace the one already there.
 *
 * The `finished` frame carries the same tool-call id as its `started` one and REPLACES it in
 * place: appending would stack a spinner beside its own result, and the activity group's live
 * count would climb while the same step re-rendered. */
function putStep(sink: TurnSink, toolCallId: string, step: StepItem): void {
  const at = sink.parts.findIndex((part) => part.kind === 'step' && part.toolCallId === toolCallId)
  if (at === -1) sink.parts.push({ kind: 'step', toolCallId, step })
  else sink.parts[at] = { kind: 'step', toolCallId, step }
}

export default function ConversationSurface({ chatId: chatIdProp, kind = 'build', projectId = null, projectName = null, projectHasSavedBuild = null, onTitleDerived, buildSessionDeps }: ConversationSurfaceProps = {}) {
  // R11/R12 — THE ONE THING THE KIND DECIDES ON THIS SURFACE. A Plan chat shows no app pane; a
  // Build chat shows it. `build` is the default because fifteen existing suites mount this surface
  // with no kind at all and every one of them is about the build surface — defaulting to `plan`
  // would silently retire the pane from all of them.
  const isPlanChat = kind === 'plan'
  const navigate = useNavigate()
  const location = useLocation()
  const params = useParams()
  const buildId = chatIdProp ?? params.chatId
  const initialPrompt = location.state?.prompt || ''
  // `theme` used to ride in here from the builder's Select Theme control. Nothing
  // downstream ever read it — not the prompt, not the sandbox, not the generated app —
  // so the control and this key went together (#157 B1). Rows written before that keep an
  // orphan `theme` in their stored context; harmless, and no migration is needed because
  // nothing reads it.
  const contextRef = useRef<{ uploadedFiles: unknown[] }>({
    uploadedFiles: location.state?.uploadedFiles || [],
  })
  const dropTransientQuery = useDropTransientQuery()

  // The one build-session owner (feed + preview + status + keep-alive timers). Tests
  // inject a mock client + FakeEventSource via `buildSessionDeps`.
  const session = useBuildSession(buildSessionDeps ?? {})

  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [builds, setBuilds] = useState<ConversationHeader[]>([])
  // THE #42 CHAT-COLLAPSE THAT USED TO LIVE HERE IS GONE (Plan 006, R13) — not moved, retired.
  // Under Plan 006 this surface no longer owns a column; it FILLS the rail, and the rail's
  // collapse belongs to `WorkspaceShell` (`usePublishRail` / `railWidthClass`), with the control
  // for it drawn by `AppPane`, which is the part of the pane that always renders. Two reasons this
  // one had to go rather than move over unchanged:
  //   1. IT WAS A SECOND COLLAPSE FOR THE SAME COLUMN. Once the conversation IS the rail, a toggle
  //      here and the shell's own toggle would collapse the identical column through two
  //      independent booleans — exactly the "two controls, one column" shape R13 forbids.
  //   2. IT WAS UNREACHABLE EXACTLY WHEN IT MATTERED. This toggle rendered into `LivePreview`'s
  //      toolbar, and `LivePreview` only mounts when there is something to frame — so on a Plan
  //      chat, on a project with nothing built yet, or on an app that had gone to sleep, the
  //      toggle simply did not exist, and a panel collapsed while an app was running lost its way
  //      back the moment the container stopped. That is the identical defect `AppPane`'s own
  //      collapse control was written to fix (see its docblock) — fixing it twice, once per
  //      collapse, is not a thing this surface gets to do.
  /**
   * THE ASSERTIVE SLOT (R65) — the things that genuinely interrupt.
   *
   * A refused send, a failed handoff, a refused stop, a cap the citizen has hit. It is one value
   * and the newest wins, for the same reason `TurnBanner` is: two platform sentences about the
   * same moment are a contradiction, not extra information.
   *
   * It replaces `attachToast`'s fixed-corner box AND the per-offer `planErrors` map. The map was
   * keyed by tool-call id to put an error under one card among several; there is one offer strip
   * on the composer now, so a keyed map would be a lookup that can only ever have one entry.
   */
  const [urgent, setUrgent] = useState<string | null>(null)
  // WHICH CHAT has a turn streaming, not merely whether one does (G2). ONE INSTANCE OF THIS
  // COMPONENT survives a chat switch under flat routing — the URL changes, this does not
  // remount — so the boolean form gated chat B's send on chat A's turn. Same per-chat scoping
  // `buildActiveHere` already applies to the build half.
  const [generatingChatId, setGeneratingChatId] = useState<string | null>(null)
  // Has the adopt round-trip settled the question "is a build still running in this chat?" (G1).
  //
  //   'checking'    — the mount/adopt is still in flight. The honest opening state: the page
  //                   cannot yet say whether a send would be accepted.
  //   'resolved'    — the question was ANSWERED. Three arms reach it: no anchor in the transcript
  //                   (every ordinary chat), the reattach resolving, and the 404 retention lapse.
  //   'unreachable' — the question could not be ASKED. Send stays shut rather than guessing over a
  //                   possibly-live build, and a Retry renders, because a permanently shut gate
  //                   whose only explanation was a vanishing toast is the dead end this plan exists
  //                   to remove.
  const [gateCheck, setGateCheck] = useState<'checking' | 'resolved' | 'unreachable'>('checking')
  // `Build it` was clicked and the atomic transition has not answered yet. A full server
  // round-trip (lock acquire + sandbox provision) lives in here — seconds, not a keystroke — and
  // the composer must be shut for ALL of it: the build has begun from the user's point of view,
  // and a send made in this window used to vanish without a word (the ref guard below is silent
  // by design, because it was only ever meant to absorb a double-Enter).
  // WHICH chat is mid-Build-it, not merely that one is. It was a bare boolean, which held
  // while `buildFromPlan` was in flight — a short window nobody noticed was unscoped. U5 made
  // the window the WHOLE BUILD (the turn watcher is awaited inside the same handler), and an
  // unscoped flag over that span gates every sibling chat in the tab: exactly the leak
  // `generatingChatId` and the per-chat `sendingRef` already exist to prevent.
  const [buildStartingChatId, setBuildStartingChatId] = useState<string | null>(null)
  // The LIVE offer (a `plan_options` frame mid-turn, before the row reaches a reload's
  // projection) + per-offer local overrides so a press updates the strip instantly (the stored
  // record catches up on the next hydration).
  //
  // THE PER-PRESS MINTED CHAT ID USED TO LIVE HERE and it moved to the strip (U16, D4): the id
  // belongs to the press-session, and the control that is pressed is the one that can hold it.
  const [livePlanOptions, setLivePlanOptions] = useState<PlanOptionsItem | null>(null)
  const [planOverrides, setPlanOverrides] = useState<Record<string, PlanOverrideValue>>({})
  // `turnError` covers the chat half (429 daily cap, refused turn, in-band failure);
  // `session.error` covers the build half. Distinct sources, both above the composer.
  const [turnError, setTurnError] = useState<string | null>(null)
  // U2 — what the PLATFORM has to say about the workspace itself: it was reset and is being put
  // back, it was reset and cannot be, we could not check it. These arrive as `workspace` frames
  // carrying a message, and they share the banner slot with `turnError` because they compete for
  // the same moment and the same square inch — see `TurnBanner` for why the slot holds one.
  //
  // IT WINS OVER `turnError` WHILE IT IS SET, rather than the two racing through one setter.
  // `setTurnError` has fifteen call sites and mirroring each one is fifteen chances to miss;
  // precedence is one expression at the render site. And precedence is the right answer on the
  // merits: a workspace sentence ENDS the turn, so nothing that follows it in the same turn can
  // be more current, and both are cleared together when the next turn starts.
  const [workspaceSays, setWorkspaceSays] = useState<string | null>(null)

  // ── The Write turn's narrative, straight off the turn stream (U5) ─────────────────────
  // A build used to speak through the C7 session feed; it is a turn now, so these four
  // read from turn frames instead. Keyed state rather than arrays for steps, because a
  // step arrives twice (started, then finished) under one `toolCallId` and the second must
  // REPLACE the first in place — appending would stack a spinner and its own result.
  const [turnSteps, setTurnSteps] = useState<Record<string, StepItem>>({})
  const [turnWorkspace, setTurnWorkspace] = useState<TurnNarrative['workspace']>(null)
  const [turnPreview, setTurnPreview] = useState<TurnNarrative['preview']>({ url: null, state: null })
  // R17/R18 — what the app's dev server is compiling, streamed on the turn feed. `null` until
  // the container reports, which for an app on an image predating the signal is forever — and
  // `unknown` when it reported that it could not tell. The pane HOLDS its cover on both; this
  // page's only job is to carry the value through without interpreting it.
  const [turnCompile, setTurnCompile] = useState<CompileState | null>(null)

  // R17 (runtime half) — the receiving end of the app's own error reporter. The app has always
  // posted its browser-side crashes to this frame (`error-capture.tsx`); `LivePreview` validates
  // the sender's origin and hands them here, and this relays them to the build harness, where a
  // reported crash makes the health verdict not-green. NOTHING about the report is shown to the
  // user — the only visible consequence is that the completion claim does not appear.
  //
  // A ref so the relay's own throttle counter survives re-renders: rebuilding it every render
  // would reset the count on every keystroke and defeat the cap entirely.
  // Lazy, not `useRef(makeClientErrorRelay())`: an argument to `useRef` is evaluated on every
  // render and then thrown away on all but the first, so the eager form allocates a fresh relay
  // (and its throttle counter) on every streamed frame of a build. The counter surviving is the
  // whole point of holding it in a ref.
  const clientErrorRelayRef = useRef<ReturnType<typeof makeClientErrorRelay> | null>(null)
  if (clientErrorRelayRef.current === null) clientErrorRelayRef.current = makeClientErrorRelay()
  const [turnDiagnostics, setTurnDiagnostics] = useState<DiagnosticFrame[]>([])
  // Read inside async callbacks that outlive their render (the build watcher's terminal),
  // where the closed-over state value would be whatever it was when the build STARTED.
  const turnPreviewRef = useRef<TurnNarrative['preview']>({ url: null, state: null })
  const [turnQuota, setTurnQuota] = useState<TurnNarrative['quota']>(null)
  // The save model (KTD-5e). `saveDirty` is TRI-STATE — null is UNKNOWN, not clean.
  const [saveDirty, setSaveDirty] = useState<boolean | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  // `projectHasSavedBuild` arrives as a PROP, read once when the route resolved, and nothing
  // refetches it. But a Save is precisely the act that writes the snapshot bundle that flag
  // reports — so saving, the one thing that makes a relaunch possible, left the Relaunch
  // affordance hidden until the user happened to reload the page. This carries the fact
  // forward for the rest of the session; it only ever flips toward "yes, there is one now",
  // which is the only direction a successful Save can move it. Keyed to the project so
  // switching projects cannot inherit another project's answer.
  const [savedBuildProjectId, setSavedBuildProjectId] = useState<string | null>(null)
  const [turnTerminal, setTurnTerminal] = useState<'completed' | 'failed' | 'stopped' | null>(null)
  // THE ELAPSED-TIME CLOCK AND THE STOPPING FLAG BOTH WENT WITH THE CARD THEY DRESSED. The card
  // showed "3m 12s" beside a spinner and greyed its own Stop button while a stop was in flight;
  // the composer's stop control owns its busy state internally, and the activity group counts
  // steps rather than seconds. Neither value has a reader left.
  // WHICH CHAT this narrative belongs to. Same scoping `sessionChatRef` gives the build half,
  // and for the same reason (CC3): the page does not remount on a chat switch, so without it a
  // sibling chat renders the neighbouring chat's build narrative — complete with a working Stop
  // button for a build its reader never started and cannot see the composer state of.
  const turnNarrativeChatRef = useRef<string | null>(null)
  // The turn currently streaming, for Stop. A ref because the stop handler is created once
  // and would otherwise close over whichever turn was live at its first render.
  const liveTurnIdRef = useRef<string | null>(null)

  /** Clear the previous turn's narrative before a new one starts — otherwise a second
   *  message renders the first build's steps and diagnostics as if they were its own. */
  useEffect(() => {
    turnPreviewRef.current = turnPreview
  }, [turnPreview])

  const turnNarrative = useMemo(
    () => ({
      steps: turnSteps,
      diagnostics: turnDiagnostics,
      quota: turnQuota,
      workspace: turnWorkspace,
      preview: turnPreview,
    }),
    [turnSteps, turnDiagnostics, turnQuota, turnWorkspace, turnPreview],
  )

  const turnEnvelopes = useMemo(() => narrativeEnvelopes(turnNarrative), [turnNarrative])
  // U24 — `null` unless today's budget is spent, in which case it carries the reset time.
  const atLimit = useMemo(() => atLimitSendState(turnEnvelopes), [turnEnvelopes])

  const turnNarrativeIsThisChat = turnNarrativeChatRef.current === buildId
  const turnBuildStatus = useMemo(
    () =>
      turnNarrativeIsThisChat
        ? turnPhase(turnNarrative, {
            running: generatingChatId === buildId,
            terminal: turnTerminal,
            // `isBuild: true` USED TO BE PASSED HERE and it is gone rather than moved: the frames
            // answer the question now. It was always the literal `true` on this page, which is
            // what made the read-turn arm unreachable — and a hardcoded answer is exactly what a
            // surface serving both kinds cannot keep.
          })
        : null,
    [turnNarrative, turnNarrativeIsThisChat, generatingChatId, buildId, turnTerminal],
  )

  /**
   * STOPPING A LIVE TURN USED TO BE THIS SURFACE'S OWN HANDLER, with its own in-flight flag and
   * its own failure sentence. It is `StopTurnControl` now (U3) — the control owns the busy state,
   * reads its target at press time and reports a failure through `onUrgent` — and `handleCancel`
   * below hands the same target to the runtime's `cancel` capability. Two callers, one target
   * resolver, and neither of them can stop a turn other than the one on screen.
   */

  /** Ask the SERVER whether there is unsaved work. Deliberately not a local flag: the
   *  comparison is container-HEAD vs saved-bundle-HEAD, which is the only one that survives a
   *  reload or a second tab — both of which lose in-memory state while the commits stay put. */
  const refreshSaveState = useCallback(async (activeProjectId: string | null) => {
    if (!activeProjectId) return
    try {
      const state = await fetchSaveState(activeProjectId)
      if (projectIdRef.current === activeProjectId) setSaveDirty(state.dirty)
    } catch {
      // UNKNOWN, never "clean". A failed check must not report the work as safe.
      if (projectIdRef.current === activeProjectId) setSaveDirty(null)
    }
  }, [])

  const handleSave = async () => {
    const activeProjectId = projectIdRef.current
    if (!activeProjectId || saving) return
    setSaving(true)
    setSaveError(null)
    try {
      await saveProject(activeProjectId)
      if (projectIdRef.current === activeProjectId) {
        setSaveDirty(false)
        // There is now a snapshot to relaunch from — say so without waiting for a reload.
        setSavedBuildProjectId(activeProjectId)
      }
    } catch (err) {
      // Surfaced, never swallowed: a Save that silently fails leaves the user believing their
      // work is stored. The 409 copy from the server already names the way out.
      if (projectIdRef.current === activeProjectId) {
        setSaveError(err instanceof Error ? err.message : 'Could not save your work. Try again.')
        setSaveDirty(null)
      }
    } finally {
      setSaving(false)
    }
  }

  const resetTurnNarrative = useCallback(() => {
    turnNarrativeChatRef.current = buildIdRef.current
    setTurnSteps({})
    setWorkspaceSays(null)
    // U4 — THE NEXT TURN OWNS THE ANSWER. Once the citizen sends anything, U2's gate is the
    // authority: it will find the same reversion, restore, and say so. Leaving the poll's
    // older card up would stack a second, staler sentence over that one.
    setWorkspaceLost(false)
    setTurnDiagnostics([])
    setTurnQuota(null)
    setTurnWorkspace(null)
    // Deliberately reset to `null` rather than to `'clean'`: a new turn has learned NOTHING
    // about compilation yet, and starting it at clean would uncover a preview whose app may
    // still be broken from the turn before.
    setTurnCompile(null)
    // A FRESH CRASH-REPORT BUDGET FOR THIS TURN. The relay's scope key is the framed url, which
    // is byte-identical across repair turns on the attach arm — so without this the budget was
    // effectively per page-load: eight crashes into a session the relay went quiet for good, and
    // every later verify came back green on silence the platform itself had caused.
    clientErrorRelayRef.current?.reset()
    setTurnTerminal(null)
  }, [])

  /**
   * R35c — WHICH ASSISTANT MESSAGES BELONG TO A TURN THAT WAS INTERRUPTED.
   *
   * A fact about the TURN, not about any part, which is why the activity group cannot work it out
   * for itself: a stopped build's steps look exactly like a finished one's, and the group would
   * otherwise seal as "9 steps" over work that never completed. The group asks this set and says
   * "stopped before it finished" instead.
   *
   * Only `stopped` goes in. A `failed` turn has a reason and says it in prose; an interruption is
   * the one terminal with nothing to show for itself.
   */
  const [interruptedIds, setInterruptedIds] = useState<ReadonlySet<string>>(() => new Set())
  const markInterrupted = useCallback((assistantId: string, terminal: TurnSink['terminal']) => {
    if (terminal !== 'stopped') return
    setInterruptedIds((prev) => new Set(prev).add(assistantId))
  }, [])

  // ATTACHMENTS, THE DRAFT AND THE SCROLL SENTINEL ALL MOVED (U10, U8). `usePendingAttachments`,
  // `readDraft`/`writeDraft`, the file input and the `scrollIntoView` sentinel were this surface's
  // and are the composer's and the thread's now. The sentinel in particular is not merely
  // relocated: it scrolled on EVERY `[messages]` change, so a reader who had scrolled up was
  // dragged back to the bottom on every delta and reading the middle of a long build was
  // impossible. The viewport's own bottom-proximity check replaces it.
  //
  // Build sessions whose outcome this instance has already appended. The in-memory half of the
  // dedupe; the transcript scan in `appendBuildOutcome` is the half that survives a reload.
  const outcomeWrittenRef = useRef<Set<string>>(new Set())
  // The transcript, readable from async callbacks without a stale closure: the send path
  // assembles its optimistic pair after an await, and a closure over `messages` would be one
  // render behind by the time it read them.
  const messagesRef = useRef(messages)
  messagesRef.current = messages
  const buildIdRef = useRef<string | null>(null) // the active CONVERSATION being viewed/persisted — never a session id
  const streamAbortRef = useRef<AbortController | null>(null) // aborts the SUBSCRIPTION only — the turn runs on server-side
  const loadedBuildRef = useRef<string | null>(null)

  // The per-conversation guardrail's SOFT half. The transcript lives here, so the estimate does
  // too — the composer is handed a finished sentence rather than a second opinion about how long
  // the conversation is. The HARD half is the server's and arrives as an ordinary `turnError`.
  //
  // IT LIVES BELOW `loadedBuildRef` BECAUSE IT READS IT, and a `useMemo` body runs during the
  // render that declares it — putting this up with the other derived state threw a TDZ error on
  // first paint that neither `tsc` nor eslint saw, because the reference is inside a closure.
  //
  // GUARDED TO THIS CHAT, the way the narrative values below are. The surface does NOT remount
  // on a chat switch and `messages` is cleared in an effect, so there is a render where
  // `buildId` already names the incoming chat while `messages` still holds the outgoing one —
  // long enough to flash the previous conversation's warning onto the new composer.
  //
  // THE DEP IS `messages`, NOT `messages.length`, AND NARROWING IT WOULD BREAK THE FEATURE.
  // `text_delta` repaints through `.map`, so the array grows a new reference on every frame
  // while its LENGTH holds still for the whole reply. Keying on length would therefore skip
  // exactly the case this warning exists for: a single enormous answer that pushes the chat
  // over on its own. The citizen would see nothing, send once more, and meet the server's 413
  // instead of the sentence that was supposed to reach them first. The walk is bounded by part
  // count (JS `.length` is O(1)), and `transcript` already re-walks the same array every frame.
  const contextWarning = useMemo(
    () => (loadedBuildRef.current === buildId ? contextState(messages).message : null),
    [messages, buildId],
  )
  // Merged step runs, held by identity for `mergeStepRun` far below — declared here because
  // the per-chat effect clears it alongside these.
  const mergedRunsRef = useRef(new Map<string, ChatMessage>())
  const initFiredRef = useRef<string | null>(null) // the chat id already seeded — fire-once per chat, not per mount
  // Lets `handleBuildIt` read the latest session without depending on the `session` object
  // itself — `useBuildSession` returns a fresh object every render, so listing it as a
  // useCallback dep would recreate the callback (and defeat ChatMessageRow's memoization
  // below) on every tick of the build's own elapsed-time clock.
  const sessionRef = useRef(session)
  sessionRef.current = session
  const projectIdRef = useRef(projectId)
  projectIdRef.current = projectId

  // The chat + project that ORIGINATED the live session (for attribution + the render gate). The
  // session is project-scoped, so its surfaces render only while viewing a chat of ITS project.
  const sessionChatRef = useRef<string | null>(null)
  const sessionProjectRef = useRef<string | null>(null)
  // The anchor arm (d) failed on, so its Retry can re-ask the same question. A ref, not state:
  // `gateCheck` is what the render reads, and this is only the argument that goes with it.
  const gateRetryRef = useRef<{ activeId: string; sessionId: string } | null>(null)
  // The session's surfaces render only while viewing a chat of ITS project (it is project-scoped).
  // `blocked`/`error` come from attempts that FAILED to start (start()'s reset leaves sessionId
  // null), so they gate on the project stamp alone; the live surfaces also require a sessionId.
  //
  // Derived HERE, above every handler, and not down beside the JSX where the rest of the render
  // derivations live: `handleSend` reads `buildActive`, and a gate that depends on declaration
  // order is a gate one reorder away from silently opening.
  const sessionProjectMatches = sessionProjectRef.current === projectId
  const showSession = session.sessionId != null && sessionProjectMatches
  // TRUE for a build's whole duration (provisioning → building → ready) and false at its
  // terminal. PROJECT-SCOPED on purpose: one instance of this component survives a project
  // switch, so the project-agnostic form would let project A's build lock project B's composer.
  const buildActive = showSession && isActiveBuildStatus(session.status)
  // The COMPOSER's half of that gate is per-CHAT, matching the server's own per-conversation
  // 409 (`live_session_for_conversation`). A sibling builder chat in the same project is NOT
  // the chat that is building: the server would accept its turn, so shutting its composer and
  // telling its reader "building your app" is a lie about someone else's build. `buildActive`
  // stays project-scoped — the cockpit, the live bubble and the delete gate all speak for the
  // project's one session, and one instance of this component survives a project switch.
  const buildActiveHere = buildActive && sessionChatRef.current === buildId
  const generating = generatingChatId === buildId
  // THE ONE GATE, AND ITS ONLY TERM IS TURN STATE (KTD-1). What a chat IS appears nowhere in it: a
  // kind is a tool-access level, not a thing that can shut the composer, and using it as a gate is
  // what produced the Write dead end.
  //
  // WHAT THE GATE WITHHOLDS IS *SENDING*, NOT TYPING (KTD-2). The text box and the attach button
  // stay live at all times, so the user can compose their next message while they wait; they
  // simply cannot submit it into a running turn. Not disabling the textarea IS the focus fix
  // (KTD-3) — `disabled` on the focused element blurs it to `document.body`, which is the
  // mechanism behind "it blurs mid-sentence and focus never comes back".
  //
  // Send additionally waits on the adopt round-trip: an unresolved gate means we do not yet know
  // whether a build is live here, and "probably fine" is not a thing to guess about a send the
  // server would refuse. The arms are assembled into one sentence-per-cause by `gate` below and
  // enforced in `handleSubmit`; there is no boolean here for a render to read, because a gate
  // that renders from one value and enforces from another is a gate that can disagree with
  // itself.
  const buildStarting = buildStartingChatId === buildId
  // `sendingRef` records WHICH CHAT is mid-send, and it is stamped synchronously before the first
  // await so a second Enter — or the seeded prompt racing a manual send — cannot start a second
  // session (the one-per-user 409 collision). Per-chat for the same reason `generating` is (G2):
  // as a bare boolean it stayed set for as long as chat A's turn was in flight, so a send in the
  // sibling chat the user had switched to returned silently at the guard.
  const sendingRef = useRef<string | null>(null)
  const seqRef = useRef(0) // next message sort key for the active build's persisted turns

  // One build at a time, per project — advisory (KTD-7): `blockedBy` is the instant cross-tab
  // pre-check; the authoritative barrier is C3 start's 409. A crashed tab's claim expires, so the
  // channel is the only way claims travel (factory, not a module singleton).
  const buildLockRef = useRef<BuildLock | null>(null)
  if (buildLockRef.current === null) buildLockRef.current = createBuildLock({ channel: openBuildLockChannel() })

  useEffect(() => {
    const lock = buildLockRef.current
    return () => lock?.dispose()
  }, [])

  useEffect(() => {
    void refreshSaveState(projectId)
  }, [projectId, refreshSaveState])

  // THE PRODUCER STAYS HERE; THE WARNING DOES NOT (Plan A, U7). `refreshSaveState` above is still
  // the only thing that asks the server, and this is still the only surface that asks it — no new
  // caller was added anywhere. What moved is the browser-unload effect, to the shell, because this
  // page is an outlet child now and unmounts on every move to the project screen: left here, the
  // warning would disarm exactly when the citizen navigated away from the conversation that knew
  // about the unsaved work. The TRI-STATE is carried, never collapsed — `null` means "could not
  // check", and the shell arms on a definite `true` alone.
  usePublishSaveState(saveDirty)
  // THE SAVE CONTROL ITSELF LIVES IN THE TOOLBAR ROW NOW (plan 002, U2), so its three values and
  // its action go up the channel rather than into the pane's view. Two cells rather than one, and
  // the split is deliberate: the values are compared and drive a render, the action is read at
  // press time and drives none. `usePublishSaveState` above is unchanged and still separate — it
  // is the tri-state the shell's unload warning arms on, and it is KEPT across an unmount, while
  // these two are cleared with their publisher.
  usePublishSave({ dirty: saveDirty, saving, error: saveError }, { save: handleSave, rename: null })

  // A genuine unmount must cancel the in-flight turn-stream reader — a chat switch already
  // aborts it before resubscribing, but nothing did on unmount, leaking the reader (and its
  // fetch) past the component's life. The turn keeps running server-side; only the read stops.
  useEffect(() => () => streamAbortRef.current?.abort(), [])

  // Hold the advisory claim while THIS chat's session is live; retract it once the session is
  // GENUINELY over — a terminal status, or a fully-reset session — so another tab's `blockedBy`
  // pre-check clears (KTD-7). A refine's start() also passes through here (its reset() drops
  // sessionId transitionally), so beginOrRefineBuild RE-ACQUIRES the claim once start() resolves
  // 'started' (finding #23). The authoritative barrier is C3's 409; this is only the fast
  // cross-tab UX mirror.
  useEffect(() => {
    const chat = sessionChatRef.current
    if (!projectId || !chat) return
    const genuinelyEnded =
      session.sessionId == null || session.status === 'ended' || session.status === 'failed'
    if (genuinelyEnded) {
      buildLockRef.current?.release(chat)
      // The BUILD terminal signals the meter too (N4). A build is where the tokens actually go —
      // minutes of model steps against one chat turn's worth — so settling the meter only at the
      // chat terminal would leave the largest spend of all invisible until a reload.
      notifyUsageChanged()
    }
  }, [session.status, session.sessionId, projectId])

  // THIS PROJECT'S CONVERSATIONS — all of them, and the kind filter that used to narrow this to
  // builder chats is gone with the in-chat list (R54).
  //
  // The list itself has no reader any more; these two do, and neither is a list:
  //   - `buildBlockedMessage` names the OTHER conversation holding this project's build;
  //   - the pane's `turnRunning` asks whether a turn is running anywhere in THIS PROJECT.
  //
  // Both of those questions are wrong the moment a Plan turn is the one running, which is what
  // made the filter worth removing rather than merely unnecessary. It is a no-op today — a project
  // has only ever created builder chats — and it becomes a defect the day Plan B makes Plan turns
  // ordinary: a Plan turn in this project would be invisible to both readers, so the refusal would
  // say "another build chat" about a conversation it could not name and the pane would claim
  // nothing was running over an app being written.
  const refreshBuilds = useCallback(async () => {
    try {
      const isHeader = (c: ConversationHeader | null): c is ConversationHeader => c !== null
      const list = projectId
        ? (await listProjectConversations(projectId)).filter(isHeader)
        : (await loadBuilds()).filter(isHeader)
      setBuilds(list.sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime()))
    } catch {
      // Keep the current list on a transient error; the next refresh recovers.
    }
  }, [projectId])

  useEffect(() => {
    refreshBuilds()
  }, [refreshBuilds])

  // Adopt the routed chat. One effect owns both arms — a brand-new build chat and a saved one arrive
  // at the same URL shape, and only the server's answer tells them apart. The live build SESSION is
  // NOT reset here: it is project-scoped and outlives a chat switch (its surfaces are gated by
  // `sessionProjectRef` below), so switching chats never tears down an in-flight build.
  useEffect(() => {
    if (!buildId) {
      navigate('/projects', { replace: true })
      return undefined
    }
    if (loadedBuildRef.current === buildId) return undefined

    let alive = true
    buildIdRef.current = buildId

    // Drop every scrap of the PREVIOUS chat before a byte of this one arrives (no remount under
    // flat routing). THE DRAFT AND THE STAGED FILES ARE NOT LISTED BELOW and their absence is the
    // point: the composer is keyed by conversation and re-hydrates its own draft on the switch
    // (G3), so a leaked draft cannot send into the wrong chat and this effect has one fewer thing
    // it can forget.
    setMessages([])
    // The merged-run cache is keyed by the ids of the messages that went into it, so nothing in it
    // can be looked up again once the transcript above is emptied. Without this it is the one thing
    // on the surface that only ever grows: the component does not remount across chats, so every
    // step run ever scrolled into view would be retained, whole, for the life of the tab.
    mergedRunsRef.current.clear()
    // AND THE GUARD ABOVE IS INVALIDATED IN THE SAME BREATH. `loadedBuildRef` means "the chat
    // whose transcript is on screen", and one line ago that stopped being true — so it has to
    // stop saying so here rather than only when the fetch below succeeds. It did not, and the
    // handoff made that reachable on the platform's commonest action: Build it push-navigates
    // to the new chat, and Back before its `getBuild` resolves returned to a chat whose ref
    // still named it. The guard then skipped the whole hydration and left the citizen looking
    // at the empty transcript this line just made, recoverable only by reloading the page.
    loadedBuildRef.current = null
    // Re-ask the live-build question for the chat we are arriving at. Carrying the previous chat's
    // answer forward would open send over a build this chat has not been checked for.
    setGateCheck('checking')
    gateRetryRef.current = null
    streamAbortRef.current?.abort()
    setLivePlanOptions(null)
    setPlanOverrides({})
    setUrgent(null)
    setTurnError(null)
    setWorkspaceSays(null)
    setWorkspaceLost(false)

    getBuild(buildId)
      .then((saved) => {
        if (!alive || buildIdRef.current !== buildId) {
          // ARRIVAL ABANDONED, so nothing on this page will ever watch a turn here — and the
          // advisory build claim is retracted by whatever watches (`endGenerating`). Leaving
          // it announced would keep the 5s heartbeat re-asserting a claim with no watcher
          // behind it, and every later Build press in this project — this tab or a sibling —
          // would be told "another chat is already building" until the tab was reloaded. A
          // release for a conversation holding no claim is a no-op, so this costs nothing on
          // the ordinary path. The build itself is unaffected: it runs server-side, and the
          // one-workspace-per-user refusal there is the authoritative gate this only mirrors.
          releaseBuildClaim(buildId)
          return
        }
        loadedBuildRef.current = buildId
        // UNCHECKED (matches pre-migration behavior): the stored context's shape is asserted.
        if (saved?.context) contextRef.current = saved.context as { uploadedFiles: unknown[] }
        const restored = saved?.messages ?? []
        if (restored.length > 0) {
          // Seed the next seq from the highest PERSISTED seq, not the array length: a transcript
          // with any gap (a failed append, a pruned turn) would otherwise mint a colliding seq.
          seqRef.current = Math.max(...restored.map((m) => m.seq ?? 0)) + 1
          setMessages(restored)
        } else {
          seqRef.current = 0
          setMessages([welcomeMessage()])
        }
        // R8's OTHER live clause — the build half. A reload (or a second tab) landing on a
        // thread whose build is STILL RUNNING has no session in memory, so nothing would gate
        // the composer: the textarea would be enabled over a live build, every send would be
        // refused by the server, and the transcript would say a build "was running" in the
        // past tense about one that is
        // running right now. The projection carries the session id on the newest
        // `build_in_progress` part — that is all a reattach needs.
        reattachToLiveBuild(buildId, restored, () => alive)
        // A HANDED-OFF PROMPT FIRES EITHER WAY. The thread is canonical and permanent now
        // (003-U1), so it is empty exactly once in its life — every "Start Chat" after the
        // first arrives at a thread with turns. Consuming the prompt only on the empty branch
        // meant the second build onward silently swallowed the user's typed prompt AND their
        // attachments: the composer was already cleared above, and nothing else reads
        // `location.state.prompt`. Fire-once is `initFiredRef` within a mount, and stripping the
        // state from history across mounts; `restored` is handed over so the send cannot race the
        // render that restores it.
        const handedOff = fireHandoffPrompt(buildId, () => alive, restored)
        // R8's live clause: a turn still running server-side gets re-subscribed, so a reload
        // mid-reply keeps streaming instead of freezing on a half-written transcript (and the
        // next send stops 409ing against a turn this tab forgot about). Skipped when a handoff
        // prompt just fired — that path owns the socket, and two subscribers would abort each
        // other over one shared controller.
        if (!handedOff && saved?.activeTurn?.turnId) {
          void reattachToTurn(buildId, saved.activeTurn, () => alive)
        } else if (!handedOff) {
          // NOTHING IS RUNNING HERE, so nothing will settle and retract the claim. This is the
          // handoff's own narrow window: the press announced a claim for a chat whose turn had
          // already ended (or had not yet reached the read projection) by the time this page
          // arrived and asked. Same reasoning as the abandoned-arrival release above.
          releaseBuildClaim(buildId)
        }
      })
      .catch(() => {
        releaseBuildClaim(buildId)
        if (alive) navigate('/projects', { replace: true })
      })
    return () => {
      alive = false
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [buildId])

  /**
   * Send a prompt handed off from another surface (the project composer's Generate, or the
   * planning chat's Launch Builder) as a RELAY turn — never as a build. The interview runs
   * first; a build starts only from the brief card the model returns.
   *
   * Fire-once per chat (`initFiredRef`): a remount (StrictMode, a re-render) must not send the
   * prompt twice. Called from BOTH adopt
   * branches, because the thread is only empty on its very first open and the handoff has to
   * work for the whole life of the project.
   */
  const fireHandoffPrompt = (id: string, isAlive: () => boolean, prior: ChatMessage[]) => {
    if (!initialPrompt) return false
    if (initFiredRef.current === id) return false
    initFiredRef.current = id
    const attachments: PendingAttachment[] = location.state?.pendingAttachments || []
    // STRIP THE HANDOFF FROM HISTORY BEFORE FIRING. `initFiredRef` is a ref, so it only survives
    // within one mount — but a RELOAD is a fresh mount over the SAME history entry, and the
    // browser keeps router state across it. Left in place, every reload of a handed-off thread
    // re-sends the prompt: a duplicate turn, billed again, on a thread the user was only reading.
    window.history.replaceState({}, '', window.location.pathname + window.location.search)
    void fireRelayTurn(initialPrompt, attachments, id, { isAlive, prior })
    return true
  }

  /**
   * RE-ATTACH to a BUILD that is still running (the reload-mid-build clause).
   *
   * `buildActive` is derived from `sessionProjectRef`, which only `handleBuildIt` /
   * `handleRelaunch` stamp — so on a fresh mount NOTHING knows a build is in flight and the one
   * gate is simply absent, exactly in the window it matters most. The transcript knows: the
   * projection emits a `build_in_progress` part (carrying its session id) for a build that began
   * and whose outcome never landed. Stamp the session's chat/project from it and let
   * `session.reattach` settle the three cases it already handles — still live (subscribe +
   * keep-alive: the gate closes, the note shows, the mode pill freezes), already terminal
   * (settles to ended/failed, no subscription, the gate stays open), gone (404, below).
   *
   * The reattach also makes the live bubble supersede the anchor row, which is what stops the
   * past-tense "a build WAS running here" line from narrating a build that is still going.
   */
  const reattachToLiveBuild = (activeId: string, restored: ChatMessage[], isAlive: () => boolean) => {
    let anchor: Extract<MessagePart, { type: 'build_in_progress' }> | null = null
    for (let i = restored.length - 1; i >= 0 && anchor === null; i -= 1) {
      anchor = restored[i].parts.find((p): p is Extract<MessagePart, { type: 'build_in_progress' }> => p?.type === 'build_in_progress') ?? null
    }
    // ARM (a) — NO ANCHOR, which is EVERY ORDINARY CHAT. Spelling this arm out is the difference
    // between a gate and a brick: keying resolution solely on `session.reattach` settling would
    // leave send permanently unavailable in almost every conversation on the platform, because
    // almost none of them has ever had a build in flight.
    if (!anchor?.sessionId) {
      setGateCheck('resolved')
      return
    }
    attachToLiveSession(activeId, anchor.sessionId, isAlive)
  }

  /**
   * Ask the server about one specific live-build anchor, and settle the composer gate on the
   * answer. Split out of `reattachToLiveBuild` so arm (d)'s Retry can re-run exactly this round
   * trip rather than re-deriving the anchor from a transcript it no longer needs to re-read.
   */
  const attachToLiveSession = (activeId: string, sessionId: string, isAlive: () => boolean) => {
    // CC1 — CLASSIFY THE CURRENT SESSION BEFORE OVERWRITING THE REFS. This is a regression of a
    // class this repo already fixed once and wrote down: the build-session learning's rule is
    // "classify the current session before overwriting the refs", because stamping first makes
    // every same-session guard tautological. Here the cost is worse than a tautology —
    // `session.reattach()`'s first act is a synchronous `reset()`, so adopting a SIBLING chat
    // that happens to carry a stale `build_in_progress` anchor tore down the live build's
    // heartbeat and lock renewal for a build belonging to another chat entirely.
    const liveElsewhere =
      session.sessionId != null &&
      session.sessionId !== sessionId &&
      isActiveBuildStatus(session.status)
    if (liveElsewhere) {
      // Someone else's build is genuinely running on this page instance. Leave it alone — and
      // resolve the gate, because we DID get an answer about this chat: no build of its own.
      setGateCheck('resolved')
      gateRetryRef.current = null
      return
    }
    gateRetryRef.current = { activeId, sessionId }
    sessionChatRef.current = activeId
    sessionProjectRef.current = projectIdRef.current
    const settle = () => {
      setGateCheck('resolved')
      gateRetryRef.current = null
    }
    session
      .reattach(sessionId)
      // ARM (b) — the server answered, whichever of its three cases it answered with (still live,
      // already terminal, evicted). Either way the page now knows, so the gate can speak.
      .then(() => {
        if (isAlive() && buildIdRef.current === activeId) settle()
      })
      .catch((err) => {
        // The session is unreachable, so this page owns no session: drop the stamps or every
        // project-scoped surface would render for one that does not exist.
        if (!isAlive() || buildIdRef.current !== activeId) return
        sessionChatRef.current = null
        sessionProjectRef.current = null
        // ARM (c) — a 404 is the ORDINARY outcome (the server restarted, or the five-minute
        // retention lapsed). It is a real answer — there is no build — so it opens the gate and
        // needs no notice.
        if (err instanceof ApiError && err.status === 404) {
          settle()
          return
        }
        // ARM (d) — we genuinely could not ask. The gate stays SHUT rather than guessing over a
        // possibly-live build, and because a shut gate with only a vanishing toast is the dead-end
        // class this whole plan exists to remove, this state renders a persistent Retry.
        setGateCheck('unreachable')
      })
  }

  /**
   * THE TURN TERMINAL, for every path that has one.
   *
   * Clears the streaming flag — but ONLY if this chat still owns it. A turn that terminates after
   * the user has switched away and started a turn in the sibling chat must not clear the sibling's
   * flag, which would open send over a reply that is genuinely still running.
   *
   * And it signals the usage meter (N4). The turn transport never did: `notifyUsageChanged` had
   * exactly one caller, in the retiring relay hook, so the header's token count only ever moved
   * on a page load — a user could spend their whole daily budget watching a number that never
   * changed. Firing it HERE rather than at each of the three call sites is deliberate: every
   * terminal routes through this one function, including the failed and stopped ones, which are
   * billed too and would otherwise leave the meter stale exactly when it matters.
   */
  /** After every turn — a turn that wrote files is exactly when the answer changes, and the
   *  user should see Save light up without having to guess or reload. */
  const settleSaveState = useCallback(
    () => void refreshSaveState(projectIdRef.current),
    [refreshSaveState],
  )

  /**
   * Retract this page's advisory build claim for one chat. A no-op when it holds none.
   *
   * Called from `endGenerating` (a turn settled) AND from the arrival effect (there is no turn
   * to settle). Both are needed because the handoff SPLIT acquire from release: the press
   * claims a chat it is about to navigate to, and the release belongs to whoever ends up
   * watching that chat — which, on an abandoned or already-finished arrival, is nobody.
   */
  const releaseBuildClaim = useCallback((activeId: string) => {
    buildLockRef.current?.release(activeId)
  }, [])

  const endGenerating = useCallback(
    (activeId: string) => {
      setGeneratingChatId((prev) => (prev === activeId ? null : prev))
      // THE ADVISORY CLAIM IS RETRACTED HERE, at the one point every turn path settles through
      // (the send, the reattach, and the reload-mid-build). It used to be released in the
      // build-watcher's `finally`, which worked only while the build ran in the chat that
      // started it; a handoff's build runs in a chat this page ARRIVES at, so the claim has to
      // be dropped by whatever ends up watching the turn rather than by whoever pressed the
      // button. A release for a conversation holding no claim is a no-op, so this is safe on
      // every ordinary reply too — and a claim nobody retracts blocks the user's next build
      // until they close the tab, which is the failure worth being generous about.
      releaseBuildClaim(activeId)
      notifyUsageChanged()
      settleSaveState()
    },
    [releaseBuildClaim, settleSaveState],
  )

  /** Arm (d)'s way out: re-run the same adopt round-trip that could not be completed. */
  const retryGateCheck = () => {
    const pending = gateRetryRef.current
    if (!pending || pending.activeId !== buildIdRef.current) return
    setGateCheck('checking')
    attachToLiveSession(pending.activeId, pending.sessionId, () => true)
  }

  /**
   * The ONE turn-frame reducer, shared by both stream consumers — the send path below and the
   * mid-turn RE-ATTACH on reload. A frame must not be interpreted two different ways depending
   * on which consumer happened to open the socket; `sink` carries the mutable accumulators
   * (the turn's ordered `parts`, the reasoning flag, the terminal status) back out to the caller.
   */
  const turnFrameHandler = useCallback((activeId: string, assistantId: string, sink: TurnSink) => {
    // A NEW OBJECT FOR THE CHANGED MESSAGE, IDENTITY PRESERVED FOR EVERY OTHER — the runtime
    // caches the conversion on object identity, so an in-place mutation is invisible and the
    // transcript simply never re-renders (convertMessage trap 4). `.map` gives exactly that.
    const paint = () =>
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantId ? { ...m, parts: streamingParts(sink) } : m)),
      )
    return (frame: TurnFrame) => {
      if (buildIdRef.current !== activeId) return // navigated away — drop the frame
      // UnknownFrame's index signature defeats discriminated-union narrowing on `frame.type`
      // below; narrow to the known-frame union first (an unknown frame type matched none of
      // the arms anyway, same as before).
      if (!isKnownFrame(frame)) return
      if (frame.type === 'text_delta') {
        appendText(sink, frame.text, frame.newBlock)
        paint()
      } else if (frame.type === 'working') {
        sink.working = frame.working
        paint()
      } else if (frame.type === 'snapshot') {
        // A snapshot of an ALREADY-SETTLED turn is itself terminal: the `error`/`turn_ended`
        // frames it consolidates may have been evicted from the ring, so treating only those
        // as terminal left a failed turn looking like it was still thinking, with no reason
        // shown. The snapshot carries the reason for exactly this case.
        if (frame.turnStatus === 'failed' || frame.turnStatus === 'stopped') {
          sink.terminal = frame.turnStatus
          if (frame.errorMessage) setTurnError(frame.errorMessage)
        } else if (frame.turnStatus === 'completed') {
          sink.terminal = 'completed'
        }
        // The catch-up half of the Write narrative. A `workspace`/`preview` frame that fired
        // before this tab connected is gone from the server's ring, so the snapshot is the
        // only thing left that can answer — which is what saves a mid-build reload from a
        // second REST round-trip, and from an empty preview pane over a running app.
        if (frame.turnId) {
          liveTurnIdRef.current = frame.turnId
          sink.turnId = frame.turnId
        }
        if (frame.workspaceState) setTurnWorkspace({ state: frame.workspaceState, message: null })
        // Only when the server actually said something. A snapshot with no compile fact must
        // leave whatever the live tail already established alone.
        if (frame.compileState) setTurnCompile(frame.compileState)
        if (frame.previewUrl || frame.previewState) {
          setTurnPreview((prev) => ({
            url: frame.previewUrl ?? prev.url,
            state: frame.previewState ?? prev.state,
          }))
        }
        // The catch-up half of the status: a tab that reattaches mid-thought would otherwise
        // sit on a still screen until the next frame changed something else.
        sink.working = frame.working
        if (frame.parts.length) {
          // THE SNAPSHOT IS THE ORDER, so the sink is REBUILT from it rather than merged into.
          // It is the server's whole account of the turn up to `frame.seq`, and the tail that
          // follows resumes from there — merging would interleave a recovered narrative with a
          // partial local one and get the order wrong in both.
          //
          // Diagnostics are the one thing it cannot know: they arrive as their own frames and
          // are drawn as failed rows, so they are carried across and re-appended rather than
          // dropped. Losing them would silently remove the self-heal narrative from a build
          // that reconnected.
          const diagnostics = sink.parts.filter(
            (part): part is Extract<SinkPart, { kind: 'step' }> =>
              part.kind === 'step' && part.toolCallId.startsWith(DIAGNOSTIC_KEY_PREFIX),
          )
          sink.parts = []
          for (const part of frame.parts) {
            if (part.type === 'text') appendText(sink, part.text, true)
            else putStep(sink, part.toolCallId, part.item)
          }
          for (const part of diagnostics) putStep(sink, part.toolCallId, part.step)
          // THE SINK FIRST, then the state — `paint()` reads the sink, and a snapshot that
          // updated only the state would leave a reloaded tab's whole recovered narrative out of
          // the transcript it is meant to be restoring.
          setTurnSteps((prev) => {
            const next = { ...prev }
            for (const part of frame.parts) {
              if (part.type === 'step') next[part.toolCallId] = part.item
            }
            return next
          })
          paint()
        }
      } else if (frame.type === 'plan_options') {
        setLivePlanOptions(frame.item)
      } else if (frame.type === 'error') {
        setTurnError(frame.message)
        setWorkspaceSays(frame.message)
      } else if (frame.type === 'step') {
        // A Write turn's whole narrative — every file written, every command run — arrives as
        // step frames. KEYED BY TOOL-CALL ID, so the `finished` frame REPLACES its own `started`
        // one in place: appending would stack a spinner beside its own result, and the activity
        // group's live count would climb while the same step re-rendered.
        putStep(sink, frame.toolCallId, frame.item)
        setTurnSteps((prev) => ({ ...prev, [frame.toolCallId]: frame.item }))
        // The transcript is what draws activity now (U6), so a step has to reach the message it
        // belongs to. Without this the group renders nothing until the next text delta happens
        // to repaint — which on a build that writes for minutes before saying anything is the
        // whole build.
        paint()
      } else if (frame.type === 'workspace') {
        setTurnWorkspace({ state: frame.state, message: frame.message ?? null })
        // U2 — `notice`, never `message`. The ordinary lifecycle pair carries a `message`
        // ("Getting your workspace ready…") on EVERY turn, and routing that here would post the
        // phase narration above the composer every time anyone sent anything. A notice is a
        // statement about the app; only the integrity gate sends one.
        if (frame.notice) setWorkspaceSays(frame.notice)
      } else if (frame.type === 'preview') {
        // Keyed on the url: a NEW url remounts the iframe, which is how a restored sandbox
        // gets reloaded rather than left showing a dead frame.
        setTurnPreview((prev) => ({
          url: frame.previewUrl ?? prev.url,
          state: frame.state,
        }))
      } else if (frame.type === 'diagnostic') {
        // NOT `setTurnError`. The turn is not failing — a repair run follows — and showing this as
        // a failure would tell the citizen their build died four times on its way to succeeding.
        setTurnDiagnostics((prev) => [...prev, frame])
        // IT JOINS THE ACTIVITY GROUP AS A FAILED ROW, which is where the self-heal narrative
        // lives now. The deleted card drew diagnostics as their own amber "trying another way"
        // block that vanished at the terminal; the group counts them instead — "4 steps · one
        // problem" — so a citizen reading a finished build still learns it hit something, which
        // the vanishing block could not tell them. Dropping the frame here would have removed the
        // self-heal narrative from the product silently, which is the one disposition not open.
        //
        // ONLY `userMessage` CROSSES. The frame's developer half (source, and the compiler title
        // that rides with it) is exactly what R36's wall exists to keep off the screen; it still
        // travels to the agent, which is the party that can act on it.
        putStep(sink, `${DIAGNOSTIC_KEY_PREFIX}${frame.seq}`, {
          type: 'step',
          seq: frame.seq,
          // The classifier's own vocabulary is for real tool calls; this row is the platform
          // speaking, and `tool` is never rendered (convertPart deliberately drops it).
          tool: 'diagnostic',
          label: frame.userMessage || DIAGNOSTIC_FALLBACK,
          state: 'failed',
          hidden: false,
        })
        paint()
      } else if (frame.type === 'compile') {
        setTurnCompile(frame.state)
      } else if (frame.type === 'quota') {
        setTurnQuota({ limit: frame.limit, used: frame.used, resetsAt: frame.resetsAt })
      } else if (frame.type === 'turn_ended') {
        // A turn that ended while its last act was thinking would otherwise leave the status
        // under a finished turn. The server clears the flag at its own terminal too; this is
        // the half that survives a terminal the ring evicted.
        sink.working = false
        sink.terminal = frame.status
        sink.reason = frame.reason ?? null
        sink.snapshotCommitted = frame.snapshotCommitted ?? null
        sink.turnId = frame.turnId ?? sink.turnId
        setTurnTerminal(frame.status)
        liveTurnIdRef.current = null
        if (frame.previewUrl) setTurnPreview({ url: frame.previewUrl, state: 'ready' })
      }
    }
  }, [])

  /**
   * RE-ATTACH to a turn that is still running server-side (R8's live clause). A reload mid-turn
   * lands on a transcript whose newest row is the user's message: the reply is being generated,
   * but this tab has no socket, so the page would sit static and the next send would 409.
   *
   * Deliberately subscribes with NO cursor even though the server reports `lastSeq`. That seq
   * counts frames THIS tab never received — passing it would put the server in tail-only replay
   * and the already-streamed prefix would be lost. Cursor-0 gets the consolidating snapshot,
   * whose ordered `parts` are exactly the turn so far — prose and steps in the order they were
   * produced, so a reattached citizen reads the same turn as one who never left.
   */
  const reattachToTurn = async (activeId: string, activeTurn: { turnId: string; lastSeq: number }, isAlive: () => boolean) => {
    const stillHere = () => isAlive() && buildIdRef.current === activeId
    const assistantSeq = seqRef.current
    seqRef.current += 1
    const assistantId = `local_${Date.now()}_r`
    const sink = newSink()
    setGeneratingChatId(activeId)
    setTurnError(null)
    resetTurnNarrative()
    setMessages((prev) => [...prev, { id: assistantId, role: 'assistant', parts: [{ type: 'text', text: '' }], seq: assistantSeq, createdAt: new Date().toISOString() }])

    streamAbortRef.current?.abort()
    const controller = new AbortController()
    streamAbortRef.current = controller
    const onFrame = turnFrameHandler(activeId, assistantId, sink)
    let outcome: StreamOutcome
    try {
      outcome = await readTurnStream({ conversationId: activeId, turnId: activeTurn.turnId, cursor: 0, signal: controller.signal, onFrame })
      // CC4 — RESUME ONCE, exactly as `fireRelayTurn` does. This path mapped any throw to
      // 'truncated' and stopped there, so a single dropped socket after a reload — the ordinary
      // case this function exists to serve — surfaced as "the connection dropped" on a turn that
      // was still happily running server-side. One resubscribe consolidates the turn so far via
      // the server snapshot then tails to the end; a SECOND truncation is a real drop, and the
      // reload advice below is then honest. (The streamed-reply learning's resume-once rule.)
      if (outcome === 'truncated' && !sink.terminal && !controller.signal.aborted) {
        outcome = await readTurnStream({ conversationId: activeId, turnId: activeTurn.turnId, cursor: 0, signal: controller.signal, onFrame })
      }
    } catch {
      outcome = 'truncated'
    }
    endGenerating(activeId)
    if (!stillHere()) return
    if (outcome === 'stalled') setTurnError('The reply stalled. Reload to catch up.')
    else if (outcome === 'truncated' && !sink.terminal) setTurnError('The connection dropped. Reload to catch up.')
    if (sink.terminal !== 'completed' && sink.parts.length === 0) {
      setMessages((prev) => prev.filter((m) => m.id !== assistantId))
      seqRef.current = assistantSeq
    }
    markInterrupted(assistantId, sink.terminal)
    announceTerminal(sink)
    refreshBuilds()
  }

  /**
   * One relay turn (U7): create-or-confirm the thread → stream — the SERVER persists both
   * sides of the turn before its terminal [DONE] (write-before-DONE), so this page appends
   * nothing.
   *
   * THIS IS THE ONLY THING A SEND DOES. It never starts a build — the routing rule (KTD): every
   * composer send goes to the relay, and builds fire ONLY from a brief card's confirmation, first
   * build and iteration alike. The direct-fire send this page used to do is what made the agent
   * silently guess at a vague prompt.
   *
   * Create-before-stream is load-bearing: the stateless relay 404s an unknown conversation,
   * and the row is what carries the project parentage + context that ground the first turn.
   *
   * ══ `onSent` IS THE MOMENT THE COMPOSER MAY EMPTY, AND IT IS NOT THE END OF THE TURN ══
   *
   * It fires when the server has the message — the row created, or the turn about to stream on an
   * existing thread — not when the reply finishes. That distinction is the whole of R58: waiting
   * for the terminal would leave a citizen's text sitting in the composer for the several minutes
   * a build runs, and clearing before it would lose the text on every refusal. `onAbort` is its
   * opposite number and fires on every path that leaves the server holding nothing.
   */
  const fireRelayTurn = async (
    rawText: string,
    attachments: PendingAttachment[],
    activeId: string,
    { isAlive = () => true, onAbort, onSent, prior }: {
      isAlive?: () => boolean
      onAbort?: () => void
      onSent?: () => void
      prior?: ChatMessage[]
    } = {},
  ) => {
    const text = rawText.trim() || (attachments.length ? 'Please review the attached file(s).' : '')
    if (!text) return

    const stillHere = () => isAlive() && buildIdRef.current === activeId

    let parts
    try {
      parts = await buildUserParts(text, attachments)
    } catch (err) {
      // ABORT — never fall through to a turn that silently forgets the attachment (R3). The user
      // attached a spreadsheet; answering as if they hadn't is the wrong-build bug in miniature.
      setUrgent(err instanceof Error ? err.message : 'Could not upload the attachment. Please try again.')
      if (stillHere()) onAbort?.()
      return
    }
    if (!stillHere()) return // switched chats mid-upload — abandon, don't clobber the new chat

    // `prior` is passed by the handoff, which fires in the same tick as the `setMessages` that
    // restores the transcript — `messagesRef` is only refreshed on the next render, so reading it
    // here would see the PRE-restore array and the handoff would overwrite the thread it just
    // loaded. Every other caller sends from a settled render and reads the ref.
    const priorMessages = (prior ?? messagesRef.current).filter((m) => !m.ephemeral)
    const userSeq = seqRef.current
    seqRef.current += 1
    const userMsg: ChatMessage = { id: `local_${Date.now()}`, role: 'user', parts, seq: userSeq, createdAt: new Date().toISOString() }
    setMessages([...priorMessages, userMsg])

    // R-18 — THE ROW'S PARENTAGE RIDES THE TURN, AND THERE IS NO SEPARATE CREATE CALL.
    //
    // This used to be a `createBuild` round trip: the conversation was COMMITTED here, a full
    // request before the turn — and that route's only workspace awareness was a project-ownership
    // check. So a first message the workspace then refused left a real, titled, empty conversation
    // in the project's list, named by `deriveTitle` after the very text that had been refused.
    // Observed live: a citizen submitted a build, watched it run for nearly two minutes, and was
    // then asked whether they wanted the workspace at all.
    //
    // The server now creates the row inside the turn's own transaction, AFTER every side-effect-free
    // refusal and with a flush rather than a commit — so a refusal rolls it back. The whole change
    // on this side is that one round trip is gone and its arguments moved onto the next one.
    const derivedTitle = userSeq === 0 && projectId ? deriveTitle(partsToText(parts)) : null
    const parentage =
      userSeq === 0 && projectId
        ? {
            projectId,
            kind,
            title: derivedTitle ?? '',
            // `context` is whatever this surface was seeded with; it travels unchanged.
            context: contextRef.current,
          }
        : undefined
    // OPTIMISTIC, AND DELIBERATELY SO. The row is created inside the turn's own transaction and a
    // refusal rolls it back, so this can name a chat that never came to exist. That is the right
    // trade for a heading: the board draws the title the moment the message is sent, and a chat
    // whose creation was refused is one the citizen is being told about in the same breath.
    if (derivedTitle) onTitleDerived?.(derivedTitle)
    dropTransientQuery(activeId)
    refreshBuilds()

    const assistantSeq = seqRef.current
    seqRef.current += 1
    const assistantId = `local_${Date.now()}_a`
    const sink = newSink()
    setGeneratingChatId(activeId)
    setTurnError(null)
    resetTurnNarrative()
    setMessages((prev) => [...prev, { id: assistantId, role: 'assistant', parts: [{ type: 'text', text: '' }], seq: assistantSeq, createdAt: new Date().toISOString() }])

    // U13: the turn API (U10). POST starts the turn DETACHED server-side; the subscription
    // below only observes — closing the tab never cancels the reply, and the server
    // persists both sides before the terminal (write-before-DONE).
    const wire = wireMessageFromParts(parts)
    // Did the server ACCEPT the turn? `startTurn` resolving (202) means the user's message is
    // persisted and the reply runs detached regardless of what this tab does next — so the
    // catch below must split on it: everything after the accept is subscription plumbing.
    let posted = false
    try {
      await startTurn(
        activeId,
        {
          text: wire.text ?? '',
          attachmentTexts: wire.attachmentTexts ?? [],
          attachmentIds: wire.attachmentIds ?? [],
        },
        {},
        // R-18: present only on a first message, and the whole reason this call can now be the
        // ONE server call the send path makes.
        parentage,
      )
      posted = true
      // THE ONE PLACE THE COMPOSER MAY EMPTY, and it is here because this is the first instant the
      // server is holding the message. A 202 means it is persisted and the reply runs detached, so
      // the citizen's text has somewhere to live other than the box they typed it in.
      //
      // IT USED TO FIRE EARLIER — on row creation, or on nothing at all for a continuing thread —
      // which is every message after the first. The promise can only settle once, so the composer
      // had already emptied by the time `startTurn` refused; the `onAbort` below then rejected an
      // already-resolved promise and did nothing, and the text and files were gone. That is the
      // 429-over-the-daily-cap path, which is exactly when losing the message is least forgivable.
      onSent?.()
      streamAbortRef.current?.abort()
      const controller = new AbortController()
      streamAbortRef.current = controller
      const onFrame = turnFrameHandler(activeId, assistantId, sink)
      let outcome = await readTurnStream({ conversationId: activeId, signal: controller.signal, onFrame })
      if (outcome === 'truncated' && !sink.terminal && !controller.signal.aborted) {
        // A dropped socket before the terminal: one resubscribe consolidates the turn so far
        // via the server snapshot then tails to the end (resume-once). A second truncation is
        // a real drop — reload is the honest fallback.
        outcome = await readTurnStream({ conversationId: activeId, signal: controller.signal, onFrame })
      }
      if (outcome === 'stalled') setTurnError('The reply stalled. Reload to catch up.')
      else if (outcome === 'truncated' && !sink.terminal) setTurnError('The connection dropped. Reload to catch up.')
    } catch (err) {
      // Whether the reclaim DIALOG has taken ownership of settling the composer's promise: its
      // retry closure carries the same `onSent`/`onAbort`, so settling here as well would reject a
      // promise the retry is still going to resolve, and a successful retry would then leave the
      // citizen's text sitting in a composer that never emptied.
      let reclaimOwnsRetry = false
      if (stillHere()) {
        if (posted) {
          // The turn was ACCEPTED — only the subscribe after it broke. The user's message is
          // persisted and the reply is being produced detached, so the N8 rollback below
          // would lie in the other direction: "could not be sent" over a message the server
          // already has invites a duplicate resend. Keep the user's bubble (it IS in the
          // database), drop only the empty reply placeholder, and point at reload — the
          // transcript there will have both sides of the turn.
          setTurnError('Your message was received, but this page lost the reply. Reload to catch up.')
          setMessages((prev) => prev.filter((m) => m.id !== assistantId))
          seqRef.current = assistantSeq
        } else {
          // #83 — a refusal the user can ACT on, not a failure. The rollback below is right
          // either way (a refused turn persisted nothing), but the dead-end banner is not:
          // the way through is one click, and the retry closure re-sends the text they wrote
          // rather than making them type it again.
          const handled = captureReclaim(err, () =>
            fireRelayTurn(rawText, attachments, activeId, { isAlive, onAbort, onSent, prior }),
          )
          reclaimOwnsRetry = handled
          if (!handled)
            setTurnError(err instanceof TurnStartError ? err.message : 'The message could not be sent. Try again.')
          // N8 — ROLL BACK BOTH BUBBLES, not just the assistant's. A `startTurn` that threw
          // means the server persisted NOTHING: the user's message was refused at the door
          // (429 over the cap, 409 busy, 503 unconfigured). Leaving their bubble on screen
          // showed a transcript that disagreed with the database — the message looked sent,
          // survived until reload, and then vanished. Roll the whole optimistic pair back and
          // let the error banner be the only account of what happened.
          setMessages((prev) => prev.filter((m) => m.id !== assistantId && m.id !== userMsg.id))
          seqRef.current = userSeq
          // CARRIED OVER FROM THE RETIRED CREATE CALL (R-18, U13), because the failures it
          // handled did not go away with it — they moved one round trip later. A refused first
          // message persisted nothing, so the blobs uploaded for it have no owner and must be
          // released, or a citizen who is refused three times leaves three orphans behind.
          releaseUploadedAttachments(parts)
          // …and a conversation deleted out from under this tab is a page whose every send will
          // fail the same way. Leaving is the only useful thing left to do.
          if (isConversationGone(err)) navigate('/projects', { replace: true })
        }
      }
      // TELL THE COMPOSER TO KEEP EVERYTHING, on every path out of here. A refused turn persisted
      // nothing, so the citizen's text and staged files must survive — the 429-over-the-daily-cap
      // path, and exactly when losing the message would be least forgivable.
      //
      // OUTSIDE `stillHere()`, because settling is not a rendering decision. An unsettled promise
      // never runs `handleSubmit`'s `finally`, so `sendingRef` keeps naming this chat and every
      // later press there matches the double-Enter guard and returns as though it had sent.
      //
      // `posted` is the one case that must NOT abort: the server has the message, `onSent` already
      // fired above, and this is only the subscription breaking afterwards.
      if (!posted && !reclaimOwnsRetry) onAbort?.()
      endGenerating(activeId)
      return
    }
    endGenerating(activeId)

    if (stillHere()) {
      if (sink.terminal !== 'completed' && sink.parts.length === 0) {
        // Failed/stopped with nothing streamed — drop the empty bubble; the error banner
        // (or the stopped state) is the feedback.
        setMessages((prev) => prev.filter((m) => m.id !== assistantId))
        seqRef.current = assistantSeq
      }
      markInterrupted(assistantId, sink.terminal)
      announceTerminal(sink)
      refreshBuilds()
    }
  }

  /**
   * Show what a build turn produced, the moment it ends.
   *
   * THE SERVER OWNS THE DURABLE WRITE (`services/build_sessions/outcome.py`), not this. Builds
   * take minutes and users close tabs, and an in-memory session is evicted five minutes after its
   * terminal — so a portal-written record would be missing for exactly the users a permanent
   * record serves. The thing that always knows a build finished is the thing that finished it.
   *
   * This renders the same outcome LOCALLY so the watching user sees it immediately rather than
   * waiting for a reload. On reload the server's row takes its place, identically.
   *
   * The local message is NOT persisted and its `seq` is display shape only — this page does not
   * try to predict which slot the server took. It cannot: the server writes while this tab may be
   * reloading, backgrounded, or closed, and a guess that is wrong is not a visible error but a
   * lost message. Allocation is the server's alone (`_free_seq` in the conversations router); this
   * page re-seeds `seqRef` from what each append reports it actually stored.
   */
  const showBuildOutcome = useCallback((outcome: Omit<BuildPartLive, 'type'>) => {
    // WHICH BUILD this describes. A build is a turn now, so the identity is `turnId`; a legacy
    // session outcome still keys on `sessionId`. Taking only `sessionId` — as this did — meant
    // every turn build deduped on `undefined`: the first one registered `undefined` in the Set,
    // and every build after it on the same page instance was silently swallowed. Worse, the
    // transcript scan below then matched ANY stored build part that lacked a session id, so a
    // genuinely new outcome could be suppressed by an unrelated older one.
    const buildKey = outcome.turnId ?? outcome.sessionId ?? null
    // No identity, no dedupe — and no card either. Rendering an outcome we cannot recognise
    // again guarantees a duplicate on the next terminal, which is the louder wrong.
    if (buildKey === null || outcomeWrittenRef.current.has(buildKey)) return
    // After a reload the transcript already holds the server's row, and a replayed terminal
    // would otherwise stack a second copy on top of it. `_id`/seq say nothing about WHICH build
    // a part describes; the turn (or session) id is the only thing that does.
    const already = messagesRef.current.some((m) =>
      m.parts.some((p) => {
        if (p?.type !== 'build') return false
        // `turnId` only exists on BuildPartLive; `sessionId` is REQUIRED on BuildPartPersisted
        // (its only identity) and optional on BuildPartLive. Do not simplify this to a bare
        // `p.sessionId` read — the `in` checks are load-bearing for the persisted/reload shape,
        // not defensive filler; dropping them silently breaks dedupe on reloaded threads.
        const key = ('turnId' in p ? p.turnId : undefined) ?? ('sessionId' in p ? p.sessionId : undefined) ?? null
        return key === buildKey
      }),
    )
    outcomeWrittenRef.current.add(buildKey)
    if (already) return

    // The summary text part mirrors what the server writes, so the local render and the reloaded
    // row read identically (`outcome.py::_summary` is the other half of this pair).
    const parts: MessagePart[] = [{ type: 'text', text: outcomeSummary(outcome) }, { type: 'build', ...outcome }]
    setMessages((prev) => [...prev, { id: `local_${Date.now()}_b`, role: 'assistant', parts, seq: seqRef.current, createdAt: new Date().toISOString() }])
    refreshBuilds()
  }, [refreshBuilds])

  /**
   * THE OUTCOME CARD, drawn by whatever watched the turn.
   *
   * It used to be drawn by the build-watcher `Build it` started, which is gone: a build runs in
   * a chat this page ARRIVES at now, so the only thing that can announce it is whatever ends up
   * watching the turn there. BOTH watchers on this page call this — an ordinary send, and the
   * reattach a reload or a handoff arrival takes — because on a build chat every turn IS a
   * build. One helper rather than two copies, because the two watchers announcing the same
   * turn differently is the failure this is guarding against.
   *
   * `showBuildOutcome` dedupes on the turn id and scans the transcript first, so a reload that
   * already holds the server's own row renders nothing here.
   */
  const announceTerminal = useCallback(
    (sink: TurnSink) => {
      if (!sink.terminal) return
      showBuildOutcome({
        status: sink.terminal === 'completed' ? 'ended' : 'failed',
        turnId: sink.turnId ?? undefined,
        previewUrl: turnPreviewRef.current.url,
        endedAt: new Date().toISOString(),
        snapshotCommitted: sink.snapshotCommitted,
        reason: sink.reason,
      })
    },
    [showBuildOutcome],
  )

  /**
   * Watch the live session for its terminal and surface the outcome once.
   *
   * Reads the C7 `ended` envelope from the feed store for the authoritative detail
   * (`snapshot_committed` is only true on the SESSION-API frame — plan 002-U7 — so an
   * envelope-less terminal must not claim otherwise). Force-end and keep-alive reclaim reach a
   * terminal with NO `ended` envelope at all, so the status enum is the fallback.
   */
  useEffect(() => {
    const sid = session.sessionId
    const activeId = sessionChatRef.current
    if (!sid || !activeId) return
    if (session.status !== 'ended' && session.status !== 'failed') return
    // Only the thread that OWNS this session shows it, and only while we are viewing it.
    if (activeId !== buildIdRef.current || sessionProjectRef.current !== projectId) return

    const ended = session.envelopes.find((e) => e.type === 'ended')
    showBuildOutcome({
      status: session.status,
      sessionId: sid,
      previewUrl: session.previewUrl ?? null,
      endedAt: new Date().toISOString(),
      // UNKNOWN, not false. `finishSession('ended')` closes the feed the moment the stop HTTP call
      // resolves, so the real `ended` frame — which for a graceful stop says snapshot_committed:
      // true, because `_do_finalize` DID snapshot — may never be dispatched here. Collapsing that
      // into `false` warned the user their code wasn't saved about a build that saved it. The card
      // warns only on an explicit `false`, and the server's row (which always carries the real
      // value) replaces this one on reload.
      snapshotCommitted: ended?.snapshot_committed ?? null,
      reason: ended?.reason ?? null,
    })
    // A HEADER RE-READ USED TO LIVE HERE, and its disappearance is the point rather than an
    // omission. The composer re-opens at this line, and the thing above it that had to be
    // telling the truth was a pill naming a per-thread setting the server moved on the citizen's
    // behalf at the end of a build. Nothing moves any more: what this chat is was decided when
    // it was created, so there is no server-side answer to go and fetch, and no window in which
    // the page could be showing a mode the next send does not run in.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.status, session.sessionId, projectId])

  /**
   * The advisory cross-tab pre-check message, or null when the coast is clear. Checked before a turn
   * is persisted so the user keeps their draft; the AUTHORITATIVE barrier is C3 start's 409 (KTD-7).
   */
  const buildBlockedMessage = useCallback(
    (conversationId: string) => {
      if (!projectId) return null
      const blocker = buildLockRef.current?.blockedBy(projectId, conversationId)
      if (!blocker) return null
      const other = builds.find((b) => b.id === blocker.conversationId)
      const which = other?.title ? `“${other.title}”` : 'another build chat'
      return `${which} is already building this project. Only one build runs at a time — wait for it to finish, or stop it first.`
    },
    [projectId, builds],
  )

  /**
   * A composer send is ALWAYS a chat turn — never a build.
   *
   * That is the routing rule: the model decides when there is enough to build and says so with an
   * offer; the citizen presses it. A post-build "add a chart" goes down this exact path too.
   *
   * ══ IT RESOLVES ON CONFIRMATION AND REJECTS ON FAILURE, AND THAT IS THE CONTRACT ══
   *
   * The composer clears its text and its staged files ONLY when this resolves. So every refusal
   * below must THROW rather than return quietly: returning would look like success and empty a
   * composer whose message never went anywhere. That inverts the old shape, where this function
   * returned after a toast and cleared the draft through an `onSent` callback — the callback is
   * gone, and "the send succeeded" is now one thing (this promise) instead of two that could
   * disagree.
   *
   * ══ THE ONE GATE, ENFORCED HERE AND ONLY HERE ══
   *
   * The composer's attributes are the AFFORDANCE: `aria-disabled` deliberately keeps Send
   * focusable (KTD-2), the textarea is never disabled at all (KTD-3), and Enter calls straight
   * through. So every arm has to be re-checked at the one place that actually starts a turn.
   */
  const handleSubmit = async ({ text: rawText, attachments, conversationId }: ComposerSubmission) => {
    const text = rawText.trim()
    if (!text && attachments.length === 0) return
    if (buildActiveHere || buildStarting) {
      throw new SendRefusal('Your app is being built — send unlocks when it finishes. Keep typing meanwhile.')
    }
    if (generating) {
      throw new SendRefusal('Send unlocks when the current reply finishes. Keep typing meanwhile.')
    }
    if (gateCheck !== 'resolved') {
      throw new SendRefusal(
        gateCheck === 'unreachable'
          ? 'Still can’t tell whether a build is running here. Try the Retry above.'
          : 'Just checking whether a build is running here — one moment.',
      )
    }
    // U24 — the same arm the display gate carries. The composer already refuses on it, so this is
    // defence in depth rather than the only guard; it is here because the sentence above promises
    // that EVERY reason is re-checked at the one place a turn actually starts, and an arm that
    // lives in only one of the two is how the two come to disagree.
    if (atLimit) throw new SendRefusal(atLimit.title)
    // Synchronous, and a REF rather than state: the two keydowns of a fast double-Enter land in the
    // SAME tick, so `generating` — set after an await — is still false for the second one. The
    // draft is deliberately held until the server confirms the turn, so that second read sees the
    // very same text and fires a second relay turn: a duplicate persisted message, a duplicate
    // model call, and two offers for one request.
    //
    // SILENTLY — the composer says nothing for it. This is one keystroke burst, not a second
    // intention, and an error sentence for a press the citizen did not knowingly make is noise.
    //
    // IT THROWS RATHER THAN RETURNING, and the difference is the whole point: returning resolved
    // the promise, and the composer empties itself on a resolve. So the second press cleared the
    // text and files while the FIRST send was still in flight — and if that one then failed, the
    // message it was still holding was already gone. `SUPERSEDED` is the one rejection the
    // composer swallows without a word; the first press still owns the outcome and the clearing.
    if (sendingRef.current === buildIdRef.current) throw new SendRefusal('', { silent: true })
    // Project-first: a thread REQUIRES a project (no lazy Default — never reintroduce).
    if (!projectId) throw new SendRefusal('Open a project to start a build.')
    if (attachments.length > 0) {
      const cap = validateConversationAttachmentCap(countAttachments(messages), attachments.length)
      if ('error' in cap) throw new SendRefusal(cap.error)
    }

    // R60 — THE CHAT THE COMPOSER STAMPED AT PRESS TIME, not whichever one is open when this
    // settles. A reader who moves to a sibling chat mid-send must not have their message land in
    // the one they are now looking at.
    const sendChatId = conversationId
    sendingRef.current = sendChatId
    try {
      // RESOLVE WHEN THE SERVER HAS THE MESSAGE, not when the reply finishes — see
      // `fireRelayTurn`'s `onSent`. Awaiting the whole turn would hold the citizen's text in the
      // composer for the minutes a build takes; rejecting on `onAbort` is what keeps it there on
      // every path that leaves the server holding nothing.
      //
      // The rejection reason is deliberately EMPTY of copy: `fireRelayTurn` has already said what
      // went wrong, in the banner, with the server's own words. The composer's catch only needs
      // to know that it must not empty itself.
      await new Promise<void>((resolve, reject) => {
        void fireRelayTurn(text, attachments, sendChatId, {
          onSent: resolve,
          onAbort: () => reject(new Error('send-aborted')),
        })
      })
    } finally {
      // Release only OUR claim: a send begun in the chat the user has since switched to must not
      // have its double-Enter guard cleared by this one settling late.
      if (sendingRef.current === sendChatId) sendingRef.current = null
    }
  }

  /**
   * WATCHING A BUILD FROM THE CHAT IT WAS PRESSED IN USED TO LIVE HERE, and it is gone because
   * the build no longer runs in this chat. `Build it` creates a SECOND conversation, seeded with
   * the plan, and the turn belongs to that one: subscribing to it from here would stream one
   * chat's frames into another chat's transcript and persist nothing, and the assistant bubble
   * it seeded would be a permanent artefact of a reply that was never made here.
   *
   * The subscription is not lost, it MOVED. This sends the citizen to the new chat, and the
   * surface's own hydration attaches to whatever turn is live on the chat it opens
   * (`reattachToTurn`, via the `activeTurn` the read projection carries) — which is the same
   * path a reload mid-build has always taken, and the only one that survives closing the tab.
   *
   * ══ THE NEW CHAT'S ID ARRIVES; IT IS NO LONGER MINTED HERE (U16, D4) ══
   *
   * The offer strip mints it, once per press-session, and hands it over — so a double press and
   * a retry carry the same id and collide on the primary key, and the server answers with the
   * chat that already exists. This used to hold its own `mintedBuildChatRef` keyed by tool-call
   * id; two mints for one press would be two build chats for one plan.
   *
   * ══ A REFUSAL IS SAID HERE, VERBATIM, AND THIS DOES NOT THROW FOR IT ══
   *
   * Each refusal has a different remedy and only the server knows which one this is: another
   * chat holds the workspace, there is nowhere to build, the daily cap is spent, the offer
   * carried no usable plan. So the server's own sentence goes to the urgent slot and the promise
   * RESOLVES — the strip's own `catch` would replace four remedies with one generic apology. The
   * strip is left pressable either way, which is what R29 asks for.
   */
  const handleBuildIt = useCallback(async (handoff: BuildHandoff) => {
    const { toolCallId, newChatId } = handoff
    if (sendingRef.current === buildIdRef.current) return
    if (!projectId) {
      setUrgent('Open a project to start a build.')
      return
    }
    // Non-null: the adopt effect sets buildIdRef.current on mount, before any offer can render.
    const activeBuildId = buildIdRef.current as string
    const blocked = buildBlockedMessage(activeBuildId)
    if (blocked) {
      setUrgent(blocked)
      return
    }
    sendingRef.current = activeBuildId
    // The gate closes on the CLICK, not on the server's answer: `buildFromPlan` is a full
    // round-trip (lock acquire + sandbox provision) and the build is already the user's
    // answer for every second of it.
    setBuildStartingChatId(activeBuildId)
    setUrgent(null)
    try {
      const session = sessionRef.current
      const sessionLive = isActiveBuildStatus(session.status) && session.sessionId != null
      if (sessionLive && sessionProjectRef.current !== projectId) {
        setUrgent('You already have a build running in another project. Stop it before starting one here.')
        return
      }
      if (sessionLive) {
        // The refine loop: end THIS project's live session gracefully before the fresh
        // build (the server would reap through it anyway; a courteous stop keeps its
        // snapshot + terminal clean).
        const stopped = await session.stop()
        if (!stopped) {
          setUrgent(session.error || 'Could not stop the running build — try again.')
          return
        }
      }
      const outcome = await buildFromPlan(activeBuildId, toolCallId, newChatId)
      setPlanOverrides((prev) => ({ ...prev, [toolCallId]: 'build' }))
      // THE CROSS-TAB CLAIM, made for the chat that will actually hold the workspace — not for
      // this one, which is about to stop being where anything happens. Advisory only: the
      // server's unconditional one-slot preflight is the real barrier and answers 409
      // `already_building_here`; this is the warning a sibling tab gets BEFORE that round-trip.
      buildLockRef.current?.acquire(projectId, outcome.chatId)
      // AND THEN LEAVE. The build runs in the chat the server just created, not in this one, so
      // the only place its narrative can honestly be watched is there. The new chat's own
      // hydration picks up the live turn on arrival.
      navigate(`/chat/${outcome.chatId}`)
    } catch (err) {
      // THE THIRD DOOR, and it could not open the dialog at all (plan 006, U5).
      //
      // A reclaim refusal arriving here rendered as plain red text — no Save, no Switch, no way to
      // act — even though the error shape is identical to the composer path's and the classifier
      // was already imported in this very file. Two paths reached the one dialog and the third did
      // not, so "nothing can release another project's workspace without the person pressing one
      // of the dialog's buttons" was simply false on this one.
      //
      // R94's widening makes it fire far more often than it used to: a clean incumbent now raises
      // where it previously reclaimed silently, so this path went from rare to ordinary.
      if (captureReclaim(err, () => handleBuildItRef.current(handoff))) return
      // Verbatim, and NOT rethrown — see the docblock. The strip's own catch would collapse four
      // different remedies into one generic apology.
      setUrgent(err instanceof Error ? err.message : 'The build could not be started. Try again.')
    } finally {
      if (sendingRef.current === activeBuildId) sendingRef.current = null
      setBuildStartingChatId((prev) => (prev === activeBuildId ? null : prev))
    }
    // `captureReclaim` and the retry ref are both read through refs / stable identities, so they
    // are deliberately absent here: listing them would rebuild this callback on every keystroke
    // for no behavioural gain.
  }, [projectId, buildBlockedMessage, navigate])

  // THE RETRY THE DIALOG RUNS, through a ref so the callback can name itself without a cycle.
  // Assigned during render rather than in an effect: a refusal can arrive on the very first press,
  // and a ref filled by an effect would still be empty when the dialog needs its retry.
  const handleBuildItRef = useRef(handleBuildIt)
  handleBuildItRef.current = handleBuildIt

  /** An offer's effective state: the stored record, overlaid with this surface's just-made choice. */
  const applyPlanOverride = useCallback(
    (item: PlanOptionsItem): PlanOptionsItem => {
      const override = planOverrides[item.toolCallId]
      if (!override) return item
      return { ...item, state: override }
    },
    [planOverrides],
  )

  /**
   * "Keep planning" — answer the tool call with `refine`, which hands the composer straight back.
   *
   * ANSWERING IS THE WHOLE POINT, not a bookkeeping detail: an open tool call blocks the
   * conversation, so a typed message while one is pending would be rejected by the server. That
   * is why the strip has two buttons rather than one, and why this one has to reach the server
   * before the composer opens.
   */
  const handleKeepPlanning = useCallback(
    async (toolCallId: string) => {
      const activeId = buildIdRef.current
      if (!activeId) return
      await resolvePlanOptions(activeId, toolCallId)
      setPlanOverrides((prev) => ({ ...prev, [toolCallId]: 'refine' }))
      setLivePlanOptions((prev) => (prev && prev.toolCallId === toolCallId ? null : prev))
    },
    [],
  )

  /**
   * THE OFFER ON THE COMPOSER (U16) — the newest `present_plan_options` call, live or stored.
   *
   * `livePlanOptions` is the frame this turn just streamed; the transcript's own newest one is
   * what a reload finds. The live one wins because it is the newer of the two by construction,
   * and the stored row replaces it identically on the next hydration.
   *
   * A SPENT OFFER STILL RENDERS AND STAYS PRESSABLE (D2). Only a PENDING one blocks the composer,
   * which is what "only one offer is live" actually means.
   */
  const offer = useMemo(() => {
    if (livePlanOptions) return applyPlanOverride(livePlanOptions)
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const part = (messages[i].parts || []).find(
        (p): p is Extract<MessagePart, { type: 'plan_options' }> => p?.type === 'plan_options',
      )
      if (part) return applyPlanOverride(part.item)
    }
    return null
  }, [messages, livePlanOptions, applyPlanOverride])

  // Reset the terminal banners so the operator can start fresh (Start-again / Dismiss).
  const handleStartAgain = () => {
    session.reset()
    sessionChatRef.current = null
    sessionProjectRef.current = null
  }

  // The #43 "come back later" journey: after a reload there is no live session, but a persisted
  // BuildOutcome part proves a build once ran — so the preview pane must offer the terminal
  // placeholder (with its Relaunch action), not the idle "submit a prompt" empty state. The
  // NEWEST outcome also drives the "Relaunch last saved version" label when that build failed
  // (paired with the server's restoredFromFailedBuild flag). Derived from the transcript so it
  // survives a reload; a live/reattached session (showSession) always wins below.
  const newestOutcome = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const part = (messages[i].parts || []).find((p) => p?.type === 'build')
      if (part) return part
    }
    return null
  }, [messages])

  /**
   * R7 — A BUILD THAT RAN AND DID NOT SAVE IS NOT A SUCCESS, and the citizen has to know before
   * they build again on top of it.
   *
   * IT MOVED TO THE BANNER RATHER THAN DYING WITH THE OUTCOME CARD. The card read
   * `snapshotCommitted` off the newest build part and printed this line inside itself; U17 deleted
   * the card, and dropping the sentence with it would have removed a warning about lost work —
   * which is the one class of thing this plan's success criterion names by hand.
   *
   * DERIVED FROM THE TRANSCRIPT, so it survives a reload exactly as the card's version did: the
   * server's own row carries `snapshotCommitted`, and a citizen who comes back tomorrow still
   * needs to be told before their next build starts from the wrong code.
   *
   * ONLY AN EXPLICIT `false`. `null` is UNKNOWN — a graceful stop closes the feed before the real
   * terminal arrives — and warning on unknown would tell people their code was thrown away about
   * builds that saved perfectly well.
   */
  const unsavedBuildWarning = useMemo(() => {
    if (!newestOutcome) return null
    const committed = 'snapshotCommitted' in newestOutcome ? newestOutcome.snapshotCommitted : undefined
    return committed === false
      ? 'This build’s code wasn’t saved, so the next build won’t start from it.'
      : null
  }, [newestOutcome])
  /**
   * WHERE THE TURN IN FLIGHT BEGINS — the seq of the newest user message while a turn is
   * streaming here, and `null` otherwise.
   *
   * It exists for ONE case, and only the reattach path can produce it. A reload landing
   * mid-build re-subscribes (`reattachToTurn`) and re-tells the turn so far into a fresh
   * assistant message, while the transcript it just hydrated ALREADY holds that same turn's
   * persisted step rows. Without a boundary both are drawn and every step appears twice.
   *
   * THE TURN BOUNDARY IS THE USER MESSAGE, not the anchor row this replaced. The old rule keyed
   * on a `build_in_progress` part belonging to the attached legacy SESSION — a narrower question
   * that could not see a turn-stream build at all, and one whose own comment records it going
   * wrong in both directions (suppressing a whole history it then failed to re-tell). A user
   * message is where a turn starts by definition, on both transports and for both kinds.
   *
   * An ordinary send needs none of this: the server has persisted no steps for a turn that has
   * only just started, so the rule finds nothing to drop and costs nothing.
   */
  const liveTurnFromSeq = useMemo(() => {
    if (generatingChatId !== buildId) return null
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].role === 'user') return messages[i].seq ?? null
    }
    return null
  }, [generatingChatId, buildId, messages])

  /**
   * Append one stored step row to the run it continues, PRESERVING OBJECT IDENTITY when nothing
   * about that run has changed.
   *
   * The identity half is not tidiness. The runtime caches each converted message on the message
   * OBJECT (convertMessage trap 4), so a merged row rebuilt on every render would re-convert — and
   * re-render — the whole stored history on every streamed token of the turn happening now. The
   * cache is keyed on the exact ids in the run, so a run that has not changed hands back the same
   * object and the cache holds.
   */
  const mergeStepRun = useCallback((run: ChatMessage, next: ChatMessage): ChatMessage => {
    const parts = [...(run.parts ?? []), ...(next.parts ?? [])]
    // The run's own id names it; every merged part's id rides in the key so a changed run misses.
    const key = `${run.id}|${parts.length}|${next.id}`
    const cached = mergedRunsRef.current.get(key)
    if (cached) return cached
    // The FIRST row's id and seq, because the merged message stands where the run starts —
    // taking the last row's would move the group down the transcript as it grew.
    const merged: ChatMessage = { ...run, parts }
    mergedRunsRef.current.set(key, merged)
    return merged
  }, [])

  /**
   * THE TRANSCRIPT AS THE THREAD RECEIVES IT.
   *
   * Two filters, and no grouping — the grouping is the thread's, via `groupPartByType`, which is
   * most of what U6 bought. What is left here is the two things a renderer cannot decide:
   *
   *  1. A MESSAGE WITH NOTHING TO DRAW IS NOT DRAWN. `convertMessage` maps `build`,
   *     `build_in_progress` and `plan_options` to no part at all — the first two are covered by
   *     the activity group's own terminal handling and the third is the offer, which renders on
   *     the composer. A message made only of those would otherwise reach the thread as an EMPTY
   *     assistant message, and an empty assistant message still draws its action bar: a stray
   *     Copy button floating in a reloaded transcript with nothing above it.
   *
   *     The streaming placeholder — one text part holding `''` — is deliberately KEPT. It looks
   *     droppable and is not: the library appends an optimistic assistant message with an id we
   *     do not control the moment `isRunning` is true and the last message is not an assistant's
   *     (`hasUpcomingMessage`), so dropping it hands identity for the whole turn to the library.
   *     An empty text part renders no element anyway, so keeping it costs nothing on screen.
   *
   *  2. THE TURN IN FLIGHT IS TOLD ONCE. See `liveTurnFromSeq`.
   */
  const transcript = useMemo(() => {
    const kept: ChatMessage[] = []
    for (const msg of messages) {
      const parts = msg.parts ?? []
      // THE WHOLE RE-TOLD TURN, not just its steps. The live message is the authority for the
      // turn in flight — the snapshot's ordered `parts` were built to make that true — so every
      // STORED row it is re-telling has to go, prose included. It used to be steps alone, and
      // that was sufficient only while the projection dropped a response's prose whenever the
      // response also called a tool. That drop is gone, so a citizen who reloads mid-build now
      // has each of those sentences on disk AND in the re-told turn, and read them twice.
      //
      // `srv_` IS WHAT KEEPS THE LIVE MESSAGE ALIVE. Only `messagesFromProjection` mints that
      // prefix, so it names a STORED row exactly; the streaming message carries the local id
      // this surface minted for it, and its seq is one past the newest stored row — so a rule
      // written on seq and parts alone would delete the very message it is protecting.
      const stepOnly = parts.length > 0 && parts.every((p) => p?.type === 'step')
      const reTold =
        msg.id.startsWith('srv_') &&
        parts.length > 0 &&
        parts.every((p) => p?.type === 'step' || p?.type === 'text')
      if (reTold && liveTurnFromSeq !== null && (msg.seq ?? 0) >= liveTurnFromSeq) continue

      // ── 2. THE ANCHOR ROW BECOMES ITS OWN SENTENCE, when nothing is re-telling it ────────────
      //
      // A `build_in_progress` part means a build began here and no outcome ever closed it: the
      // reattach lost it, or the server went down mid-build. It converts to no part, so left
      // alone it would vanish — and a citizen returning to that chat would see a transcript that
      // simply stops, with no account of what happened to the build they started.
      //
      // Stating the durable truth is better than either a dead spinner (what it replaced) or
      // silence (what deleting it would give). It is SUPPRESSED while a build is genuinely live
      // here, because then the past tense is a lie about something still happening.
      if (parts.length > 0 && parts.every((p) => p?.type === 'build_in_progress')) {
        if (buildActiveHere || generatingChatId === buildId) continue
        kept.push({ ...msg, parts: [{ type: 'text', text: BUILD_WAS_RUNNING }] })
        continue
      }

      if (parts.length !== 0 && parts.every((p) => p == null || UNDRAWN_PARTS.has(p.type))) continue

      // ── 3. A RUN OF STORED STEP ROWS IS ONE MESSAGE, so it is one group ──────────────────────
      //
      // The reload projection emits ONE MESSAGE PER STEP; the live path puts every step of a turn
      // on a single streaming message. `groupPartByType` coalesces ADJACENT PARTS WITHIN A
      // MESSAGE, so without this a build watched live shows one group of nine and the same build
      // after a reload shows nine groups of one — which is precisely the live/reload parity
      // (AE43) that having one converter was supposed to make automatic.
      //
      // Merging here rather than in the converter is deliberate: the converter maps one message
      // to one library message and must stay that shape, and this is a fact about the PROJECTION
      // (how the server chose to store a turn) rather than about any part.
      const previous = kept[kept.length - 1]
      if (stepOnly && previous && (previous.parts ?? []).every((p) => p?.type === 'step')) {
        kept[kept.length - 1] = mergeStepRun(previous, msg)
        continue
      }
      kept.push(msg)
    }
    return kept
  }, [messages, liveTurnFromSeq, mergeStepRun, buildActiveHere, generatingChatId, buildId])
  // WHAT GETS FRAMED, and what the pane says about it — resolved in `utils/previewAddress.ts` and
  // nowhere else (Plan A, U2). The precedence and its two scoping predicates used to be spelled
  // out inline at each of the framing sites, which is why they could not be asked from ABOVE the
  // chat; the shell mounts one iframe for the whole workspace and needs exactly this answer.
  //
  // The predicates travel INTO the module as arguments rather than being read there as free
  // variables. That is the point: the comment beside their declaration above already records that
  // a gate depending on declaration order is one reorder away from silently opening, and this
  // removes the ordering dependency for the address.
  //
  // `buildActive` still reads the session's own status, so Stop and the delete-gate never light up
  // on a relaunch, which has no build lifecycle at all.
  // The URL the workspace's own start control just produced — see `onStarted` at the publish
  // block below for why this arm needs a second producer at all. Declared here rather than beside
  // the poll's epoch because the address resolution reads it, and that runs further up.
  const [startedPreviewUrl, setStartedPreviewUrl] = useState<string | null>(null)
  const [startPending, setStartPending] = useState(false)
  const address = resolvePreviewAddress({
    turnPreviewUrl: turnPreview.url,
    turnStatus: turnBuildStatus,
    narratingChatIsOpenChat: turnNarrativeIsThisChat,
    // TWO PRODUCERS FOR ONE ARM, and the newer one wins. `session.relaunchedPreviewUrl` comes from
    // this surface's own relaunch path; `startedPreviewUrl` from the workspace's start control,
    // which calls the same endpoint directly. Neither can be dropped: the first still fires from
    // the build-session hook, and the second is the only producer the pane's own control has.
    //
    // THE RELAUNCHED ARM, NOT THE PROJECT ONE. They are gated identically, so the choice is about
    // RANKING: a restore the citizen just asked for outranks the session's own URL, which describes
    // the build before it. Demoting it to the project arm — ranked last — puts a stale session URL
    // in front of the app they pressed a button to bring up.
    relaunchedUrl: startedPreviewUrl ?? session.relaunchedPreviewUrl,
    projectPreviewUrl: null,
    sessionUrl: session.previewUrl,
    sessionStatus: session.status,
    sessionId: session.sessionId,
    sessionBelongsToOpenProject: sessionProjectMatches,
    transcriptHasBuildOutcome: newestOutcome !== null,
  })
  const framedPreviewUrl = address.url
  // NOT an address derivation, and deliberately left here: `restoredFromFailedBuild` below asks
  // whether a restore happened in this project, which is a different question from which arm won
  // the precedence — a live turn can outrank the relaunch while the restore remains the reason
  // the app on screen is the last saved one.
  const relaunchedUrl = sessionProjectMatches ? session.relaunchedPreviewUrl : null

  // The receiving end of the app's own error reporter (see `clientErrorRelayRef` above). Declared
  // HERE rather than beside the ref because it reads `framedPreviewUrl`, which is derived further
  // down the render.
  const handleFrameMessage = useCallback(
    (data: unknown) => {
      // No project, nothing to address the report to. A conversation with no project behind it
      // has no app for the harness to judge either, so nothing is lost by dropping it.
      if (!projectId) return
      // Scoped to the FRAMED URL, not to the project: a rebuild or a restore gives the app a new
      // container, and the crash loop that silenced the relay belonged to the old one.
      void clientErrorRelayRef.current?.relay(projectId, framedPreviewUrl ?? '', data)
    },
    [projectId, framedPreviewUrl],
  )
  // R104's stop-clock, handed to the pane below. The pane knows WHEN a citizen is looking at
  // their app; only this page knows WHICH project it is. `markAppVisible` is idempotent per
  // project and silently does nothing when no clock was started — a project with nothing built,
  // or a deep link straight into a chat, which never opened the project page that starts it.
  const handlePreviewRevealed = useCallback(() => {
    markAppVisible(projectId ?? null)
  }, [projectId])
  // THE BUBBLE'S TWO SOURCES ARE GONE WITH THE BUBBLE. `buildEnvelopes` and `buildStatus` merged
  // the turn stream and the legacy session feed so ONE card could draw either; the transcript
  // draws activity from message parts now, and the legacy feed's envelopes have exactly two
  // readers left — the at-limit sentence and the session banners — each asking its own question.
  //
  // `narratingTurn` went with them. It gated the bubble AND the stop control on "the turn has
  // said something about the app yet", which for the stop control meant a build was unstoppable
  // between the press of Send and its first step frame. `isRunning` is the honest predicate and
  // is what the control reads now.
  // #13/R2 — "done, preview live": this LIVE session completed, so the server pardoned its
  // container (idle lease) and the framed URL still serves. Gated on `showSession`
  // deliberately: a reloaded page (no live session, `framedStatus` synthesized from the
  // transcript's newest outcome) keeps the terminal placeholder + Relaunch — never coerce
  // "no live status" into a prior build's live-preview claim (the framedStatus lesson).
  // TWO HALVES OF ONE DECISION, and they must read the same source. `framedStatus` now comes
  // from the turn, so leaving this on the session meant a completed Write build showed
  // "The preview is no longer running" + Relaunch over an app the server had just PARDONED and
  // that was still serving — the framedStatus lesson, reintroduced from the other side.
  //
  // Keyed on `turnTerminal` (this tab watched the turn complete) rather than on `framedStatus`:
  // a reloaded page has no live turn and must keep the terminal placeholder, which is exactly
  // what the comment above defends.
  const completedLive =
    (turnNarrativeIsThisChat && turnTerminal === 'completed' && turnPreview.url != null) ||
    (showSession && session.status === 'ended' && session.endReason === 'completed')
  // #83, second half — IS THE PREVIEW STILL REAL?
  //
  // A reclaimed preview is visually identical to a working app: the last render stays painted,
  // the iframe reports nothing on a dead origin (a `load` event fires for a 500 exactly as for
  // a 200), and a cross-origin pane cannot read a status code. Once a build ends this tab holds
  // no SSE and no timer, and the teardown happens inside ANOTHER project's request — so there
  // is nothing to push here and no way to notice locally. The tab has to ask.
  //
  // Driven by focus/visibility first because that is the actual flow: the user tabs back to the
  // project whose workspace was taken. The slow interval only covers the second-monitor case,
  // and is deliberately lazy — this is honesty, not telemetry.
  //
  // FOUR STATES, NOT ONE BOOLEAN (C3 §8.3). This held `previewReclaimed: boolean`, set from
  // `!state.alive` — which meant a Redis blip, a sleeping workspace, a sibling project holding
  // the slot and a project nobody ever built all arrived as the same "your preview is gone".
  // The whole verdict is kept now, and `unknown` deliberately changes NOTHING on screen.
  // U4 — the app stopped running while nobody was sending messages. A REVERSION FOUND AT THE
  // PREVIEW POLL, which is the only place it can be found: the turn that would otherwise catch it
  // may never come, and until then the standing completion claim goes on being displayed above a
  // dead app for as long as the tab stays open.
  //
  // Cleared by the next turn, never by the poll: once the citizen sends anything, U2's gate is the
  // authority and it will restore and say so. Leaving this set would stack a second, older card
  // over that one.
  const [workspaceLost, setWorkspaceLost] = useState(false)
  // WHICH message is making the claim. The newest assistant message, whatever it says — the
  // annotation is deliberately content-agnostic, because the completion sentence is model prose
  // today and a rendered summary tomorrow, and an annotation that pattern-matched either would
  // go silently inert the day it changed.
  const standingClaimId = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const msg = messages[i]
      if (msg.role === 'assistant' && !msg.ephemeral) return msg.id
    }
    return null
  }, [messages])
  // MIRRORED INTO REFS because the poll effect must not re-subscribe when either changes. Its
  // deps are the things that INVALIDATE a workspace verdict; a new assistant message and a
  // retraction we just set are not among them, and listing them would tear down and rebuild the
  // 45-second timer every time the transcript grew.
  const standingClaimRef = useRef<string | null>(null)
  const workspaceLostRef = useRef(false)
  standingClaimRef.current = standingClaimId
  workspaceLostRef.current = workspaceLost
  const [previewState, setPreviewState] = useState<PreviewState | null>(null)
  // U22 — WHAT MAKES A VERDICT STALE, written as a dependency list rather than left implicit.
  //
  // Stopping the poll on a settled "gone" is two lines (below). The actual work is this: the
  // moment the timer no longer re-asks, a verdict is only as true as the last thing that
  // invalidated it — and a restored container REUSES THE SAME PREVIEW URL byte for byte, so
  // `framedPreviewUrl` alone never changes and the effect never re-runs. That left
  // "Preview unavailable" painted over a live app, permanently, with the 45-second tick (the
  // thing being removed) as its only corrective.
  //
  // So the workspace lifecycle is named here as an input: a workspace being PREPARED, a
  // preview declared READY, or a relaunch in flight all mean the same thing — a restore
  // outranks whatever the last poll decided. A change in any of them re-runs the effect,
  // which drops the stale verdict and asks again from scratch.
  //
  // Scoped to THIS chat/project (`turnNarrativeIsThisChat`, `sessionProjectMatches`) like every
  // other cross-surface read on this page: a sibling chat's build says nothing about this
  // project's container.
  //
  // `previewProbeEpoch` is the fourth input and it is not derived from anything: the Relaunch
  // CLICK is a synchronous fact, and the state it moves (`relaunching` true→false around an
  // awaited POST) can be collapsed into a single commit by React's batching, so an
  // invalidation that could only be spelled as "relaunching changed" is one a fast enough
  // server erases. A counter cannot be batched away — the value the effect sees is always
  // different from the one before it.
  const workspaceSignal = turnNarrativeIsThisChat ? (turnWorkspace?.state ?? null) : null
  const previewSignal = turnNarrativeIsThisChat ? turnPreview.state : null
  const restoreInFlight = sessionProjectMatches && session.relaunching
  const [previewProbeEpoch, setPreviewProbeEpoch] = useState(0)
  useEffect(() => {
    // IT ASKS WITH NO FRAME NOW, and that is a deliberate widening (Plan F, U4).
    //
    // This used to read "only worth asking while a frame is actually on screen claiming to be
    // live", which was true while the poll's only job was catching a framed app being reclaimed
    // underneath it. Its answer now decides something else as well: whether the pane offers the
    // one control that starts the app.
    //
    // THE FAILURE THAT FORCED IT. Reload a chat whose build has ended. The address resolves a
    // STATUS and no URL — the transcript proves a build ran — so the pane drew "The preview is no
    // longer running" and the poll returned right here without asking anything. The workspace state
    // stayed unknown for the life of the tab, so the pane could never learn the workspace was
    // asleep and never offered the way back: R3's one control, satisfied by zero, on the most
    // ordinary return journey in the product.
    //
    // The cost is bounded and was already accepted: `fetchPreviewState` is cheap by contract (one
    // cache read, no container call), and the terminal rule below still stops the timer on a
    // settled answer.
    if (!projectId) {
      setPreviewState(null)
      return undefined
    }
    // Every re-run is an invalidation event by construction (see the dependency list): the
    // previous answer described a workspace that has since moved, so it is dropped rather than
    // left on screen to be contradicted by the frame loading underneath it.
    setPreviewState(null)
    let live = true
    // TABBING BACK FIRES TWO PROBES, and `live` alone cannot tell them apart. `visibilitychange`
    // and `focus` both land on the same gesture, and the interval can be mid-flight underneath
    // them — so up to three requests are in the air at once, all with `live === true`, and they
    // settle in whatever order the network decides. The slowest one wins the `setPreviewState`,
    // which is the one place this pane must not be wrong: a stale `asleep` painted over a fresh
    // `alive` tells somebody their workspace is gone while it is running in front of them.
    //
    // A generation counter rather than an in-flight boolean, deliberately. A boolean would DROP
    // the later probe — and the later probe is the one holding the fresher answer, so on exactly
    // the gesture where the user is asking to be brought up to date, it would answer with the
    // reading they already had.
    let latestProbe = 0
    let timer: ReturnType<typeof setInterval> | null = null
    const stopAsking = () => {
      if (timer !== null) clearInterval(timer)
      timer = null
    }
    const keepAsking = () => {
      timer ??= setInterval(() => void probe(), PREVIEW_PROBE_MS)
    }
    const probe = async () => {
      if (!live || document.visibilityState !== 'visible') return
      const generation = ++latestProbe
      try {
        const state = await fetchPreviewState(projectId)
        // Superseded: a probe started after this one, so its answer is newer whatever order the
        // two responses arrived in. Bail before touching state OR the timer — an overtaken probe
        // calling `stopAsking()` would end the poll on a verdict that has already been replaced.
        if (!live || generation !== latestProbe) return
        // An `unknown` never OVERWRITES a decided verdict — a blip must not pull a live
        // preview off screen, and it must not wipe a "gone" the user is already reading
        // either. It is recorded only when nothing has been decided yet, because "we could
        // not check" is a real thing to say when it is the only thing we know.
        setPreviewState((prev) => (state.state === 'unknown' && prev ? prev : state))
        // R16/R17 — A TERMINAL ANSWER ENDS THE POLL. `asleep` / `slot_taken` / `never_built`
        // are settled facts about a workspace: nothing that could change them happens without
        // one of this effect's inputs changing first, so re-asking every 45 seconds forever
        // was a timer that could only ever hear the same sentence again. `unknown` is
        // POINTEDLY not terminal — it decided nothing, so it must not be allowed to end the
        // asking (that would pin "we could not check" for the life of the tab).
        //
        // AND THE SAME RULE BINDS `restorable`. A settled `state` with `restorable === null`
        // is half an answer: the workspace is confirmed gone, but whether the work can be
        // brought back was NOT decided — that is the tri-state's explicit "no claim", which
        // the server returns when the object store could not be reached. Ending the poll
        // there pins the one sentence this pane must never say wrongly ("no saved build yet,
        // so it will start fresh") over a workspace sitting safely on Blob, with no way back
        // and no timer left to correct it. A thing that decided nothing is not terminal,
        // whichever field declined to decide.
        // R17/R18 — THE COMPILE SIGNAL FOR A TAB WITH NO LIVE TURN. During a turn the state
        // arrives on the turn stream; that producer stops at the terminal, so a tab that
        // reloads after a red turn has nothing to cover a broken preview with and comes up
        // showing the framework's error screen under a live-preview label.
        //
        // Asked on THIS tick rather than on a timer of its own — same cadence, same visibility
        // rule, same generation guard — and only when there is something to ask about: a live
        // container, and no turn already reporting. Both conditions matter. Without the first
        // the call is an attach against a dead workspace; without the second it races the
        // stream and can move the pane backwards to an older reading.
        if (state.state === 'alive' && liveTurnIdRef.current === null) {
          const compiling = await fetchCompileState(projectId)
          if (!live || generation !== latestProbe) return
          // Still no live turn: one may have started while this was in flight, and the stream
          // is the better authority the moment it exists.
          if (liveTurnIdRef.current === null) setTurnCompile(compiling)
        }
        // U4 — AND IS THE APP STILL THEIRS? A container can revert while the citizen is reading,
        // in another tab, or at lunch, and the turn that would catch it may never come. Same tick,
        // same visibility rule, same generation guard as the compile probe above.
        //
        // GATED ON A STANDING CLAIM, not just on a live container. This costs a container exec and
        // it can raise an operational alarm, so it only runs when there is something to retract:
        // an app the platform has told this citizen is finished. A project with no assistant
        // message yet has made no claim, so there is nothing to be wrong about.
        //
        // Once set it stays set until the next turn clears it — re-asking a question whose answer
        // is already on screen would spend a container call to learn nothing.
        if (
          state.state === 'alive' &&
          liveTurnIdRef.current === null &&
          standingClaimRef.current !== null &&
          !workspaceLostRef.current
        ) {
          const lost = await checkWorkspace(projectId)
          if (!live || generation !== latestProbe) return
          if (lost && liveTurnIdRef.current === null) setWorkspaceLost(true)
        }
        if (SETTLED_GONE.has(state.state) && state.restorable !== null) stopAsking()
        else keepAsking()
      } catch {
        // A probe that could not answer says NOTHING. Painting "gone" on a network blip would
        // pull a working preview off screen — the same over-claiming this fix exists to remove.
      }
    }
    // KEPT LIVE EVEN AFTER THE TIMER STOPS, deliberately. These fire on a deliberate human act
    // (tabbing back to the project), never on a clock, so they are bounded by the user rather
    // than by a cadence — and they are the backstop for the one thing the invalidation list
    // above cannot see: another tab restoring this project's workspace. A `gone` that has gone
    // stale costs one request to notice, on the very interaction where someone is looking.
    const onVisible = () => void probe()
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onVisible)
    keepAsking()
    void probe()
    return () => {
      live = false
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', onVisible)
      stopAsking()
    }
  }, [projectId, framedPreviewUrl, workspaceSignal, previewSignal, restoreInFlight, previewProbeEpoch])

  // R18 — CAN THE SERVER PUT THIS APP BACK? Three sources, newest-and-most-certain first:
  //
  //  1. this session's own successful Save, which is the one thing that can only move the
  //     answer toward "yes" (`projectHasSavedBuild` is a prop read once at route resolution,
  //     and nothing refetches it — so saving, the very act that makes a relaunch possible,
  //     used to leave the affordance hidden until the user happened to reload the page);
  //  2. the preview poll's `restorable`, which is the freshest server answer and — unlike the
  //     old `snapshot_presence` behind the prop — counts the platform's turn-boundary recovery
  //     copy, i.e. the builder who worked for an hour and never pressed Save;
  //  3. the prop, for a cold load before the first poll lands.
  //
  // `??` on a TRI-STATE, deliberately: a `null` from the poll means the object store was
  // unreachable, which is not an answer, so it falls through to the older-but-real reading
  // rather than retracting a claim the server once made confidently.
  const hasSavedBuild =
    savedBuildProjectId && savedBuildProjectId === projectId
      ? true
      : (previewState?.restorable ?? projectHasSavedBuild)

  // #83 — the other project standing in the way, plus how to resume what the user was doing.
  // Held together because they are useless apart: the banner names the project, and the retry
  // is the whole reason the refusal is survivable rather than just informative. Cleared as one.
  const [reclaim, setReclaim] = useState<{ blocked: ReclaimBlocked; retry: () => Promise<void> } | null>(null)
  // What the hand-over is doing right now, so the dialog narrates instead of spinning through a
  // sequence that genuinely takes tens of seconds (plan 002, U9).
  const [handoverStep, setHandoverStep] = useState<HandoverStep | null>(null)
  // HOW THE LAST START ENDED, or `null` when none has been attempted or the last one reached the
  // app. Fed to the same pure map the project screen uses — see the report below.
  const [startOutcome, setStartOutcome] = useState<StartOutcome | null>(null)

  // The refusal is the SAME on both paths a user can take into the one workspace — a Write
  // message and the Relaunch button — so the mapping lives once here. Returns true when it
  // handled the error, so callers keep their own copy of "what else could go wrong".
  //
  // FIRST REFUSAL WINS — the dialog must not change under the person reading it.
  //
  // `reclaim` is a single slot and the composer stays live while the dialog is up (KTD-3: the
  // textarea is never disabled), so a second send — or the Relaunch button — can 409 behind
  // an open dialog. An unconditional overwrite swaps `blocked` and `retry` mid-decision: the
  // banner names one project, the user reads it, and by the time they press "Switch without
  // saving" the props describe a different one. That is an irreversible action taken against
  // a sentence the user never saw. Worse if it lands during a save, when the dialog's own
  // `busy` state is still tracking the operation it started for the PREVIOUS refusal.
  //
  // Discarding the newer closure costs nothing: `fireRelayTurn` holds the draft and staged
  // attachments until the server confirms the turn (`onSent` is what clears them), so a
  // refused send leaves the user's text exactly where they typed it. The message is in the
  // composer, not in the closure we dropped (#83 review, finding 7).
  const captureReclaim = (err: unknown, retry: () => Promise<void>) => {
    const blocked = asReclaimBlocked(err)
    if (!blocked) return false
    setReclaim((current) => current ?? { blocked, retry })
    return true
  }

  // Rejects rather than swallows: the dialog's `run()` wrapper is the only thing that can
  // report a failure here, and it can only do that while the dialog is still mounted.
  const resolveReclaim = async (save: boolean) => {
    if (!reclaim) return
    const { blocked, retry } = reclaim
    try {
    // Stop, then WAIT FOR THE STOP TO GENUINELY FINISH, then save, then release — the ordering
    // invariant lives in `handOverWorkspace`, and so does the refusal to proceed on a stop that
    // only timed out. The narration is the dialog's; this is what feeds it.
    await handOverWorkspace(blocked.projectId, save, {}, setHandoverStep)
    setHandoverStep('starting')
    // The retry is awaited BEFORE the dialog is dismissed (#83 review, finding 8). Clearing
    // first unmounts the only surface that can say "that didn't work", so a retry that failed
    // — another tab took the slot, the network blipped — looked exactly like one that worked:
    // the dialog vanished and the user reasonably concluded the switch went through. Dismiss
    // only after it settles, and let a rejection travel back to `run()`.
    await retry()
    setReclaim(null)
    } finally {
      setHandoverStep(null)
    }
  }

  // The dialog itself is mounted by the shell, so a refusal has somewhere to appear that is not
  // owned by whichever surface happened to make the call. Only the OPEN STATE travels; the
  // classification (`captureReclaim` above) and every handler stay here, because they are this
  // surface's knowledge and a second classifier is how a refusal loses its one authority.
  usePublishReclaim(
    useMemo(
      () =>
        reclaim
          ? {
              blocked: reclaim.blocked,
              step: handoverStep,
              // Issue #161's framing half: the dialog leads with the app being STARTED, and this
              // surface is that app. `null` while the route is still resolving the project name —
              // the dialog then falls back to the plain phrasing rather than quoting an empty
              // string, which is the failure that framing fix must not introduce.
              startingProjectName: projectName,
              resolve: resolveReclaim,
              cancel: () => setReclaim(null),
            }
          : null,
      // `resolveReclaim` is redefined every render and is not worth memoising on its own — it
      // reads `reclaim` directly. Keyed on the refusal itself, which is what the dialog renders.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      [reclaim, projectName],
    ),
  )

  const handleRelaunch = () => {
    if (!projectId) return
    // Stamp the project so the relaunch surfaces (Restoring…, the framed URL, its errors) render:
    // on a fresh mount no session originated here, so the ref is unset and every
    // sessionProjectMatches gate would otherwise drop the relaunch state on the floor (#43).
    sessionProjectRef.current = projectId
    // U22 — a restore is starting, so whatever the poll last decided about this workspace is
    // now history. Bumped HERE, on the click, rather than left to the `relaunching` flag: the
    // restored container comes back on the SAME url, so nothing else in the effect's inputs is
    // guaranteed to move, and a relaunch that resolves inside one commit moves the flag from
    // true back to false without any render in between (see the effect's dependency note).
    setPreviewProbeEpoch((n) => n + 1)
    // The hook re-throws ONLY the #83 refusal (everything else it discriminates into
    // `relaunchError`), so this catch is that one case and must not swallow anything else.
    void session.relaunch(projectId).catch((err) => {
      if (!captureReclaim(err, () => session.relaunch(projectId))) throw err
    })
  }

  // `chatNeedsAttention` — the badge on the retired chat-panel toggle below — is gone with it.
  // Nothing else read it: SessionBanners and TurnBanner already render these same conditions
  // in-flow a few lines below, on a rail that is always reachable once R13's single collapse is
  // the only one there is.

  // ─── THE APP PANE, PUBLISHED UPWARD (Plan A, U4) ─────────────────────────────────────────────
  //
  // This used to be a twenty-three-prop `<LivePreview>` mount in the right-hand column, and the
  // pane existed only because this page rendered it — which is why leaving a build chat destroyed
  // the running app. The pane is now hosted by the shell and this surface DECLARES rather than
  // renders: what to frame, what chrome to put on it, and whether it wants it seen.
  //
  // THE PROPS KEEP THEIR SCOPES, verbatim, and the reasons travel with them — the whole hazard of
  // a move like this is that someone narrows the app-scoped ones "for consistency" on the way past.
  useWorkspaceProject(projectId)
  usePublishAddress(address, projectId)
  // R11/R12. A Build chat wants the pane seen; a Plan chat does not.
  //
  // HIDDEN, NOT UNMOUNTED, and that distinction is the whole reason this is a visibility
  // declaration rather than an address that is withheld. A planning question READS the running app
  // and starts it if it is stopped — the origin document reverses the canvas on this — so the app
  // may well be up and held by this very conversation. Retiring the address here would tear down a
  // container the citizen is about to build in.
  useAppPaneVisible(!isPlanChat)
  // WHAT TO SAY ABOUT THE WORKSPACE, from the SAME map the project screen uses (Plan F, U2/U4).
  //
  // NO SECOND READ AND NO SECOND POLL: this is `previewState`, which the effect above already
  // fetched on this surface's own cadence, put through the pure map. What it buys is that the
  // sentence a citizen reads when this chat's pane has nothing to frame has exactly one author in
  // the product — the same one the project screen's pane and a Plan chat's line read from.
  //
  // ★ A FAILED START IN A CHAT SAYS WHY (plan 002, U11), and it did not.
  //
  // `startOutcome` was hardcoded to `null` here on the grounds that this surface has its own
  // relaunch path with its own error handling. That path is the SESSION's relaunch, which is a
  // different control: a press on the PANE's start or retry — the one a citizen reaches when
  // their app is asleep in a build chat — reported nothing at all. The spinner stopped, the same
  // sentence came back, and pressing again did the same thing. This surface holds the outcome now,
  // exactly as the project surface's hook does, and hands it to the same pure map so the wording
  // still has one author.
  usePublishWorkspaceReport(
    projectId
      ? {
          state: resolveWorkspaceState({
            preview: previewState,
            projectHasSavedBuild,
            startOutcome,
            startInFlight: startPending,
          }),
          projectId,
          // R-18/U4 — WHERE A START'S URL LANDS ON THIS SURFACE, and without it the start control
          // did nothing visible here. This surface feeds the resolver's project-scoped arm with
          // `null` (its own poll only runs over an ALREADY framed URL, by design), and its
          // `relaunchedUrl` arm used to be fed by a Relaunch button inside the pane that this plan
          // retired — so a fresh start had no arm left to populate and the app came up in a
          // container nothing framed. The relaunched arm is exactly right for it: a restore has no
          // build lifecycle, which is why that arm resolves its own status to `ready`.
          onStarted: (previewUrl) => {
            // STAMP THE PROJECT, then record the URL — and the order does not matter, but the
            // stamp does. Every project-scoped arm of the address resolver is gated by a stamp a
            // SESSION leaves, and a chat with no session has none; without this a start fired here
            // resolved to no address at all and the app came up in a container the pane refused to
            // point at. Setting it is not a widening of the predicate, which must stay independent
            // of the chat one — it is this surface honestly claiming the project's workspace,
            // exactly as a reattach does when it adopts a live build.
            sessionProjectRef.current = projectId
            setStartedPreviewUrl(previewUrl)
          },
          // This surface has no map state of its own to move — it hands the pure map a `preview`
          // and nothing else — so an in-flight press is local state here, exactly as it is in the
          // hook the project surface uses.
          onStartPending: setStartPending,
          onStartOutcome: (outcome) => {
            setStartOutcome(outcome)
            // A start that REACHED the app clears the outcome and asks again immediately, so the
            // pane arrives at the running app on the press rather than on the next poll tick.
            if (outcome === null) setPreviewProbeEpoch((n) => n + 1)
          },
          onRefresh: () => setPreviewProbeEpoch((n) => n + 1),
          // The SAME single-slot capture the composer and the relaunch button use — first refusal
          // wins, so the dialog cannot change under the person reading it. Set directly rather
          // than through `captureReclaim`, which classifies a raw error; by the time a refusal
          // arrives here the pane's control has already classified it, and re-classifying an
          // already-narrowed value is how a second authority on a refusal gets created.
          onReclaimRefusal: (blocked, retry) => setReclaim((current) => current ?? { blocked, retry }),
        }
      : null,
  )
  usePublishPaneView({
    /* NO TOOLBAR SLOTS TO FILL ANY MORE (plan 002, U2). This published the publish chip into a
       row `LivePreview` drew inside the pane, with a note saying "Plan F retires this mount when
       it merges the two screens". It is retired here instead, and by a different move than that
       note expected: the chip is not re-homed on another surface, it is drawn by the shell's own
       toolbar row beside the chat title, from the same computed state, so there is one mount for
       both screens rather than two that happen never to be live at once. */
    iterating: showSession && session.iterating,
    onRelaunch: handleRelaunch,
    relaunching: sessionProjectMatches && session.relaunching,
    relaunchError: sessionProjectMatches ? session.relaunchError : null,
    lastBuildFailed: newestOutcome?.status === 'failed',
    restoredFromFailedBuild: relaunchedUrl != null && session.relaunchedFromFailedBuild,
    completedLive,
    hasSavedBuild,
    previewState: previewState?.state ?? null,
    occupyingProjectName: previewState?.occupyingProjectName ?? null,
    reconnecting: (turnNarrativeIsThisChat && turnPreview.state === 'reconnecting') || (showSession && session.reconnecting),
    /* NOT gated on `turnNarrativeIsThisChat`, unlike the narrative values above it. This is a fact
       about the PROJECT'S APP — one app per project — not about which conversation happens to be
       open, and it now has a producer that outlives the turn (the preview probe above). Gating it
       would blank the signal the moment the user opened a sibling chat, and blanking it is what
       leaves an error screen uncovered. */
    compileState: turnCompile,
    /* Which of the cover's sentences is true — see `turnRunning` below. SCOPED TO THIS PROJECT,
       not merely to "some turn somewhere". One instance of this component survives a project
       switch and `generatingChatId` is cleared only by the finishing chat's own handler, so the bare
       `!== null` form kept project B's pane claiming "putting the latest change together" about a
       turn running on project A. `builds` is this project's conversations; the open chat is
       checked separately because a brand-new one is not in that list yet. */
    workspaceLost,
    turnRunning:
      generatingChatId !== null &&
      (generatingChatId === buildId || builds.some((b) => b.id === generatingChatId)),
    onFrameMessage: handleFrameMessage,
    /* R104's stop-clock. IT TRAVELS AS A PAYLOAD RATHER THAN A PROP so it survives Plan D's
       deletion of this page: without it `project_to_app_visible_ms` stops being produced and
       nothing announces that, which is the one failure a measurement cannot detect. */
    onRevealed: handlePreviewRevealed,
  })


  // ─── THE RENDER ──────────────────────────────────────────────────────────────────────────────
  //
  // NO PAGE FRAME AND NO NAVBAR. Both belong to the workspace shell, which is what lets this
  // surface be replaced without the app pane going with it. The reclaim dialog is the shell's too
  // — its open state is published above, its classification stayed here.
  //
  // WHAT USED TO BE HERE: a hand-rolled transcript loop with its own bubble, avatar and timestamp
  // per row, a build-progress card, a plan-options card, a pending-attachment chip row, a
  // fixed-corner toast, an image lightbox and a composer. All of it is one `ChatThread` and one
  // `Composer` now, and both are shared with the kind of chat that used to have its own page.

  /**
   * A TURN IS RUNNING IN THIS CHAT — the one signal the transcript, the stop control and the
   * composer all read.
   *
   * It is `generating`, and deliberately NOT the old `narratingTurn` (`turnPhase(...) !== null`).
   * That derivation could not answer until a frame saying something about the app had arrived, so
   * between pressing send and the first step frame a running build had no stop control at all —
   * the seconds in which someone who has just realised they asked for the wrong thing is most
   * likely to reach for it.
   */
  const isRunning = generating

  /**
   * The runtime's `onNew`, which NOTHING ON THIS SURFACE CALLS — and that is the design, not a
   * stub left behind.
   *
   * `useExternalStoreRuntime` requires it, and it is reached only through the library's own
   * composer. That composer is not rendered: `createActionButton` gives its Send a real
   * `disabled` for the whole of every turn (R45/R64), so ours is the only send path and it goes
   * to `handleSubmit`. Registering a working `onNew` here would create a SECOND way to start a
   * turn, bypassing the gate chain, the attachment cap and the double-Enter guard — which is why
   * this refuses rather than quietly duplicating them.
   */
  const handleAppend = useCallback(async () => {
    throw new Error(
      'The library composer is not mounted on this surface — sends go through `handleSubmit`, ' +
        'which owns the gate chain. Reaching this means a library send path was rendered.',
    )
  }, [])

  /** R55's stop, shared by the composer's control and the runtime's `cancel` capability. */
  const stopTarget = useCallback(() => {
    // Read at PRESS time. `liveTurnIdRef` is a ref precisely because a value captured at render
    // stops the wrong turn — see its declaration.
    const conversationId = buildIdRef.current
    const turnId = liveTurnIdRef.current
    return conversationId && turnId ? { conversationId, turnId } : null
  }, [])

  const handleStopSession = useCallback(async () => {
    await sessionRef.current.stop()
  }, [])

  /**
   * The thread's own cancel, which is what registers the runtime's `cancel` capability (U4).
   *
   * It shares `stopTarget` with the composer's control rather than deriving a second one: two
   * paths to one stop is two chances to stop a different turn from the one on screen.
   */
  const handleCancel = useCallback(async () => {
    const target = stopTarget()
    if (target) {
      await stopTurn(target.conversationId, target.turnId)
      return
    }
    await handleStopSession()
  }, [stopTarget, handleStopSession])

  /**
   * WHY SEND IS UNAVAILABLE, when the reason is not simply "a reply is in flight".
   *
   * The composer states the running case itself; everything here is a fact only this surface
   * knows. `undefined` means Send is free as far as this surface is concerned.
   */
  const gate = useMemo(() => {
    if (gateCheck === 'unreachable') {
      return { blocked: true, reason: 'Couldn’t check whether a build is running here. Try Retry below.' }
    }
    if (gateCheck === 'checking') {
      return { blocked: true, reason: 'Checking whether a build is still running here…' }
    }
    if (buildActiveHere || buildStarting) {
      return { blocked: true, reason: 'Building your app — keep typing if you like; send unlocks when it’s done.' }
    }
    // U24 — today's budget is spent. The composer stays live so the citizen can copy their draft
    // out; only sending waits, and the sentence names the moment it works again.
    if (atLimit) return { blocked: true, reason: atLimit.title }
    return undefined
  }, [gateCheck, buildActiveHere, buildStarting, atLimit])

  const hasPendingOffer = offer !== null && offer.state === 'pending'

  /**
   * The return-to-latest control, rendered under the viewport (U8, R29a).
   *
   * MEMOISED, because `ViewportFooter` is a component TYPE: a fresh function identity every render
   * would unmount and remount whatever it draws. This one is stateless, so a remount would only be
   * waste — but the COMPOSER is deliberately not rendered through this slot for exactly that
   * reason, and the rule is worth stating where someone would be tempted to move it here.
   */
  const ViewportFooter = useMemo(() => {
    const Footer: FC = () => <ScrollToLatest isRunning={isRunning} hasPendingOffer={hasPendingOffer} />
    return Footer
  }, [isRunning, hasPendingOffer])

  /**
   * R66's TWO ANNOUNCEMENTS. The turn's start is this surface's to say, because nothing else knows
   * a turn began; what a group amounted to is reported UP from the group as it seals, because the
   * group is the only place that count is already correct (see `GroupSealedContext`).
   *
   * This used to pass a hardcoded `null` under a comment saying the group announced the second
   * half itself. It did not — the group had no live region and no announcer — so a screen-reader
   * user was told the agent had started and never told what it did.
   */
  const [sealedSummary, setSealedSummary] = useState<string | null>(null)
  const announcement = useActivityAnnouncement({ isRunning, sealedSummary })

  // Named once, because the region's presence test and its content have to be the same value —
  // written out twice they are two expressions that can be edited apart, and the failure mode is
  // an empty `role="alert"` box or a sentence with no box around it.
  const urgentText = urgent ?? (sessionProjectMatches ? session.error : null)

  return (
    <div className="flex flex-1 min-h-0 overflow-hidden">
      {/* NO COLLAPSE HERE ANY MORE. This panel used to CSS-collapse itself (width:0,
          `HIDDEN_BUT_MOUNTED`) behind its own `chatCollapsed` state — the whole mechanism R13
          retired (see the state's removal note above). The rail this surface fills is collapsed
          by `WorkspaceShell` instead, as a class on the `workspace-outlet` element one level up;
          from here that is invisible.

          AND NO FIXED WIDTH EITHER (plan 002, U6). It was `w-72 xl:w-80` — a second, narrower
          column INSIDE the rail the shell had already sized, which left a dead band of ground
          between the transcript and the app pane. The panel fills the rail now, and the rail's
          width is the one the citizen can drag.

          A PLAN CHAT IS THE OTHER HALF OF THE SAME RULE. It declares no pane, so the shell gives
          it the whole window — and a transcript run edge to edge across 1440px is unreadable. So
          it centres itself in one column at a comfortable measure, which is exactly what the
          board draws. A build chat does not: it sits beside the app it is changing, and the two
          are meant to read as one screen. */}
      {/* ONE RUNTIME, AROUND EVERYTHING THAT READS IT (plan 002, U5). It used to be built inside
          `ChatThread`, whose provider therefore wrapped only the transcript — fine while the
          composer was entirely hand-rolled and read nothing from it, and wrong the moment the
          composer became the library's, because every composer primitive resolves against
          `useAui()`. Hoisted rather than duplicated: two runtimes would give this screen two
          composer states, and the one the citizen typed into would not be the one the transcript
          belonged to. */}
      <ChatRuntimeProvider
        messages={transcript}
        isRunning={isRunning}
        onNew={handleAppend}
        onCancel={handleCancel}
      >
      <div
        id="chat-panel"
        data-testid="chat-panel"
        data-chat-kind={kind}
        className={`flex min-w-0 flex-1 flex-col overflow-hidden bg-white ${
          isPlanChat
            ? // ONE CENTRED COLUMN. `mx-auto` on a `max-w` inside the full-width rail, rather
              // than a narrower rail — the rail's width is the citizen's to drag, and pinning it
              // for one chat kind would take that away and reintroduce the measured breakpoint
              // the shell exists without.
              'mx-auto w-full max-w-3xl'
            : 'border-r border-bial-border'
        }`}
      >
        {/* THE BORDERED HEADER IS SURRENDERED TO THE TOOLBAR ROW (plan 002, U2). It held one
            breadcrumb link back to the project and nothing else — the only thing in the product
            that named where a chat belonged, and it named neither the chat nor its kind. The row
            above both columns draws all three now: project, kind pill, chat title. `projectName`
            is still a prop because the reclaim dialog names the project being started with it. */}

        {/* ONE SCROLL CONTAINER, and it is the thread's own viewport — not this wrapper, which
            only gives the thread a box to fill. `min-h-0` is what lets it shrink inside the column
            instead of pushing the composer off the bottom. */}
        <div className="flex-1 min-h-0">
          <ChatThread
            onGroupSealed={setSealedSummary}
            interruptedMessageIds={interruptedIds}
            footer={ViewportFooter}
          />
        </div>

        {/* The polite region for turn activity, permanently mounted so its text is announced when
            it arrives rather than being injected together with its region. */}
        <Announcer message={announcement} />

        {/* Session lifecycle banners (U15) — right where the operator is looking. */}
        <SessionBanners
          blocked={sessionProjectMatches ? session.blocked : null}
          feedDisconnected={showSession && session.feedDisconnected}
          quota={showSession ? session.quota : null}
          onForceEnd={(sid) => session.forceEnd(sid)}
          onReconnect={() => session.reconnect()}
          onStartAgain={handleStartAgain}
        />

        {/* The one banner slot: the state the app is in NOW, newest wins. A workspace sentence
            ENDS the turn, so nothing later in the same turn can be more current — which is why
            precedence is one expression here rather than fifteen mirrored `setTurnError` calls. */}
        {/* The unsaved-build warning is LAST in the precedence and that is deliberate: it is a
            standing fact about the newest build, so anything the platform has to say about the
            turn happening NOW is more current and outranks it. It comes back on its own once the
            newer sentence is cleared, because it is derived from the transcript rather than set. */}
        <TurnBanner text={workspaceSays ?? turnError ?? unsavedBuildWarning} />

        {/* ASSERTIVE, and reserved for the things that genuinely interrupt: a refused send, a
            failed handoff, a failed relaunch or save. Permanently mounted for the same reason the
            polite regions are — a region injected together with its text is frequently not
            announced at all. */}
        <div aria-live="assertive" role="alert">
          {urgentText ? (
            <div
              data-testid="urgent-banner"
              className="mx-3 mb-1 rounded-lg border border-danger/20 bg-danger/5 px-2.5 py-1.5 text-[11px] text-danger"
            >
              {urgentText}
            </div>
          ) : null}
        </div>

        {/* Arm (d)'s way out. It sits beside the composer's gate note rather than inside it: the
            note says what is happening, and this is the one thing a citizen can DO about a gate
            that otherwise has nothing to offer them. */}
        {gateCheck === 'unreachable' && (
          <div className="px-3 pb-1">
            <button
              type="button"
              onClick={retryGateCheck}
              data-testid="gate-retry"
              className="text-[11px] text-primary underline underline-offset-2 hover:text-primary-600"
            >
              Retry
            </button>
          </div>
        )}

        {/* R97 — A PLAN CHAT SAYS EVERYTHING THE PANE WOULD HAVE SAID. Above the composer, from the
            same computed workspace value the pane renders, so there is one author for every
            workspace sentence in the product and "no pane" cannot mean "says nothing". */}
        {isPlanChat && <PlanChatWorkspaceLine />}

        <Composer
          conversationId={buildId ?? null}
          // THE BOARD WRITES A DIFFERENT ONE PER KIND, and it is a hint rather than a mode: a
          // build chat asks for a change to the app, a plan chat asks for a change to the plan.
          // One sentence for both said neither.
          placeholder={kind === 'plan' ? 'Tell me what to change…' : 'Ask for another change…'}
          onSubmit={handleSubmit}
          isRunning={isRunning}
          gate={gate}
          contextWarning={contextWarning}
          // R55 — BOTH ways a build can be live here. `isRunning` is a turn this tab is streaming;
          // `buildActiveHere` is a LEGACY build session adopted on a reload, which sets no
          // streaming flag at all. Gating on the turn alone left a reloaded mid-build tab with a
          // running build and no way to stop it — the exact hole the deleted bubble's own
          // session-scoped condition used to cover. `resolveTarget` returns `null` when there is
          // no turn id, which is precisely how the control reaches `onStopSession`.
          stop={
            isRunning || buildActiveHere
              ? {
                  running: true,
                  resolveTarget: stopTarget,
                  onStopTurn: stopTurn,
                  onStopSession: handleStopSession,
                }
              : undefined
          }
          offer={
            offer
              ? {
                  toolCallId: offer.toolCallId,
                  conversationId: buildId ?? null,
                  spent: offer.state !== 'pending',
                  onBuild: handleBuildIt,
                  onKeepPlanning: handleKeepPlanning,
                }
              : undefined
          }
          onUrgent={setUrgent}
        />
      </div>
      </ChatRuntimeProvider>
    </div>
  )
}
