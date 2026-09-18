/**
 * The activity pill beside an application's NAME — starting, open right now, or closing down.
 *
 * NOT A STATUS. `AppStatusBadge` answers whether an app is published and reachable; this answers
 * whether the platform is holding a container open for it right now, which is a fact about the
 * platform's own housekeeping rather than about the app itself — the reason it sits beside the
 * name and never inside the Status column. It borrows that badge's pill SHAPE but none of its
 * tones: one colour family for all three phases keeps them reading as one kind of thing, not as a
 * second severity scale next to the real one.
 *
 * QUALIFIED WHEN THE APP IS LIVE. A citizen reading "Closing down" beside a published app could
 * take it for their app going offline for its users — it is not; only the editor's own container
 * is going away. `live` swaps the closing phase's words for ones that can only mean the editor.
 */
import type { ActivityPhase } from '../../utils/buildSessionApi'
import { assertNever } from '../../utils/assertNever'
import { BusyGlyph } from '../ui/Waiting'
import { CircleDot } from 'lucide-react'

const TONE = 'bg-sky-50 text-sky-700 ring-1 ring-sky-600/20'

function copyFor(phase: ActivityPhase, live: boolean): string {
  switch (phase) {
    case 'starting':
      return 'Starting'
    case 'open':
      return 'Open now'
    case 'closing':
      return live ? 'Editor closing' : 'Closing down'
    default:
      return assertNever(phase)
  }
}

export interface AppActivityMarkerProps {
  phase: ActivityPhase
  /** Whether Status reads `Live` for this row — see the module docblock. */
  live: boolean
}

export default function AppActivityMarker({
  phase,
  live,
}: AppActivityMarkerProps): React.JSX.Element {
  return (
    <span
      data-testid="app-activity-marker"
      data-phase={phase}
      className={`inline-flex flex-shrink-0 items-center gap-1 text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full whitespace-nowrap ${TONE}`}
    >
      {phase === 'open' ? (
        <CircleDot size={11} aria-hidden="true" data-testid="app-activity-glyph" className="flex-shrink-0" />
      ) : (
        <BusyGlyph size={11} testId="app-activity-glyph" />
      )}
      {copyFor(phase, live)}
    </span>
  )
}
