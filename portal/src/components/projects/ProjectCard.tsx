/**
 * One OWNED application in the `/projects` grid — `AppTile` plus the status chip, the two dates
 * and the `⋯` menu that only an owner gets. The same facts the list row shows, so the two views
 * cannot describe one application differently, and the same presentational half the recipient's
 * grid uses, so the two lists stay one component family.
 *
 * Purely presentational: the page owns navigation and deletion and injects them as
 * `onOpen`/`onDelete`/`onSettings`, so this renders trivially in a test with no router.
 */
import type { Project } from '../../utils/projectApi'
import { statusFor, TONE_CLASS } from '../../utils/appStatusLabel'
import { tileDateRange } from '../../utils/projectDates'
import AppTile from './AppTile'
import AppRowMenu from './AppRowMenu'
import type { AppRowMenuProps } from './AppRowMenu'

/**
 * The status pill, in the SHARED vocabulary.
 *
 * The card says the same words the project page's chip does, via `appStatusLabel`. The list
 * row reads the identical helper: two views of one list must not describe the same project
 * differently.
 */
export function AppStatusBadge({
  project,
}: {
  project: Pick<Project, 'appStatus' | 'isServing'>
}): React.JSX.Element {
  const status = statusFor(project)
  return (
    <span
      className={`text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full whitespace-nowrap ${TONE_CLASS[status.tone]}`}
    >
      {status.label}
    </span>
  )
}

export interface ProjectCardProps {
  project: Project
  onOpen: () => void
  onSettings: () => void
  onDelete: () => void
  live?: AppRowMenuProps['live']
}

export default function ProjectCard({ project, onOpen, onSettings, onDelete, live }: ProjectCardProps): React.JSX.Element {
  const name = project.name || 'Untitled project'

  return (
    <AppTile
      testId="project-card"
      name={name}
      description={project.description}
      onOpen={onOpen}
      trailing={
        <AppRowMenu
          appName={name}
          onOpen={onOpen}
          onSettings={onSettings}
          onDelete={onDelete}
          live={live}
          where="tile"
        />
      }
      foot={
        // Status and BOTH dates on one line, as the grid board draws it — the same two facts
        // the list row's columns carry. The tile has no room for column headings, so the range
        // is written as created → updated and the list carries the honest labels.
        <div className="flex items-center justify-between gap-2">
          <AppStatusBadge project={project} />
          <span className="text-[11px] text-neutral tabular-nums whitespace-nowrap">
            {tileDateRange(project.createdAt, project.updatedAt)}
          </span>
        </div>
      }
    />
  )
}
