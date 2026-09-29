/**
 * START A CHAT — the rail's composer, its kind picker, and the mint-and-navigate protocol.
 *
 * IT MOUNTS `Composer`, NOT `ComposerBox`: sharing only the inner box left this screen with no
 * character cap, no counter and no draft, and it carries the longest message anyone writes. The
 * box resolves against `useAui()`, so a composer-only runtime is mounted here — empty transcript,
 * nothing streaming in, nothing rendering from it — purely to hold the text and the staged files.
 * `onNew` is deliberately unreachable, because a working one would be a second way to start a
 * chat that bypasses the checks in `startChat` below. The kind picker is this file's: a chat's
 * kind is fixed at creation, and what each kind is CALLED and what it DOES come from
 * `utils/chatKind.ts`.
 */
import { useCallback, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { X } from 'lucide-react'
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react'
import { ToggleGroup, ToggleGroupItem } from '../ui/toggle-group'
import { uuidv7 } from '../../utils/conversationApi'
import { chatKindFor } from '../../utils/chatKind'
import { useWorkspaceReport } from './workspaceChannel'
import Composer from '../chat/Composer'
import { type ComposerSubmission } from '../chat/ComposerBox'
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
      {/* ★ EVERY ATTACHMENT PROVIDER, MOUNTED AS ONE. Assembling them by hand here is what
          dropped the pending-read count, so the rail's Send never waited for a file still being
          read: a spreadsheet dropped here and sent mid-read landed nowhere, and the chat started
          from the sentence alone. */}
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
  const [urgent, setUrgent] = useState<string | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)

  /**
   * ASKS FOR THE WORKSPACE BEFORE IT NAVIGATES, which is why this is not a two-line navigate.
   * The start doubles as the preflight: a refusal rejects here with the message still in the
   * composer, and the chat is minted only once the server has admitted the start. The app comes
   * up behind the navigate, and the chat's first turn waits for it on the server.
   */
  const startChat = useCallback(
    async ({ text, attachments }: ComposerSubmission) => {
      setUrgent(null)
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

      // NOTHING TO ASK FOR WITHOUT A REPORT. A rail rendered outside a workspace has no pane to
      // start anything into; opening the chat is then the same behaviour this surface has always
      // had.
      if (!report) {
        open()
        return
      }

      // THE APP IS ASKED FOR BEFORE THE ADDRESS MOVES — `open()`, the navigate, sits BELOW this
      // await, which is what stops a citizen landing in a chat whose workspace turns out not to be
      // theirs. The POST answers once the start is admitted, every refusal decided by then; the
      // app comes up afterwards and the pane narrates that from the poll, not from here.
      //
      // JOINS A START ALREADY RUNNING rather than making a second one. Opening the project starts
      // the app, and a citizen who sends before that start is admitted would otherwise fire a
      // second request and be shown its answer instead of their own.
      const result = await report.start()
      // A REFUSAL IS RE-SAID WHERE THEY ARE STANDING, and stops the address: the message stays in
      // the composer and no chat opens onto a workspace this citizen has not got. Everything else
      // opens the chat — including a project with nothing saved to bring back, whose first message
      // is the very thing that provisions a workspace.
      if (result.kind === 'failed') throw result.error
      open()
    },
    [kind, navigate, projectId, report],
  )

  const picked = useMemo(() => chatKindFor(kind), [kind])

  return (
    <div ref={rootRef} className="font-manrope">
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
        onAttachPress={() => setUrgent(null)}
        // The rail section owns its own gutter and ground already.
        frameClassName="flex flex-col gap-1.5"
      />

      {/* Attachment refusals and send failures, said out loud. `role="alert"` because a refused
          file is a thing the citizen has to act on before their message means what they think. */}
      {urgent && (
        <div role="alert" className="mt-1.5 flex items-start gap-1.5 text-[11.5px] text-danger">
          <p className="min-w-0 flex-1">{urgent}</p>
          <button
            type="button"
            aria-label="Dismiss"
            onClick={() => {
              setUrgent(null)
              rootRef.current?.querySelector('textarea')?.focus()
            }}
            className="-my-1 inline-flex h-6 w-6 flex-shrink-0 items-center justify-center rounded text-danger/70 transition hover:bg-danger/10 hover:text-danger"
          >
            <X size={12} />
          </button>
        </div>
      )}
    </div>
  )
}
