/**
 * One row of "Shared with me" — a project the viewer does not own. Structurally close to
 * `ProjectCard`, but with no delete affordance (a recipient cannot delete someone else's
 * project) and "Shared by {displayName}" in place of the status pill, following the exact
 * "Built by {displayName}" attribution `MarketplacePage.tsx`'s `EntryCard` already
 * established for showing one citizen's identity to another — display name only, never email.
 */
import { relativeTime } from '../../utils/relativeTime'
import type { SharedProject } from '../../utils/sharingApi'
import { Card } from '../ui/card'

export interface SharedProjectCardProps {
  project: SharedProject
  onOpen: () => void
}

export default function SharedProjectCard({ project, onOpen }: SharedProjectCardProps): React.JSX.Element {
  const hasDescription =
    typeof project.projectDescription === 'string' && project.projectDescription.trim().length > 0
  const sharedBy = project.sharedByDisplayName ?? 'a colleague'
  return (
    <Card
      data-testid="shared-project-card"
      className="group relative flex flex-col gap-3 rounded-2xl px-5 py-4 hover:border-primary/40 hover:shadow-sm transition font-manrope"
    >
      <h3 className="min-w-0">
        <button
          type="button"
          onClick={onOpen}
          className="block w-full truncate text-left text-sm font-bold text-tertiary cursor-pointer rounded-sm after:absolute after:inset-0 after:rounded-2xl focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
        >
          {project.projectName || 'Untitled project'}
        </button>
      </h3>

      {hasDescription ? (
        <p className="text-xs text-neutral leading-relaxed line-clamp-2">{project.projectDescription}</p>
      ) : (
        <p className="text-xs text-neutral/70 italic">No description yet</p>
      )}

      <div className="mt-auto pt-1 flex items-center justify-between gap-2">
        <span className="text-[11px] text-neutral truncate">Shared by {sharedBy}</span>
        <span className="text-[11px] text-neutral whitespace-nowrap">{relativeTime(project.sharedAt)}</span>
      </div>
    </Card>
  )
}
