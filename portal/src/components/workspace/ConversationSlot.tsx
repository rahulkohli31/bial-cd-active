/**
 * ONE SLOT FOR THE MOUNTED CONVERSATION. It owns five things and no more.
 *
 * WHICH BODY renders: one `ConversationSurface` for both kinds, with `kind` passed through.
 * WHETHER IT IS VISIBLE: the hide treatment is applied here and defined in `hiddenSubtree.ts`,
 * because the chat-panel collapse is its other caller and a page importing this slot would close a
 * cycle. THE DRAFT: both kinds share `utils/composerDraft.ts` — `sessionStorage`, keyed per
 * conversation, cleared only on a successful send. THE WAY BACK: a project chat is headed by the
 * strip that returns to its application's chat list. NOTHING ELSE: transport, transcript and
 * attachments live on the surface, and the router still decides which conversation is mounted —
 * this slot keeps no stack alive, so a project↔chat move unmounts the conversation and only the
 * draft and the app pane survive it.
 */
import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router-dom'
import ConversationSurface from '../chat/ConversationSurface'
import type { ChatKind } from '../../pages/ChatRoute'
import type { Project } from '../../utils/projectApi'
import { chatKindFor } from '../../utils/chatKind'
import { chatHistoryPath, chatListOpenedFrom, returningFromChat } from '../../utils/chatHistoryAddress'
import { HIDDEN_BUT_MOUNTED } from './hiddenSubtree'
import { KindChip, RoundBackLink, untitledChatName } from './chatHistoryColumns'
import { useWorkspaceHeading, useWorkspacePaneVisible } from './workspaceChannel'

/** What `ChatRoute` resolved: which conversation, of which kind, in which project. */
export interface MountedConversation {
  chatId: string
  kind: ChatKind
  projectId: string | null
  /** `null` while the project loads, or after its fetch failed. */
  project: Project | null
  projectHasSavedBuild: boolean | null
}

interface Props {
  conversation: MountedConversation
  /** Passed through to the surface — see `ConversationSurfaceProps.onProjectUpdate`. */
  onProjectUpdate: (project: Project) => void
  /** Passed through to the surface — see `ConversationSurfaceProps.onTitleDerived`. */
  onTitleDerived?: (title: string) => void
  /**
   * Hide the conversation without discarding it. Nothing sets it yet — the builder surface's own
   * chat-panel collapse hides a panel, not the whole conversation. It exists here so that the
   * first caller needing a whole-conversation hide finds one, rather than inventing a second one
   * next to it.
   */
  hidden?: boolean
}

/**
 * The chat's header strip: back to the chat list, the chat's title and its kind. It links to the
 * list the chat was opened from, tab, search, sort and page included, and never steps through
 * browser history to get there.
 */
function AllChatsStrip({ chatId, kind, projectId }: { chatId: string; kind: ChatKind; projectId: string }) {
  const heading = useWorkspaceHeading()
  const paneVisible = useWorkspacePaneVisible()
  const listSearch = chatListOpenedFrom(useLocation().state)
  const titleRef = useRef<HTMLHeadingElement>(null)
  const openedFromList = listSearch !== null
  const name = heading.chatTitle || untitledChatName(chatKindFor(kind))

  useEffect(() => {
    if (openedFromList) titleRef.current?.focus()
  }, [chatId, openedFromList])

  return (
    <div data-testid="all-chats-strip" className="flex-shrink-0 border-b border-bial-border bg-white px-[18px] py-3">
      {/* Without the app beside it the chat fills the window, and the strip keeps to the
          transcript's measure. */}
      <div className={`flex items-center gap-2.5 ${paneVisible ? '' : 'mx-auto w-full max-w-thread'}`}>
        <RoundBackLink
          to={chatHistoryPath(projectId) + (listSearch ?? '')}
          state={returningFromChat(chatId)}
          label="Back to all chats"
        />
        <div className="min-w-0">
          <p className="text-[11px] font-semibold text-neutral">All chats</p>
          <h2
            ref={titleRef}
            tabIndex={-1}
            title={name}
            data-testid="all-chats-strip-title"
            className="truncate text-[13.5px] font-bold text-primary-900 focus:outline-none"
          >
            {name}
          </h2>
        </div>
        <span className="ml-auto flex flex-shrink-0 items-center">
          <KindChip kind={kind} />
        </span>
      </div>
    </div>
  )
}

export default function ConversationSlot({ conversation, hidden = false, onProjectUpdate, onTitleDerived }: Props) {
  // `kind` decides whether the surface declares the app pane visible, and names the strip's chip.
  // What a turn may do to the app is the server toolset's decision — see
  // `ConversationCreateRequest.kind`.
  const { chatId, kind, projectId, project, projectHasSavedBuild } = conversation
  const shared = { chatId, projectId, project }

  return (
    <div
      data-testid="conversation-slot"
      aria-hidden={hidden}
      className={`flex-1 min-h-0 flex flex-col overflow-hidden ${hidden ? HIDDEN_BUT_MOUNTED : ''}`}
    >
      {projectId !== null && <AllChatsStrip chatId={chatId} kind={kind} projectId={projectId} />}
      <ConversationSurface
        {...shared}
        kind={kind}
        projectHasSavedBuild={projectHasSavedBuild}
        onProjectUpdate={onProjectUpdate}
        onTitleDerived={onTitleDerived}
      />
    </div>
  )
}
