/**
 * THE PROJECT SURFACE (Plan F, U1) — the rail's contents, and the project-scoped publisher.
 *
 * ═══ THE HEADLINE BEHAVIOUR THIS FILE EXISTS FOR ═══
 *
 * Before this, the workspace channel had exactly ONE publisher in the whole tree: the conversation
 * surface, which mounts only when a chat is open. The project page subscribed and never published,
 * so on a fresh load of a bare `/projects/:id` the pane host hit its own "no pane and no address"
 * early return and rendered NOTHING. R3's whole point — the app is there, behind one deliberate
 * press, on the project screen — was unreachable, and no test could see it because the project
 * page had no pane to assert about.
 *
 * This is the symmetric counterpart: the project-scoped publisher. Exactly one of the two is ever
 * mounted for a given address, so there is no contest — only continuity across the hop, which the
 * channel's per-payload rules already handle (`address` and `project` survive an unmount; `pane`,
 * `visible` and the workspace report clear).
 *
 * ═══ ONE READ, TWO CONSUMERS — NOT A SECOND POLL ═══
 *
 * `useWorkspaceState` performs the preview-state read with its own cadence and visibility
 * handling. The address is built from THAT SAME RESULT, feeding only the project-scoped input and
 * leaving every chat-scoped one at rest. U2's rule that its pure map neither takes nor returns an
 * address is about the MAP's type; it is not a bar on the caller that already holds the read.
 *
 * THE PRECEDENCE IS `previewAddress.ts`'s AND IS NOT RE-DERIVED HERE. Its two comments already name
 * this caller: the conversation surface's `projectPreviewUrl: null` block says the populated arm
 * "exists for the caller that has a project and no chat — the project surface", and the resolver's
 * own docblock says that arm "is the only one that does not require a chat … without it the project
 * screen frames nothing." That gap is closed; both comments now describe a closed one.
 *
 * ═══ PUBLISH THROUGH THE HOOKS, NEVER A RAW CHANNEL SET ═══
 *
 * The hooks carry the "I have nothing yet is not the same as there is nothing" protection: a
 * publisher abstains on its first renders until it has resolved something, which is what stops a
 * remount from retiring a frame the departing surface left standing. Reimplementing that with a
 * direct `channel.address.set` is how the round trip breaks — silently, and only on the return leg.
 *
 * ═══ WHAT THIS FILE DELIBERATELY DOES NOT DO ═══
 *
 * It does NOT fire the project-opened beacon. `ProjectPage` does, from its successful-load branch,
 * and from exactly one place. This component independently needs `project.appId` for the rail's
 * status line, which is precisely the pull that would make somebody add a second tracker here —
 * and `observe.ts`'s per-project guard makes a repeated call a safe no-op, so the risk is not
 * defeating that guard but BYPASSING it with a second mechanism it does not cover.
 */
import { useCallback, useMemo, useState } from 'react'
import WorkspaceRail from './WorkspaceRail'
import ProjectRenameDialog from '../projects/ProjectRenameDialog'
import { useWorkspaceState } from './useWorkspaceState'
import type { StartOutcome } from './workspaceState'
import {
  useAppPaneVisible,
  usePublishAddress,
  usePublishPaneView,
  usePublishReclaim,
  usePublishSave,
  usePublishSaveState,
  usePublishWorkspaceReport,
  useWorkspaceProject,
} from './workspaceChannel'
import type { ReclaimRequest } from './workspaceChannel'
import { resolvePreviewAddress } from '../../utils/previewAddress'
import { handOverWorkspace } from '../../utils/buildSessionApi'
import type { ReclaimBlocked } from '../../utils/buildSessionApi'
import type { Project } from '../../utils/projectApi'

export interface ProjectWorkspaceProps {
  project: Project
  onProjectUpdate: (project: Project) => void
}

export default function ProjectWorkspace(props: ProjectWorkspaceProps) {
  const { project } = props
  // THE URL A START JUST PRODUCED, fed into the resolver's RELAUNCHED arm — the one arm that needs
  // no session and no chat, and which resolves its own status to `ready` because a restore has no
  // build lifecycle. Without it the pane waits for the next poll tick to frame an app the citizen
  // just pressed a button to bring up, which reads as the press having done nothing.
  const [startedPreviewUrl, setStartedPreviewUrl] = useState<string | null>(null)
  const [reclaim, setReclaim] = useState<{ blocked: ReclaimBlocked; retry: () => Promise<void> } | null>(null)
  // THE RENAME'S STATE IS HERE BECAUSE ITS DATA IS. The control is in the shell's toolbar row,
  // which sits above the Outlet and has no project object; this surface has both the project and
  // the update callback, so the row publishes a press upward and the editing happens down here.
  const [renaming, setRenaming] = useState(false)
  const startRename = useCallback(() => setRenaming(true), [])

  const workspace = useWorkspaceState({
    projectId: project.id,
    // The project row's own cold-load answer. `hasRelaunchableSnapshot` is the honest predicate —
    // whether a restore would actually FIND something — rather than `appId`, which is minted by
    // provision before anything is built and so advertised a saved build for every project whose
    // first build failed.
    projectHasSavedBuild: project.hasRelaunchableSnapshot,
  })

  // WHAT TO FRAME. Only the project-scoped arm is fed: this surface has a project and no chat, so
  // every chat-scoped input is genuinely absent rather than merely unavailable here. `alive` is the
  // one state whose `previewUrl` is framable — that is the wire's own contract — and any other
  // state resolves to no address, which is a correct answer and not a fallback to invent one for.
  const address = resolvePreviewAddress({
    turnPreviewUrl: null,
    turnStatus: null,
    narratingChatIsOpenChat: false,
    relaunchedUrl: startedPreviewUrl,
    sessionUrl: null,
    sessionStatus: null,
    sessionId: null,
    projectPreviewUrl: workspace.preview?.state === 'alive' ? workspace.preview.previewUrl : null,
    // The project predicate is trivially true here: these signals came from a read keyed on the
    // project this surface is showing. It is passed rather than assumed because the resolver's own
    // note says an arm must carry its predicate INTO the module — a gate that depends on where it
    // was declared is one reorder away from silently opening.
    sessionBelongsToOpenProject: true,
    transcriptHasBuildOutcome: false,
  })

  const onReclaimRefusal = useCallback((blocked: ReclaimBlocked, retry: () => Promise<void>) => {
    // FIRST REFUSAL WINS. The dialog must not change under the person reading it: they read one
    // project's name, and by the time they press a button the props would describe another — an
    // irreversible action taken against a sentence nobody saw.
    setReclaim((held) => held ?? { blocked, retry })
  }, [])

  const request: ReclaimRequest | null = useMemo(() => {
    if (!reclaim) return null
    return {
      blocked: reclaim.blocked,
      // The project this surface IS — the one being started, which is what the dialog leads with.
      startingProjectName: project.name,
      resolve: async (save: boolean) => {
        // Stop, then save, then release — the ordering invariant lives in `handOverWorkspace`.
        // The retry is AWAITED BEFORE the dialog is dismissed, so a switch that fails can still
        // be reported instead of vanishing with the dialog.
        await handOverWorkspace(reclaim.blocked.projectId, save)
        await reclaim.retry()
        setReclaim(null)
      },
      cancel: () => setReclaim(null),
    }
  }, [reclaim, project.name])

  const report = useMemo(
    () => ({
      state: workspace.state,
      projectId: project.id,
      onStarted: setStartedPreviewUrl,
      onStartPending: workspace.reportStartPending,
      onStartOutcome: (outcome: StartOutcome | null) => {
        workspace.reportStartOutcome(outcome)
        // A start that reached the app clears the outcome AND asks again immediately, so the pane
        // arrives at the running app on the press rather than on the next tick of a 45-second timer.
        if (outcome === null) workspace.refresh()
      },
      onRefresh: workspace.refresh,
      onReclaimRefusal,
    }),
    [workspace, project.id, onReclaimRefusal],
  )

  const paneView = useMemo(
    () => ({
      // NO TURN RUNS ON THIS SURFACE. Every one of these describes a build in flight, and there is
      // none: this screen starts no turn and owns no session. `completedLive` is the one to look
      // at twice — it is the "this container is alive under an idle lease" pardon that lets a frame
      // outrank a terminal status, and an app reached from the project screen is exactly that.
      iterating: false,
      reconnecting: false,
      relaunching: false,
      relaunchError: null,
      lastBuildFailed: false,
      restoredFromFailedBuild: false,
      completedLive: true,
      turnRunning: false,
      hasSavedBuild: workspace.preview?.restorable ?? project.hasRelaunchableSnapshot,
      previewState: workspace.preview?.state ?? null,
      occupyingProjectName: workspace.preview?.occupyingProjectName ?? null,
      // App-scoped facts this surface does not read. The compile state's producer is a turn and
      // `checkWorkspace` costs a container exec; neither is a question the project screen asks
      // (R3 — the screen must not cause a container call).
      compileState: null,
      workspaceLost: false,
    }),
    [workspace.preview, project.hasRelaunchableSnapshot],
  )

  useWorkspaceProject(project.id)
  usePublishAddress(address, project.id)
  usePublishPaneView(paneView)
  usePublishWorkspaceReport(report)
  usePublishSaveState(workspace.save?.dirty ?? null)
  // THE SAVE CONTROL'S VALUES, FOR THE TOOLBAR ROW, AND STILL NO ACTION FROM HERE.
  //
  // `rename` IS an action this surface owns: the row draws the project's name, and the control
  // that edits it used to sit in the rail header the boards replace. `save` is deliberately still
  // `null` — this plan's U11 gives the project screen a writer of the bundle, and until it does,
  // publishing a handler would put a pressable Save on a screen with nothing behind it. The row
  // reads `canSave` from this and draws a status rather than a button.
  usePublishSave(
    { dirty: workspace.save?.dirty ?? null, saving: false, error: null },
    { save: null, rename: startRename },
  )
  usePublishReclaim(request)
  // TWO COLUMNS ARE THE REST STATE of the project screen — not something contingent on a build
  // having run. A project with nothing built shows the empty-state sentence IN the pane, not a
  // hidden pane, because "there is nothing here yet" is a thing the app pane should say rather
  // than an absence a citizen has to interpret.
  useAppPaneVisible(true)

  return (
    <>
      <WorkspaceRail {...props} workspace={workspace.state} save={workspace.save} />
      {renaming && (
        <ProjectRenameDialog
          project={project}
          onProjectUpdate={props.onProjectUpdate}
          onClose={() => setRenaming(false)}
        />
      )}
    </>
  )
}
