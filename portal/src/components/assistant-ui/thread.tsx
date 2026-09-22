/**
 * THE THREAD, hand-ported from the assistant-ui registry's `thread` source to Tailwind v3.
 *
 * WHY THIS EXISTS. It could not be `shadcn add`ed: the registry source is authored for
 * Tailwind v4, and the CLI both fails silently AND rewrites `src/index.css` with v4-only
 * `@custom-variant` declarations — so it was fetched as reference and rewritten here.
 * `__tests__/tailwind-tokens.test.js` is what stops a v4 token creeping back in; every one
 * renders as NOTHING on 3.4.17, with no build error and no failing test otherwise.
 *
 * `tw-shimmer` IS THE ONE TRAP WORTH NAMING: the registry's `tool-group` uses its
 * `ShimmerLabel`, so reaching for the package is the obvious move. DO NOT INSTALL IT — it
 * requires `tailwindcss: ">=4.0.0-0"` and ships `@property`/`@theme inline`/`@utility`, none
 * of which 3.4.17 understands; its CSS would pass through as dead text with nothing saying
 * so. A shimmer here is a keyframe to write, not a dependency to add.
 *
 * What was ported vs. dropped, and the full v4→v3 token rewrite, are recorded in the two
 * comment blocks immediately below rather than repeated here.
 */
// ONLY WHAT RENDERS WAS PORTED. Dropped ON ARRIVAL rather than carried through the rewrite
// and then deleted — applying the v4→v3 table to a block about to be removed ships nothing:
//   Composer, ComposerAction   — our own composer replaces them.
//   ComposerPrimitive.Send     — it ships a real `disabled`; ours is hand-built without one.
//   ThreadPrimitive.ScrollToBottom — the BUTTON only; the hook is kept and drives our own
//                                    return control.
//   EditComposer, UserActionBar, BranchPicker — driven by `edit`, `feedback` and
//                                    `switchToBranch`, all three pinned FALSE by the
//                                    exact-equality capability snapshot; vendoring UI for an
//                                    absent capability is dead code by construction.
//   ToolFallback               — 627 lines dumping the tool's raw arguments and
//                                `JSON.stringify(result)` in `text-xs` — the exact fields
//                                this portal redacts. Nothing imports it; `ToolGroup`
//                                renders our own row instead.
//   ThreadWelcome, Suggestions, follow-ups, history skeleton — no requirement mounts them.
//
// THE FLAT LOOK IS THE LIBRARY'S, NOT OURS: an assistant reply is plain flush text, no
// bubble/avatar/card; only the user message gets a muted rounded fill. That is the
// registry's own treatment and what the boards draw — nothing here re-styles it into panels.
//
// THE v4→v3 REWRITE APPLIED HERE:
//   `@container`                       → dropped (v4 only; nothing depended on it)
//   `max-w-(--thread-max-width)`       → `max-w-thread`, a v3 theme key; the variable goes with it
//   `wrap-break-word`                  → `break-words`
//   `-mb-7.5` / `pb-7.5` / `min-h-7.5` → the action bar's reserved space, rewritten as
//                                        arbitrary rem values; v3 has no fractional spacing
//                                        above 3.5
//   `var(--color-foreground)` etc.     → not carried; this portal declares `--foreground`,
//                                        the v4 spelling resolves to nothing
//   `data-open:` / `duration-(--x)`    → none survived into what we kept
import {
  ActionBarPrimitive,
  AuiIf,
  ErrorPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  groupPartByType,
  useAuiState,
  useMessagePartText,
  type ToolCallMessagePartComponent,
} from '@assistant-ui/react'
import { CheckIcon, CopyIcon } from 'lucide-react'
import { createContext, useContext, type ComponentType, type FC, type PropsWithChildren } from 'react'

import type { AttachmentDescriptor } from '../../utils/attachmentStore'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

export type ThreadGroupPart = MessagePrimitive.GroupedParts.GroupPart

/**
 * The slots this portal fills. `ToolGroup` is the activity group; `TextPart` is
 * `MessageContent`, re-hosted so the whole sanitisation pipeline — the image refusal, the
 * CSV-injection table control, `remark-breaks`, the `mode="static"` corruption guard — comes across
 * intact rather than being replaced by `@assistant-ui/react-markdown`, which has none of them.
 */
export type ThreadComponents = {
  /** Renders one text part. Receives the already-assembled text of that part. */
  TextPart: ComponentType<{ text: string }>
  /** The activity group. Given the group part and its rendered children. */
  ToolGroup: ComponentType<PropsWithChildren<{ group: ThreadGroupPart }>>
  /** The working status: status only, never reasoning content. */
  ReasoningGroup: ComponentType<PropsWithChildren<{ group: ThreadGroupPart }>>
  /** One row inside a group. */
  ToolPart: ToolCallMessagePartComponent
  /** Rendered above the viewport's bottom — the return control and the offer strip. */
  ViewportFooter?: ComponentType | undefined
  /**
   * The chips under a user message's prose. Attachments are the portal's own pipeline, so they
   * arrive as descriptors on `metadata.custom` rather than through the library's attachment model
   * — this slot is where they are drawn.
   */
  UserAttachments?: ComponentType<{ attachments: AttachmentDescriptor[] }> | undefined
}

const ThreadComponentsContext = createContext<ThreadComponents | null>(null)

function useThreadComponents(): ThreadComponents {
  const value = useContext(ThreadComponentsContext)
  if (!value) throw new Error('Thread components are required — render <Thread components={…} />.')
  return value
}

export const Thread: FC<{ components: ThreadComponents }> = ({ components }) => (
  <ThreadComponentsContext.Provider value={components}>
    <ThreadRoot />
  </ThreadComponentsContext.Provider>
)

/**
 * ONE SCROLL CONTAINER, and it is this viewport.
 *
 * `ConversationSlot` owns the slot's height; nothing here positions itself against the viewport
 * and there is no `calc(100vh - …)` anywhere in this file. This must stay the only
 * `overflow-y-auto` inside the chat slot. A test asserts it.
 */
const ThreadRoot: FC = () => {
  const { ViewportFooter } = useThreadComponents()

  return (
    <ThreadPrimitive.Root className="flex h-full min-h-0 flex-col bg-transparent">
      {/* `bottom`, and the choice is load-bearing rather than a default being spelled out.
          `turnAnchor="top"` pins each new user message near the top for a focused read — and it
          also makes the library default `autoScroll` to false (`autoScroll = turnAnchor !== "top"`),
          then suppresses the resize-driven follow for the WHOLE duration of a run while an active
          top anchor exists. So the one positioning scroll happened, the reply grew past the fold
          unfollowed, `isAtBottom` correctly went false, and `ScrollToLatest` offered "jump to it"
          on essentially every build — with the reader never having scrolled anywhere. Continuous
          follow and a top anchor are mutually exclusive by the library's own design, so a chat
          whose defining moment is a long reply arriving while the citizen watches takes `bottom`.
          The follow-unless-scrolled-up behaviour the pane needs is then the library's, already
          correct, including re-arming when the reader returns to within 1px of the bottom. */}
      <ThreadPrimitive.Viewport
        turnAnchor="bottom"
        data-testid="thread-viewport"
        className="relative flex min-h-0 flex-1 flex-col overflow-y-auto scroll-smooth"
      >
        <div className="mx-auto flex w-full max-w-thread flex-1 flex-col px-4 pt-4">
          <div data-testid="thread-messages" className="flex flex-col gap-y-6 pb-4 empty:hidden">
            <ThreadPrimitive.Messages>{() => <ThreadMessage />}</ThreadPrimitive.Messages>
          </div>
        </div>
      </ThreadPrimitive.Viewport>
      {ViewportFooter ? <ViewportFooter /> : null}
    </ThreadPrimitive.Root>
  )
}

const ThreadMessage: FC = () => {
  const role = useAuiState((s) => s.message.role)
  // No `isEditing` branch: `edit` is pinned false, so `EditComposer` can never be reached.
  return role === 'user' ? <UserMessage /> : <AssistantMessage />
}

/**
 * The in-thread error, authored FLAT. The registry ships a bordered, tinted box
 * (`border-destructive bg-destructive/10 … rounded-md border p-3`) — the nested-panel look
 * this surface does without. Border and fill go; the colour and `role="alert"` (from
 * `ErrorPrimitive.Root`) stay, since those carry the meaning. `elements-error-state` was
 * rejected too: it uses raw `red-500`, not the `destructive` token.
 */
const MessageError: FC = () => (
  <MessagePrimitive.Error>
    <ErrorPrimitive.Root className="mt-2 text-sm text-destructive">
      <ErrorPrimitive.Message className="line-clamp-2" />
    </ErrorPrimitive.Root>
  </MessagePrimitive.Error>
)

const AssistantMessage: FC = () => {
  const { TextPart, ToolGroup, ReasoningGroup, ToolPart } = useThreadComponents()
  // A MESSAGE WITH NOTHING IN IT RENDERS NOTHING — the message-level twin of the empty-text-part
  // rule in `AssistantText` below, and reachable for the same reason it was: the seam drops
  // `build` / `build_in_progress` / `plan_options` outright, so "a message whose parts all drop
  // still exists, empty, unrendered" (`convertMessage`'s own words).
  //
  // That case used to be unreachable on the live path because every finished build ALSO carried
  // the outcome sentence. Withholding the neutral "Build finished." made it reachable, and what it
  // produced was worse than the sentence it removed: an empty bubble with a copy button beside it,
  // which copies an empty string. The `build` part still has to travel — it is what tells the
  // preview pane an app was built here and carries its URL — so the part stays and the CHROME goes.
  const isEmpty = useAuiState((s) => s.message.content.length === 0)
  if (isEmpty) return null

  return (
    <MessagePrimitive.Root
      data-testid="assistant-message"
      data-role="assistant"
      className="animate-in fade-in slide-in-from-bottom-1 relative duration-150"
    >
      <div className="break-words px-2 leading-relaxed text-foreground">
        <MessagePrimitive.GroupedParts
          // `groupPartByType`, NOT an inline function. The memo fingerprint the primitive uses
          // (`GROUPBY_MEMO_KEY`) applies only to the exported grouper — with an inline one the
          // whole group tree rebuilds on every delta.
          groupBy={groupPartByType({
            reasoning: ['group-chainOfThought', 'group-reasoning'],
            'tool-call': ['group-chainOfThought', 'group-tool'],
            'standalone-tool-call': [],
          })}
        >
          {({ part, children }) => {
            switch (part.type) {
              case 'group-chainOfThought':
                return <>{children}</>
              case 'group-tool':
                return <ToolGroup group={part}>{children}</ToolGroup>
              case 'group-reasoning':
                return <ReasoningGroup group={part}>{children}</ReasoningGroup>
              case 'text':
                return <AssistantText Component={TextPart} />
              case 'tool-call':
                return <ToolPart {...part} />
              // `reasoning` deliberately renders NOTHING. The decision is status-only: the
              // reasoning text is technical and far too much for the people who read this, and
              // `useMessagePartReasoning` must not be used to render content.
              //
              // `default: return null` is load-bearing — the primitive ships a
              // `PartChildrenSentinel` that throws loudly on `default: return children`, and
              // returning null is what makes an unhandled part type render no element at all.
              default:
                return null
            }
          }}
        </MessagePrimitive.GroupedParts>
        <MessageError />
      </div>

      <div className="ms-2 flex min-h-[1.875rem] items-center pt-1.5">
        <AssistantActionBar />
      </div>
    </MessagePrimitive.Root>
  )
}

/** Bridges the library's text-part state into `MessageContent`, which is where prose is rendered. */
const AssistantText: FC<{ Component: ThreadComponents['TextPart'] }> = ({ Component }) => {
  // `useMessagePartText`, not `useAuiState(s => s.part.text)`: `PartState` is a union across every
  // part kind and `text` is not on all of them, so the state selector is untyped guesswork here.
  const { text } = useMessagePartText()
  // An empty text part renders NO element rather than an empty box — a defect this surface shipped
  // once and fixed, re-established here because the renderer changed underneath it.
  if (!text) return null
  return <Component text={text} />
}

/**
 * Every assistant message carries ONLY a copy action, deliberately. No Reload, Edit, feedback,
 * More menu, or branch picker. `autohide="not-last"` IS set (non-default): persistent on the
 * latest turn, hover-revealed on history, tested not just commented.
 *
 * COPY ARRIVES WHEN THE TURN IS OVER, NOT WHILE IT RUNS. A copy button under a half-written
 * message is the same signal every chat product uses to mean "this reply is finished" — so
 * showing it mid-turn told citizens the assistant had stopped when it had not, and offered them
 * a copy of a partial answer while the rest was still arriving. Reported against a build whose
 * transcript was still growing under the button.
 *
 * WHY NOT `hideWhenRunning`. The library's own prop reads the THREAD's running state and applies
 * it to EVERY message, so it would strip copy off the entire history for the whole turn — which
 * is why it was deliberately left unset, and why "just set the prop" is not the fix. The honest
 * predicate is BOTH facts together: the thread is running AND this is the message being written.
 * Every earlier message in the transcript is finished no matter what the thread is doing, and
 * keeps its copy button throughout — which is the property `hideWhenRunning` cannot express.
 */
const AssistantActionBar: FC = () => (
  <AuiIf condition={(s) => !(s.thread.isRunning && s.message.isLast)}>
  <ActionBarPrimitive.Root
    autohide="not-last"
    data-testid="assistant-action-bar"
    className="animate-in fade-in flex gap-1 text-muted-foreground duration-200"
  >
    <ActionBarPrimitive.Copy asChild copiedDuration={2000}>
      <Button
        variant="ghost"
        size="icon"
        // The accessible name does NOT change to "Copied" — renaming a control mid-interaction is
        // its own defect. The icon swaps and the polite region announces; the name stays put.
        aria-label="Copy message"
        className="h-7 w-7"
      >
        <AuiIf condition={(s) => s.message.isCopied}>
          <CheckIcon className="h-4 w-4" />
        </AuiIf>
        <AuiIf condition={(s) => !s.message.isCopied}>
          <CopyIcon className="h-4 w-4" />
        </AuiIf>
      </Button>
    </ActionBarPrimitive.Copy>
  </ActionBarPrimitive.Root>
  </AuiIf>
)

/**
 * The user's own message — the one place a fill is correct.
 *
 * `MessageContent` renders it through the same markdown pipeline as every other message. The
 * bubble below carries its own `prose-headings`/width overrides so a pasted heading or table
 * reads as body content here, rather than the renderer branching on who wrote it.
 */
const UserMessage: FC = () => {
  const { TextPart, UserAttachments } = useThreadComponents()
  // The portal's own descriptors, put here by `convertMessage` — the library's attachment model is
  // deliberately not adopted, so this is where they live.
  const attachments = useAuiState((s) => s.message.metadata?.custom?.['attachments']) as
    | AttachmentDescriptor[]
    | undefined

  return (
    <MessagePrimitive.Root
      data-testid="user-message"
      data-role="user"
      className="animate-in fade-in slide-in-from-bottom-1 flex flex-col items-end gap-y-2 px-2 duration-150"
    >
      {/* ABOVE the prose, and OUTSIDE the bubble's `empty:hidden` — an attachment sent with no
          message of its own is a real thing a citizen does, and it must still be visible. */}
      {UserAttachments && attachments && attachments.length > 0 ? (
        <UserAttachments attachments={attachments} />
      ) : null}
      {/* WHITE WITH A HAIRLINE, which is what every board draws for the citizen's own message —
          `BuildChat`, `PlanChat`, `PlainAnswer`, `PlanReady`. The grey fill it shipped with was
          the library default; on a white transcript it read as a second surface rather than as a
          quoted line, and on the plan chat's edge-to-edge white it was the only grey on screen. */}
      <div className="max-w-full break-words rounded-xl border border-bial-border bg-white px-4 py-2 text-foreground empty:hidden prose-headings:my-1 prose-headings:text-sm prose-headings:font-semibold">
        <MessagePrimitive.Parts components={{ Text: () => <UserText Component={TextPart} /> }} />
      </div>
    </MessagePrimitive.Root>
  )
}

const UserText: FC<{ Component: ThreadComponents['TextPart'] }> = ({ Component }) => {
  const { text } = useMessagePartText()
  if (!text) return null
  return <Component text={text} />
}

export { cn }
