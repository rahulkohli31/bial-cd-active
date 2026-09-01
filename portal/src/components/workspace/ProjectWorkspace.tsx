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
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import WorkspaceRail from './WorkspaceRail'
import type { ChatSummary } from './WorkspaceRail'
import { WORKSPACE_RAIL_ID } from './WorkspaceShell'
import { useWorkspaceState } from './useWorkspaceState'
import type { StartOutcome } from './workspaceState'
import {
  useAppPaneVisible,
  usePublishAddress,
  usePublishPaneView,
  usePublishRailSlot,
  usePublishReclaim,
  usePublishSaveState,
  usePublishWorkspaceReport,
  useWorkspaceProject,
} from './workspaceChannel'
import type { ReclaimRequest } from './workspaceChannel'
import { resolvePreviewAddress } from '../../utils/previewAddress'
import { releaseProject, saveProject, stopActiveBuild } from '../../utils/buildSessionApi'
import type { ReclaimBlocked } from '../../utils/buildSessionApi'
import type { Project } from '../../utils/projectApi'

export interface ProjectWorkspaceProps {
  project: Project
  chats: ChatSummary[]
  chatsError: string | null
  onProjectUpdate: (project: Project) => void
  onBack: () => void
  onOpenChat: (chatId: string) => void
  onDeleteChat: (chatId: string) => void
  editingName: boolean
  nameDraft: string
  nameError: string | null
  onStartRename: () => void
  onNameDraftChange: (value: string) => void
  onSubmitRename: () => void
  onCancelRename: () => void
  menuOpenId: string | null
  onToggleMenu: (chatId: string | null) => void
}

/** The bag the rail hands the shell. Frozen, so the channel's value comparison stays meaningful. */
const NO_RAIL_STATE: Record<string, unknown> = Object.freeze({})

export default function ProjectWorkspace(props: ProjectWorkspaceProps) {
  const { project } = props
  const [collapsed, setCollapsed] = useState(false)
  const [reclaim, setReclaim] = useState<{ blocked: ReclaimBlocked; retry: () => Promise<void> } | null>(null)

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
    relaunchedUrl: null,
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
      resolve: async (save: boolean) => {
        // STOP, THEN SAVE, THEN RELEASE, THEN RETRY — the existing ordering, which is load-bearing:
        // save and release both refuse while a live session owns the container, so the stop is what
        // unblocks them. The retry is AWAITED BEFORE the dialog is dismissed, so a switch that
        // fails can still be reported instead of vanishing with the dialog.
        await stopActiveBuild(reclaim.blocked.projectId)
        if (save) await saveProject(reclaim.blocked.projectId)
        await releaseProject(reclaim.blocked.projectId)
        await reclaim.retry()
        setReclaim(null)
      },
      cancel: () => setReclaim(null),
    }
  }, [reclaim])

  const railSlot = useMemo(
    () => ({ mode: null, state: NO_RAIL_STATE, stacked: false, collapsed }),
    [collapsed],
  )

  const report = useMemo(
    () => ({
      state: workspace.state,
      projectId: project.id,
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
      // THE COLLAPSE CONTROL LIVES IN THE PANE, and it has to. A collapsed rail is `w-0` and
      // `invisible` — out of the tab order and out of the accessibility tree — so a toggle inside
      // it would be a one-way door. The pane is what stays on screen, which makes it the only place
      // the control is reachable in both states. `aria-controls` is what ties the two ends together
      // for anyone reading or navigating the markup.
      toolbarLeading: (
        <button
          type="button"
          onClick={() => setCollapsed((was) => !was)}
          aria-expanded={!collapsed}
          aria-controls={WORKSPACE_RAIL_ID}
          aria-label={collapsed ? 'Show project details' : 'Hide project details'}
          title={collapsed ? 'Show project details' : 'Hide project details'}
          className="p-1.5 rounded-lg text-neutral hover:text-primary hover:bg-bial-bg transition"
        >
          {collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
        </button>
      ),
      // The publishing chip is the rail's, beside the project name (R37). Nothing here.
      toolbarTrailing: null,
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
      // The save model. `dirty` is TRI-STATE and stays tri-state — `null` is UNKNOWN, never clean.
      // No `onSave`: saving is the user's click from a surface that has one, and this plan adds no
      // second writer of the bundle.
      saveDirty: workspace.save?.dirty ?? null,
      saving: false,
      saveError: null,
    }),
    [collapsed, workspace.preview, workspace.save, project.hasRelaunchableSnapshot],
  )

  useWorkspaceProject(project.id)
  usePublishAddress(address, project.id)
  usePublishPaneView(paneView)
  usePublishRailSlot(railSlot)
  usePublishWorkspaceReport(report)
  usePublishSaveState(workspace.save?.dirty ?? null)
  usePublishReclaim(request)
  // TWO COLUMNS ARE THE REST STATE of the project screen — not something contingent on a build
  // having run. A project with nothing built shows the empty-state sentence IN the pane, not a
  // hidden pane, because "there is nothing here yet" is a thing the app pane should say rather
  // than an absence a citizen has to interpret.
  useAppPaneVisible(true)

  return <WorkspaceRail {...props} workspace={workspace.state} save={workspace.save} />
}
