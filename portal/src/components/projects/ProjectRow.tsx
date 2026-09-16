/**
 * One OWNED application as a list row — `AppListRow` plus the columns and the `⋯` menu that
 * only an owner gets. The presentational half is shared with the recipient's list, so the two
 * cannot come to look like different products; what an owner may DO arrives here, through the
 * trailing slot, and nowhere inside the shared part.
 *
 * TWO DATE COLUMNS, BOTH LEFT-ALIGNED AND BOTH ABSOLUTE. One right-aligned relative timestamp
 * answered neither question a citizen brings to this list — when was this made, and when did I
 * last touch it — and could not be scanned down the page, because relative strings share no left
 * edge and no width. "Details updated" keeps its name: the field moves on a rename or a
 * description edit and never on a build or a deploy, so a bare "Updated" would claim otherwise.
 */
import { statusFor, TONE_CLASS } from '../../utils/appStatusLabel'
import { listDate } from '../../utils/projectDates'
import type { Project } from '../../utils/projectApi'
import AppListRow from './AppListRow'
import AppRowMenu from './AppRowMenu'
import type { AppRowMenuProps } from './AppRowMenu'

export interface ProjectRowProps {
  project: Project
  onOpen: () => void
  onSettings: () => void
  onDelete: () => void
  live?: AppRowMenuProps['live']
}

export default function ProjectRow({ project, onOpen, onSettings, onDelete, live }: ProjectRowProps): React.JSX.Element {
  const status = statusFor(project)

  return (
    <AppListRow
      testId="project-row"
      name={project.name}
      description={project.description}
      onOpen={onOpen}
      columns={
        <>
          {/* FIXED WIDTHS, LEFT-ALIGNED, TABULAR FIGURES — the three together are what make a
              column of dates a ruler down the page rather than a ragged edge. Hidden below
              `sm`, where the row has no width to spare and the name is what a citizen is
              scanning for. */}
          <p className="hidden sm:block w-28 flex-shrink-0 text-xs text-neutral tabular-nums whitespace-nowrap">
            {listDate(project.createdAt)}
          </p>
          <p className="hidden sm:block w-28 flex-shrink-0 text-xs text-neutral tabular-nums whitespace-nowrap">
            {listDate(project.updatedAt)}
          </p>
          <span
            className={`w-[104px] flex-shrink-0 text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full whitespace-nowrap text-center ${TONE_CLASS[status.tone]}`}
          >
            {status.label}
          </span>
        </>
      }
      trailing={
        <AppRowMenu
          appName={project.name}
          onOpen={onOpen}
          onSettings={onSettings}
          onDelete={onDelete}
          live={live}
          where="row"
        />
      }
    />
  )
}
