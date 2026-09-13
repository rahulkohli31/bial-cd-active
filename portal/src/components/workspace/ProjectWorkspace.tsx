/**
 * THE PROJECT SURFACE — the rail's contents, and the project-scoped publisher.
 *
 * WHY THIS EXISTS: the symmetric counterpart to the chat-scoped publisher — exactly one of the
 * two is ever mounted for a given address, so there's no contest, only continuity across the
 * hop, which the channel's per-payload rules already handle (`address`/`project` survive an
 * unmount; `pane`, `visible` and the workspace report clear).
 *
 * ONE READ, TWO CONSUMERS, NOT A SECOND POLL: `useWorkspaceState` owns the preview-state read
 * and its cadence/visibility handling; the address is built from that SAME result, feeding only
 * the project-scoped input. Precedence lives in `previewAddress.ts` and is not re-derived here —
 * its resolver docblock and the conversation surface's `projectPreviewUrl: null` comment both
 * name this caller as the one that needs a chat-less project address.
 *
 * PUBLISH THROUGH THE HOOKS, NEVER A RAW CHANNEL SET: they carry the "nothing yet ≠ there is
 * nothing" protection — a publisher abstains on its first renders until it has resolved
 * something, which stops a remount from retiring a frame the departing surface left standing. A
 * direct `channel.address.set` breaks that round trip silently, only on the return leg.
 *
 * DOES NOT FIRE THE PROJECT-OPENED BEACON — `ProjectPage` does, from one place. This component
 * needs `project.appId` for the rail's status line, which is exactly the pull that tempts a
 * second tracker; `observe.ts`'s per-project guard makes a repeat call a no-op, so the risk is
 * bypassing that guard with a second mechanism it doesn't cover.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import WorkspaceRail from './WorkspaceRail'
import ProjectRenameDialog from '../projects/ProjectRenameDialog'
import SharePanel from '../projects/SharePanel'
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
import { announceDeploymentChanged } from '../../hooks/usePublishState'
import { resolvePreviewAddress } from '../../utils/previewAddress'
import { fetchCompileState, handOverWorkspace, saveProject } from '../../utils/buildSessionApi'
import type { HandoverStep, ReclaimBlocked } from '../../utils/buildSessionApi'
import type { CompileState } from '../../utils/compileState'
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
  // WHAT THE HAND-OVER IS DOING RIGHT NOW, published to the dialog so it narrates instead of
  // spinning. Held here because this surface performs the sequence.
  const [step, setStep] = useState<HandoverStep | null>(null)
  const [renaming, setRenaming] = useState(false)
  const startRename = useCallback(() => setRenaming(true), [])
  // THE SHARE PANEL'S STATE IS HERE FOR THE SAME REASON RENAME'S IS: the control that opens it
  // lives in the shell's toolbar row, which has no project object, while this surface has the
  // project id and name the panel needs (#198).
  const [sharing, setSharing] = useState(false)
  const startShare = useCallback(() => setSharing(true), [])

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
    // …AND IT IS ALSO THIS SCREEN'S WHOLE ANSWER ON LIVENESS. A non-null value here is the read
    // saying `alive`, which is what the resolver builds `serving` from — so the pardon that used to
    // be asserted as `completedLive: true` on the pane view below is now READ rather than claimed,
    // and this screen no longer states anything about a build it never ran.
    // The project predicate is trivially true here: these signals came from a read keyed on the
    // project this surface is showing. It is passed rather than assumed because the resolver's own
    // note says an arm must carry its predicate INTO the module — a gate that depends on where it
    // was declared is one reorder away from silently opening.
    sessionBelongsToOpenProject: true,
    // NO SESSION ON THIS SURFACE AT ALL, so there is no session end to have been a success.
    sessionEndedCompleted: false,
    transcriptHasBuildOutcome: false,
  })

  /**
   * DID THE NEWEST BUILD COMPILE? — asked of the server, gated on liveness.
   *
   * THE MECHANISM IS NOT BUILT HERE; IT IS WIRED HERE. The route, the four-valued type whose
   * `unknown` means "hold the cover, never read as clean", the client and `LivePreview`'s
   * hold-the-cover effect all ship already, and the conversation surface has been calling them for
   * some time. The project screen was the one call site that did not: it passed `compileState: null`
   * under a comment reasoning that this screen must not cause a container call.
   *
   * THAT REASONING WAS NARROWER THAN IT READ. What the ban forbids is a screen that STARTS a stopped
   * container to answer a question nobody asked — and the route already refuses to attach when
   * nothing is live, short-circuiting before the expensive part. So the ban is honoured by gating
   * the read on the same liveness the save read is gated on: no dark pane pays for this.
   *
   * WHAT IT COSTS WHEN IT DOES RUN: one `/dev/compile` read of an in-memory value inside a
   * container that is already up. It never touches the dev server.
   *
   * THE ONE RULE THIS MUST NOT DEFEAT.
   *
   * `fetchCompileState` answers `unknown` for everything unanswerable — a refusal, an unreadable
   * body, a thrown request, a container image older than the signal — and never throws. `unknown`
   * and `null` both HOLD whatever cover is showing and assert nothing in either direction. The
   * failure mode to avoid is not building something too weak; it is translating an unreadable
   * answer into `clean` on the way through, which would uncover the frame over the very error
   * screen the cover exists to hide. So the verdict is stored EXACTLY as it arrives.
   *
   * AND IT IS NOT WIRED TO A DISPLAY BOOLEAN. `revealed` and `covered` inside the pane are
   * deliberately permissive and diverge from what is actually rendered around workspace loss; a
   * verdict read off one of those would be a measurement taken from a display signal, which is how
   * a dead preview once got recorded as a fast successful view.
   */
  const alive = workspace.preview?.state === 'alive'
  const framedUrl = address.url
  const [compileState, setCompileState] = useState<CompileState | null>(null)
  useEffect(() => {
    // NOT ALIVE, NOTHING ASKED, AND NOTHING CLAIMED. `null` is "nothing has been reported", which
    // is the pre-signal state and behaves exactly as this screen did before this read existed —
    // deliberately NOT `'clean'`, and deliberately not a stale verdict about a container that has
    // since gone.
    if (!alive || !framedUrl) {
      setCompileState(null)
      return
    }
    // ONE READ PER POLL TICK, riding the workspace poll's own cadence rather than a timer of its
    // own — `workspace.readTick` in the deps is the whole mechanism.
    //
    // IT USED TO BE ONE READ PER MOUNT, and the reasoning was that no turn runs on this screen so
    // nothing could change what the app compiles to. That premise is false: the app compiles its
    // routes ON DEMAND, so merely opening the page can start a compile the read then catches
    // mid-flight. `building` raises a cover over a working app (`LivePreview`), the cover comes
    // down only on an affirmative `clean`, and with a single read there was never a second answer
    // to bring it down — the pane spun for the life of the tab over an app that had finished
    // loading underneath it, and only a reload cleared it. Reported from production as needing
    // four reloads to see the app.
    //
    // The chat surface never had this: it re-reads the same verdict on every background tick and
    // self-corrects within one cadence. This is that, and nothing more. The read is cheap by
    // construction (one in-memory value; it never touches the dev server), and the `!alive ||
    // !framedUrl` guard above still keeps it off entirely when there is nothing to ask about.
    let live = true
    fetchCompileState(project.id)
      // IN FRONT OF THE HANDLER, not after it. Behind it, the same `.catch` that covers the
      // (impossible) fetch rejection would also swallow anything the state update threw —
      // a real error in this component silently becoming nothing. Here it covers exactly the
      // promise it is about, and `unknown` is the right substitute: it is what the client
      // itself answers for anything unreadable, and it HOLDS whatever cover is showing.
      .catch(() => 'unknown' as const)
      .then((verdict) => {
        // The answer describes the workspace this effect was armed for. A late reply after a
        // teardown, a project switch or a restart would otherwise land on a different app.
        if (live) setCompileState(verdict)
      })

    return () => {
      live = false
    }
  }, [project.id, alive, framedUrl, workspace.readTick])

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
      step,
      resolve: async (save: boolean) => {
        try {
          // Stop, then WAIT FOR THE STOP TO GENUINELY FINISH, then save, then release — the
          // ordering invariant lives in `handOverWorkspace`, and so does the refusal to proceed
          // on a stop that only timed out.
          await handOverWorkspace(reclaim.blocked, save, {}, setStep)
          // The retry is AWAITED BEFORE the dialog is dismissed, so a switch that fails can still
          // be reported instead of vanishing with the dialog. It is the whole of what was refused:
          // starting this project's app, and — from the rail — opening the chat with the message
          // the citizen typed, which has been held in the composer throughout.
          setStep('starting')
          await reclaim.retry()
          setReclaim(null)
        } finally {
          setStep(null)
        }
      },
      cancel: () => {
        // CANCELLING CHANGES NOTHING ANYWHERE. Nothing has been stopped, nothing released, and the
        // typed message and its staged files are still in the composer that never sent them.
        setReclaim(null)
        setStep(null)
      },
    }
  }, [reclaim, project.name, step])

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
      // none: this screen starts no turn and owns no session.
      //
      // `completedLive: true` USED TO SIT HERE, AND IT WAS THE FALSE-COMPLETION DEFECT. It was
      // the pardon that let a frame outrank a terminal status — which an app reached from the
      // project screen genuinely is — but the same flag also drew "Build complete — your app is
      // live below", so a screen where a build can never run stated a build outcome on every
      // cold load and after every restore. It could not simply be flipped to `false` either: that would have overridden
      // the value the pane host held across the chat→project hop and collapsed the iframe right
      // after a successful build.
      //
      // BOTH PROBLEMS ARE GONE RATHER THAN TRADED. The claim went with the chip, and liveness
      // went onto the address, where this surface feeds it from the preview-state read instead
      // of asserting it. Nothing is lost on the framing side: `previewAddress.ts` already resolves
      // this screen's status to `ready`, which is not terminal, so there is nothing for a pardon to
      // outrank here in the first place.
      iterating: false,
      reconnecting: false,
      turnRunning: false,
      previewState: workspace.preview?.state ?? null,
      // THE SERVER'S VERDICT ON THE NEWEST BUILD, read above and passed through UNTRANSLATED.
      // `null` and `unknown` both mean "nothing is claimed" and hold whatever cover is showing;
      // only an affirmative `clean` uncovers, and only `failed` names the failure. See the read.
      compileState,
      // `checkWorkspace` STAYS UNASKED, and the distinction from the line above is the cost. The
      // compile route short-circuits before any attach when nothing is live; the workspace check is
      // a container exec that can raise an operational alarm, and it is gated on a STANDING
      // COMPLETION CLAIM — which this screen, having stopped making one, no longer has.
      workspaceLost: false,
    }),
    // `project.hasRelaunchableSnapshot` LEFT THIS LIST WITH `hasSavedBuild`. It still reaches the
    // workspace map above, where the restore question actually gets answered; this view stopped
    // carrying it when the pane stopped writing sentences about the workspace.
    [workspace.preview, compileState],
  )

  useWorkspaceProject(project.id)
  usePublishAddress(address, project.id)
  usePublishPaneView(paneView)
  usePublishWorkspaceReport(report)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  /**
   * PUSH THE WORKSPACE TO DURABLE STORAGE from the project screen — the conversation surface's own
   * call, unshared because only one is mounted per address. SURFACED, NEVER SWALLOWED: a silent
   * failure leaves the citizen believing their work is stored, so the server's error copy is passed
   * through. TWO READS GO STALE, NOT ONE — `workspace.refresh()` covers the save chip; the rail's
   * last-saved row and the toolbar's publish chip come off the deployment read, whose `savedHead`
   * and `savedAt` this save just changed — so it raises the shared nudge, never a second reader.
   */
  const save = useCallback(async () => {
    if (saving) return
    setSaving(true)
    setSaveError(null)
    try {
      await saveProject(project.id)
      workspace.refresh()
      announceDeploymentChanged(project.id)
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Could not save your work. Try again.')
    } finally {
      setSaving(false)
    }
  }, [project.id, saving, workspace])

  // ONE OBJECT, BOTH FACTS. `workspace.save` is a single `GET save-state` response, so the pair
  // published here is by construction one reading — and a project screen holding no reading at all
  // publishes the "nobody has said" pair rather than a bare `null` that has lost the second half.
  usePublishSaveState({
    dirty: workspace.save?.dirty ?? null,
    recoveryAt: workspace.save?.recoveryAt ?? null,
  })
  // SAVE IS REACHABLE FROM THE PROJECT SCREEN, and it was not.
  //
  // The only writer of the bundle lived on the conversation surface, so a citizen who had built
  // something, gone back to the project screen and then closed the tab lost it — with the rail
  // telling them, correctly, that they had unsaved changes and offering nothing to do about it.
  // The toolbar row is where the control lives; this is the producer behind it.
  usePublishSave(
    { dirty: workspace.save?.dirty ?? null, saving, error: saveError },
    { save: workspace.save ? save : null, rename: startRename, share: startShare },
  )
  usePublishReclaim(request)
  // TWO COLUMNS ARE THE REST STATE of the project screen — not something contingent on a build
  // having run. A project with nothing built shows the empty-state sentence IN the pane, not a
  // hidden pane, because "there is nothing here yet" is a thing the app pane should say rather
  // than an absence a citizen has to interpret.
  useAppPaneVisible(true)

  return (
    <>
      <WorkspaceRail {...props} save={workspace.save} />
      {renaming && (
        <ProjectRenameDialog
          project={project}
          onProjectUpdate={props.onProjectUpdate}
          onClose={() => setRenaming(false)}
        />
      )}
      {sharing && (
        <SharePanel
          projectId={project.id}
          projectName={project.name}
          onClose={() => setSharing(false)}
        />
      )}
    </>
  )
}
