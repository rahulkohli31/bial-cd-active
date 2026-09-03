/**
 * A PLAN CHAT HAS NO PANE, AND STILL SAYS EVERYTHING (Plan F, U6).
 *
 * ═══ TWO DIFFERENT SENTENCES, AND CONFUSING THEM IS THE MISTAKE TO AVOID ═══
 *
 * THE STANDING LINE says what this chat is for, and it is THE BOARD'S, VERBATIM (plan 002, U6).
 *
 * IT WAS REWRITTEN ONCE, AND THAT WAS A MISREADING. An earlier pass read the board's "your app is
 * not open here" as a claim that the app is not RUNNING, which would be false — a planning question
 * reads the live app and starts it if it is stopped, so the container may well be up and held by
 * this very conversation. But the board is not talking about the container. It is talking about the
 * SCREEN: a plan chat has no app pane, which is the whole of what "not open here" says, and the
 * second clause is true for a different reason again — a plan chat's toolset carries no write, no
 * schema change, no sandbox command and no finish tool, so the run genuinely cannot alter the app.
 *
 * Both halves are true, so the board's own words stand. Recorded at this length because the line
 * has now been argued about twice, and the next reader should not have to have it a third time.
 *
 * THE WORKSPACE LINE is the same value the pane renders, as text. Same source, same wording,
 * different surface. That is what makes "no pane" structurally incapable of meaning "says nothing":
 * a Plan chat is a second RENDERER of one computed state, not a surface with its own vocabulary.
 *
 * ═══ SENTENCE ALWAYS, ACTION SELECTIVELY — AND THE RULE IS STATED BECAUSE IT IS NOT OBVIOUS ═══
 *
 * `StartAppControl` renders wherever the map offers an action, with no surface predicate of its
 * own. So the gate is here, and it lets exactly ONE of the three members through: go to the project
 * that holds the workspace.
 *
 *  - START never renders. A Plan chat with a "Launch Application" button contradicts R11's framing
 *    and the register — this is the surface that deliberately does not put the app on screen.
 *  - RETRY never renders. It would be a second author for a state the pane already owns, and a
 *    person in a Plan chat has no pane to watch the retry land in.
 *  - GO-TO does render, and it has to. R4b makes that remedy the answer for a taken workspace, and
 *    R94 says the asking happens in the chat the person is actually in — so a Plan chat that showed
 *    the sentence with no way to act would leave the remedy unreachable from the only surface that
 *    can offer it.
 */
import StartAppControl from './StartAppControl'
import { useWorkspaceReport } from './workspaceChannel'
import type { WorkspaceStateName } from './workspaceState'

/**
 * The states R97 requires a Plan chat to speak for, and only those. Scoped deliberately: asserting
 * sameness across `never_built` and `not-running` too would pin wording R97 does not ask for, and
 * which R11's framing may well want different — a Plan chat has no business inviting somebody to
 * press a start control it does not render.
 */
const SPOKEN_HERE: ReadonlySet<WorkspaceStateName> = new Set<WorkspaceStateName>([
  'starting',
  'held-by-another-project',
  'held-unattributed',
  'could-not-read',
])

export default function PlanChatWorkspaceLine() {
  const report = useWorkspaceReport()
  const state = report?.state
  const speak = state !== undefined && SPOKEN_HERE.has(state.name)
  // The ONE action member this surface may render. Read before the early return below so the rule
  // is visible beside the states it applies to rather than buried in a branch.
  const remedy = state?.action?.kind === 'go-to-project' ? state.action : null

  return (
    // MOUNTED ALWAYS, even before the first read lands. A region that appears together with its
    // first sentence arrives without warning under whatever the person was reading; one that is
    // always in the document simply gains a line.
    <div data-testid="plan-chat-workspace-line" className="px-4 pb-2 text-xs leading-relaxed text-neutral">
      <p>
        {/* THE BOARD'S SENTENCE, VERBATIM — see the docblock for why both of its halves are true. */}
        Planning is a conversation. Your app is not open here and nothing you say changes it.
      </p>
      {speak && state && (
        <div data-testid="plan-chat-workspace-state" className="mt-1.5 flex flex-wrap items-center gap-2">
          {/* THE SAME SENTENCE THE PANE SHOWS, from the same computed value — never a second
              wording for the same state. */}
          <span className="font-semibold text-tertiary">{state.headline}</span>
          {remedy && report && <StartAppControl action={remedy} report={report} />}
        </div>
      )}
    </div>
  )
}
