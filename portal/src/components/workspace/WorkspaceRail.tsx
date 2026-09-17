import RailComposer from './RailComposer'
import WorkspaceLifecycleNotes from './WorkspaceLifecycleNotes'
import type { Project } from '../../utils/projectApi'

/**
 * THE RAIL — one white column holding one thing: the way to start a chat.
 *
 * IT IS THE CHAT COLUMN'S EMPTY STATE. An application with no chat yet has nothing to show on
 * this side, so the composer IS the side; once a transcript exists the address is a chat and the
 * conversation surface renders here instead. That is why the composer is CENTRED here and sits at
 * the foot there: a lone block parked at the bottom of a tall empty column reads as a page that
 * failed to draw the rest of itself.
 *
 * It fills the shell's `<Outlet/>` column and must not draw the app pane as well: an iframe built
 * in here sits inside the content a route change replaces, so it remounts and reloads the running
 * app on the first navigation to a chat. The shell holds the pane as a sibling of the Outlet, and
 * a rail-plus-pane layout nested in here is how that gets undone.
 *
 * WHAT DELIBERATELY DOES NOT LIVE HERE: a second control that starts the app (the pane's Start is
 * the only one — two would race the same endpoint), the project's name, its publish chip and its
 * rename control (all surrendered to the toolbar row the shell draws above both columns), the
 * recents list (deleted by the owner's ruling), and the application's data access, status and
 * description — each of which now has a home in that application's settings, reachable from the
 * toolbar's menu and from the home list alike.
 */
export interface WorkspaceRailProps {
  /**
   * WHAT THE PLATFORM OWES THIS CITIZEN ABOUT THEIR APP'S LIFE. Optional because the rail is
   * also rendered where neither fact is known, and a missing note must never read as "no
   * ceiling" or "the write-back was fine".
   */
  lifecycle?: { drainingAt: string | null; writeBackRefusedAt: string | null }
  project: Project
}

export default function WorkspaceRail({ project, lifecycle }: WorkspaceRailProps) {
  return (
    // `min-h-0` is what actually lets this flex child scroll: without it the child's min-content
    // height wins and the overflow never has anywhere to happen. `justify-center` is what puts the
    // composer in the middle of a column it is alone in; on a short window the scroller takes over
    // and nothing is pushed out of reach.
    <main className="flex flex-1 min-h-0 flex-col justify-center overflow-y-auto bg-white">
      {lifecycle && (
        <WorkspaceLifecycleNotes
          drainingAt={lifecycle.drainingAt}
          writeBackRefusedAt={lifecycle.writeBackRefusedAt}
        />
      )}
      <section className="px-[18px] py-4">
        <h2 className="text-[10.5px] font-bold tracking-[.7px] text-primary-900">START A CHAT</h2>
        <RailComposer projectId={project.id} />
      </section>
    </main>
  )
}
