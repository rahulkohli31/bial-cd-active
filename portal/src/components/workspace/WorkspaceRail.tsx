/**
 * THE RAIL — one white column, three sections, hairlines between them (plan 002, U3).
 *
 * ═══ WHAT THIS IS NOT ═══
 *
 * It is not a page and it is not a grid. The two-column frame belongs to `WorkspaceShell`, above
 * the Outlet, and this fills the Outlet's column. An implementer who builds rail-plus-pane in here
 * has produced a second two-column layout nested in the first, and every "the app did not remount"
 * assertion fails on the first navigation to a chat — because this component does not match that
 * address and the one holding the iframe must.
 *
 * ═══ THE SHAPE THE BOARDS DRAW, AND WHAT IT REPLACES ═══
 *
 * A #F0F4F8 rail with four floating white cards on it became a #FFFFFF rail with sections divided
 * by 1px #E2E8F0 rules. That is not decoration: it changes what those two colours MEAN. #E2E8F0 is
 * the divider on every board (262 occurrences) and had become a card outline; #F0F4F8 is the page
 * behind the APP and had become the rail's own ground.
 *
 * Three sections, in the board's order — START A CHAT, APP STATUS, DESCRIPTION — with the
 * description pushed to the foot by a spacer, exactly as `Main` and `NothingBuilt` draw it.
 *
 * ═══ THE CONVERSATIONS LIST IS GONE, AND WHAT THAT COSTS IS THE OWNER'S DECISION ═══
 *
 * A fourth card listed this project's past chats. It is deleted — the list, its read, the prop
 * chain that fed it all the way up through the page, and the delete-chat handler that had no other
 * caller. The client asked not to have a list of past conversations, and the ruling of 2026-09-02
 * is that NOTHING points back to a chat, running or finished: leaving a chat means starting a new
 * one.
 *
 * TWO CAPABILITIES GO WITH IT, KNOWINGLY. The only route back to an EXISTING chat, and the only
 * way to delete one. Chats, their plans and their uploaded files all stay in the database,
 * untouched; cleanup, if it is ever wanted, is a scheduled job the client can ask for later. This
 * is recorded here rather than only in a pull request because the next person to read this file
 * will otherwise reasonably assume it was an oversight.
 *
 * ═══ R6's FOUR THINGS, AND THE ONE WITH A CONSTRAINT ATTACHED ═══
 *
 * A composer with the kind beside it; the app's status; when it was last saved; the description.
 * Three are unconditional. The save half is not, and the reason is cost rather than design:
 * `fetchSaveState` runs two `git` executions inside the container, so it may only be asked while
 * the workspace is alive — asking a stopped project whether it has unsaved work is a start the
 * screen caused, which R3 forbids. So a stopped project's rail shows the status sentence and NO
 * save state, and the save half appears while the app is running.
 *
 * ═══ TWO DIFFERENT STATUSES, AND ONLY ONE OF THEM IS THIS SECTION'S ═══
 *
 * APP STATUS is about PUBLISHING — what is live, what was approved, what the citizen last saved.
 * Whether the container is up is a different question with a different answer, and the boards give
 * it to the PANE, which is where a citizen is already looking for their app. The rail used to
 * carry that sentence too, on the grounds that nobody should have to look across; the boards put
 * the publish panel in that space instead, and the workspace sentence keeps the one author it
 * already had.
 *
 * This section still carries no START control. R3 says exactly one starts the app and that one is
 * the pane's; a second here would satisfy "exactly one" with two. The action it DOES carry is the
 * publish ladder's, which the pane has never had.
 *
 * ═══ THE HEADER AND THE COLLAPSE CONTROL ARE BOTH ELSEWHERE, ON PURPOSE ═══
 *
 * Back, the project name, the status chip and rename went to the toolbar row (U2), which is drawn
 * above both columns and therefore survives a collapse — the rail's own header did not, which is
 * what made the project name vanish on the very board that draws it staying. The collapse control
 * is there too: a collapsed rail is `w-0` and `invisible`, out of the tab order and out of the
 * accessibility tree, so a toggle living inside it would be a one-way door.
 */
import ProjectDescriptionEditor from '../projects/ProjectDescriptionEditor'
import RailComposer from './RailComposer'
import AppStatusPanel from './AppStatusPanel'
import type { Project } from '../../utils/projectApi'
import type { SaveState } from '../../utils/buildSessionApi'

export interface WorkspaceRailProps {
  project: Project
  /** Non-null only while the workspace is alive — see the cost note above. */
  save: SaveState | null
  onProjectUpdate: (project: Project) => void
}

/** The board's section label: 10.5px, weight 700, .7px tracking. Its colour is per-section. */
function SectionLabel({ children, className = 'text-neutral' }: { children: string; className?: string }) {
  return <h2 className={`text-[10.5px] font-bold tracking-[.7px] ${className}`}>{children}</h2>
}

export default function WorkspaceRail({ project, save, onProjectUpdate }: WorkspaceRailProps) {
  return (
    // ITS OWN SCROLLER, and a COLUMN. The shell is a full-height frame that does not scroll and
    // this is a flex child of it; `min-h-0` is what actually lets a flex child scroll, because
    // without it the child's min-content height wins and the overflow never has anywhere to
    // happen. The column is what lets the description sit at the foot on a tall screen, as the
    // boards draw it, and scroll normally on a short one.
    <main className="flex flex-1 min-h-0 flex-col overflow-y-auto bg-white">
      <section className="px-[18px] pb-[15px] pt-4">
        <SectionLabel className="text-primary-900">START A CHAT</SectionLabel>
        <RailComposer projectId={project.id} />
      </section>

      <div className="h-px flex-shrink-0 bg-bial-border" />

      {/* R6's app status. THE SENTENCE ONLY — the action belongs to the pane (see the docblock).
          Same computed value, so the two surfaces cannot say different things. U4 gives this
          section its provenance rows, its colour-coded states and its own action button. */}
      <section data-testid="rail-app-status" className="px-[18px] py-[15px]">
        <SectionLabel>APP STATUS</SectionLabel>
        {/* WHERE THE APP STANDS — publishing, approval and the citizen's own last save, from the
            one server-computed state. `AppStatusPanel` and the toolbar row's chip put that state
            through the same presentation map, so the two can never say different things. */}
        <AppStatusPanel projectId={project.id} />
        {/* WHETHER THE CONTAINER HAS WORK THE BUNDLE DOES NOT — a DIFFERENT question from the
            panel's, and it is worth being clear about why both are here. The panel's saved row
            says which version the citizen last SAVED, from object-store metadata, on a project
            whose container may be long gone. This says whether the LIVE container has moved on
            since, which only a running container can answer at all. `dirty` is TRI-STATE and its
            `null` is "could not tell", never "clean". */}
        {save && (
          <div data-testid="rail-save-state" className="mt-3 border-t border-bial-border pt-3">
            <p className="text-[11.5px] text-neutral">
              {save.dirty === true
                ? 'You have changes that are not saved yet.'
                : save.dirty === false
                  ? 'Everything is saved.'
                  : 'We could not check for unsaved changes.'}
            </p>
          </div>
        )}
      </section>

      {/* THE BOARD'S SPACER. On a tall screen the description sits at the foot of the rail rather
          than floating under the status; on a short one it collapses and the rail scrolls. */}
      <div className="min-h-0 flex-1" />

      <div className="h-px flex-shrink-0 bg-bial-border" />

      {/* THE TESTID IS KEPT DELIBERATELY. This is no longer a bordered card — the rail is one
          panel of sections now — but it is the same description block with the same
          read-view-plus-Edit-pop-up behaviour, and several assertions still say something true
          about it. Renaming the handle would have retired them as collateral of a layout change,
          which is exactly the silent removal the removal convention forbids. */}
      <section data-testid="description-rail" className="px-[18px] pb-4 pt-3.5">
        <ProjectDescriptionEditor
          projectId={project.id}
          description={project.description}
          onProjectUpdate={onProjectUpdate}
        />
      </section>
    </main>
  )
}
