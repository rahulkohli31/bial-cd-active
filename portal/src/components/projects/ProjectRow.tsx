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
 * BOTH TOOLTIPS ARE CONDITIONAL, per §10 (description) and §14 (name): clipped text reveals
 * itself on hover, and text that already fits shows nothing. A tooltip firing on text the
 * reader can see in full is noise, so each is gated on the element actually being clipped
 * (`scrollWidth > clientWidth`), measured after layout rather than guessed from length.
 *
 * THE DESCRIPTION HAS TO BE LIFTED ABOVE THE STRETCHED `::after` to be hoverable at all —
 * see `ClampedDescription`. That is the same reason Delete carries `z-10`, and it is why the
 * description also wires `onOpen` back: lifting it out of the overlay takes it out of the
 * row's click target unless you put it back.
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

/**
 * Is this element's text ACTUALLY clipped? Measured after layout, never guessed from length —
 * character heuristics are wrong at every breakpoint — and re-measured on resize, because a
 * column that widens un-clips text that was clipped a moment ago.
 */
function useClipped<T extends HTMLElement>(text: string | null) {
  const ref = useRef<T>(null)
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
  }, [measure, text])

  return { ref, clipped }
}

/**
 * The description cell: one line, ellipsis, and a tooltip ONLY when it is really clipped.
 *
 * `relative z-10` IS LOAD-BEARING AND WAS MISSING. The name button's stretched `::after` is
 * positioned against the row, so it painted over this static sibling and took every pointer
 * event: hovering a clipped description hit the BUTTON, and the tooltip — correctly built and
 * correctly measured — could never open. jsdom does no hit testing, so no unit test in the
 * suite could see it.
 *
 * Lifting it costs the row's click target here, so `onOpen` is wired back explicitly. It stays
 * a `<p>` rather than becoming a button: the name is already the row's one keyboard-reachable
 * open affordance, and a second interactive element covering the same action is the shape
 * F-10 exists to prevent.
 */
function ClampedDescription({
  text,
  onOpen,
}: {
  text: string | null
  onOpen: () => void
}): React.JSX.Element {
  const { ref, clipped } = useClipped<HTMLParagraphElement>(text)

  if (text === null) {
    return <p className="relative z-10 text-xs text-neutral/70 italic truncate">No description yet</p>
  }

  const paragraph = (
    <p ref={ref} onClick={onOpen} className="relative z-10 text-xs text-neutral truncate cursor-pointer">
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

/**
 * The name, with the SAME treatment §14 asks for: clamp with an ellipsis, show the full title
 * on hover. The cap is not retroactive, so stored 120-character names are precisely the ones
 * that clip — and the tooltip is the only way to read them.
 *
 * The button keeps its stretched `::after`: this is the row's open affordance, so it must stay
 * the thing covering the row.
 */
function ClampedName({ name, onOpen }: { name: string; onOpen: () => void }): React.JSX.Element {
  const { ref, clipped } = useClipped<HTMLButtonElement>(name)

  const button = (
    <button
      ref={ref}
      onClick={onOpen}
      className="text-sm font-semibold text-tertiary hover:text-primary transition text-left truncate max-w-full after:absolute after:inset-0 after:content-[''] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 rounded"
    >
      {name}
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

export default function ProjectRow({ project, onOpen, onDelete }: ProjectRowProps): React.JSX.Element {
  const status = statusFor(project)

  return (
    <div className="relative flex items-center gap-4 px-4 py-3 border-b border-bial-border last:border-0 hover:bg-bial-bg/60 transition">
      <div className="min-w-0 flex-1">
        {/* The open affordance. Its ::after covers the row, so the whole row is the target
            without the row itself being interactive. */}
        <ClampedName name={project.name} onOpen={onOpen} />
        <ClampedDescription text={project.description} onOpen={onOpen} />
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
