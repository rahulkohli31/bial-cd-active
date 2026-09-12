/**
 * THE ONE CONTROL THAT STARTS THE APP. Four action members exist — start, retry, go to the project
 * holding the workspace, and take it back — and this renders whichever it is handed; there is no
 * fifth, so no unreadable signal reaches a teardown or a restore from here. What the press does
 * once it lands is the server's, and has its own tests.
 *
 * WHY THIS EXISTS
 *
 * `useTakeBack` is exported for `AppPane` rather than handled here, for three reasons: this
 * component unmounts the moment a start reaches the map, long before the up-to-two-minute modal
 * wait ends; the held arm and this one are siblings that must go inert together, which siblings
 * sharing no state cannot do; and its busy flag stays local, because reporting it through
 * `onStartPending` puts the map in `gettingReady()`, which offers no action — so the control would
 * unmount its own button and un-frame the pane it is trying to fill.
 *
 * `relaunchPreview` reaches a two-armed endpoint. ATTACH is safe: it reuses the live container and
 * fails open on a readiness timeout. RESTORE tears the container down before pulling the last saved
 * bundle, so a guard keeps an unreadable attach — the recorded data-loss path — out of it. A stale
 * `asleep` read stays reachable, the registry hash having no TTL, and this control answers it with
 * one start and whatever comes back, refusals included, never a retry or an invented recovery verb.
 *
 * `aria-disabled`, never `disabled`: disabling a focused control blurs it to `document.body` and
 * takes its name and reason with it. The VISIBLE label carries the state, where once only the
 * `aria-label` did over words reading "Launch Application" either way; that override is gone
 * rather than kept beside them, because a second name for one control is what WCAG's label-in-name
 * rule forbids. No live region: `LivePreview` owns one polite region for every pane state.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { PlayCircle, RotateCcw, ArrowRight, Undo2 } from 'lucide-react'
import { BusyGlyph } from '../ui/Waiting'
import {
  BuildSessionAlreadyActiveError,
  asReclaimBlocked,
  handOverWorkspace,
  relaunchPreview,
} from '../../utils/buildSessionApi'
import type { HandoverStep, ReclaimBlocked } from '../../utils/buildSessionApi'
import { ApiError } from '../../utils/apiError'
import { assertNever } from '../../utils/assertNever'
import type { StartOutcome, WorkspaceAction } from './workspaceState'
import type { WorkspaceReport } from './workspaceChannel'

/** A build already running in THIS project — a different cause with a different remedy. */
const BUILD_ALREADY_RUNNING = 'A build is already running in this project.'

export interface StartAppControlProps {
  action: WorkspaceAction
  report: WorkspaceReport
  /**
   * A TAKE-BACK IS IN FLIGHT ON THIS PANE, so every control on it is inert.
   *
   * It is a prop rather than local state because the two controls the held arm draws are SIBLING
   * mounts of this component, and "both go inert while one of them works" is a fact neither of them
   * can hold. `AppPane` owns it, along with the sequence.
   */
  inert?: boolean
  /**
   * THE TAKE-BACK'S TRIGGER, from the parent that owns the sequence and outlives it.
   *
   * A surface that does not own one renders no take-back — which is not a fallback but the same
   * rule `PlanChatWorkspaceLine` already states for the other three members: the verb appears where
   * the thing behind it lives. `AppPane` is the only such surface.
   */
  takeBack?: TakeBack | null
}

export default function StartAppControl({ action, report, inert = false, takeBack = null }: StartAppControlProps) {
  const navigate = useNavigate()
  const [pending, setPending] = useState(false)
  // TWO GUARDS, AND THEY ARE NOT THE SAME GUARD. The ref is synchronous, so two presses in one
  // tick collapse to one request — state would not have committed between them. `mounted` is what
  // keeps every `await` below from writing into a component the citizen has already navigated
  // away from.
  const inFlight = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const start = useCallback(async () => {
    const projectId = report.projectId
    if (!projectId || inFlight.current) return
    inFlight.current = true
    setPending(true)
    // THE PANE HEARS THE PRESS IMMEDIATELY, not on the next poll tick. The server's own `starting`
    // is the authority and it arrives later; this is what stops the sentence above this button
    // saying nothing happened for up to forty-five seconds.
    report.onStartPending(true)
    try {
      const res = await relaunchPreview({ projectId })
      // NO MOUNTED GUARD BEFORE THE REPORT, and the distinction is the bug it was written as.
      //
      // `mounted` protects THIS component's own state. The report's handlers write into the
      // SURFACE — the workspace read, the address, the outcome slot — all of which outlive this
      // button and all of which need the answer. And this control unmounts routinely mid-flight:
      // the moment the press reaches the map, the state becomes `starting`, which offers no
      // action, so the button that fired the request is gone before the request comes back.
      //
      // Guarding here meant a start that SUCCEEDED reported nothing: no URL for the pane to frame,
      // no outcome to clear the wait. The screen sat on "Getting your app ready." forever while a
      // perfectly good container served underneath it.
      // THE URL FIRST, and before the outcome. It is what the surface frames, and reporting it
      // second would leave one commit in which the state says "running" and the pane has no
      // address to show for it. Handed over even when `ready` is false: the container is up and
      // the document is what has not arrived, so the frame's own load-gated reveal is the right
      // thing to be waiting on rather than a sentence in front of it.
      if (res.previewUrl) report.onStarted(res.previewUrl)
      // `ready === false` is "started but not painted yet", NOT "dead" — and an ABSENT `ready`
      // reads `true` by the wire's recorded contract, which is exactly why liveness can never hang
      // off this boolean. Safe here only because both sides of the read are non-destructive.
      report.onStartOutcome(res.ready ? null : { kind: 'not-painted' })
    } catch (err) {
      // Same reasoning as the success path above: a refusal has to reach the surface whether or
      // not the button that provoked it is still on screen.
      // DISCRIMINATED ON THE CODE BEFORE ANYTHING ELSE. A bare 409 is not self-describing: it
      // fires for a same-project reattach and for a cross-project block, and the two have
      // different remedies.
      const blocked = asReclaimBlocked(err)
      if (blocked) {
        // Another project holds the one workspace. Routed to the ONE dialog rather than shown as
        // a retry — retrying against an occupied slot can only fail the same way again.
        report.onReclaimRefusal(blocked, start)
        return
      }
      if (err instanceof BuildSessionAlreadyActiveError) {
        // Your own other chat is building. A different cause with a different remedy — finish or
        // stop it — so it must not be merged into the reclaim dialog, which would offer a Save
        // button that cannot help.
        report.onStartOutcome({ kind: 'failed', reason: BUILD_ALREADY_RUNNING })
        return
      }
      report.onStartOutcome(outcomeFor(err))
    } finally {
      inFlight.current = false
      report.onStartPending(false)
      if (mounted.current) setPending(false)
    }
  }, [report])

  switch (action.kind) {
    case 'start':
      return (
        <Control
          label={action.label}
          pending={pending}
          inert={inert}
          pendingLabel="Starting your app"
          icon={<PlayCircle size={15} />}
          onPress={() => void start()}
        />
      )
    case 'retry':
      return (
        <Control
          label={action.label}
          pending={pending}
          inert={inert}
          pendingLabel="Trying again"
          icon={<RotateCcw size={15} />}
          onPress={() => {
            // A retry clears the last outcome and asks again, then starts. Clearing first matters:
            // otherwise a second failure of the same kind would leave the sentence unchanged and
            // the press would look like it did nothing.
            report.onStartOutcome(null)
            void start()
          }}
        />
      )
    case 'go-to-project':
      return (
        <Control
          label={action.label}
          pending={false}
          // UNCHANGED IN LABEL AND BEHAVIOUR, by decision: opening the holder is the remedy the
          // product leads with, and it keeps its own words. The only thing the take-back adds to
          // it is that it goes inert while its neighbour works, which is not a change to what it
          // does.
          inert={inert}
          pendingLabel=""
          icon={<ArrowRight size={15} />}
          onPress={() => navigate(`/projects/${action.projectId}`)}
        />
      )
    case 'take-back':
      // No sequence in the parent, no verb. See `StartAppControlProps.takeBack`.
      return takeBack ? (
        <Control
          label={action.label}
          pending={takeBack.working}
          // ONE SENTENCE FOR THE WHOLE SEQUENCE, and it is true at every step of it — the ask, the
          // stop, the save, the release and the start are all "taking your workspace back". The
          // step-by-step narration belongs to the dialog standing in front of this button
          // (`STEP_SAYS`), which is the surface a citizen is actually looking at while it runs.
          pendingLabel="Taking your workspace back"
          icon={<Undo2 size={15} />}
          // THE ALTERNATIVE, DRAWN AS ONE. `Open “<holder>”` is the remedy the product has always
          // offered and stays the thing the pane leads with; two solid primary buttons side by
          // side would put them on equal footing and make the destructive one look like the
          // expected answer.
          secondary
          onPress={takeBack.press}
        />
      ) : null
    default:
      return assertNever(action)
  }
}

// ─── the take-back ───────────────────────────────────────────────────────────────────────────

/**
 * What a pane needs in order to draw the take-back and the question behind it.
 *
 * Produced by {@link useTakeBack}, held by `AppPane`, and handed down to the button. Nothing here
 * is a `WorkspaceState` field: the map answers what is TRUE about the workspace, and a press that
 * is half way through a sequence is true about this tab only.
 */
export interface TakeBack {
  /** Press it. A second press while one is running is ignored, not queued. */
  press: () => void
  /** A take-back is in flight. Every control on the pane is inert; this one says what it is doing. */
  working: boolean
  /** The refusal the hand-over dialog is asking about, or `null` when no dialog is up. */
  asking: ReclaimBlocked | null
  /** How far the hand-over has got, for the dialog's own narration. `null` before it starts. */
  step: HandoverStep | null
  /** `true` saves the holder first. RESOLVES on every ending — see the docblock. */
  resolve: (save: boolean) => Promise<void>
  /** Close the question. Nothing has been stopped, saved or released. */
  cancel: () => void
  /**
   * WHAT A TAKE-BACK THAT WORKED DID — the ending that used to report nothing.
   *
   * Every FAILING ending has a sentence; the succeeding one had none: the dialog closed,
   * the pane framed an app, and the citizen who had just stopped somebody else's work was told
   * nothing about it. The other four endings still come through `onStartOutcome` and the map's
   * `note`, and they are deliberately NOT duplicated here — a second producer for a sentence the
   * board already renders is that sentence read twice, which `AppPane.test.tsx` pins.
   *
   * `null` UNTIL ONE SUCCEEDS, cleared at the start of every press so a second sequence never shows
   * the first one's ending, and cleared with the rest of the sequence when the project changes.
   * Clearing also matters for the announcement itself: a live region speaks on a CHANGE, so a
   * second hand-over to the same holder would say nothing at all if the string never went empty.
   */
  outcome: string | null
}

/**
 * TAKE THE ONE WORKSPACE BACK — the whole sequence, and every way it can end.
 *
 * THE PRESS ASKS FOR THE WORKSPACE; IT DOES NOT REACH FOR THE HOLDER
 *
 * The first thing a press does is `relaunchPreview` for THIS project — the same call the start
 * control makes, unchanged. The server is what refuses, with `sandbox_reclaim_blocked`, and that
 * refusal is what opens the dialog. Three things follow from doing it that way rather than from
 * synthesising a refusal out of the `slot_taken` reading in hand:
 *
 *  - THE DIALOG GETS REAL DATA. Its three copy arms are chosen from `dirty`, and it withholds the
 *    Save button entirely on a confirmed-clean holder. A `PreviewState` carries no `dirty`, no
 *    `building` and no `agentWorking`, so a synthesised refusal could only ever say "may have
 *    unsaved changes" — the exact hedge already banned from this copy, wrong in front of somebody
 *    whose work is safe.
 *  - THE READING CAN BE STALE. If the slot was freed since the last poll, the ask simply succeeds
 *    and the app comes up: one press, no dialog, nothing stopped.
 *  - THE SAME CALL CLOSES THE SEQUENCE. What runs after the hand-over is this same function, so
 *    another tab taking the slot mid-sequence lands on the same refusal handling and re-asks the
 *    question with the NEW holder in it — the take-back's fifth ending — instead of needing an
 *    arm of its own.
 *
 * THE HANDLERS RESOLVE, THEY DO NOT REJECT
 *
 * `ReclaimWorkspaceDialog` owns `busy` and `error` itself, and its `run()` catches EVERY rejection
 * into its own "That did not work. Please try again." alert while staying mounted. A take-back
 * whose handlers rejected would therefore report every failure through that one sentence, and the
 * take-back's five endings — which are pane states, with different copy and different remedies —
 * would be unreachable. So every ending here resolves, and the caller dismisses the dialog on all
 * of them. The pane is the single reporting surface.
 *
 * AND IT NEVER TOUCHES `captureReclaim`
 *
 * On `/chat/{id}` the surface already owns a reclaim slot, and it is single-use, first-refusal-wins,
 * and its `resolve` awaits `retry()` — `fireRelayTurn(rawText, …)` for a refused send. Routing the
 * take-back through it would mean confirming the hand-over SENDS the message the citizen is holding
 * in the composer as a build instruction, and a refused send already holding the slot would swallow
 * the take-back's refusal outright. The take-back owns its own dialog and its own closure.
 *
 * THEY CAN BOTH BE ON SCREEN, and this used to claim "the two never meet". They do: the pane's
 * take-back dialog and the surface's own reclaim dialog are mounted by different owners
 * (`AppPane` and `WorkspaceShell`) and neither suppresses the other. Suppressing the pane's was
 * tried and REVERTED — a citizen who answers the send's identical dialog thereby starts a build,
 * which is the exact harm the take-back exists to avoid. So what is true is narrower and worth
 * saying precisely: they never share the reclaim SLOT, so neither can swallow the other's
 * refusal. Two dialogs is a presentation problem; one swallowed refusal is a lost answer.
 *
 * WHAT `mounted` GUARDS, AND WHAT IT DELIBERATELY DOES NOT
 *
 * State writes only. The report's handlers are called regardless, exactly as the start path calls
 * them: they write into the SURFACE, which outlives this pane's controls and needs the answer. So a
 * citizen who clicks away during the two-minute stop wait produces no state update and no crash,
 * and the server sequence — which is running server-side anyway — still completes.
 *
 * `mounted` IS NOT ENOUGH, BECAUSE THE PANE DOES NOT UNMOUNT
 *
 * `AppPane` is a SIBLING of the Outlet, not a child of it — that is the whole point of the shell,
 * and it is why leaving a build chat for the project screen does not reload the running app. The
 * consequence for this hook is that a move from `/projects/A` to `/projects/B`, or from a project
 * to a chat, runs no cleanup here at all: the same `useState`s carry straight over. Without an
 * identity, A's hand-over dialog stands over B's pane naming A's holder, and A's `working` flag
 * leaves B's control busy for a sequence that was never about B.
 *
 * So the sequence is OWNED BY A PROJECT, and the owner is `report.projectId` — the same field
 * `sameReport` compares, the same field `useWorkspaceAddress` retires a stale address by, and the
 * same field `useWorkspaceState` guards its own late writes with. Two halves, and both are needed:
 *
 *  - A CHANGE OF OWNER CLEARS THE SEQUENCE, during the render that first sees it rather than in an
 *    effect afterwards, so no committed frame ever carries A's dialog over B's pane.
 *  - EVERY LATE WRITE NAMES THE OWNER IT STARTED UNDER (`ifStillOurs`). Clearing alone would be
 *    undone a moment later: A's stop is still running server-side, and its refusal, its narration
 *    and its `finally` would all land in B's state. The report's handlers are still called
 *    unguarded, exactly as above — they belong to A's surface and A's surface still wants them.
 *
 * `null` COUNTS AS A CHANGE, unlike `useWorkspaceAddress`'s "no claim is not a different project".
 * The two rules are about different things. There, a `null` project must not tear down a running
 * app somebody is looking at. Here, a report of `null` means the surface that raised this question
 * is gone — a cold open of a chat address publishes exactly that for its first frames — and a
 * modal about a project nobody is showing any more is the defect, not the remedy.
 */
export function useTakeBack(report: WorkspaceReport | null): TakeBack {
  const [working, setWorking] = useState(false)
  const [asking, setAsking] = useState<ReclaimBlocked | null>(null)
  const [step, setStep] = useState<HandoverStep | null>(null)
  // THE SUCCESS ENDING'S SENTENCE. See `TakeBack.outcome` — every other ending travels on
  // `onStartOutcome` and is said by the map, and only this one had nowhere to be said at all.
  const [outcome, setOutcome] = useState<string | null>(null)
  // Synchronous, so two presses in one tick collapse to one sequence — state would not have
  // committed between them.
  const inFlight = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  // READ AT PRESS TIME, NEVER HELD ACROSS A RENDER. `press` and `resolve` are stable identities so
  // the pane can pass them down without re-rendering its controls on every poll tick, and a stable
  // identity that closed over `report` would go on calling last minute's handlers.
  const reportRef = useRef(report)
  reportRef.current = report
  const askingRef = useRef(asking)
  askingRef.current = asking

  // WHOSE SEQUENCE THIS IS. A ref rather than state because every reader of it is either this
  // render or a callback firing after one, which is the same reason `usePublishAddress` keeps its
  // standing in a ref and assigns it during render.
  const shownProject = report?.projectId ?? null
  const owner = useRef(shownProject)
  if (owner.current !== shownProject) {
    owner.current = shownProject
    // ADJUSTED DURING RENDER, which React documents for exactly this case and which an effect
    // cannot do: an effect commits one frame first, and that frame is A's modal standing over B's
    // pane. The guards are so a project change with nothing running re-renders nothing.
    if (working) setWorking(false)
    if (asking) setAsking(null)
    if (step) setStep(null)
    // AND THE ENDING GOES WITH THEM, for the same reason: it names A's holder, and left standing it
    // would announce over B's pane what happened to a project B has nothing to do with.
    if (outcome) setOutcome(null)
    // AND THE PRESS GUARD GOES WITH IT, or B's control is dead: A's sequence is still running, so
    // the flag is still raised, and every press on the new project would be swallowed as a double
    // press. Two projects are two sequences; what keeps them from writing over each other is
    // `ifStillOurs` below, not this flag.
    inFlight.current = false
  }

  /**
   * WRITE ONLY IF THIS PANE IS STILL HERE AND STILL ABOUT THE PROJECT THAT ASKED.
   *
   * `theirs` is the project the caller captured when its sequence began. `mounted` covers the
   * citizen closing the workspace; this covers the citizen moving to another project, which the
   * pane survives — see the docblock.
   *
   * THE PRESS GUARD IS RELEASED THROUGH HERE TOO, and under the same ownership rule rather than a
   * second one: a sequence that began on A and ended after the move must not un-arm a flag B
   * raised, or B's take-back becomes pressable twice — the exact double press this ref exists to
   * collapse.
   */
  const ifStillOurs = (theirs: string, write: () => void) => {
    if (mounted.current && owner.current === theirs) write()
  }

  /**
   * ONE ASK FOR THE WORKSPACE, and all three things the answer can be.
   *
   * `stoppedHolder` is what this sequence has ALREADY done to the other project before getting
   * here — `null` on the opening ask, the holder's name once it has been stopped — and it travels
   * into the outcome untouched: any ending which stopped the holder says so.
   *
   * IT RETURNS WHICH OF THE THREE THINGS HAPPENED. Every ending except one writes itself into the
   * report on its way past, so the caller never had to ask; the succeeding one writes only
   * `onStartOutcome(null)`, which is indistinguishable from "no attempt has been made". `resolve`
   * needs to tell a start that WORKED from a start that was refused again by a new holder, because
   * only the first has an outcome sentence to say. Reading `askingRef` afterwards would be
   * guessing from a side effect; this answers directly.
   */
  const askForTheWorkspace = async (
    rep: WorkspaceReport,
    projectId: string,
    stoppedHolder: string | null,
  ): Promise<'started' | 'blocked' | 'failed'> => {
    try {
      const res = await relaunchPreview({ projectId })
      // THE URL FIRST, AND BEFORE THE OUTCOME, for the reason the start path records: reporting it
      // second leaves one commit in which the state says running and the pane has no address.
      if (res.previewUrl) rep.onStarted(res.previewUrl)
      rep.onStartOutcome(res.ready ? null : { kind: 'not-painted' })
      ifStillOurs(projectId, () => setAsking(null))
      return 'started'
    } catch (err) {
      const blocked = asReclaimBlocked(err)
      if (blocked) {
        // THE QUESTION, WITH WHOEVER IS HOLDING IT NOW. On the opening ask this is the dialog
        // appearing; after a hand-over it is the take-back's fifth ending — another tab took the
        // freed slot — and it is a return to the CHOICE screen with new data, never the dialog's
        // generic caught error. The caller force-remounts on the holder's id, so the copy and the
        // focus move together.
        ifStillOurs(projectId, () => setAsking(blocked))
        return 'blocked'
      }
      rep.onStartOutcome({
        kind: 'take-back-failed',
        reason: err instanceof BuildSessionAlreadyActiveError ? BUILD_ALREADY_RUNNING : reasonFor(err),
        stoppedHolder,
      })
      ifStillOurs(projectId, () => setAsking(null))
      return 'failed'
    }
  }

  const press = useCallback(() => {
    const rep = reportRef.current
    const projectId = rep?.projectId
    if (!rep || !projectId || inFlight.current) return
    inFlight.current = true
    setWorking(true)
    // A FRESH SEQUENCE SAYS NOTHING YET. Clearing here is also what lets the SAME sentence be
    // announced twice: a live region speaks on a change, so re-taking the same holder over a
    // string that never went empty would be silent.
    setOutcome(null)
    void (async () => {
      try {
        await askForTheWorkspace(rep, projectId, null)
      } finally {
        // THE PANE RE-ARMS THE MOMENT THE DIALOG IS UP. The citizen is deciding, not waiting, and
        // a Cancel that left both controls inert would be a dead end.
        ifStillOurs(projectId, () => {
          inFlight.current = false
          setWorking(false)
        })
      }
    })()
    // Every value it reads comes through a ref, so this identity is correct for the life of the
    // pane — which is what keeps the two controls from re-rendering on every poll tick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const resolve = useCallback(async (save: boolean): Promise<void> => {
    const rep = reportRef.current
    const holder = askingRef.current
    const projectId = rep?.projectId
    if (!rep || !holder || !projectId || inFlight.current) return
    inFlight.current = true
    setWorking(true)
    setOutcome(null)
    // HOW FAR THE HAND-OVER GOT, and it is the ONLY thing that tells the take-back's endings
    // apart. The order is `handOverWorkspace`'s and lives there: stop, wait for the stop to
    // genuinely finish, save, release. A rejection while the step is still `stopping` therefore
    // means nothing was stopped — the holder is untouched and its own ceiling sentence says so —
    // while a rejection at `saving` or `releasing` means the holder is down and the slot is still
    // held, which is a pair of facts the pane has to state. `holder.isSharedView`'s arm never
    // reaches `saving`/`releasing` on a rejection either, for the same reason it starts at
    // `stopping`: `giveUpSharedView` narrates only on success, so a throw leaves `reached` right
    // where it started and `stoppedHolder` correctly comes out `null` — nothing was ever stopped
    // on that arm, on either outcome.
    let reached: HandoverStep = 'stopping'
    try {
      try {
        await handOverWorkspace(holder, save, {}, (next) => {
          reached = next
          ifStillOurs(projectId, () => setStep(next))
        })
      } catch (err) {
        rep.onStartOutcome({
          kind: 'take-back-failed',
          // VERBATIM, including `buildSessionApi`'s own two-minute ceiling sentence. That one is
          // authored there, is true only on that ending, and is not to be replaced here.
          reason: reasonFor(err),
          stoppedHolder: reached === 'stopping' ? null : holder.projectName,
        })
        ifStillOurs(projectId, () => setAsking(null))
        rep.onRefresh()
        return
      }
      ifStillOurs(projectId, () => setStep('starting'))
      const ended = await askForTheWorkspace(rep, projectId, holder.projectName)
      // THE ENDING THAT WORKED, SAID OUT LOUD.
      //
      // The other four endings are already sentences on the pane, written by the map from
      // `onStartOutcome`. This one wrote only `onStartOutcome(null)` and then framed an app, so the
      // citizen who had just stopped a colleague's work learned nothing about having done it — and
      // the colleague, who is not here, learns nothing either way.
      //
      // TWO FACTS IN ONE SENTENCE, and both are things only this sequence knows: what became of the
      // holder, including whether its work was saved (the answer the citizen gave the dialog), and
      // that the workspace is now this project's. Neither is recoverable from the reading that
      // follows.
      //
      // ONLY ON `started`. A refusal from a NEW holder is the take-back's fifth ending — the
      // question reopens and nothing has concluded — and a failure already has its own sentence.
      if (ended === 'started') {
        ifStillOurs(projectId, () =>
          setOutcome(
            // "STOPPED" IS THE WRONG VERB FOR A SHARED VIEW. Nothing of the colleague's was ever
            // running under this citizen's account — there is no build to stop or save, only
            // their own one-per-user slot to give up, so `save` is meaningless on this arm too.
            holder.isSharedView
              ? `You closed your view of “${holder.projectName}”. Your app has the workspace now.`
              : save
                ? `“${holder.projectName}” was saved and stopped. Your app has the workspace now.`
                : `“${holder.projectName}” was stopped without saving. Your app has the workspace now.`,
          ),
        )
      }
      // THE READING IS STALE WHATEVER JUST HAPPENED. The slot was released, so `slot_taken` is no
      // longer the answer — on the ending where the relaunch failed the pane needs the fresh
      // reading to reach `start-failed` rather than sitting on a hand-over that is over.
      rep.onRefresh()
    } finally {
      ifStillOurs(projectId, () => {
        inFlight.current = false
        setWorking(false)
        setStep(null)
      })
    }
    // As `press` above: every value is read through a ref at call time.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const cancel = useCallback(() => {
    // NOTHING HAS BEEN STOPPED, SAVED OR RELEASED. Cancel is only reachable while the dialog is
    // idle — it disables all three buttons while a request is in flight — so there is nothing to
    // undo and nothing to say about the other project.
    setAsking(null)
    setStep(null)
  }, [])

  return { press, working, asking, step, resolve, cancel, outcome }
}

/**
 * DID THE SERVER ANSWER, AND WHAT DID IT SAY? — the one question both callers below ask.
 *
 * An `ApiError` carrying a message means the server answered and named something, including
 * `handOverWorkspace`'s two-minute ceiling sentence, which is authored there and reaches the pane
 * unedited. Anything else — an aborted fetch, a dropped socket, a body that would not parse — is
 * not prose anybody wrote for a citizen, so it is `null` and the caller says its own thing.
 *
 * One function rather than two, because the two callers used to make this judgement separately
 * with identical code, and "what counts as the server having answered" is exactly the kind of rule
 * that drifts when it is stated twice. What they still decide for themselves is what to SAY when
 * the answer is `null` — and those two sentences are deliberately different.
 */
function serverMessage(err: unknown): string | null {
  return err instanceof ApiError && err.message ? err.message : null
}

/** WHY A TAKE-BACK STOPPED, in the server's own words wherever it gave any. */
function reasonFor(err: unknown): string {
  return serverMessage(err) ?? 'Nothing came back, so we could not tell what happened.'
}

/**
 * Anything the server named, carried verbatim; anything it did not, called a timeout.
 *
 * A start that does not end in a running app says WHICH WAY it ended: "we waited and nothing came
 * back" is a different sentence from "the server said why".
 */
function outcomeFor(err: unknown): StartOutcome {
  const reason = serverMessage(err)
  return reason === null ? { kind: 'timed-out' } : { kind: 'failed', reason }
}

interface ControlProps {
  label: string
  /** THIS control is the one working: it renames itself and spins. */
  pending: boolean
  /**
   * Something ELSE on this pane is working, so this control is unavailable — but it is NOT
   * working, so it neither renames itself nor spins. Two flags rather than one because a
   * neighbour's spinner on a button nobody pressed says the wrong thing about what is happening.
   */
  inert?: boolean
  pendingLabel: string
  icon: React.ReactNode
  onPress: () => void
  /** The alternative rather than the expected answer — see the take-back's arm. */
  secondary?: boolean
}

function Control({ label, pending, inert = false, pendingLabel, icon, onPress, secondary = false }: ControlProps) {
  const unavailable = pending || inert
  return (
    <button
      type="button"
      // `aria-disabled`, NEVER `disabled` — see the docblock. The click handler checks the same
      // flag, so the control is inert without being unfocusable.
      aria-disabled={unavailable}
      // A property, not a speech: it marks the control as working without announcing anything,
      // which is what keeps this off the pane's live region. It goes on the control that is
      // ACTUALLY working, never on the one merely waiting for it.
      aria-busy={pending}
      onClick={() => {
        if (!unavailable) onPress()
      }}
      className={`inline-flex items-center gap-2 rounded-xl px-5 py-2.5 text-sm font-bold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 ${
        secondary
          ? 'border border-canvas-rule bg-white text-tertiary hover:bg-bial-bg'
          : 'bg-primary text-white shadow-sm shadow-primary/30 hover:bg-primary-600'
      } ${unavailable ? 'opacity-60' : ''}`}
    >
      {pending ? <BusyGlyph size={15} /> : icon}
      {/* THE WORDS ARE WHAT CHANGES. With no `aria-label` over the top, this is also the
          accessible name — so the button renames itself from "Launch Application" to "Starting
          your app…" as it goes, and a reader on the control hears the change. */}
      {pending ? `${pendingLabel}…` : label}
    </button>
  )
}
