/**
 * START A CHAT — the rail's composer, its kind picker, and the mint-and-navigate protocol.
 *
 * IT MOUNTS `Composer`, NOT `ComposerBox`: sharing only the inner box left this screen with no
 * character cap, no counter and no draft, and it carries the longest message anyone writes. The
 * box resolves against `useAui()`, so a composer-only runtime is mounted here — empty transcript,
 * nothing streaming in, nothing rendering from it — purely to hold the text and the staged files.
 * `onNew` is deliberately unreachable, because a working one would be a second way to start a
 * chat that bypasses the guardrail below. The kind picker is this file's: a chat's kind is fixed
 * at creation, and what each kind is CALLED and what it DOES come from `utils/chatKind.ts`.
 */
import { useCallback, useMemo, useRef, useState } from 'react'
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
  AttachmentAdapterProviders,
  useBoundAttachmentAdapter,
} from '../chat/runtime/stagedAttachments'
import type { ChatKind } from '../../pages/ChatRoute'

/**
 * THE TWO KINDS, IN THE BOARD'S ORDER — Plan first, then Build, and PLAN IS SELECTED.
 *
 * The order is the board's. The default was Build, inherited from the retired composer, which
 * minted a Build chat for every send; the argument for keeping it was that changing it would
 * silently change what the control does.
 *
 * CHANGED TO PLAN, PER THE OWNER (2026-09-10), and what it costs is exactly what that argument
 * warned about: a citizen who types into a fresh project and presses send now gets a plan to read
 * and a `Build this plan` button, instead of an app being built from their first sentence. That is
 * the point. A first prompt is the one most likely to be a rough description rather than a brief,
 * and building straight from it spends a container and several minutes of the model's time on a
 * guess nobody agreed to — which is also the moment the citizen has the least idea what the
 * platform is about to do.
 *
 * IT ONLY MOVES THE DEFAULT. Build is one click away and unchanged, `?kind=build` still mints a
 * build chat, and a chat's kind is still fixed at creation. Order and default remain separate
 * decisions, and this is still the one place both are made.
 */
const KINDS: readonly ChatKind[] = ['plan', 'build']

export interface RailComposerProps {
  /** The project every chat minted here is filed under. */
  projectId: string
}

export default function RailComposer({ projectId }: RailComposerProps) {
  const bound = useBoundAttachmentAdapter()
  // A COMPOSER-ONLY RUNTIME. Empty transcript, never running, nothing to cancel — every capability
  // off except `attachments`, which is what makes the library's add control, its chips and its
  // dropzone render at all.
  const runtime = useExternalStoreRuntime<ChatMessage>({
    messages: EMPTY_TRANSCRIPT,
    isRunning: false,
    onNew: refuseLibrarySend,
    convertMessage,
    adapters: { attachments: bound.adapter },
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      {/* ★ ALL THREE ATTACHMENT PROVIDERS, NOT TWO. This used to mount the
          refusal sink and the staged binding by hand and miss the pending-read count, so the
          rail's Send never waited for a file still being read: a spreadsheet dropped here and sent
          mid-read landed nowhere, and the chat started from the sentence alone. */}
      <AttachmentAdapterProviders bound={bound}>
        <RailComposerBody projectId={projectId} />
      </AttachmentAdapterProviders>
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
  // PLAN, per the owner — see the `KINDS` docblock above for what that changes and what it costs.
  const [kind, setKind] = useState<ChatKind>('plan')
  const [guardRailModal, setGuardRailModal] = useState<PromptViolation | null>(null)
  const [urgent, setUrgent] = useState<string | null>(null)
  const railRef = useRef<HTMLDivElement>(null)

  /**
   * CLOSING THE GUARDRAIL PUTS THE CARET BACK IN THE MESSAGE.
   *
   * This dialog is hand-rolled — no Radix `DialogContent`, so no `FocusScope`, so nothing
   * captures the element that had focus and nothing restores it. Dismissing it dropped focus on
   * `<body>`, where the next Tab starts again from the top of the document: a keyboard citizen
   * who pressed Send had to tab all the way back through the shell to reach their own message.
   *
   * THE BOX, NOT THE SEND CONTROL, is the target — the primary action here says "Edit My Prompt",
   * and the refusal deliberately keeps everything typed (`SendRefusal`, above), so the one thing
   * left to do is edit the text that is still sitting there. Both routes out of the dialog lead
   * to the same place, so both use this.
   *
   * FOUND IN THIS RAIL'S OWN SUBTREE, the way the library's `ComposerPrimitive.Root` finds it to
   * implement click-blank-space-to-focus: the composer's input is the one textarea here, and it
   * belongs to a component this file mounts rather than renders, so there is no ref to hold. The
   * scope is the ref, never the document — the same idiom the four hand-rolled dialogs beside
   * this one use for their focus traps.
   */
  const closeGuardRail = useCallback((): void => {
    setGuardRailModal(null)
    railRef.current?.querySelector('textarea')?.focus()
  }, [])

  /**
   * ASKS FOR THE WORKSPACE BEFORE IT NAVIGATES, which is why this is not a two-line navigate.
   * Nothing starts and no address changes until the citizen has answered: the start doubles as
   * the preflight, a held workspace opens the dialog and rejects here, a transfer re-runs this
   * whole function including the navigate, and the chat is minted only once the container is
   * ready. The typed message stays in the composer throughout, which makes cancelling free.
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
        // this id becomes the conversation's PRIMARY KEY, which needs to be sortable.
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
        // THE FLAG GOES UP BEFORE THE REQUEST, AND THE NAVIGATE HAPPENS AFTER IT — which is what
        // makes this the pane's ONLY narration for the whole wait, not a duplicate of the chat's.
        //
        // IT LOOKS DELETABLE AND IS NOT. The tempting reading is that it "fires on a page
        // navigation is about to unmount", which would make it a wasted commit and a wasted
        // announcement. That reading is wrong, and the order below is the proof: `open()` —
        // the navigate — sits BELOW the `await` on the next line, not beside this call, and the
        // no-saved-build arm's `open()` is below it too. This surface stays mounted for the
        // entire `relaunchPreview` POST, which blocks server-side until the
        // container answers and whose cold arm is bounded at `_COLD_READY_BUDGET_SECONDS` = 120s
        // (`build_sessions/manager.py`). So for a project with a saved build the citizen sits on
        // THIS page, watching THIS pane, for up to two minutes. Without this line that wait is
        // silent: the pane goes on saying "Your app is saved." over a start that is already
        // running, and a screen reader is told nothing at all — the one moment somebody most
        // needs to be told something is happening.
        //
        // THE DUPLICATE IS THE SECOND SENTENCE, NOT THIS ONE. What repeats is the chat surface
        // re-publishing the same state for the same project after the navigate, and the place to
        // stop that is where it is produced — silencing the first author to quieten the second
        // trades a real duplicate for a real silence.
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
          // NOTHING SAVED TO BRING BACK IS NOT A FAILED SEND. Asking for the workspace
          // is how this surface poses the one-workspace question, but a project that has never been
          // built has nothing to restore: the server's snapshot gate answers 404 by design — there
          // is no blank-template arm — and the first message is the very thing that provisions one,
          // through the turn's own `ensure_sandbox`. Rethrowing it made onboarding a dead end: a
          // citizen who created a project, described their app and pressed Send read "That message
          // did not send" and got no chat, on every attempt.
          //
          // ON THE CODE, NOT THE STATUS. This endpoint answers 404 for a second, unrelated reason
          // — a project that is deleted or is not this citizen's — and a bare status match opened
          // a chat onto it too, which then failed a beat later with nothing left to blame. Only
          // the snapshot gate carries `no_saved_build`; every other 404 is a real failure and is
          // reported as one.
          //
          // THE QUESTION IS STILL ASKED FIRST, which is why this is a mapping and not a skipped
          // preflight. The server refuses a held workspace ABOVE the snapshot gate, so a brand-new
          // project's first message still meets the dialog before any address changes.
          if (err instanceof ApiError && err.status === 404 && err.code === 'no_saved_build') {
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
    <div ref={railRef} className="font-manrope">
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
        // THE HINT FOLLOWS THE KIND, from the catalogue — never a comparison here. Branching on
        // a chat's kind is forbidden in this directory, and the words belong beside the other
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
                onClick={closeGuardRail}
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
                onClick={closeGuardRail}
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
