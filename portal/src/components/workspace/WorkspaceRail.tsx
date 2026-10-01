import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router-dom'
import RailComposer from './RailComposer'
import ChatHistoryPanel from './ChatHistoryPanel'
import LastChatCard from './LastChatCard'
import { useProjectChats } from '../../hooks/useProjectChats'
import { isChatHistoryPath } from '../../utils/chatHistoryAddress'
import type { Project } from '../../utils/projectApi'

/**
 * THE RAIL ON AN APPLICATION'S OWN SCREEN — the way to start a chat, and the way back to past ones.
 *
 * On the project address it holds the composer, CENTRED because it is alone in a tall column, with
 * the last chat under it; on the chat list address it holds that list. One component serves both
 * because `ProjectPage` serves both: the move between them re-renders this rail and nothing else,
 * and the one chats read here feeds the card and the list alike. A chat itself renders in
 * `ConversationSlot`, never here.
 *
 * It fills the shell's `<Outlet/>` column and must not draw the app pane as well: an iframe built
 * in here sits inside the content a route change replaces, so it remounts and reloads the running
 * app on the first navigation to a chat. The shell holds the pane as a sibling of the Outlet, and
 * a rail-plus-pane layout nested in here is how that gets undone.
 *
 * WHAT DELIBERATELY DOES NOT LIVE HERE: a second control that starts the app (the pane's Start is
 * the only one — two would race the same endpoint), the project's name, its publish chip and its
 * rename control (all surrendered to the toolbar row the shell draws above both columns), and the
 * application's data access, status and description — each of which now has a home in that
 * application's settings, reachable from the toolbar's menu and from the home list alike. Nor what
 * the platform owes the citizen about their app's life — its ceiling and a refused write-back are
 * stated in the pane column, so that a citizen mid-conversation is told, and a column that is a
 * chat's empty state cannot reach them.
 */
export interface WorkspaceRailProps {
  project: Project
}

export default function WorkspaceRail({ project }: WorkspaceRailProps) {
  const chats = useProjectChats(project.id)
  const listing = isChatHistoryPath(useLocation().pathname)
  const viewAllRef = useRef<HTMLAnchorElement>(null)

  // Closing the list from its own back control leaves focus nowhere; it goes to View all.
  const wasListing = useRef(listing)
  useEffect(() => {
    const closed = wasListing.current && !listing
    wasListing.current = listing
    if (closed && (document.activeElement === null || document.activeElement === document.body)) {
      viewAllRef.current?.focus()
    }
  }, [listing])

  if (listing) {
    return (
      <main className="flex min-h-0 flex-1 flex-col overflow-y-auto bg-white">
        <ChatHistoryPanel projectId={project.id} chats={chats} />
      </main>
    )
  }

  const last = chats.chats[0]
  return (
    // `min-h-0` is what actually lets this child scroll: without it the child's min-content height
    // wins and the overflow never has anywhere to happen. The two equal outer rows are what centre
    // the composer in the whole column, so the last chat arriving in the lower one moves nothing.
    <main className="grid min-h-0 flex-1 grid-rows-[1fr_auto_1fr] overflow-y-auto bg-white">
      <section className="row-start-2 px-[18px] py-4">
        <h2 className="text-[10.5px] font-bold tracking-[.7px] text-primary-900">START A CHAT</h2>
        <RailComposer projectId={project.id} />
      </section>
      {last !== undefined && (
        <div className="row-start-3 self-end">
          <LastChatCard projectId={project.id} chat={last} viewAllRef={viewAllRef} />
        </div>
      )}
    </main>
  )
}
