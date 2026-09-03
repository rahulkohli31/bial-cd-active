/**
 * THE APP PANE (Plan F, U4) — what the pane is called, and how to get past it.
 *
 * ═══ IT CONTRIBUTES THREE THINGS AND MOUNTS NO IFRAME ═══
 *
 * The region label, the skip control, and the sentence for when there is nothing to frame. The
 * frame's mounting, its identity, its hiding and its reload nonce all stay in Plan A's
 * `AppPaneHost`. An implementer who calls `LivePreview` from here has built a SECOND host, and a
 * second host is the remount that AE4 and AE37 exist to forbid — the app would reload on every
 * navigation and every crossing of the layout threshold, with nothing red anywhere.
 *
 * This removes work rather than adding it: the pane needs no framing logic of its own.
 *
 * ═══ THE SEAM: THE RESOLVED ADDRESS, AND THE STATE THAT CAN INVALIDATE IT ═══
 *
 * The address comes from `previewAddress.ts` — never `PreviewState.previewUrl` — with its
 * precedence intact: the live turn's preview outranks the session URL, because the live turn is
 * the app being built in front of the person while the session URL describes the previous build.
 *
 * TWO THINGS AN EARLIER CUT OF THIS FILE GOT WRONG BY READING ONLY `address.url`, both caught by
 * the suites that pin the surfaces around this one:
 *
 *  1. A URL IS NOT THE ONLY THING THE RESOLVER RETURNS. Its own docblock says so: "a build that is
 *     provisioning has a STATUS and no URL yet, and that pair is what renders the loading state
 *     instead of an empty pane". Gating on the URL alone put "We could not check on your app." in
 *     front of a citizen watching their first build come up.
 *  2. AN ADDRESS OUTLIVES ITS PUBLISHER, DELIBERATELY — that is R8's whole mechanism — so a URL
 *     stays held after the container behind it has stopped. Framing it regardless meant an app
 *     that went to sleep showed a card saying "nothing is lost" with NO way to bring it back:
 *     R3's "exactly one control starts it", satisfied by zero, in an ordinary state.
 *
 * So the workspace state gets a veto, and only for the states that DEFINITELY mean nothing is
 * serving. `could-not-read` is pointedly not one of them: an answer that decided nothing must not
 * pull a working app off the screen, which is the rule the whole preview reshape exists for.
 *
 * ═══ WHY A SKIP CONTROL, AND WHY IT CANNOT LIVE INSIDE THE FRAME ═══
 *
 * The pane is a cross-origin iframe. It swallows the tab sequence into a document whose length
 * nothing here can know, and whose focus behaviour is the generated app's business — so a way PAST
 * it has to exist outside it. Without one, a person navigating by keyboard is trapped in somebody
 * else's application (R67).
 *
 * Nothing here makes any claim about the framed document's own accessibility. The pane says what it
 * is; what is inside is the app's.
 *
 * ═══ L10 — DO NOT ASSUME THE APPS ROUTER SERVES A BRANDED PAGE ═══
 *
 * ACA wildcard DNS answers for hostnames whose container is long gone, so an "app is gone" 404 is
 * not a reliable discriminator and a framed URL can resolve to a working-looking host serving
 * nothing. The empty, stopped and gone states are therefore drawn HERE, from the workspace state,
 * rather than left to whatever the framed origin happens to return.
 */
import { useCallback } from 'react'
import AppPaneHost from './AppPaneHost'
import { HIDDEN_BUT_MOUNTED } from './hiddenSubtree'
import StartAppControl from './StartAppControl'
import { WORKSPACE_RAIL_ID } from './WorkspaceShell'
import type { DeviceName } from './WorkspaceToolbar'
import { useWorkspaceAddress, useWorkspacePaneVisible, useWorkspaceReport } from './workspaceChannel'
import type { WorkspaceStateName } from './workspaceState'

/** See `frameIt` below. Kept beside the component so the veto's members are readable at a glance. */
const NOTHING_IS_SERVING: ReadonlySet<WorkspaceStateName> = new Set<WorkspaceStateName>([
  'not-running',
  'never-built',
  'held-by-another-project',
  'held-unattributed',
  // `starting` IS one of these, and the wire says so in as many words: it means "a start is in
  // flight … not `alive` (NO CONTAINER YET)". A held address survives its publisher, so without
  // this a press over a stale URL re-framed a container that is not there — the pane showing an
  // app while the platform is still bringing one up. The wait is the honest thing to show, and it
  // is what the map's `starting` arm says.
  'starting',
])

export interface AppPaneProps {
  /** The width the app is framed at. Shell-owned, because its control is in the toolbar row. */
  device: DeviceName
  /** Bumped by the row's Reload control; the frame re-requests its document on a change. */
  reloadNonce: number
}

export default function AppPane({ device, reloadNonce }: AppPaneProps) {
  const address = useWorkspaceAddress()
  const report = useWorkspaceReport()
  // THE COLUMN ITSELF ANSWERS TO THE VISIBILITY, NOT ONLY THE FRAME INSIDE IT (plan 002, U6).
  //
  // `AppPaneHost` hides itself when no surface declares a pane — but the host is only reached when
  // there is something to frame. A plan chat is the opposite case: nothing to frame AND no pane
  // declared, so `frameIt` is false, `NoFrame` renders instead of the host, and this section's
  // `flex-1` went on claiming half the window for a card offering to start an app the citizen did
  // not ask for. That is exactly the layout `PlanChat` forbids and U6 promises: the board draws one
  // centred column across the full width, and `ConversationSurface`'s `mx-auto max-w-3xl` cannot
  // centre inside a rail that is only half the screen.
  //
  // ZERO IN BOTH DIRECTIONS, because this column sits in a flex row above the stacking threshold
  // and a flex COLUMN below it — a width alone leaves a full-height band under a stacked rail.
  const visible = useWorkspacePaneVisible()

  /**
   * MOVE FOCUS BACK TO THE RAIL, and do it by focusing the region rather than hunting for its
   * first control. A `tabindex="-1"` container is programmatically focusable without joining the
   * tab order, so the next Tab continues from the rail's top — which is what a person escaping the
   * frame actually wants. Querying for "the first button" would break the moment the rail's first
   * element is not one.
   */
  // THE STATES THAT MEAN NOTHING IS SERVING, and therefore that a held address is stale.
  //
  // `could-not-read` is deliberately absent: a read that decided nothing must not retire a frame
  // somebody is looking at. So are the three start outcomes — they describe a press that did not
  // land, not a container that went away, and a frame already up is evidence enough.
  const stale = report !== null && NOTHING_IS_SERVING.has(report.state.name)
  // A STATUS WITH NO URL IS THE LOADING STATE, not an empty pane — see the docblock.
  const frameIt = !stale && (address.url !== null || address.status !== null)

  const skipPastTheApp = useCallback(() => {
    const rail = document.getElementById(WORKSPACE_RAIL_ID)
    if (!rail) return
    if (!rail.hasAttribute('tabindex')) rail.setAttribute('tabindex', '-1')
    rail.focus()
  }, [])

  return (
    <section
      data-testid="app-pane-region"
      aria-label="Your app"
      aria-hidden={!visible}
      className={
        visible
          ? 'flex-1 min-w-0 min-h-0 flex flex-col overflow-hidden'
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

      {/* THE COLLAPSE CONTROL IS NOT HERE ANY MORE (plan 002, U2). It moved to the toolbar row,
          which is drawn once above the two-column grid. Here it was already better than living
          inside the rail it hides — a collapsed rail is invisible and untabbable, so a toggle in
          it is a one-way door — but it still appeared and disappeared with the pane. In the row it
          has one home in every state, beside the title that now also survives a collapse. */}

      {frameIt ? (
        // THE FRAME IS THE HOST'S. Everything from the frame inward — the cover that holds on an
        // unknown, the `load`-gated reveal, the frame key, the inbound-message gate on origin AND
        // source, the sandbox token list — is unchanged and stays there. The device WIDTH is the
        // shell's now, because the control that picks it is in the row, and it is passed through
        // rather than held: two owners of one width is how the card and the switcher disagree.
        //
        // IT DRAWS ITS OWN CARD — `LivePreview` frames the iframe in a padded `#e8edf2` box with a
        // rounded, shadowed white surround — which is why the card below is on the EMPTY arm only.
        // A second card around the first would be two borders and two shadows on one app.
        <AppPaneHost device={device} reloadNonce={reloadNonce} />
      ) : (
        // THE EMPTY PANE IS A NAMED REGION WITH A CARD IN IT, which is what `PreviewOff`,
        // `NothingBuilt` and `PreviewStarting` draw — and only those three. The label is the tell:
        // it appears on exactly the boards where the pane holds no app, because a blank half of the
        // screen needs to say what it is for, and a running application says that itself. Drawn
        // here rather than at the section, so it comes and goes with the emptiness it explains.
        <div className="flex min-h-0 flex-1 flex-col px-4 pb-4 pt-3.5">
          <p className="mb-2.5 text-[11.5px] font-bold tracking-[0.6px] text-neutral">YOUR APP</p>
          <div className="flex min-h-0 flex-1 overflow-hidden rounded-xl border border-canvas-rule bg-white shadow-app-card">
            <NoFrame report={report} />
          </div>
        </div>
      )}
    </section>
  )
}

/**
 * WHAT THE PANE SAYS WHEN THERE IS NOTHING TO FRAME — one author, and it is the state map.
 *
 * These arms used to live inside `LivePreview` as a six-prop placeholder precedence spelled at the
 * pane's edge (`showRestoring` / `showTerminal` / `showReconnecting` / `showUnavailable`). They are
 * removed there and drawn here from one computed value, so a pane sentence has exactly one author
 * and a state nobody is in cannot have chrome drawn for it.
 */
function NoFrame({ report }: { report: ReturnType<typeof useWorkspaceReport> }) {
  // NOBODY HAS COMPUTED A STATE. A surface mounted outside a workspace, or one still resolving its
  // project. Saying nothing is the honest answer — inventing a sentence here would be a second
  // author for the one thing this whole design gives a single one.
  if (!report) return null

  const { state } = report
  return (
    <div
      data-testid="app-pane-empty"
      // THE INTERNAL STATE NAME, EXPOSED FOR TESTS AND NEVER RENDERED. `not-running` is the one to
      // watch: it is a state name here and on the wire, and the exact phrase R-16 forbids on
      // screen. It is an attribute rather than text for that reason — and it gives a suite a handle
      // on WHICH state the pane reached without pinning the copy, which the client has changed
      // twice and may change again.
      data-workspace-state={state.name}
      className="flex flex-1 items-center justify-center p-8"
    >
      <div className="max-w-sm text-center">
        <p className="text-base font-bold text-tertiary">{state.headline}</p>
        {state.detail && <p className="mt-2 text-sm text-neutral leading-relaxed">{state.detail}</p>}
        {state.action && (
          <div className="mt-5 flex justify-center">
            <StartAppControl action={state.action} report={report} />
          </div>
        )}
      </div>
    </div>
  )
}
