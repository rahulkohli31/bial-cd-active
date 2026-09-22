/**
 * THE TRANSCRIPT — the portal's composition of the ported thread. One surface for both
 * chat kinds: nothing here consults kind, so a Plan transcript cannot show a build (no
 * build parts ever arrive in it) — a test asserts identical DOM either way. It mounts
 * into `ConversationSlot`, which owns height/hide (no `calc(100vh - …)` here); the
 * runtime lives at the SURFACE, not here, so the composer can share it via `useAui()`.
 *
 * `MessageContent` is RE-HOSTED, not replaced: it holds four guarantees —
 * `disallowedElements` (this repo's only `img-src` protection), the CSV-injection
 * control, `remark-breaks`, and the `mode="static"` guard — that
 * `@assistant-ui/react-markdown` lacks; its 21-case parity checklist must pass first.
 */
import { createContext, useContext, useMemo, type FC } from 'react'

import { Thread, type ThreadComponents } from '../assistant-ui/thread'
import MessageContent from './MessageContent'
import AttachmentChips from '../AttachmentChips'
import ActivityGroup, { InterruptedMessagesContext, GroupSealedContext } from './ActivityGroup'
import ActivityRow from './ActivityRow'
import { WaitingLine } from '../ui/Waiting'

export interface ChatThreadProps {
  /**
   * Messages whose turn ended on an interrupted terminal. Supplied by the surface because
   * it is a fact about the turn, not about any part.
   */
  interruptedMessageIds?: ReadonlySet<string>
  /** Rendered under the viewport — the composer, the offer strip, the return-to-latest control. */
  footer?: FC | undefined
  /** Told what an activity group amounted to as it seals. */
  onGroupSealed?: ((summary: string) => void) | undefined
  /** When the running turn began (`Date.now()`), so the working line's elapsed count measures the
   *  TURN. The row itself is torn down and rebuilt between bursts and cannot time itself. */
  turnStartedAt?: number | null
}

/**
 * The running turn's start, held here because this component outlives the row that reads it.
 *
 * `working` goes false whenever a tool call takes the floor and true again on the next reasoning
 * burst, so `streamingParts` drops and re-appends the reasoning part several times in one turn and
 * `ReasoningGroup` is a NEW component instance each time. A clock owned by that row therefore
 * measures the burst, and a citizen watching a long build sees the number fall back. The anchor
 * lives above the remount so the count belongs to the turn.
 */
const TurnStartedAtContext = createContext<number | null>(null)

/**
 * The text part, rendered by the portal's own renderer — one Streamdown pipeline for every
 * message, with no branch on who wrote it. The remote-image block and the rest of the
 * parse/sanitise pipeline apply to a citizen's own prose exactly as they do to a model's.
 */
const TextPart: ThreadComponents['TextPart'] = ({ text }) => <MessageContent parts={text} />

/**
 * THE WORKING STATUS — status only, never the reasoning content (too technical here;
 * `useMessagePartReasoning` unused): a narrow exception to no-indicator-without-tools,
 * driven by the server's `working` flag, not TURN STATUS (once shown on every
 * message) — gone the instant writing or a call starts. ALSO APPEARS ON A TOOL-RUNNING
 * TURN: grouping is HIERARCHICAL, `reasoning` and `tool-call` sharing
 * `group-chainOfThought` but rendering separate children, so a build shows status first.
 *
 * IT CARRIES A CLOCK NOW, AND THAT IS THE WHOLE POINT. A single static line does not read as
 * "the machine is working"; it reads as the last thing that happened before everything stopped.
 * The reported symptom was exactly that — a citizen watching a build with no way to tell a
 * thinking model from a hung one. `WaitingLine` is the primitive the rest of the portal already
 * waits with: a glyph that spins under motion and becomes a static clock face under
 * `prefers-reduced-motion`, plus a live elapsed count once the wait outlives five seconds. The
 * number is what proves liveness without motion, and past five seconds it is the only question
 * the person in front of it has.
 *
 * The label stays deliberately plain. The platform can see THAT the model has the floor — it
 * cannot see what the model is thinking about, and the reasoning text is withheld from the
 * browser by design at three separate layers. So the line says the true thing and stops.
 */
const ReasoningGroup: ThreadComponents['ReasoningGroup'] = () => {
  const turnStartedAt = useContext(TurnStartedAtContext)
  return (
    <p data-testid="working-status" className="my-1 text-xs text-neutral">
      <WaitingLine label="Working on your app" active since={turnStartedAt} />
    </p>
  )
}

const noAnnouncement = () => {}

const ChatThread: FC<ChatThreadProps> = ({
  interruptedMessageIds,
  footer,
  onGroupSealed,
  turnStartedAt = null,
}) => {
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
    <InterruptedMessagesContext.Provider value={interrupted}>
      <GroupSealedContext.Provider value={announceSealed}>
        <TurnStartedAtContext.Provider value={turnStartedAt}>
          <Thread components={components} />
        </TurnStartedAtContext.Provider>
      </GroupSealedContext.Provider>
    </InterruptedMessagesContext.Provider>
  )
}

export default ChatThread
