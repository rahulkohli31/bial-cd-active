/**
 * START A CHAT — the rail's composer, its kind picker, and the mint-and-navigate protocol.
 *
 * ═══ THE SAME BOX AS THE CHAT'S, AND WHY THAT NEEDED A RUNTIME HERE ═══
 *
 * The boards draw ONE composer, on both screens: a bordered box with the attachment control and
 * the send control inside it. Two independently hand-rolled boxes is how the two drifted apart —
 * one had a wide gold "Start Chat" button and a separate "Upload File" pill, the other a gold
 * square beside the input — so the box is shared.
 *
 * IT IS `Composer`, NOT `ComposerBox`, AND THAT DISTINCTION IS THE WHOLE FIX. Sharing only the
 * inner box let the two surfaces drift again in every way the box does not own: this screen had
 * no character cap, no counter and no draft, because all three live in `Composer`. A citizen
 * could paste 45,000 characters here with Send still lit — the server refuses at 64,000 — and
 * lose a half-written description by stepping to another screen and back. This screen carries the
 * LONGEST message anyone writes, the one describing the whole app, so it was the worst place to
 * be missing them. Mount the whole composer, not its box; anything the chat's composer learns,
 * this one learns with it.
 *
 * That box is built on the library's composer primitives, and every one of them resolves against
 * `useAui()`. This surface had NO runtime mounted at all, so one is mounted here: a composer-only
 * runtime with an empty transcript, whose whole purpose is to hold the text and the staged files
 * the box reads. Nothing streams into it and nothing renders from it.
 *
 * `onNew` IS DELIBERATELY UNREACHABLE. `useExternalStoreRuntime` requires it, and it is reached
 * only through the library's own `composer.send()` — which this project never calls, because it
 * empties the text before it awaits anything and restores it only when the ATTACHMENT tasks throw
 * (`ComposerBox`'s docblock has the full account). Registering a working `onNew` here would create
 * a second way to start a chat that bypasses the guardrail below, so it refuses instead.
 *
 * ═══ WHAT THIS FILE SETTLES, AND WHAT IT KEEPS ═══
 *
 * The protocol below — mint a v7 id, navigate to a flat chat address, carry the draft in router
 * state — is this file's. So is the kind picker: a citizen has to be able to choose a Plan chat or
 * a Build chat BEFORE the first message, because a chat's kind is fixed at creation and never
 * mutates.
 *
 * THE TWO OPTIONS' WORDS COME FROM ONE PLACE AND ARE NOT RE-WRITTEN HERE. `utils/chatKind.ts` reads
 * the kind catalogue off the bootstrap profile — the same catalogue the server's toolset registry
 * sits beside — so what a kind is CALLED and what it DOES have exactly one author.
 */
import { useCallback, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react'
import { ShieldAlert, X } from 'lucide-react'
import { ToggleGroup, ToggleGroupItem } from '../ui/toggle-group'
import { validatePrompt } from '../../utils/promptGuardrails'
import type { PromptViolation } from '../../utils/promptGuardrails'
import { uuidv7 } from '../../utils/conversationApi'
import { chatKindFor } from '../../utils/chatKind'
import { asReclaimBlocked, relaunchPreview } from '../../utils/buildSessionApi'
import { ApiError } from '../../utils/apiError'
import { useWorkspaceReport } from './workspaceChannel'
import Composer from '../chat/Composer'
import { type ComposerSubmission } from '../chat/ComposerBox'
import { SendRefusal } from '../chat/sendRefusal'
import { convertMessage } from '../chat/runtime/convertMessage'
import type { ChatMessage } from '../../utils/messageTypes'
import {
  RefusalSinkProvider,
  StagedAttachmentsBinding,
  useBoundAttachmentAdapter,
} from '../chat/runtime/stagedAttachments'
import type { ChatKind } from '../../pages/ChatRoute'

/**
 * THE TWO KINDS, IN THE BOARD'S ORDER — Plan first, then Build, with Build selected.
 *
 * The order is the board's and the default is this rail's inheritance: the retired composer minted
 * a Build chat for every send, so defaulting to Plan would silently change what the control does.
 * Order and default are separate decisions and this is the one place both are made.
 */
const KINDS: readonly ChatKind[] = ['plan', 'build']

export interface RailComposerProps {
  /** The project every chat minted here is filed under. */
  projectId: string
}

export default function RailComposer({ projectId }: RailComposerProps) {
  const { adapter, stagedRef, refusalRef } = useBoundAttachmentAdapter()
  // A COMPOSER-ONLY RUNTIME. Empty transcript, never running, nothing to cancel — every capability
  // off except `attachments`, which is what makes the library's add control, its chips and its
  // dropzone render at all.
  const runtime = useExternalStoreRuntime<ChatMessage>({
    messages: EMPTY_TRANSCRIPT,
    isRunning: false,
    onNew: refuseLibrarySend,
    convertMessage,
    adapters: { attachments: adapter },
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <RefusalSinkProvider value={refusalRef}>
        <StagedAttachmentsBinding target={stagedRef} />
        <RailComposerBody projectId={projectId} />
      </RefusalSinkProvider>
    </AssistantRuntimeProvider>
  )
}

/** Module-level, so the runtime is not handed a fresh array on every render. */
const EMPTY_TRANSCRIPT: readonly ChatMessage[] = []

async function refuseLibrarySend(): Promise<never> {
  throw new Error(
    'The library composer send is not used here — sends go through `ComposerBox`, which holds the ' +
      'message until the server accepts it. Reaching this means a library send path was rendered.',
  )
}

function RailComposerBody({ projectId }: RailComposerProps) {
  const navigate = useNavigate()
  // THE WORKSPACE'S ONE REPORT, read rather than passed: this component sits several levels below
  // the surface that publishes it, and the alternative is a prop chain through the rail that no
  // component in between has any business carrying.
  const report = useWorkspaceReport()
  const [kind, setKind] = useState<ChatKind>('build')
  const [guardRailModal, setGuardRailModal] = useState<PromptViolation | null>(null)
  const [urgent, setUrgent] = useState<string | null>(null)

  /**
   * THE ONE-WORKSPACE RULE, ASKED AT SUBMIT (plan 002, U9) — and the whole of why this is not a
   * two-line navigate any more.
   *
   * IT USED TO NAVIGATE FIRST. The browser jumped to the new chat address, the chat mounted, its
   * send hit the server, and only THEN did the refusal arrive — so a citizen who had been building
   * in another project for two minutes was interrupted by a question about a chat that had already
   * opened in front of them. The backend's own ordering was always right: every refusal on the
   * send path is side-effect-free before anything is persisted. It was the browser that jumped
   * ahead of it.
   *
   * So the order is inverted. Nothing starts, and no address changes, before the citizen has
   * answered:
   *
   *   1. ask for the workspace — the preflight, and also the start this project needs anyway
   *   2. if it is held, the dialog opens naming BOTH projects, and this rejects so the composer
   *      keeps the typed message and its staged files
   *   3. on transfer the shell's dialog drains the other project, waits for a genuinely clean
   *      stop, releases it, and then re-runs this whole function — including the navigate
   *   4. the chat is created only once the container is ready, carrying the held message
   *
   * THE MESSAGE IS HELD IN THE COMPOSER THROUGHOUT, which is what makes cancelling free: nothing
   * has been stopped, nothing released, and the text is exactly where it was.
   */
  const startChat = useCallback(
    async ({ text, attachments }: ComposerSubmission) => {
      const violation = validatePrompt(text)
      if (violation) {
        setGuardRailModal(violation)
        // REJECTS RATHER THAN RESOLVES, so the box keeps the message. A resolve here would empty
        // the composer for a send that never left — the exact loss the acceptance rule exists to
        // prevent, arriving through the guardrail instead of through the server.
        //
        // SILENT, because the modal in front of them IS the explanation. A second sentence under
        // the composer saying "that message did not send" would be the composer talking over a
        // dialog that has already said more, and better.
        throw new SendRefusal('blocked by the prompt guardrail', { silent: true })
      }

      const open = () => {
        // THROUGH THE SHARED `uuidv7`, never an inline `crypto.randomUUID()`. That mints a v4, and
        // this id becomes the conversation's PRIMARY KEY, which ADR-0006 wants sortable.
        //
        // THE KIND TRAVELS AS A QUERY PARAM, THE DRAFT AS ROUTER STATE, and the split is
        // deliberate. Router state dies on reload and never travels in a shared link, so a
        // bookmarked `/chat/{id}` must still be able to take its kind from somewhere — and the
        // server is the authority once the row exists. The draft is the opposite: it is this
        // navigation's payload and has no business surviving a reload or appearing in a URL.
        //
        // `freshlyMinted` tells `ChatRoute` the row does not exist yet, so it can skip a GET that
        // can only ever 404 — doubled by two hydration fetches and doubled again by StrictMode.
        navigate(`/chat/${uuidv7()}?projectId=${encodeURIComponent(projectId)}&kind=${kind}`, {
          state: { prompt: text, pendingAttachments: attachments, freshlyMinted: true },
        })
      }

      // NOTHING TO ASK FOR WITHOUT A REPORT. A rail rendered outside a workspace has no channel to
      // route a refusal onto and no pane to start anything into; opening the chat is then the same
      // behaviour this surface has always had.
      if (!report) {
        open()
        return
      }

      const attempt = async (): Promise<void> => {
        report.onStartPending(true)
        try {
          const res = await relaunchPreview({ projectId })
          // THE PANE FRAMES IT BEFORE THE CHAT OPENS, so the app is on screen as the transcript
          // arrives rather than a beat behind it.
          if (res.previewUrl) report.onStarted(res.previewUrl)
          report.onStartOutcome(res.ready ? null : { kind: 'not-painted' })
          open()
        } catch (err) {
          // DISCRIMINATED ON THE CODE. Another project holding the one workspace is a QUESTION
          // with a remedy; everything else is a failure to report.
          const blocked = asReclaimBlocked(err)
          if (blocked) {
            // The retry is this whole function again — start, then open — so a transfer that
            // succeeds lands the citizen in the chat their message was typed for, exactly once.
            report.onReclaimRefusal(blocked, attempt)
            // REJECTS, so the composer keeps everything. SILENT for the same reason as the
            // guardrail above: the dialog is the explanation, and a line under the composer
            // repeating it in weaker words is noise over the top of it.
            throw new SendRefusal('the workspace is held by another project', { silent: true })
          }
          // NOTHING SAVED TO BRING BACK IS NOT A FAILED SEND (review #1). Asking for the workspace
          // is how this surface poses the one-workspace question, but a project that has never been
          // built has nothing to restore: the server's snapshot gate answers 404 by design — there
          // is no blank-template arm — and the first message is the very thing that provisions one,
          // through the turn's own `ensure_sandbox`. Rethrowing it made onboarding a dead end: a
          // citizen who created a project, described their app and pressed Send read "That message
          // did not send" and got no chat, on every attempt.
          //
          // THE QUESTION IS STILL ASKED FIRST, which is why this is a mapping and not a skipped
          // preflight. The server refuses a held workspace ABOVE the snapshot gate, so a brand-new
          // project's first message still meets the dialog before any address changes — and issue
          // #161's own reproduction is a submit in a project with nothing built yet.
          if (err instanceof ApiError && err.status === 404) {
            open()
            return
          }
          throw err
        } finally {
          report.onStartPending(false)
        }
      }

      await attempt()
    },
    [kind, navigate, projectId, report],
  )

  const picked = useMemo(() => chatKindFor(kind), [kind])

  return (
    <div className="font-manrope">
      {/* THE BOARD'S SEGMENTED CONTROL: a #F0F4F8 track with a white pill on the selected item.
          No hue at all — the selection is signalled by elevation, which is what keeps it legible
          and is why the icon takes its colour from the label rather than from the kind. */}
      <div className="mt-[11px]">
        <ToggleGroup
          type="single"
          value={kind}
          onValueChange={(next) => {
            // `type="single"` with no deselect: a chat is always one kind or the other, so an
            // empty value is not a state this control may reach — Radix hands back `''` on a
            // re-press of the active item, and ignoring that is what keeps the two options
            // exhaustive rather than three-valued.
            if (next === 'build' || next === 'plan') setKind(next)
          }}
          size="sm"
          aria-label="What kind of chat"
          className="inline-flex gap-[3px] rounded-[9px] bg-bial-bg p-[3px]"
        >
          {KINDS.map((candidate) => {
            const look = chatKindFor(candidate)
            return (
              <ToggleGroupItem
                key={candidate}
                value={candidate}
                aria-label={look.word}
                className="gap-[5px] rounded-[7px] px-[11px] py-[5px] text-[11.5px] font-semibold data-[state=on]:font-bold"
              >
                {/* NO COLOUR OF ITS OWN — `currentColor`, so the icon matches its label in both
                    states, which is the whole of how the board keeps this control legible. */}
                <look.Icon size={12} />
                {look.word}
              </ToggleGroupItem>
            )
          })}
        </ToggleGroup>
      </div>

      {/* ONE LINE, FROM THE CATALOGUE, describing BOTH kinds — the board writes it as a single
          sentence under the control rather than one line per option. The empty string is the
          honest fallback for a bootstrap that has not resolved or a kind this build does not
          recognise; rendering an empty paragraph is better than inventing a description here. */}
      {picked.description && (
        <p data-testid="kind-description" className="my-[9px] text-[11.5px] leading-relaxed text-neutral">
          {picked.description}
        </p>
      )}

      <Composer
        // The chat does not exist yet — the id is minted at submit. `projectId` stands in as the
        // stamp, which is the right one for this surface: what a rail send belongs to is a
        // project, not a conversation. It is also the DRAFT's key, which is what makes a
        // half-written first message survive a trip to another screen and back.
        conversationId={projectId}
        // THE HINT FOLLOWS THE KIND, from the catalogue — never a comparison here. R72 forbids
        // branching on a chat's kind in this directory, and the words belong beside the other
        // per-kind wording rather than being re-written at the one place that renders them.
        placeholder={picked.composerPlaceholder}
        onSubmit={startChat}
        // Nothing streams into this runtime — the chat does not exist until submit mints it — so
        // there is no turn here that could be running.
        isRunning={false}
        onUrgent={setUrgent}
        // The rail section owns its own gutter and ground already.
        frameClassName="flex flex-col gap-1.5"
      />

      {/* Attachment refusals and send failures, said out loud. `role="alert"` because a refused
          file is a thing the citizen has to act on before their message means what they think. */}
      {urgent && (
        <p role="alert" className="mt-1.5 text-[11.5px] text-danger">
          {urgent}
        </p>
      )}

      {guardRailModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div role="dialog" aria-modal="true" aria-label="Prompt blocked" className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-2xl">
            <div className="mb-4 flex items-start justify-between">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-full bg-red-100">
                  <ShieldAlert size={20} className="text-red-500" />
                </div>
                <h2 className="text-base font-extrabold text-tertiary">Prompt Blocked</h2>
              </div>
              <button
                type="button"
                aria-label="Close"
                onClick={() => setGuardRailModal(null)}
                className="text-neutral hover:text-tertiary"
              >
                <X size={16} />
              </button>
            </div>
            <p className="mb-4 text-sm leading-relaxed text-neutral">{guardRailModal.message}</p>
            <div className="mb-6 rounded-xl border border-red-100 bg-red-50 px-4 py-3">
              <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-red-500">Flagged keywords</p>
              <div className="flex flex-wrap gap-2">
                {guardRailModal.flaggedKeywords.map((kw) => (
                  <span key={kw} className="rounded-full bg-red-100 px-2 py-0.5 text-xs font-bold text-red-600">
                    {kw}
                  </span>
                ))}
              </div>
            </div>
            <div className="flex justify-end gap-3">
              <button
                type="button"
                onClick={() => setGuardRailModal(null)}
                className="rounded-xl bg-primary px-5 py-2 text-sm font-bold text-white transition hover:bg-primary/90"
              >
                Edit My Prompt
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
