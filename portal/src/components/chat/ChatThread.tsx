/**
 * THE TRANSCRIPT — the portal's composition of the ported thread (R49, R50, R51, R52, R72).
 *
 * One surface for both kinds of chat. Nothing in this file, or anywhere below it, consults the
 * chat's kind: a Plan chat's transcript cannot show a build because no build parts arrive in it,
 * never because a renderer checked. The same fixture rendered in either kind produces identical
 * DOM, and a test asserts exactly that.
 *
 * ── WHAT IT MOUNTS INTO ──
 *
 * Plan A's `ConversationSlot` owns the slot's height and its hide treatment. This builds nothing
 * that positions itself against the viewport and adds no `calc(100vh - …)` — the one in
 * `ChatPage.tsx` was the only one in the repo, and it died with that file.
 *
 * ── `MessageContent` IS RE-HOSTED, NOT REPLACED ──
 *
 * It comes across whole, because it *is* four guarantees rather than a markdown renderer:
 * the image refusal (`disallowedElements` — and there is NO `img-src` CSP anywhere in this repo, so
 * that prop is the only thing holding it), the CSV-injection table control, `remark-breaks`, and
 * the `mode="static"` corruption guard. `@assistant-ui/react-markdown` has none of them and
 * reintroduces the engine this repo deliberately removed. Its 21-case parity checklist is
 * re-pointed at this host and must pass BEFORE anything is deleted.
 */
import { useMemo, type FC } from 'react'
import { AssistantRuntimeProvider, type AppendMessage } from '@assistant-ui/react'

import { Thread, type ThreadComponents } from '../assistant-ui/thread'
import { useChatRuntime } from './runtime/useChatRuntime'
import type { ChatMessage } from '../../utils/messageTypes'
import MessageContent from './MessageContent'
import AttachmentChips from '../AttachmentChips'
import ActivityGroup, { InterruptedMessagesContext, GroupSealedContext } from './ActivityGroup'
import ActivityRow from './ActivityRow'

export interface ChatThreadProps {
  /** The server-owned transcript. Live assembly and reload projection produce the same shape. */
  messages: readonly ChatMessage[]
  isRunning: boolean
  onNew: (message: AppendMessage) => Promise<void>
  /** R55's relocated stop, as the runtime sees it. Passing it is what registers `cancel`. */
  onCancel: () => Promise<void>
  /**
   * Messages whose turn ended on an interrupted terminal (R35c). Supplied by the surface because
   * it is a fact about the turn, not about any part.
   */
  interruptedMessageIds?: ReadonlySet<string>
  /** Rendered under the viewport — the composer, the offer strip, the return-to-latest control. */
  footer?: FC | undefined
  /** Told what an activity group amounted to as it seals — R66's second announcement. */
  onGroupSealed?: ((summary: string) => void) | undefined
}

/**
 * The text part, rendered by the portal's own renderer.
 *
 * `isUser` keeps user prose VERBATIM — markdown is never parsed in a user message, so a citizen
 * who types `**` sees `**`. That is a safety guarantee, not a style choice, and it is one of the
 * 21 parity cases.
 */
const TextPart: ThreadComponents['TextPart'] = ({ text, isUser }) => (
  <MessageContent parts={text} isUser={isUser} />
)

/**
 * THE WORKING STATUS — status only, never the reasoning content.
 *
 * The decision, taken by the product owner: render THAT the agent is working, never what it is
 * reasoning about. The reasoning text is technical and far too much for the people who read this,
 * so `useMessagePartReasoning` is not used and the group renders one plain line.
 *
 * This is a deliberate, narrow amendment to R35, recorded where it renders rather than left for an
 * implementer to trip over. R35 forbids a progress indicator on a turn that ran no tools — and it
 * was written to kill an indicator driven by TURN STATUS, which appeared on every message
 * including a plain question. This one is driven by a real signal: the model is actually
 * reasoning, and it disappears the instant it starts writing or calling something.
 *
 * IT DOES APPEAR ON A TURN THAT RUNS TOOLS, and an earlier version of this comment said the
 * opposite — that `groupPartByType` files `reasoning` and `tool-call` under one chain-of-thought
 * key, so the activity group would cover it. The grouping is HIERARCHICAL: the two share a
 * `group-chainOfThought` parent and get separate `group-reasoning` / `group-tool` children, so
 * both render. That is the intended behaviour — a build shows the status before its first step —
 * but it was worth stating correctly, because the sentence read as a guarantee that this never
 * fires beside an activity group.
 */
const ReasoningGroup: ThreadComponents['ReasoningGroup'] = () => (
  <p data-testid="working-status" className="my-1 text-xs text-neutral">
    Working on your app
  </p>
)

const noAnnouncement = () => {}

const ChatThread: FC<ChatThreadProps> = ({
  messages,
  isRunning,
  onNew,
  onCancel,
  interruptedMessageIds,
  footer,
  onGroupSealed,
}) => {
  const runtime = useChatRuntime({ messages, isRunning, onNew, onCancel })

  const components = useMemo<ThreadComponents>(
    () => ({
      TextPart,
      UserAttachments: AttachmentChips,
      ToolGroup: ActivityGroup,
      ToolPart: ActivityRow,
      ReasoningGroup,
      ViewportFooter: footer,
    }),
    [footer],
  )

  const interrupted = useMemo(
    () => interruptedMessageIds ?? new Set<string>(),
    [interruptedMessageIds],
  )

  // A stable identity for the default, so a surface that passes nothing does not hand the groups a
  // new callback on every render and re-run their announce effect.
  const announceSealed = useMemo(() => onGroupSealed ?? noAnnouncement, [onGroupSealed])

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <InterruptedMessagesContext.Provider value={interrupted}>
        <GroupSealedContext.Provider value={announceSealed}>
          <Thread components={components} />
        </GroupSealedContext.Provider>
      </InterruptedMessagesContext.Provider>
    </AssistantRuntimeProvider>
  )
}

export default ChatThread
