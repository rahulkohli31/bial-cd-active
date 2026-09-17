/**
 * THE TWO THINGS THE PLATFORM OWES THE CITIZEN ABOUT THEIR APP'S LIFE, and it owes them because
 * nothing asks them any more.
 *
 * Apps start when a project is opened and go away when everyone leaves. Nothing prompts, nothing
 * guards the exit, and no control is pressed — which is the point, and which is also why silence
 * here would be dishonest. Two facts a screen cannot infer:
 *
 *  1. A container has an absolute ceiling. No amount of sitting on the screen postpones it, so a
 *     citizen mid-task deserves to know it is coming rather than to watch their app vanish.
 *  2. A write-back can be REFUSED. When the platform's automatic save-back does not descend from
 *     the citizen's own saved version, the tree is set aside and the app comes back from the
 *     saved one instead — which from the screen looks exactly like an ordinary reopen. Saying so
 *     is the whole of what makes removing the exit prompts honest rather than merely quieter.
 *
 * Neither is a control and neither is dismissible: both are statements of fact that stop being
 * true on their own.
 */
import { Clock, FileWarning } from 'lucide-react'
import { formatResetTime } from '../../utils/turnNarrative'

export interface WorkspaceLifecycleNotesProps {
  /** When this app's container reaches its ceiling, or `null` when no ceiling applies. */
  drainingAt: string | null
  /** When a platform write-back for this app was last refused, or `null` if none ever was. */
  writeBackRefusedAt: string | null
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

export default function WorkspaceLifecycleNotes({
  drainingAt,
  writeBackRefusedAt,
}: WorkspaceLifecycleNotesProps) {
  const closingAt = drainingAt !== null ? closingTime(drainingAt) : null
  if (closingAt === null && writeBackRefusedAt === null) return null

  return (
    // `polite`, never `assertive`: neither of these interrupts anything, and one of them can
    // arrive while somebody is typing.
    <section className="px-[18px] py-3" aria-live="polite" data-testid="workspace-lifecycle-notes">
      {writeBackRefusedAt !== null && (
        <p
          className="flex gap-2 text-[12.5px] leading-snug text-primary-900"
          data-testid="writeback-refused-note"
        >
          <FileWarning size={14} className="mt-[2px] flex-shrink-0" aria-hidden="true" />
          <span>
            Some recent changes could not be saved back automatically, so this app opened from
            your last saved version. The newer work was set aside — ask an administrator if you
            need it.
          </span>
        </p>
      )}
      {closingAt !== null && (
        <p
          className="flex gap-2 text-[12.5px] leading-snug text-primary-900"
          data-testid="closing-soon-note"
        >
          <Clock size={14} className="mt-[2px] flex-shrink-0" aria-hidden="true" />
          <span>This app closes at {closingAt}. Open it again whenever you want it back.</span>
        </p>
      )}
    </section>
  )
}
