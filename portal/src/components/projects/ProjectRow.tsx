/**
 * One project as a LIST row: name · description · details updated · status, plus Delete.
 *
 * INVARIANT F-10 — NO NESTED INTERACTIVE ELEMENTS, and this row is the shape that most
 * wants to break it. The whole row opens the project AND it carries a Delete button, which
 * is a button inside a button the moment anyone reaches for the obvious implementation.
 *
 * So the same construction the card already uses:
 *   - the NAME is a real `<button>`, and its stretched `::after` covers the row
 *   - DELETE is a SIBLING, layered above with `z-10`
 *
 * Neither is a descendant of the other. Making the row itself `<div role="button">`, or
 * wrapping it in a link, is what breaks it. Native buttons carry Enter and Space for free,
 * so there is no key handler here and there should not be one.
 *
 * THE DESCRIPTION TOOLTIP IS CONDITIONAL, per §10: a clamped description reveals its full
 * text on hover, and a short one shows nothing. A tooltip that fires on text the reader can
 * already see in full is noise, so it is gated on the element actually being clipped
 * (`scrollWidth > clientWidth`), measured after layout rather than guessed from length.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Trash2 } from 'lucide-react'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '../ui/tooltip'
import { statusFor, TONE_CLASS } from '../../utils/appStatusLabel'
import { relativeTime } from '../../utils/relativeTime'
import type { Project } from '../../utils/projectApi'

export interface ProjectRowProps {
  project: Project
  onOpen: () => void
  onDelete: () => void
}

/** The description cell: one line, ellipsis, and a tooltip ONLY when it is really clipped. */
function ClampedDescription({ text }: { text: string | null }): React.JSX.Element {
  const ref = useRef<HTMLParagraphElement>(null)
  const [clipped, setClipped] = useState(false)

  // Measured, not guessed. Character-count heuristics are wrong at every breakpoint, and
  // this has to be re-measured when the column width changes, not only on mount.
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
  }, [measure, text])

  if (text === null) {
    return <p className="text-xs text-neutral/70 italic truncate">No description yet</p>
  }

  const paragraph = (
    <p ref={ref} className="text-xs text-neutral truncate">
      {text}
    </p>
  )
  if (!clipped) return paragraph

  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>{paragraph}</TooltipTrigger>
        <TooltipContent side="bottom" align="start" className="max-w-md">
          {text}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

export default function ProjectRow({ project, onOpen, onDelete }: ProjectRowProps): React.JSX.Element {
  const status = statusFor(project)

  return (
    <div className="relative flex items-center gap-4 px-4 py-3 border-b border-bial-border last:border-0 hover:bg-bial-bg/60 transition">
      <div className="min-w-0 flex-1">
        {/* The open affordance. Its ::after covers the row, so the whole row is the target
            without the row itself being interactive. */}
        <button
          onClick={onOpen}
          className="text-sm font-semibold text-tertiary hover:text-primary transition text-left truncate max-w-full after:absolute after:inset-0 after:content-[''] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 rounded"
        >
          {project.name}
        </button>
        <ClampedDescription text={project.description} />
      </div>

      <p className="hidden sm:block text-xs text-neutral whitespace-nowrap w-28 text-right">
        {relativeTime(project.updatedAt)}
      </p>

      <span
        className={`text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full whitespace-nowrap ${TONE_CLASS[status.tone]}`}
      >
        {status.label}
      </span>

      {/* SIBLING of the name button, not a descendant — z-10 lifts it above the stretched
          ::after so it is clickable rather than covered. */}
      <button
        onClick={onDelete}
        aria-label={`Delete ${project.name}`}
        className="relative z-10 p-1.5 text-neutral hover:text-danger rounded-lg hover:bg-red-50 transition"
      >
        <Trash2 size={15} />
      </button>
    </div>
  )
}
