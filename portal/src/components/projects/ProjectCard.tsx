/**
 * One project in the `/projects` grid: name, a description snippet (or a muted
 * "No description yet" when the field is null), the status in the shared vocabulary, and
 * when the details were last updated — the same two facts the list row shows, so the two
 * views cannot describe one project differently.
 *
 * The status is NOT the "App / No app yet" binary this file used to draw. That reasoning
 * (lifecycle belongs to the admin registry) stopped being true when publishing moved onto
 * the citizen's own surfaces; `AppStatusBadge` below carries the current argument.
 *
 * A NAME TOO LONG FOR ITS TILE gets an ellipsis AND a tooltip (§14). The 8-word cap is not
 * retroactive, so stored 120-character names are exactly the ones that clip, and the tooltip
 * is the only way to read them. Gated on the span being MEASURED as clipped, like the list's.
 *
 * Purely presentational: the page owns navigation and deletion and injects them as
 * `onOpen` / `onDelete`, so this component is trivial to render in a test with no
 * router.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Trash2 } from 'lucide-react'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '../ui/tooltip'
import type { Project } from '../../utils/projectApi'
import { statusFor, TONE_CLASS } from '../../utils/appStatusLabel'
import { relativeTime } from '../../utils/relativeTime'
import { Card } from '../ui/card'

/**
 * The tile's name: clipped with an ellipsis, and revealed in full on hover ONLY when it is
 * really clipped (§14). Measured on the inner span rather than the button, because that is
 * the element `truncate` acts on — the button is as wide as the tile either way.
 *
 * The span, not the button, also keeps `overflow:hidden` off the button so its stretched
 * `::after` still covers the tile.
 */
function NameButton({ name, onOpen }: { name: string; onOpen: () => void }): React.JSX.Element {
  const ref = useRef<HTMLSpanElement>(null)
  const [clipped, setClipped] = useState(false)

  const measure = useCallback(() => {
    const el = ref.current
    if (el) setClipped(el.scrollWidth > el.clientWidth)
  }, [])

  useEffect(() => {
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    if (ref.current) observer.observe(ref.current)
    return () => observer.disconnect()
  }, [measure, name])

  const button = (
    <button
      type="button"
      onClick={onOpen}
      // The name truncates on the inner <span> so its overflow:hidden clips the text
      // WITHOUT clipping the button's stretched ::after (a sibling of the span).
      className="block w-full text-left text-sm font-bold text-tertiary cursor-pointer rounded-sm after:absolute after:inset-0 after:rounded-2xl focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
    >
      <span ref={ref} className="block truncate">
        {name}
      </span>
    </button>
  )
  if (!clipped) return button

  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>{button}</TooltipTrigger>
        <TooltipContent side="bottom" align="start" className="max-w-md">
          {name}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

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
    // shadcn `Card` is the tile, per §12 — the surface (border, radius, background) comes
    // from the primitive so a grid tile here and a card anywhere else cannot drift apart.
    // The layout and hover behaviour stay local, because they belong to THIS tile.
    <Card className="group relative flex flex-col gap-3 rounded-2xl px-5 py-4 hover:border-primary/40 hover:shadow-sm transition font-manrope">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2.5 min-w-0">
          <h3 className="min-w-0 flex-1">
            <NameButton name={project.name || 'Untitled project'} onOpen={onOpen} />
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
    </Card>
  )
}
