import type { Project } from '../../utils/projectApi'
import { statusFor, TONE_CLASS } from '../../utils/appStatusLabel'

/**
 * The status pill, in the SHARED vocabulary and the shared shape.
 *
 * The list row, the grid tile and the project page's chip all say the same words, via
 * `appStatusLabel` — and they are now drawn by the same element too, because two views of one
 * list must not describe the same application differently in EITHER half. `className` is the
 * caller's layout only: the row sits it in a fixed-width column, the tile lets it size itself.
 */
export default function AppStatusBadge({
  project,
  className = '',
}: {
  project: Pick<Project, 'appStatus' | 'isServing'>
  className?: string
}): React.JSX.Element {
  const status = statusFor(project)
  return (
    <span
      className={`text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full whitespace-nowrap ${className} ${TONE_CLASS[status.tone]}`}
    >
      {status.label}
    </span>
  )
}
