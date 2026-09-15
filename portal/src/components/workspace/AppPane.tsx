/**
 * THE APP PANE — what the pane is called, and how to get past it.
 *
 * It contributes the region label, the skip control, and the sentence for when there is nothing to
 * frame. The frame's mounting, identity, hiding and reload nonce are `AppPaneHost`'s; calling
 * `LivePreview` from here builds a second host, which reloads the app on every navigation and every
 * crossing of the layout threshold, uncaught by the suite.
 *
 * WHY THIS EXISTS
 *
 * The address comes from `previewAddress.ts`, never `PreviewState.previewUrl`, so the live turn's
 * preview outranks the session URL describing the previous build. Its `.url` alone is not enough: a
 * provisioning build has a status and no URL yet, driving the loading state instead of an empty
 * pane, and an address outlives its publisher on purpose, so framing a stale one leaves a sleeping
 * app with no way to wake it. The workspace state is what decides whether that address is framed
 * at all, and after the serving stamp it takes exactly one name to mount the frame — see `frameIt`.
 *
 * The pane is a cross-origin iframe whose tab sequence and focus belong to the framed app, so a way
 * past it must live outside it. The take-back's sequence and dialog mount here because this is what
 * outlives their wait: the controls that start it live inside `NoFrame` and stop rendering the
 * instant the state moves, so a dialog owned by one of them would vanish mid-sequence. The dialog
 * is drawn OUTSIDE the section, which carries `aria-hidden` and `inert` whenever no surface wants
 * the pane; inside it the modal would be announced as absent and unreachable by keyboard.
 *
 * ACA wildcard DNS answers for a hostname whose container is gone, so a framed URL can resolve to a
 * host serving nothing; the workspace state draws the empty, stopped and gone states, not the origin.
 */
import { memo, useCallback, useEffect, useState } from 'react'
import { Box, FolderOpen, Locate, Play, WifiOff, type LucideIcon } from 'lucide-react'
import AppPaneHost from './AppPaneHost'
import { HIDDEN_BUT_MOUNTED } from './hiddenSubtree'
import { inertWhile, usePaneLeaving } from './paneExit'
import StartAppControl, { useTakeBack, type TakeBack } from './StartAppControl'
import ReclaimWorkspaceDialog from '../projects/ReclaimWorkspaceDialog'
import type { DeviceName } from './devices'
import { WORKSPACE_RAIL_ID } from './railId'
import {
  useWorkspaceAddress,
  useWorkspaceHeading,
  useWorkspacePaneVisible,
  useWorkspaceReport,
} from './workspaceChannel'
import type { WorkspaceStateName } from './workspaceState'

/**
 * THE MARK ABOVE THE HEADLINE, ON EVERY BOARD THAT DRAWS ONE. Each empty-pane board puts a 30px
 * #9AA5B1 glyph directly above its headline; without it a blank half-screen reads as a page that
 * failed to load. A lookup here rather than a field on `WorkspaceState`, which stays a pure
 * module: the words are the map's, the icon is local. Exhaustive over `WorkspaceStateName`, so a
 * new state cannot be added without somebody deciding here.
 *
 * ONE ENTRY IS `null` AND IT IS AN ANSWER, NOT A GAP. `running` draws no card at all — the frame
 * IS the state — so there is nothing for a mark to sit above.
 *
 * IT USED TO BE SEVEN NULLS, WHICH WAS THE SAME DRIFT THE FRAME VETO HAD. Four of the seven named
 * states the ten-to-five collapse deleted outright, and `running` is the one that keeps its null.
 * The last two draw a real card — a real headline, a real detail, real buttons — and stood there
 * bare. Both now carry the mark THIS PRODUCT ALREADY USES for what they are, which is neither
 * borrowing one of the three boards' marks nor inventing a vocabulary: `ReclaimWorkspaceDialog`
 * draws `FolderOpen` for the project holding the workspace, and `LivePreview` draws `WifiOff` for
 * the platform having lost touch with what it was watching.
 */
const STATE_GLYPH: Readonly<Record<WorkspaceStateName, LucideIcon | null>> = {
  'never-built': Locate, // NothingBuilt — the ticked circle, the same mark the rail's Plan picker has
  'not-running': Play, // PreviewOff — "Your app is saved", and the press that brings it back
  starting: Box, // PreviewStarting — "Setting up somewhere for it to run"
  running: null, // The frame is up; this pane draws no card at all.
  'held-by-another-project': FolderOpen, // The other project that is holding the one workspace
  'could-not-read': WifiOff, // The read itself never came back — see `couldNotRead` in the map
}

export interface AppPaneProps {
  /** The width the app is framed at. Shell-owned, because its control is in the toolbar row. */
  device: DeviceName
  /** Bumped by the row's Reload control; the frame re-requests its document on a change. */
  reloadNonce: number
}

function AppPane({ device, reloadNonce }: AppPaneProps) {
  const address = useWorkspaceAddress()
  const report = useWorkspaceReport()
  // THE COLUMN ITSELF ANSWERS TO THE VISIBILITY, NOT ONLY THE FRAME INSIDE IT.
  //
  // `AppPaneHost` hides itself when no surface declares a pane — but the host is only reached when
  // there is something to frame. A plan chat is the opposite case: nothing to frame AND no pane
  // declared, so `frameIt` is false, `NoFrame` renders instead of the host, and this section's
  // `flex-1` went on claiming half the window for a card offering to start an app the citizen did
  // not ask for. That is exactly the layout `PlanChat` forbids: the board draws one
  // centred column across the full width, and a column cannot centre on a window it only owns
  // half of.
  //
  // ZERO IN BOTH DIRECTIONS, because this column sits in a flex row above the stacking threshold
  // and a flex COLUMN below it — a width alone leaves a full-height band under a stacked rail.
  const visible = useWorkspacePaneVisible()
  // THE TAKE-BACK'S WHOLE SEQUENCE — held here because this is what outlives it. See the docblock.
  // `null` when nobody has computed a state; the hook is unconditional, as hooks are.
  //
  // THE WHOLE REPORT, NOT ITS HANDLERS: the hook reads `projectId` off it to know whose sequence
  // it is holding, and a pane that outlives a navigation would otherwise carry one project's
  // dialog and busy flag onto the next.
  const takeBack = useTakeBack(report)
  // THE APP THE CITIZEN IS TRYING TO OPEN — the framing half the dialog leads with. Published by
  // the routes (`ProjectPage` / `ChatRoute`), not by the surfaces, so it is read from the channel
  // rather than derived here. `null` before a project's own fetch lands, and the dialog then falls
  // back to its plain phrasing rather than quoting an empty string.
  const heading = useWorkspaceHeading()
  // THE EXIT THE BOARD DRAWS, and the reason it needs a state of its own: see `paneExit.ts`. This
  // column is the outermost thing that collapses, so the hold is decided here and handed to the
  // host — the two must not disagree about whether they are still on their way out.
  const leaving = usePaneLeaving(visible)

  // THE FRAME MOUNTS IF AND ONLY IF THE PLATFORM HAS PROOF THE APP SERVED, and `running` is the
  // only name that carries that proof: the wire's `alive` is now gated on a stamp written where
  // something watched the app ANSWER a request, where it used to mean only that a container had
  // been SCHEDULED. The eight seconds a citizen spent reading "This app isn't running right now"
  // INSIDE this pane on 2026-09-10 are the distance between those two meanings.
  //
  // IT REPLACES A FIVE-MEMBER VETO SET WITH EXCEPTIONS WRITTEN BESIDE IT, and the shape is the
  // point rather than the line count: a list of the states that withhold the frame has to be
  // revisited every time the map grows, and every exception on it is one more judgement about a
  // state whose meaning can move underneath it — which is how a list of names drifts away from
  // the question it was answering. A rule cannot fall behind. Every state but one withholds, and
  // none of them needs an entry anywhere to do it: the three start outcomes that used to need an
  // exception are gone from the map entirely, and so is the second held state that needed a member.
  //
  // AND ONLY A VERDICT MOVES THE FRAME — the invariant this pane has always kept, restated as a
  // rule about EVIDENCE rather than as a name on an exception list: an unreadable read must never
  // retire a frame somebody is looking at, so the platform has to have SAID something before the
  // pane acts on it. Two ordinary shapes say nothing, and neither of them is exotic:
  //
  //   `could-not-read`  a read that decided nothing at a moment when nothing had ever been
  //                     decided — which is every surface's own first render, before its first
  //                     poll answers. From the second reading onward the map renders the last
  //                     settled one instead, and this arm is unreachable.
  //   no report at all  `usePublishWorkspaceReport` CLEARS on unmount, so every hop between two
  //                     surfaces has a window with a live address and nobody reporting.
  //
  // Withholding on either would unmount the host on every navigation into a running app and on
  // every cold mount over one: a cross-origin `src` re-issued, and the citizen's form entries,
  // scroll position and open tab thrown away. That is the one thing `AppPaneHost` exists to
  // forbid, and it is why "no verdict" is not a carve-out but the rule's own precondition.
  const verdict =
    report !== null && report.state.name !== 'could-not-read' ? report.state.name : null
  // A STATUS WITH NO URL IS THE LOADING STATE, not an empty pane — see the docblock.
  const somethingToFrame = address.url !== null || address.status !== null
  const frameIt = somethingToFrame && (verdict === null || verdict === 'running')

  /**
   * MOVE FOCUS BACK TO THE RAIL, and do it by focusing the region rather than hunting for its
   * first control. A `tabindex="-1"` container is programmatically focusable without joining the
   * tab order, so the next Tab continues from the rail's top — which is what a person escaping the
   * frame actually wants. Querying for "the first button" would break the moment the rail's first
   * element is not one.
   */
  const skipPastTheApp = useCallback(() => {
    const rail = document.getElementById(WORKSPACE_RAIL_ID)
    if (!rail) return
    if (!rail.hasAttribute('tabindex')) rail.setAttribute('tabindex', '-1')
    rail.focus()
  }, [])

  return (
    <>
      <section
        data-testid="app-pane-region"
        aria-label="Your app"
        // ANNOUNCED AS GONE THE MOMENT IT IS UNWANTED, even while it is still on its way out. The
        // movement is for the eye; a reader who is not watching it should not be told about an app
        // that is leaving.
        aria-hidden={!visible}
        // AND OUT OF REACH ON THE SAME FACT, from the same moment. `aria-hidden` is the half a
        // screen reader obeys; this is the half a keyboard obeys, and they are given one condition
        // so they cannot come apart.
        //
        // IT IS THE LEAVE THAT NEEDS IT. At rest the pane is `visibility:hidden`, which drops its
        // subtree from the tab order on its own — but the column holds its SIZE for one animation so
        // the card can be watched going, and an invisible element has nothing to animate. For that
        // quarter of a second the skip control below and the framed app were both still one Tab
        // away, on a region already announced as gone. See `paneExit.ts` for the empty string.
        {...inertWhile(!visible)}
        className={
          visible
            ? 'flex-1 min-w-0 min-h-0 flex flex-col overflow-hidden'
            : leaving
              ? // ON ITS WAY OUT. The column keeps its size for one animation while the card slides
                // right and fades — `w-0` here instead would make the keyframe unobservable, which
                // is why the utility existed unused. The rail beside it is already growing, which
                // is the board's "the conversation is already settling towards the middle of the
                // window".
                'flex-1 min-w-0 min-h-0 flex flex-col overflow-hidden animate-pane-leave'
              : // Hidden, never unmounted — the whole point of the sibling host is that leaving a
                // build chat for a plan chat must not re-issue the frame's `src`.
                `w-0 h-0 flex-shrink-0 overflow-hidden ${HIDDEN_BUT_MOUNTED}`
        }
      >
        {/* VISIBLE ON FOCUS ONLY. It is the standard skip-link treatment: out of the way for a
            pointer, and the first thing a keyboard reaches on its way into the frame. */}
        <button
          type="button"
          onClick={skipPastTheApp}
          className="sr-only focus:not-sr-only focus:absolute focus:z-30 focus:m-2 focus:rounded-lg focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:font-semibold focus:text-primary focus:shadow-lg focus:outline-none focus:ring-2 focus:ring-primary/40"
        >
          Skip past your app
        </button>

        {/* THE COLLAPSE CONTROL IS NOT HERE ANY MORE. It moved to the toolbar row, which is drawn
            once above the two-column grid. Here it was already better than living inside the rail
            it hides — a collapsed rail is invisible and untabbable, so a toggle in it is a one-way
            door — but it still appeared and disappeared with the pane. In the row it has one home
            in every state, beside the title that now also survives a collapse. */}

        {/* THE PANE'S LIVE REGION, MOUNTED UNCONDITIONALLY AND WRAPPING ITS CONTENT.

            IT USED TO LIVE INSIDE THE ROW THAT DRAWS THE BUTTONS, which meant the one state with a
            wait in it and no button — `starting`, whose `action` is `null` — had NO REGION AT ALL.
            A citizen sat through a two-minute sandbox start with nothing said, entering or leaving.
            That is the whole defect, and moving this element out of `NoFrame`'s
            `state.action &&` block is the whole of the fix: the region is now born with the pane,
            holds whatever the board is saying, and outlives every transition between boards.

            IT WRAPS THE EMPTY-PANE CONTENT AND POINTEDLY NOT THE FRAMED HOST. `LivePreview` keeps
            its own permanent region and speaks for every framed state; wrapping the host as well
            would put a second polite region around the first and announce the cover, the stall and
            the reveal twice — the exact duplication `LivePreview`'s own docblock forbids. The two
            regions divide the pane between them: this one owns the states with no app in them.

            SO WHEN THE APP IS FRAMED THIS ELEMENT IS EMPTY, and it keeps standing anyway. A live
            region inserted together with its text announces inconsistently — the convention stated
            at `LivePreview.tsx` and at `TurnBanner.tsx` — so it must exist before it has anything
            to say. Empty, its only child is absolutely positioned, so it occupies no height and the
            host beside it is unaffected. */}
        <div
          data-testid="app-pane-live"
          role="status"
          aria-live="polite"
          // NO `aria-busy` HERE, DELIBERATELY. The wait's busy flag goes on the board that draws
          // the wait (see `NoFrame`), because `aria-busy` on a live region tells a reader to hold
          // its announcements until the busy clears — which would silence the very "entering the
          // wait" announcement this region exists to make.
          className={frameIt ? '' : 'flex min-h-0 flex-1 flex-col px-4 pb-4 pt-3.5'}
        >
          {!frameIt && (
            // THE EMPTY PANE IS A NAMED REGION WITH A CARD IN IT, which is what every state but
            // `running` draws. The label is the tell: it appears on exactly the boards where the
            // pane holds no app, because a blank half of the screen needs to say what it is for,
            // and a running application says that itself. Drawn here rather than at the section,
            // so it comes and goes with the emptiness it explains.
            <>
              {/* DECORATIVE, and it has to be now that it is inside the region: the section above
                  is already labelled "Your app", so this caption is that label a second time, and
                  a reader would otherwise hear "YOUR APP" announced every time the pane emptied. */}
              <p
                aria-hidden="true"
                className="mb-2.5 text-[11.5px] font-bold tracking-[0.6px] text-neutral"
              >
                YOUR APP
              </p>
              <div className="flex min-h-0 flex-1 overflow-hidden rounded-xl border border-canvas-rule bg-white shadow-app-card">
                <NoFrame report={report} takeBack={takeBack} />
              </div>
            </>
          )}
          {/* WHAT A TAKE-BACK THAT WORKED DID TO THE OTHER PROJECT.

              THE FAILURE ARM ALREADY HAD A SENTENCE and it is NOT produced here: the map writes it
              onto `state.note` and the board above renders it, and it announces now purely because
              this region moved. A second producer for it would be the same news said twice.

              THE SUCCESS ARM HAD NOTHING, which is the half this adds. A take-back that works ends
              with the other citizen's app stopped and this pane quietly framing an app — so the
              person who pressed it learned nothing at all about what they had just taken. It is
              `sr-only` because on this ending the pane is showing the running app: there is no card
              left to put a sentence in, and a line floating over the frame would be the pane
              talking about itself. Nothing else on screen says it, so this is not a duplicate. */}
          {takeBack.outcome !== null && <p className="sr-only">{takeBack.outcome}</p>}
        </div>
        {/* THE FRAME IS THE HOST'S. Everything from the frame inward — the cover that holds on an
            unknown, the `load`-gated reveal, the frame key, the inbound-message gate on origin AND
            source, the sandbox token list — is unchanged and stays there. The device WIDTH is the
            shell's now, because the control that picks it is in the row, and it is passed through
            rather than held: two owners of one width is how the card and the switcher disagree.

            IT DRAWS ITS OWN CARD — `LivePreview` frames the iframe in a padded `#e8edf2` box with a
            rounded, shadowed white surround — which is why the card above is on the EMPTY arm only.
            A second card around the first would be two borders and two shadows on one app. */}
        {frameIt && <AppPaneHost device={device} reloadNonce={reloadNonce} leaving={leaving} />}
      </section>
      {/* THE TAKE-BACK IS ROUTED THROUGH THE DIALOG THAT ALREADY EXISTS, with its three copy arms
          and its `agentWorking` sentence reused unchanged. No new copy is written for it anywhere
          in this unit.

          FORCE-REMOUNTED, KEYED ON THE HOLDER, and that is not a rendering nicety. When another tab
          takes the freed slot mid-sequence the refusal names a DIFFERENT project, and this dialog
          captures focus in a MOUNT-time effect: updating it in place would leave a keyboard user's
          focus parked on the card where the busy state put it, while the copy in front of them
          silently changed which project it is talking about — an irreversible choice, re-aimed
          under their hands. A new key is a new mount, so the focus goes where the new question is.

          AND THE HANDLERS ARE PASSED THROUGH RATHER THAN WRAPPED. The dialog's `run()` turns any
          rejection into its own alert and stays up; every ending of `resolve` resolves, and the
          pane behind is what reports. */}
      {takeBack.asking && (
        <ReclaimWorkspaceDialog
          key={takeBack.asking.projectId}
          blocked={takeBack.asking}
          startingProjectName={heading.projectName}
          step={takeBack.step}
          onSaveAndSwitch={() => takeBack.resolve(true)}
          onSwitchAnyway={() => takeBack.resolve(false)}
          onCancel={takeBack.cancel}
        />
      )}
    </>
  )
}

/**
 * WHAT THE PANE SAYS WHEN THERE IS NOTHING TO FRAME — one author, and it is the state map.
 *
 * They are drawn here from one computed value, so a pane sentence has exactly one author and a
 * state nobody is in cannot have chrome drawn for it.
 */
function NoFrame({
  report,
  takeBack,
}: {
  report: ReturnType<typeof useWorkspaceReport>
  takeBack: TakeBack
}) {
  // NOBODY HAS COMPUTED A STATE. A surface mounted outside a workspace, or one still resolving its
  // project. Saying nothing is the honest answer — inventing a sentence here would be a second
  // author for the one thing this whole design gives a single one.
  if (!report) return null

  const { state } = report
  // See `STATE_GLYPH`: every board that draws an empty pane draws a mark above the headline, and
  // `running` — the one state with no board at all — is the only entry that answers with none.
  const Glyph = STATE_GLYPH[state.name]
  return (
    <div
      data-testid="app-pane-empty"
      // THE INTERNAL STATE NAME, EXPOSED FOR TESTS AND NEVER RENDERED. `not-running` is the one to
      // watch: it is a state name here and on the wire, and the exact phrase the copy rule forbids
      // on screen. It is an attribute rather than text for that reason — and it gives a suite a
      // handle on WHICH state the pane reached without pinning the copy, which may change again.
      data-workspace-state={state.name}
      // THE WAIT'S BUSY FLAG SITS ON THE BOARD THAT DRAWS THE WAIT, not on the region that
      // announces it — the same placement `LivePreview`'s `BouncingWait` already uses. It is
      // a property, not a speech: it marks this content as unsettled without saying anything, so
      // the region above stays free to announce the wait entering and leaving.
      //
      // FROM THE MAP, NEVER FROM `state.name === 'starting'` HERE. A second derivation of the same
      // claim is a second author for it; see `WorkspaceState.busy`.
      aria-busy={state.busy === true}
      className="flex flex-1 items-center justify-center p-8"
    >
      <div className="flex max-w-sm flex-col items-center text-center">
        {/* 30px, 1.6 stroke, #9AA5B1 — the board's own numbers, 14px above the headline.
            Decorative: the headline beneath it says the same thing in words. */}
        {Glyph && (
          <Glyph
            data-testid="app-pane-glyph"
            size={30}
            strokeWidth={1.6}
            aria-hidden="true"
            className="mb-3.5 flex-shrink-0 text-canvas-placeholder"
          />
        )}
        <p className="text-base font-bold text-tertiary">{state.headline}</p>
        {state.detail && <p className="mt-2 text-sm text-neutral leading-relaxed">{state.detail}</p>}
        {/* WHAT A TAKE-BACK DID TO SOMEBODY ELSE'S APP — its own line, because it has its own
            subject. Emphasised rather than greyed: it is the half of the outcome a citizen cannot
            find out any other way without opening the other project. */}
        {state.note && (
          <p data-testid="app-pane-note" className="mt-2 text-sm font-semibold text-tertiary leading-relaxed">
            {state.note}
          </p>
        )}
        {/* HOW LONG THIS HAS BEEN GOING ON — the honest half of the progress bar that was dropped.
            A still card that never changes reads as a hung screen after about twenty seconds; a
            number that moves is the cheapest possible evidence that the platform is still working,
            and unlike a bar every position on it is a measured fact. */}
        {state.busy === true && <ElapsedSinceTheWaitBegan />}
        {/* THE ROW IS NO LONGER THE POLITE REGION. It was, and that was the defect: a region
            mounted inside `state.action &&` does not exist on the one state that has a wait and no
            action. The region moved up to `AppPane`, where it wraps this whole board — so the
            take-back still renames ITSELF inside it and is still announced once on entering and
            again on leaving, and the headline, the detail and the note are announced too. */}
        {state.action && (
          <div className="mt-5 flex flex-wrap justify-center gap-2.5">
            {/* THE SEQUENCE GOES TO BOTH SLOTS, and the leading one needs it as much as the second.
                It used to be handed only to `secondAction`, on the reasonable-looking assumption
                that a take-back can only ever be the second control. The held-state merge broke
                that assumption: when the platform cannot name the holder there is no go-to to
                lead with, so the take-back IS `action` — and `StartAppControl` renders NOTHING at
                all for a take-back it was given no sequence for ("no sequence in the parent, no
                verb"). The card then named the problem and offered nothing to press, which is the
                exact dead end the merge existed to remove, rebuilt one slot along.
                Harmless on every other kind: only the take-back arm reads this prop. */}
            <StartAppControl action={state.action} report={report} takeBack={takeBack} inert={takeBack.working} />
            {state.secondAction && (
              <StartAppControl
                action={state.secondAction}
                report={report}
                takeBack={takeBack}
                inert={takeBack.working}
              />
            )}
          </div>
        )}
      </div>
    </div>
  )
}

/**
 * HOW LONG THE WAIT HAS BEEN RUNNING — the elapsed time, and the one number on this pane.
 *
 * WHY IT IS A COUNT AND NOT A BAR
 *
 * The bar the design asks for has a STEP-determinate fill, advancing on the workspace claim, the
 * container start and the first document served. The platform observes all three and the browser
 * can read none of them: they happen inside one synchronous backend call and the wire carries a
 * single opaque `starting`/`ready` field. A bar built on what IS readable could only be
 * time-determinate, and a bar that sits at 80% for two minutes is worse than the honest still
 * card. Elapsed time is what is left that is true.
 *
 * IT COUNTS FROM ITS OWN MOUNT, WHICH IS EXACTLY THE WAIT
 *
 * No timestamp travels on the report and none needs to: this renders only while `state.busy`, so
 * mounting IS the wait beginning and unmounting IS it ending. A stamp on the state would have to be
 * compared in `sameWorkspaceState` — where a value that changes every render defeats the whole
 * comparator — and would re-render the entire shell once a second for a number only this pane
 * shows.
 *
 * AND IT IS NOT ANNOUNCED
 *
 * `aria-live="off"` because this element sits INSIDE the pane's polite region, and a counter that
 * ticks inside a live region is a screen reader reading a number every second for two minutes. Off
 * on the nearest ancestor means the value is still in the accessibility tree — a reader can go and
 * read it whenever they want to know — without being pushed at anybody.
 */
function ElapsedSinceTheWaitBegan() {
  const [seconds, setSeconds] = useState(0)
  useEffect(() => {
    // MEASURED AGAINST THE CLOCK, NEVER COUNTED IN TICKS. `setSeconds(was => was + 1)` counts
    // how many times the interval FIRED, and a browser throttles a hidden tab's timers — to
    // once a second, and to once a MINUTE after about five minutes hidden. A citizen who
    // switches away during a two-minute start and comes back would be told "40s so far" for a
    // wait that really took two minutes, which is the single thing this line exists to be
    // honest about. Reading the clock makes the throttling a refresh-rate question instead of
    // an accuracy one: the number may be stale by up to a tick, but it is never wrong.
    // `performance.now()`, NOT `Date.now()`: monotonic, so an NTP correction or somebody
    // changing the system clock mid-wait cannot make this count backwards or jump. It keeps
    // the property the change is for — it advances while the tab is hidden, which is exactly
    // what the throttled interval does not.
    const began = performance.now()
    const tick = setInterval(
      () => setSeconds(Math.floor((performance.now() - began) / 1_000)),
      1_000,
    )
    return () => clearInterval(tick)
  }, [])
  return (
    <p
      data-testid="app-pane-elapsed"
      aria-live="off"
      className="mt-3 text-xs tabular-nums text-neutral"
    >
      {formatElapsed(seconds)} so far
    </p>
  )
}

/** `0s`, `45s`, `1m 05s`. Seconds stay two-digit past the minute so the line does not jitter. */
function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`
}

/**
 * MEMOISED BECAUSE THE RAIL DRAG RE-RENDERS THE SHELL ON EVERY POINTER MOVE. `RailResizeHandle`
 * reports each move into the shell's own width state, and this column is its sibling — so without
 * this the whole pane subtree re-renders at pointer frequency for the length of a drag, for a
 * width that is not its own. Both props are primitives, so the default shallow compare is exactly
 * right; nothing else this component reads comes through props, and context and cell subscriptions
 * reach it regardless of memo.
 */
export default memo(AppPane)
