/**
 * THE THREAD, hand-ported from the assistant-ui registry's `thread` source to Tailwind v3.
 *
 * It could not be `shadcn add`ed. The registry source is authored for Tailwind v4 and the CLI both
 * fails silently AND rewrites `src/index.css` with v4-only `@custom-variant` declarations and a
 * `tw-shimmer` import. So it was fetched as reference and rewritten here, and
 * `src/__tests__/tailwind-tokens.test.js` is what stops a v4 token creeping back in — every one of
 * them renders as NOTHING on 3.4.17 with no build error and no failing test.
 *
 * ══ ONLY WHAT RENDERS WAS PORTED ══
 *
 * Dropped ON ARRIVAL rather than carried through the rewrite and then deleted — applying the v4→v3
 * table to a block that is about to be removed is the expensive way to ship nothing:
 *
 *   Composer, ComposerAction   — U10 replaces them with one composer of ours.
 *   ComposerPrimitive.Send     — see below. It ships a real `disabled`.
 *   EditComposer, UserActionBar, BranchPicker
 *                              — driven by `edit`, `feedback` and `switchToBranch`, all of which
 *                                U4's exact-equality capability snapshot pins to FALSE. Vendoring
 *                                UI for a capability we have a test asserting is absent is dead
 *                                code by construction.
 *   ToolFallback               — 627 lines whose entire payload is a `<pre>` of the tool's raw
 *                                arguments and a `<pre>` of `JSON.stringify(result)` in `text-xs`:
 *                                the exact fields R36 redacts, at a size R68 forbids. Nothing here
 *                                imports it. `ToolGroup` renders our own row instead.
 *   ThreadWelcome, Suggestions, follow-ups, history skeleton
 *                              — no requirement mounts them.
 *
 * ══ WHY THE LIBRARY'S OWN BUTTONS ARE NOT USED ══
 *
 * `createActionButton` renders `<button disabled={props.disabled || !callback}>`. For Send,
 * `useComposerSend` returns no callback while `isRunning && !capabilities.queue` — and `queue` is
 * never registered — so the library's Send is a HARD `disabled` for the whole of every turn. That
 * is the focus-dropping bug R45 and R64 forbid: `disabled` on the focused element blurs it to
 * `document.body`. `ThreadPrimitive.ScrollToBottom` has the same shape (`useThreadScrollToBottom`
 * returns `null` at the bottom, so the control renders disabled rather than disappearing), which is
 * why U8 keeps the hook and drops the button.
 *
 * ══ THE FLAT LOOK IS THE LIBRARY'S, NOT OURS ══
 *
 * An assistant reply is plain flush text on the page background — no bubble, no avatar, no card.
 * Only the USER message gets a muted rounded fill. That is the registry's own treatment and it is
 * what R49 asks for, so nothing here re-styles it into panels.
 *
 * ══ THE v4→v3 REWRITE APPLIED HERE ══
 *
 *   `@container`                    → dropped (v4 only; nothing depended on it)
 *   `max-w-(--thread-max-width)`    → `max-w-[44rem]`, and the CSS variable goes with it
 *   `wrap-break-word`               → `break-words`
 *   `-mb-7.5` / `pb-7.5` / `min-h-7.5` → the action bar's reserved space, rewritten as arbitrary
 *                                      rem values; v3 has no fractional spacing above 3.5
 *   `var(--color-foreground)` etc.  → not carried; this portal declares `--foreground`, and the v4
 *                                      spelling resolves to nothing and paints elements transparent
 *   `data-open:` / `duration-(--x)` → none survived into what we kept
 */
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
 * The slots this portal fills. `ToolGroup` is the activity group (U6); `TextPart` is
 * `MessageContent`, re-hosted (U5) so the whole sanitisation pipeline — the image refusal, the
 * CSV-injection table control, `remark-breaks`, the `mode="static"` corruption guard — comes across
 * intact rather than being replaced by `@assistant-ui/react-markdown`, which has none of them.
 */
export type ThreadComponents = {
  /** Renders one text part. Receives the already-assembled text of that part. */
  TextPart: ComponentType<{ text: string; isUser: boolean }>
  /** The activity group (U6). Given the group part and its rendered children. */
  ToolGroup: ComponentType<PropsWithChildren<{ group: ThreadGroupPart }>>
  /** The working status (U1's decision): status only, never reasoning content. */
  ReasoningGroup: ComponentType<PropsWithChildren<{ group: ThreadGroupPart }>>
  /** One row inside a group. */
  ToolPart: ToolCallMessagePartComponent
  /** Rendered above the viewport's bottom — U8's return control, U16's offer strip. */
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
 * Plan A's `ConversationSlot` owns the slot's height; nothing here positions itself against the
 * viewport and there is no `calc(100vh - …)` anywhere in this file. What the old surface had —
 * four nested scrollers on the planning page and another on the builder — is exactly what R49
 * deletes, so this must stay the only `overflow-y-auto` inside the chat slot. A test asserts it.
 */
const ThreadRoot: FC = () => {
  const { ViewportFooter } = useThreadComponents()

  return (
    <ThreadPrimitive.Root className="flex h-full min-h-0 flex-col bg-transparent">
      <ThreadPrimitive.Viewport
        turnAnchor="top"
        data-testid="thread-viewport"
        className="relative flex min-h-0 flex-1 flex-col overflow-y-auto scroll-smooth"
      >
        <div className="mx-auto flex w-full max-w-[44rem] flex-1 flex-col px-4 pt-4">
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
 * The in-thread error, authored FLAT.
 *
 * The registry ships `border-destructive bg-destructive/10 … rounded-md border p-3` — a bordered
 * tinted box, which is the nested-panel look R49 exists to remove. The border and the fill go; the
 * colour and the `role="alert"` that `ErrorPrimitive.Root` sets both stay, because those carry the
 * meaning. `elements-error-state` was rejected for the same reason plus a second one: it uses raw
 * `red-500` rather than the `destructive` token.
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
  return <Component text={text} isUser={false} />
}

/**
 * N1 — every assistant message carries a copy action, and ONLY a copy action.
 *
 * `hideWhenRunning` is deliberately NOT set, and that is the whole of N1's difficulty.
 * `useActionBarFloatStatus` reads `hideWhenRunning && s.thread.isRunning` — the THREAD, not the
 * message — and a hidden Root returns `null`. Setting it would remove copy from EVERY assistant
 * message for the whole of every turn, so a citizen watching a build could not copy the plan they
 * are reading. That directly undercuts the reason copy exists here: it is what makes "build it
 * again next week" real without any storage.
 *
 * `autohide="not-last"` IS set and is non-default: persistent on the latest turn, hover-revealed on
 * history. Its consequence belongs in the tests rather than in a comment nobody reads — without
 * hover the Root returns `null`, so there is no element and no attribute, and a hover test must
 * drive `message.isHovering` and assert presence or absence.
 *
 * No Reload, no Edit, no feedback, no More menu (which carries ExportMarkdown), no branch picker.
 * Rendering only Copy is simply rendering only Copy.
 */
const AssistantActionBar: FC = () => (
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
        // its own defect. The icon swaps and U9's polite region announces; the name stays put.
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
)

/**
 * The user's own message — the one place a fill is correct.
 *
 * `MessageContent` renders it with `isUser`, which is what keeps user prose VERBATIM: markdown is
 * never parsed in a user message, so a citizen who types `**` sees `**`. That is a safety
 * guarantee pinned by the parity checklist, not a branch on state.
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
      <div className="max-w-[85%] break-words rounded-xl border border-bial-border bg-white px-4 py-2 text-foreground empty:hidden">
        <MessagePrimitive.Parts components={{ Text: () => <UserText Component={TextPart} /> }} />
      </div>
    </MessagePrimitive.Root>
  )
}

const UserText: FC<{ Component: ThreadComponents['TextPart'] }> = ({ Component }) => {
  const { text } = useMessagePartText()
  if (!text) return null
  return <Component text={text} isUser />
}

export { cn }
