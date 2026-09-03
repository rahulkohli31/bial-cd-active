/**
 * START A CHAT — the rail's composer, its kind picker, and the mint-and-navigate protocol.
 *
 * ═══ THE SAME BOX AS THE CHAT'S, AND WHY THAT NEEDED A RUNTIME HERE ═══
 *
 * The boards draw ONE composer, on both screens: a bordered box with the attachment control and
 * the send control inside it. Two independently hand-rolled boxes is how the two drifted apart —
 * one had a wide gold "Start Chat" button and a separate "Upload File" pill, the other a gold
 * square beside the input — so the box is `ComposerBox` now, shared.
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
import ComposerBox, { type ComposerSubmission } from '../chat/ComposerBox'
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
  const [kind, setKind] = useState<ChatKind>('build')
  const [guardRailModal, setGuardRailModal] = useState<PromptViolation | null>(null)
  const [urgent, setUrgent] = useState<string | null>(null)

  /**
   * Mint a fresh chat and hand the draft to it. EVERY submit mints a NEW conversation — there is no
   * canonical thread per project; continuity lives in the app and its snapshots.
   */
  const startChat = useCallback(
    async ({ text, attachments }: ComposerSubmission) => {
      const violation = validatePrompt(text)
      if (violation) {
        setGuardRailModal(violation)
        // REJECTS RATHER THAN RESOLVES, so the box keeps the message. A resolve here would empty
        // the composer for a send that never left — the exact loss the acceptance rule exists to
        // prevent, arriving through the guardrail instead of through the server.
        throw new Error('blocked by the prompt guardrail')
      }
      // THROUGH THE SHARED `uuidv7`, never an inline `crypto.randomUUID()`. That mints a v4, and
      // this id becomes the conversation's PRIMARY KEY, which ADR-0006 wants sortable.
      //
      // THE KIND TRAVELS AS A QUERY PARAM, THE DRAFT AS ROUTER STATE, and the split is deliberate.
      // Router state dies on reload and never travels in a shared link, so a bookmarked
      // `/chat/{id}` must still be able to take its kind from somewhere — and the server is the
      // authority once the row exists. The draft is the opposite: it is this navigation's payload
      // and has no business surviving a reload or appearing in a URL.
      //
      // `freshlyMinted` tells `ChatRoute` the row does not exist yet, so it can skip a GET that can
      // only ever 404 — doubled by two hydration fetches and doubled again by StrictMode in dev.
      navigate(`/chat/${uuidv7()}?projectId=${encodeURIComponent(projectId)}&kind=${kind}`, {
        state: { prompt: text, pendingAttachments: attachments, freshlyMinted: true },
      })
    },
    [kind, navigate, projectId],
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

      <ComposerBox
        // The chat does not exist yet — the id is minted at submit. `projectId` stands in as the
        // stamp, which is the right one for this surface: what a rail send belongs to is a
        // project, not a conversation.
        conversationId={projectId}
        placeholder="Describe the change you need…"
        onSubmit={startChat}
        unavailableReason={null}
        onUrgent={setUrgent}
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
