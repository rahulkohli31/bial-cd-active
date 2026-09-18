/**
 * THE ONE THING THE PLATFORM OWES THE CITIZEN ABOUT THEIR APP'S LIFE, and it owes it because
 * nothing asks them any more.
 *
 * Apps start when a project is opened and go away when everyone leaves. Nothing prompts, nothing
 * guards the exit, and no control is pressed — which is the point, and which is also why silence
 * here would be dishonest. A container has an absolute ceiling: no amount of sitting on the screen
 * postpones it, so a citizen mid-task deserves to know it is coming rather than to watch their app
 * vanish.
 *
 * It is not a control and it is not dismissible: it is a statement of fact that stops being true
 * on its own.
 */
import { Clock } from 'lucide-react'
import { formatResetTime } from '../../utils/turnNarrative'

export interface WorkspaceLifecycleNotesProps {
  /** When this app's container reaches its ceiling, or `null` when no ceiling applies. */
  drainingAt: string | null
}

/**
 * HOW CLOSE IS CLOSE ENOUGH TO MENTION. Far out, the ceiling is noise — every container has one,
 * and announcing it on arrival would make a routine fact read as a warning. Half an hour is long
 * enough to finish a thought and short enough that the sentence is about now.
 */
const WORTH_MENTIONING_MS = 30 * 60 * 1000

/**
 * The clock time to name, or `null` when there is nothing worth naming.
 *
 * BOUNDED AT BOTH ENDS. Past its ceiling the platform is collecting the container, not planning
 * to — "this app closes at 4:15" said at 4:30 is a promise about a time that has gone, which is
 * the one thing a note written to be honest must not become. Nothing is said instead: no instant
 * this component holds describes what an app does after its ceiling. An unreadable instant fails
 * the same comparisons and says nothing for the same reason.
 */
function closingTime(drainingAt: string): string | null {
  const untilItCloses = new Date(drainingAt).getTime() - Date.now()
  const worthMentioning = untilItCloses > 0 && untilItCloses <= WORTH_MENTIONING_MS
  return worthMentioning ? formatResetTime(drainingAt) : null
}

export default function WorkspaceLifecycleNotes({ drainingAt }: WorkspaceLifecycleNotesProps) {
  const closingAt = drainingAt !== null ? closingTime(drainingAt) : null
  if (closingAt === null) return null

  return (
    // `polite`, never `assertive`: this interrupts nothing, and it can arrive while somebody is
    // typing.
    <section className="px-[18px] py-3" aria-live="polite" data-testid="workspace-lifecycle-notes">
      <p
        className="flex gap-2 text-[12.5px] leading-snug text-primary-900"
        data-testid="closing-soon-note"
      >
        <Clock size={14} className="mt-[2px] flex-shrink-0" aria-hidden="true" />
        <span>This app closes at {closingAt}. Open it again whenever you want it back.</span>
      </p>
    </section>
  )
}
