/**
 * ONE SLOT FOR THE MOUNTED CONVERSATION (Plan A, U5).
 *
 * ═══ WHAT IT OWNS — four things, and no more ═══
 *
 *  1. WHICH BODY renders for the resolved kind. This is the largest kind branch in the product and
 *     it used to be the reason two page components existed: `ChatRoute` picked a whole page, so
 *     each kind got its own root layout, its own height model and its own opinion about the app
 *     pane. Moving it here does not delete it — saying that plainly matters, because claiming the
 *     collapse would make R72's surface half sound delivered while a citizen still gets a
 *     different React page per kind. What it did was give the branch exactly ONE home, so Plan D
 *     had one line to delete — and it has now deleted it. The slot mounts `ConversationSurface`
 *     for both kinds and no longer reads `kind` at all.
 *  2. WHETHER THE MOUNTED CONVERSATION IS VISIBLE. The slot is where the hide treatment is
 *     APPLIED — `hiddenSubtree.ts` is where it is defined and where its WCAG reasoning lives,
 *     because the builder surface's chat-panel collapse is its other caller and a page importing
 *     the slot that mounts pages is a cycle.
 *  3. THE DRAFT for it — see below.
 *  4. Nothing else. Transport, transcript and attachments live on the surface itself.
 *
 * ═══ THE ROUTER STILL OWNS WHICH CONVERSATION IS MOUNTED ═══
 *
 * This slot keeps no stack of visited conversations alive. A project↔chat move still unmounts the
 * conversation, exactly as it does today; what survives that is the DRAFT, in the shared store,
 * and the app pane, which the shell holds. R8a's "hidden, not discarded" is about the rail showing
 * something else while a conversation stays mounted — Plan F's modes are its first real caller.
 *
 * ═══ THE DRAFT IS THE SLOT'S, AND IT IS ONE STORE FOR BOTH KINDS ═══
 *
 * The builder surface already kept its composer text in `utils/composerDraft.ts` — `sessionStorage`,
 * keyed per conversation, cleared only on a successful send. The planning surface did not: its text
 * lived only in assistant-ui's in-memory composer store and was cleared on every chat change
 * INCLUDING the first mount after a reload, so a planning draft died on a reload and on a round
 * trip to a sibling chat.
 *
 * Both kinds now use the one store. That is the single deliberate behaviour change in an otherwise
 * behaviour-preserving plan, it is a strict gain, and it is what R72 asks for. Plan D consumes this
 * store and specifies none of it, so there is exactly one writer per key.
 *
 * ═══ WHAT THIS SLOT DOES NOT DELIVER, AND MUST NOT PRETEND TO ═══
 *
 * Resume after a dropped connection. The two kinds are on two different transports and only one of
 * them has a reattach path at all; Plans B and D own that. Nor does this collapse the two page
 * components — moving a branch is not deleting it.
 */
import ConversationSurface from '../chat/ConversationSurface'
import type { ChatKind } from '../../pages/ChatRoute'
import { HIDDEN_BUT_MOUNTED } from './hiddenSubtree'

/** What `ChatRoute` resolved: which conversation, of which kind, in which project. */
export interface MountedConversation {
  chatId: string
  kind: ChatKind
  projectId: string | null
  projectName: string | null
  projectHasSavedBuild: boolean | null
}

interface Props {
  conversation: MountedConversation
  /**
   * Hide the conversation without discarding it. No caller in this plan sets it — the builder
   * surface's own chat-panel collapse hides a panel, not the whole conversation, and Plan F's rail
   * modes are the first real caller. It exists here so that when they arrive there is one hide
   * with the reasoning already attached, rather than a second one invented next to it.
   */
  hidden?: boolean
}

export default function ConversationSlot({ conversation, hidden = false }: Props) {
  // `kind` is deliberately NOT destructured. It is still resolved by the route and still carried
  // on `MountedConversation`, but nothing here reads it — and pulling it into scope is the first
  // step back toward branching on it.
  const { chatId, projectId, projectName, projectHasSavedBuild } = conversation
  const shared = { chatId, projectId, projectName }

  return (
    <div
      data-testid="conversation-slot"
      aria-hidden={hidden}
      className={`flex-1 min-h-0 flex flex-col overflow-hidden ${hidden ? HIDDEN_BUT_MOUNTED : ''}`}
    >
      {/* THE KIND COMPARISON THAT LIVED HERE IS GONE (Plan D U17). It picked one of two page
          components; there is one surface now, and it renders identically for both kinds because
          nothing in it — or below it — asks what kind a conversation is. `kind` is still resolved
          by `ChatRoute` and still travels on `MountedConversation`, because the route genuinely
          knows it and a later reader may need it; what has stopped is anything BRANCHING on it.
          Everything about a conversation is decided by what is present or absent instead: a Plan
          chat shows no build because no build parts arrive in it, never because a renderer
          checked. */}
      <ConversationSurface {...shared} projectHasSavedBuild={projectHasSavedBuild} />
    </div>
  )
}
