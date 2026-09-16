/**
 * One project in the `/projects` grid: name, description (or muted "No description yet"),
 * status in the shared vocabulary, and last-updated — the same facts the list row shows, so
 * the two views cannot describe one project differently.
 *
 * A name too long for its tile gets an ellipsis AND a tooltip, gated on the span being
 * MEASURED as clipped (like the list's) — the 8-word cap is not retroactive, so stored
 * 120-character names are exactly the ones that clip.
 *
 * Purely presentational: the page owns navigation/deletion and injects them as
 * `onOpen`/`onDelete`, so this renders trivially in a test with no router.
 */
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '../ui/tooltip'
import type { Project } from '../../utils/projectApi'
import { statusFor, TONE_CLASS } from '../../utils/appStatusLabel'
import { tileDateRange } from '../../utils/projectDates'
import { useClipped } from '../../hooks/useClipped'
import { Card } from '../ui/card'
import AppRowMenu from './AppRowMenu'

/**
 * The tile's name: clipped with an ellipsis, and revealed in full on hover ONLY when it is
 * really clipped. Measured on the inner span rather than the button, because that is
 * the element `truncate` acts on — the button is as wide as the tile either way.
 *
 * The span, not the button, also keeps `overflow:hidden` off the button so its stretched
 * `::after` still covers the tile.
 */
function NameButton({ name, onOpen }: { name: string; onOpen: () => void }): React.JSX.Element {
  // Shared with `ProjectRow`.
  // Measured on the inner span rather than the button, because that is the element
  // `truncate` acts on — the button is as wide as the tile either way.
  const { ref, clipped } = useClipped<HTMLSpanElement>(name)

  // ALWAYS MOUNTED; only `TooltipContent` is conditional. Swapping the tooltip subtree in
  // and out by branch put the ref'd `<span>` at a different tree position depending on
  // `clipped`, which React treats as a remount — the `ResizeObserver` never rebinds to the
  // new node, so a tile widening past its clip point kept a stale tooltip armed forever.
  // One stable position keeps one observer working for the tile's lifetime.
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
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
        </TooltipTrigger>
        {clipped && (
          <TooltipContent side="bottom" align="start" className="max-w-md">
            {name}
          </TooltipContent>
        )}
      </Tooltip>
    </TooltipProvider>
  )
}

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
}

export default function ProjectCard({ project, onOpen, onSettings, onDelete }: ProjectCardProps): React.JSX.Element {
  const hasDescription = typeof project.description === 'string' && project.description.trim().length > 0
  // The card is a plain container (no role="button"). The primary open affordance is a
  // real <button> on the title whose stretched ::after covers the whole card, so the card stays
  // clickable — but Delete is a SIBLING button layered above it (z-10), never an interactive
  // descendant of another interactive element. Native buttons carry keyboard activation for free
  // (Enter/Space), so no onKeyDown handler is needed.
  return (
    // shadcn `Card` is the tile — the surface (border, radius, background) comes
    // from the primitive so a grid tile here and a card anywhere else cannot drift apart.
    // The layout and hover behaviour stay local, because they belong to THIS tile.
    <Card data-testid="project-card" className="group relative flex flex-col gap-3 rounded-2xl px-5 py-4 hover:border-primary/40 hover:shadow-sm transition font-manrope">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2.5 min-w-0">
          <h3 className="min-w-0 flex-1">
            <NameButton name={project.name || 'Untitled project'} onOpen={onOpen} />
          </h3>
        </div>
        {/* ALWAYS VISIBLE, not `opacity-0 group-hover:opacity-100` as the bare delete control
            was: a hover-only control has no keyboard route and no touch route at all. */}
        <AppRowMenu
          appName={project.name || 'Untitled project'}
          onOpen={onOpen}
          onSettings={onSettings}
          onDelete={onDelete}
          where="tile"
        />
      </div>

      {hasDescription ? (
        <p className="text-xs text-neutral leading-relaxed line-clamp-2">{project.description}</p>
      ) : (
        <p className="text-xs text-neutral/70 italic">No description yet</p>
      )}

      {/* Status and BOTH dates on one line, as the grid board draws it — the same two facts
          the list row's columns carry, so the two views cannot describe a project differently.
          The tile has no room for column headings, so the range is written as created → updated
          and the list carries the honest labels. */}
      <div className="mt-auto pt-1 flex items-center justify-between gap-2">
        <AppStatusBadge project={project} />
        <span className="text-[11px] text-neutral tabular-nums whitespace-nowrap">
          {tileDateRange(project.createdAt, project.updatedAt)}
        </span>
      </div>
    </Card>
  )
}
