/**
 * A PLAN CHAT HAS NO PANE, AND STILL SAYS EVERYTHING.
 *
 * THE STANDING LINE is the board's wording, verbatim, and it is a claim about the SCREEN — a plan
 * chat has no app pane — not about the container, which a planning question may well have started
 * and be holding. Its second clause holds for its own reason: a plan turn is handed no mutating tools.
 *
 * THE WORKSPACE LINE below it is the value the pane renders, as text — one computed state, two
 * renderers, never a second wording.
 *
 * WHY THIS EXISTS
 *
 * A PLAN CHAT SAYS, AND DOES NOT OFFER. It renders the sentence for the states below and no verb
 * at all: START is the one thing a surface that deliberately keeps the app off screen must not
 * invite, and RETRY would be a second author for a state the pane already owns. Those are the only
 * two verbs there are, so this surface draws no control — a failure here is a missing button,
 * never a press that reaches a container from a screen that cannot show what happened to it.
 */
import { useWorkspaceReport } from './workspaceChannel'
import type { WorkspaceStateName } from './workspaceState'

/**
 * The exact set of workspace states a Plan chat must describe in the board's own wording,
 * and no others. Scoped deliberately: extending the same wording to `never_built` and
 * `not-running` would lock in phrasing those states don't need, and whose copy may
 * reasonably want to say something different — a Plan chat has no business inviting somebody
 * to press a start control it does not render.
 */
const SPOKEN_HERE: ReadonlySet<WorkspaceStateName> = new Set<WorkspaceStateName>([
  'starting',
  'could-not-read',
])

export default function PlanChatWorkspaceLine() {
  const report = useWorkspaceReport()
  const state = report?.state
  const speak = state !== undefined && SPOKEN_HERE.has(state.name)

  return (
    // MOUNTED ALWAYS, even before the first read lands. A region that appears together with its
    // first sentence arrives without warning under whatever the person was reading; one that is
    // always in the document simply gains a line.
    // THE BOARD'S TREATMENT, and it is a caption rather than a paragraph: 11px `#9AA5B1`, centred
    // under the box with 8px of air. `PlanChat` and `PlanReady` both draw it in that slot; it read
    // as a left-aligned 12px sentence ABOVE the composer, which puts a standing disclaimer between
    // the transcript and the box the citizen is typing in.
    <div
      data-testid="plan-chat-workspace-line"
      className="mt-2 px-4 text-center text-[11px] leading-relaxed text-canvas-placeholder"
    >
      <p>
        {/* THE BOARD'S SENTENCE, VERBATIM — see the docblock for why both of its halves are true. */}
        Planning is a conversation. Your app is not open here and nothing you say changes it.
      </p>
      {speak && state && (
        <div data-testid="plan-chat-workspace-state" className="mt-1.5 flex flex-wrap items-center gap-2">
          {/* THE SAME SENTENCE THE PANE SHOWS, from the same computed value — never a second
              wording for the same state. */}
          <span className="font-semibold text-tertiary">{state.headline}</span>
        </div>
      )}
    </div>
  )
}
