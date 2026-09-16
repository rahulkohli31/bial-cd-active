/**
 * One project as a LIST row: name · description · created · details updated · status · `⋯`.
 *
 * TWO DATE COLUMNS, BOTH LEFT-ALIGNED AND BOTH ABSOLUTE. One right-aligned relative timestamp
 * answered neither question a citizen brings to this list — when was this made, and when did I
 * last touch it — and could not be scanned down the page, because relative strings share no left
 * edge and no width. "Details updated" keeps its name: the field moves on a rename or a
 * description edit and never on a build or a deploy, so a bare "Updated" would claim otherwise.
 *
 * NO NESTED INTERACTIVE ELEMENTS is the invariant, and this row is the shape that most
 * wants to break it. The whole row opens the project AND it carries a menu, which
 * is a button inside a button the moment anyone reaches for the obvious implementation.
 *
 * So the same construction the card already uses:
 *   - the NAME is a real `<button>`, and its stretched `::after` covers the row
 *   - the MENU is a SIBLING, layered above with `z-10`
 *
 * Neither is a descendant of the other. Making the row itself `<div role="button">`, or
 * wrapping it in a link, is what breaks it. Native buttons carry Enter and Space for free,
 * so there is no key handler here and there should not be one.
 *
 * ONE TOOLTIP, ON THE NAME ONLY. It is conditional: clipped text reveals itself on hover,
 * text that already fits shows nothing, and the gate is the element actually being clipped
 * (`scrollWidth > clientWidth`), measured after layout rather than guessed from length. A
 * tooltip firing on text the reader can already see in full is noise.
 *
 * THE DESCRIPTION'S TOOLTIP AND POINTER CURSOR ARE GONE — it is clipped by CSS with its full
 * text left in the DOM, so there is nothing a hover could reveal that assistive technology
 * does not already read. `ClampedDescription` carries the whole argument, including why
 * JavaScript truncation would have reproduced the defect rather than fixed it.
 *
 * THE DESCRIPTION IS STILL LIFTED ABOVE THE STRETCHED `::after` — the same `z-10` Delete
 * carries — so its `onOpen` is what receives the click rather than the overlay. That is a
 * deliberate keep, not a leftover: see `ClampedDescription`.
 */
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '../ui/tooltip'
import { statusFor, TONE_CLASS } from '../../utils/appStatusLabel'
import { listDate } from '../../utils/projectDates'
import { useClipped } from '../../hooks/useClipped'
import type { Project } from '../../utils/projectApi'
import AppRowMenu from './AppRowMenu'
import type { AppRowMenuProps } from './AppRowMenu'

export interface ProjectRowProps {
  project: Project
  onOpen: () => void
  onSettings: () => void
  onDelete: () => void
  live?: AppRowMenuProps['live']
}

/**
 * The description cell: the WHOLE description, clipped to two lines by CSS.
 *
 * WHAT WENT, AND WHY THE OBVIOUS FIX WAS THE WRONG ONE. This cell used to be one `truncate`d
 * line carrying a `cursor-pointer` and a hover tooltip. Three defects in one small element: it
 * advertised an interaction (the pointer) that had no keyboard route, it hid most of the text
 * behind a HOVER — which a keyboard or touch reader never triggers — and it did all that for an
 * action the row's name already offers.
 *
 * The obvious remedy is to cut the string in JavaScript and show the rest in a tooltip. That
 * reproduces both defects rather than fixing either: a JS-truncated description is truncated in
 * the ACCESSIBLE TREE too, so a screen reader loses the same 58% a sighted reader loses, and the
 * tooltip that "solves" it is the hover-only affordance we are removing. So the complete text
 * stays in the DOM and only the BOX is bounded — `line-clamp-2`, which clips visually and leaves
 * the text intact for anything that is not painting pixels.
 *
 * NO SECOND INTERACTIVE ELEMENT IS ADDED, and the name keeps its own tooltip because a clipped
 * NAME has no other route to its full value — the description now reads two lines of itself,
 * and its full text is available to assistive technology either way.
 *
 * `relative z-10` STAYS, AND ITS REASON CHANGED. It used to lift this cell above the name
 * button's stretched `::after` so the tooltip could be hovered at all. There is no tooltip now,
 * but the lift is what keeps `onOpen` reachable: without it the `::after` takes the click and
 * the row opens anyway, which is fine in a browser and INVISIBLE to jsdom — so the explicit
 * handler is the version this suite can actually hold. It stays a `<p>`: the name is the row's
 * one keyboard-reachable open affordance, and a second one is exactly the nested interactive
 * element this row is built to avoid.
 */
function ClampedDescription({
  text,
  onOpen,
}: {
  text: string | null
  onOpen: () => void
}): React.JSX.Element {
  if (text === null) {
    // Still part of the row's click target — this branch used to lack `onClick` entirely
    // (round-4 finding 8): a project with nothing typed yet had a dead strip across its row,
    // on exactly the newest, emptiest projects most likely to be clicked into.
    return (
      <p onClick={onOpen} className="relative z-10 text-xs text-neutral/70 italic">
        No description yet
      </p>
    )
  }

  return (
    <p onClick={onOpen} className="relative z-10 text-xs text-neutral leading-relaxed line-clamp-2">
      {text}
    </p>
  )
}

/**
 * The name: clamp with an ellipsis, show the full title on hover. The cap is not retroactive,
 * so stored 120-character names are precisely the ones that clip — and the tooltip is the only
 * way to read them.
 *
 * The button keeps its stretched `::after`: this is the row's open affordance, so it must stay
 * the thing covering the row.
 */
function ClampedName({ name, onOpen }: { name: string; onOpen: () => void }): React.JSX.Element {
  const { ref, clipped } = useClipped<HTMLButtonElement>(name)

  // Always mounted, `TooltipContent` alone conditional — see `ClampedDescription` for why:
  // the ref'd button must stay at one tree position for its `ResizeObserver` to keep working
  // across a `clipped` transition in either direction.
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            ref={ref}
            onClick={onOpen}
            className="text-sm font-semibold text-tertiary hover:text-primary transition text-left truncate max-w-full after:absolute after:inset-0 after:content-[''] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 rounded"
          >
            {name}
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

export default function ProjectRow({ project, onOpen, onSettings, onDelete, live }: ProjectRowProps): React.JSX.Element {
  const status = statusFor(project)

  return (
    <div data-testid="project-row" className="relative flex items-center gap-4 px-4 py-3 border-b border-bial-border last:border-0 hover:bg-bial-bg/60 transition">
      <div className="min-w-0 flex-1">
        {/* The open affordance. Its ::after covers the row, so the whole row is the target
            without the row itself being interactive. */}
        <ClampedName name={project.name} onOpen={onOpen} />
        <ClampedDescription text={project.description} onOpen={onOpen} />
      </div>

      {/* FIXED WIDTHS, LEFT-ALIGNED, TABULAR FIGURES — the three together are what make a column
          of dates a ruler down the page rather than a ragged edge. Hidden below `sm`, where the
          row has no width to spare and the name is what a citizen is scanning for. */}
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

      {/* SIBLING of the name button, not a descendant — z-10 lifts it above the stretched
          ::after so it is clickable rather than covered. */}
      <AppRowMenu appName={project.name} onOpen={onOpen} onSettings={onSettings} onDelete={onDelete} live={live} where="row" />
    </div>
  )
}
