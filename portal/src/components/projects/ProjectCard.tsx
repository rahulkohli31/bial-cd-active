/**
 * One project in the `/projects` grid: name, a description snippet (or a muted
 * "No description yet" when the field is null), and a badge for the project's one
 * app. `appStatus === null` reads as "No app yet" — never a blank badge, because a
 * project that has not been built yet is a real, common state, not missing data.
 *
 * Purely presentational: the page owns navigation and deletion and injects them as
 * `onOpen` / `onDelete`, so this component is trivial to render in a test with no
 * router.
 */
import { Trash2, Boxes } from 'lucide-react'
import type { AppStatus, Project } from '../../utils/projectApi'

/** The registry status vocabulary, mirrored from `AppRegistryPanel` so the two surfaces read alike. */
const APP_STATUS_BADGE: Record<AppStatus, { label: string; cls: string }> = {
  draft: { label: 'Draft', cls: 'bg-gray-100 text-gray-500' },
  pending: { label: 'Pending review', cls: 'bg-amber-100 text-amber-700' },
  approved: { label: 'Approved', cls: 'bg-green-100 text-green-700' },
  rejected: { label: 'Rejected', cls: 'bg-red-100 text-red-700' },
  disabled: { label: 'Disabled', cls: 'bg-gray-200 text-gray-600' },
}

const NO_APP_BADGE = { label: 'No app yet', cls: 'bg-bial-bg text-neutral' }

export function AppStatusBadge({ appStatus }: { appStatus: AppStatus | null }): React.JSX.Element {
  const badge = appStatus === null ? NO_APP_BADGE : APP_STATUS_BADGE[appStatus]
  return (
    <span className={`text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full ${badge.cls}`}>
      {badge.label}
    </span>
  )
}

export interface ProjectCardProps {
  project: Project
  onOpen: () => void
  onDelete: () => void
}

export default function ProjectCard({ project, onOpen, onDelete }: ProjectCardProps): React.JSX.Element {
  const hasDescription = typeof project.description === 'string' && project.description.trim().length > 0
  return (
    <div
      onClick={onOpen}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen()
        }
      }}
      className="group flex flex-col gap-3 bg-white border border-bial-border rounded-2xl px-5 py-4 cursor-pointer hover:border-primary/40 hover:shadow-sm transition font-manrope"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="w-9 h-9 rounded-xl bg-primary/10 flex items-center justify-center flex-shrink-0">
            <Boxes size={16} className="text-primary" />
          </div>
          <h3 className="text-sm font-bold text-tertiary truncate">{project.name || 'Untitled project'}</h3>
        </div>
        <button
          onClick={(e) => {
            e.stopPropagation()
            onDelete()
          }}
          title="Delete project"
          aria-label={`Delete ${project.name || 'project'}`}
          className="opacity-0 group-hover:opacity-100 focus:opacity-100 text-neutral hover:text-danger p-1 -m-1 transition flex-shrink-0"
        >
          <Trash2 size={15} />
        </button>
      </div>

      {hasDescription ? (
        <p className="text-xs text-neutral leading-relaxed line-clamp-2">{project.description}</p>
      ) : (
        <p className="text-xs text-neutral/70 italic">No description yet</p>
      )}

      <div className="mt-auto pt-1">
        <AppStatusBadge appStatus={project.appStatus} />
      </div>
    </div>
  )
}
