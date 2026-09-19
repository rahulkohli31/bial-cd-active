/**
 * One OWNED application in the `/projects` grid — `AppTile` plus the status chip, the two dates
 * and the `⋯` menu that only an owner gets. The same facts the list row shows, so the two views
 * cannot describe one application differently, and the same presentational half the recipient's
 * grid uses, so the two lists stay one component family.
 *
 * Purely presentational: the page owns navigation and injects it as `onOpen`/`onSettings`, so
 * this renders trivially in a test with no router.
 */
import type { Project } from '../../utils/projectApi'
import { tileDateRange, tileDateTitle } from '../../utils/projectDates'
import AppTile from './AppTile'
import AppStatusBadge from './AppStatusBadge'
import AppRowMenu from './AppRowMenu'

export interface ProjectCardProps {
  project: Project
  onOpen: () => void
  onSettings: () => void
  /** Starting, open, or closing down right now — `undefined` draws no marker. See `ProjectRow`. */
}

export default function ProjectCard({
  project,
  onOpen,
  onSettings,
}: ProjectCardProps): React.JSX.Element {
  const name = project.name || 'Untitled application'

  return (
    <AppTile
      testId="project-card"
      name={name}
      description={project.description}
      onOpen={onOpen}
      marker={
        undefined
      }
      trailing={
        <AppRowMenu
          appName={name}
          onOpen={onOpen}
          onSettings={onSettings}
          where="tile"
        />
      }
      foot={
        // Status and BOTH dates on one line, as the grid board draws it — the same two facts
        // the list row's columns carry. The tile has no room for column headings, so the range
        // is written as created → updated and the list carries the honest labels.
        <div className="flex items-center justify-between gap-2">
          <AppStatusBadge project={project} />
          <span
            className="text-[11px] text-neutral tabular-nums whitespace-nowrap"
            title={tileDateTitle(project.createdAt, project.updatedAt)}
          >
            {tileDateRange(project.createdAt, project.updatedAt)}
          </span>
        </div>
      }
    />
  )
}
