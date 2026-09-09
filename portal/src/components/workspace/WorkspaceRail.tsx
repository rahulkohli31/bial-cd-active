/**
 * THE RAIL — one white column: start a chat, data, app status, description, hairlines between.
 *
 * It fills the shell's `<Outlet/>` column and must not draw the app pane as well: an iframe built
 * in here sits inside the content a route change replaces, so it remounts and reloads the running
 * app on the first navigation to a chat. The shell holds the pane as a sibling of the Outlet, and
 * a rail-plus-pane layout nested in here is how that gets undone.
 *
 * WHAT DELIBERATELY DOES NOT LIVE HERE: a second control that starts the app (the pane's Start is
 * the only one — two would race the same endpoint), the project's name, its publish chip and its
 * rename control (all surrendered to the toolbar row the shell draws above both columns), and the
 * recents list (deleted by the owner's ruling).
 *
 * WHAT DOES, AS OF THE CONNECTOR PASS: the DATA section, between START A CHAT and APP STATUS —
 * and it is the WHOLE of the connector's presence on this screen. The app pane says nothing about
 * data access and the composer carries no per-chat data control; both are rulings, and
 * `DataSection`'s own docblock carries the reasons.
 *
 * THE RAIL OWNS THE INTEGRATIONS DIALOG MOUNT, not the section that opens it, because the dialog
 * opens OVER the rail and its close is what has to drive `DataSection`'s re-read. Everything the
 * dialog can change — the switch, the chip, the count — is a fact this section fetched before it
 * opened, and the dialog reports none of it back; there is no query cache in this portal to
 * invalidate. So `onClose` closes it AND reloads, and dropping that reload is a silent regression
 * a component test cannot see (`DataSection.integration.test.tsx` is where it goes red).
 */
import { useState } from 'react'
import ProjectDescriptionEditor from '../projects/ProjectDescriptionEditor'
import RailComposer from './RailComposer'
import AppStatusPanel from './AppStatusPanel'
import DataSection from './DataSection'
import IntegrationsDialog from '../connectors/IntegrationsDialog'
import type { Project } from '../../utils/projectApi'
import { canBePutBack } from '../../utils/buildSessionApi'
import type { SaveState } from '../../utils/buildSessionApi'

export interface WorkspaceRailProps {
  project: Project
  /**
   * Null while the workspace is stopped. Reading the save state runs `git` inside the container,
   * so asking it of a stopped project would start one — a start the screen caused rather than the
   * citizen.
   */
  save: SaveState | null
  onProjectUpdate: (project: Project) => void
}

/** The board's section label: 10.5px, weight 700, .7px tracking. Its colour is per-section. */
function SectionLabel({ children, className = 'text-neutral' }: { children: string; className?: string }) {
  return <h2 className={`text-[10.5px] font-bold tracking-[.7px] ${className}`}>{children}</h2>
}

/**
 * WHAT THE PLATFORM MAY HONESTLY SAY ABOUT THE CITIZEN'S WORK — FOUR readings, not three, and
 * the fourth is the whole reason this is a function instead of a ternary in the markup.
 *
 * `dirty === true` was one arm, and on a freshly built app it said the wrong thing: somebody
 * described an app, the platform built it, they touched nothing, and the rail told them they had
 * changes that were not saved — then the exit guard stopped them on the way out over work they
 * had never done. `dirty` is NOT the defect and is not being softened here. It answers "is
 * there a saved version of this?", and on a fresh build the answer is genuinely no, because
 * Save is the citizen's own act and nothing else in the platform performs it.
 *
 * THE FACT THAT WAS MISSING IS `recoveryAt`. Non-null means the platform is holding this app's
 * newest tree somewhere it can be brought back from — and that is a fact, not a hope:
 * `newest_restore_source` (backend `services/build_sessions/manager.py`) is the single path
 * EVERY automatic restore goes through, and it hands back the recovery copy in preference to
 * the saved one whenever the recovery copy is the newer of the two. So the work of a build
 * nobody has saved survives a reclaim, a relaunch and a reload.
 *
 * HENCE THE SPLIT, AND WHY IT MUST NOT BE FOLDED BACK TO THREE. The two `true` arms are two
 * different situations wearing one flag: with `recoveryAt` there is nothing to lose and only a
 * version left to make; without it, the old warning is the honest sentence and stays exactly as
 * it was. Collapsing them puts the alarm back on every first build.
 *
 * AND IT STILL DOES NOT SAY "SAVED", deliberately. A recovery copy is not a version the citizen
 * chose; `dirty` stays true, the Save control stays where it is, and Save stays MANUAL. This
 * arm's whole job is to stop overstating the danger — never to understate the Save.
 */
function saveSentence(save: SaveState): string {
  // Order matters: the tri-state's `null` is answered FIRST, so a check that could not run can
  // never fall through into a claim about recoverable work.
  if (save.dirty === null) return 'We could not check for unsaved changes.'
  if (save.dirty === false) return 'Everything is saved.'
  // `canBePutBack`, not `recoveryAt !== null`: an `undefined` instant — a caller that predates
  // the field, a body without it — would otherwise read as a copy that exists and put the
  // reassuring sentence over work nothing is holding. Absent means say the warning.
  return canBePutBack(save.recoveryAt)
    ? 'Your work is safe. Save it to keep a version you can come back to.'
    : 'You have changes that are not saved yet.'
}

export default function WorkspaceRail({ project, save, onProjectUpdate }: WorkspaceRailProps) {
  const [integrationsOpen, setIntegrationsOpen] = useState(false)

  return (
    // `min-h-0` is what actually lets this flex child scroll: without it the child's min-content
    // height wins and the overflow never has anywhere to happen. The column is what lets the
    // description sit at the foot on a tall screen and scroll normally on a short one.
    <main className="flex flex-1 min-h-0 flex-col overflow-y-auto bg-white">
      <section className="px-[18px] pb-[15px] pt-4">
        <SectionLabel className="text-primary-900">START A CHAT</SectionLabel>
        <RailComposer projectId={project.id} />
      </section>

      <div className="h-px flex-shrink-0 bg-bial-border" />

      {/* APP STATUS's padding, not START A CHAT's — the boards give the two lower sections the
          same 15px band, and the composer's is the one that differs. */}
      <section data-testid="rail-data" className="px-[18px] py-[15px]">
        <DataSection
          projectId={project.id}
          label={<SectionLabel>DATA</SectionLabel>}
          onOpenIntegrations={() => setIntegrationsOpen(true)}
        />
      </section>

      <div className="h-px flex-shrink-0 bg-bial-border" />

      <section data-testid="rail-app-status" className="px-[18px] py-[15px]">
        {/* THE LABEL GOES DOWN INTO THE PANEL rather than being drawn above it, because the boards
            put it and the state pill on ONE row. The treatment is still this file's — the panel
            receives the rendered label, it does not write one. */}
        <AppStatusPanel projectId={project.id} label={<SectionLabel>APP STATUS</SectionLabel>} />
        {/* A DIFFERENT QUESTION FROM THE PANEL'S SAVED ROW, which reports the version the citizen
            last saved: this is whether the LIVE container has moved on since. `dirty` is TRI-STATE
            and its `null` is "could not tell" — collapsing it to a boolean turns a failed check
            into a confident "everything is saved". Which of the four sentences that makes true is
            `saveSentence`'s decision, above, where the reasoning can be read. */}
        {save && (
          <div data-testid="rail-save-state" className="mt-3 border-t border-bial-border pt-3">
            <p className="text-[11.5px] text-neutral">{saveSentence(save)}</p>
          </div>
        )}
      </section>

      <div className="min-h-0 flex-1" />

      <div className="h-px flex-shrink-0 bg-bial-border" />

      {/* THE TESTID IS KEPT DELIBERATELY. This is no longer a bordered card, but it is the same
          description block with the same read-view-plus-Edit-pop-up behaviour, and renaming the
          handle would retire the assertions that still hold as collateral of a layout change. */}
      <section data-testid="description-rail" className="px-[18px] pb-4 pt-3.5">
        <ProjectDescriptionEditor
          projectId={project.id}
          description={project.description}
          onProjectUpdate={onProjectUpdate}
        />
      </section>

      {/* The SAME dialog the profile menu opens (R5) — one component, two doors, no new route.
          THE RE-READ IS NOT WIRED HERE ANY MORE, deliberately: this door used to call
          `dataSection.reload()` on close and the profile-menu door called nothing, so entering
          from the avatar menu — which is on this very screen — left the section describing a
          project the drill-down had just changed. `IntegrationsDialog` now announces the write
          itself and `DataSection` subscribes, which covers both doors and any added later. Do
          not re-add a reload here: it would fire a second, redundant read on this door only. */}
      {integrationsOpen && <IntegrationsDialog onClose={() => setIntegrationsOpen(false)} />}
    </main>
  )
}
