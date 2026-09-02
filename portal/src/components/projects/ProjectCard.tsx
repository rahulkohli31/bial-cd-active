/**
 * One project in the `/projects` grid: name, a description snippet (or a muted
 * "No description yet" when the field is null), and a badge marking whether the
 * project has an app yet. The app's lifecycle status (draft / approved / …) is
 * deliberately NOT surfaced here — lifecycle lives on the admin registry, not the
 * citizen project surfaces — so the badge is just "App" vs "No app yet".
 *
 * Purely presentational: the page owns navigation and deletion and injects them as
 * `onOpen` / `onDelete`, so this component is trivial to render in a test with no
 * router.
 */
import { Trash2 } from 'lucide-react'
import type { Project } from '../../utils/projectApi'
import { statusFor, TONE_CLASS } from '../../utils/appStatusLabel'
import { relativeTime } from '../../utils/relativeTime'

/**
 * The status pill, in the SHARED vocabulary (#158 §10).
 *
 * It used to read "App" or "No app yet" — a binary, on the reasoning that lifecycle belonged
 * to the admin registry. The publishing work moved that lifecycle onto the citizen's own
 * surfaces, so the card now says the same words the project page's chip does, via
 * `appStatusLabel`. The list row reads the identical helper: two views of one list must not
 * describe the same project differently.
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
  onDelete: () => void
}

export default function ProjectCard({ project, onOpen, onDelete }: ProjectCardProps): React.JSX.Element {
  const hasDescription = typeof project.description === 'string' && project.description.trim().length > 0
  // F-10: the card is a plain container (no role="button"). The primary open affordance is a
  // real <button> on the title whose stretched ::after covers the whole card, so the card stays
  // clickable — but Delete is a SIBLING button layered above it (z-10), never an interactive
  // descendant of another interactive element. Native buttons carry keyboard activation for free
  // (Enter/Space), so the old onKeyDown handler is gone too.
  return (
    <div className="group relative flex flex-col gap-3 bg-white border border-bial-border rounded-2xl px-5 py-4 hover:border-primary/40 hover:shadow-sm transition font-manrope">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2.5 min-w-0">
          <h3 className="min-w-0 flex-1">
            <button
              type="button"
              onClick={onOpen}
              // The name truncates on the inner <span> so its overflow:hidden clips the text
              // WITHOUT clipping the button's stretched ::after (a sibling of the span).
              className="block w-full text-left text-sm font-bold text-tertiary cursor-pointer rounded-sm after:absolute after:inset-0 after:rounded-2xl focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
            >
              <span className="block truncate">{project.name || 'Untitled project'}</span>
            </button>
          </h3>
        </div>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation()
            onDelete()
          }}
          title="Delete project"
          aria-label={`Delete ${project.name || 'project'}`}
          className="relative z-10 opacity-0 group-hover:opacity-100 focus:opacity-100 text-neutral hover:text-danger p-1 -m-1 transition flex-shrink-0"
        >
          <Trash2 size={15} />
        </button>
      </div>

      {hasDescription ? (
        <p className="text-xs text-neutral leading-relaxed line-clamp-2">{project.description}</p>
      ) : (
        <p className="text-xs text-neutral/70 italic">No description yet</p>
      )}

      {/* Status and the timestamp on one line, as the grid board draws it — and the SAME
          timestamp the list row shows, so the two views cannot describe a project
          differently. `updatedAt` tracks details, not activity, which is why the list
          column is labelled "Details updated"; there is no room for that label here, so
          the card shows the value and the list carries the honest heading. */}
      <div className="mt-auto pt-1 flex items-center justify-between gap-2">
        <AppStatusBadge project={project} />
        <span className="text-[11px] text-neutral whitespace-nowrap">
          {relativeTime(project.updatedAt)}
        </span>
      </div>
    </div>
  )
}
