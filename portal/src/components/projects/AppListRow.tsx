/**
 * The presentational half of a list row: name, description, the fixed-width cells a surface
 * puts between them, and one trailing control. The owner's list and the shared list both
 * compose it, which is what keeps the two visibly one component family.
 *
 * `trailing` IS REQUIRED, HAS NO DEFAULT, AND NOTHING IN HERE ASKS WHO IS LOOKING. Reuse across
 * a permission boundary is how an owner-only action leaks by omission — the owner passes a `⋯`
 * menu, a recipient passes Open — so the guarantee is structural rather than a test's: a future
 * edit cannot grant a recipient the owner's menu by forgetting something, because there is
 * nothing here to forget.
 *
 * NO NESTED INTERACTIVE ELEMENTS is the invariant, and this row is the shape that most wants to
 * break it. The whole row opens the application AND it carries a trailing control, which is a
 * button inside a button the moment anyone reaches for the obvious implementation.
 *
 * So:
 *   - the NAME is a real `<button>`, and its stretched `::after` covers the row
 *   - the TRAILING control is a SIBLING, which the caller layers above with `z-10`
 *
 * Neither is a descendant of the other. Making the row itself `<div role="button">`, or wrapping
 * it in a link, is what breaks it. Native buttons carry Enter and Space for free, so there is no
 * key handler here and there should not be one.
 *
 * ONE TOOLTIP, ON THE NAME ONLY. It is conditional: clipped text reveals itself on hover, text
 * that already fits shows nothing, and the gate is the element actually being clipped
 * (`scrollWidth > clientWidth`), measured after layout rather than guessed from length. A
 * tooltip firing on text the reader can already see in full is noise.
 */
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '../ui/tooltip'
import { useClipped } from '../../hooks/useClipped'

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
    // Still part of the row's click target — this branch used to lack `onClick` entirely: an
    // application with nothing typed yet had a dead strip across its row, on exactly the
    // newest, emptiest ones most likely to be clicked into.
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

export interface AppListRowProps {
  name: string
  description: string | null
  onOpen: () => void
  /** The fixed-width cells between the name and the trailing control. The two lists carry
   *  different ones — dates and a status chip against a sharer and two dates — and four
   *  differing columns do not earn a generic table layer. */
  columns: React.ReactNode
  /** REQUIRED, and with no owner-aware branch behind it: see the module docblock. */
  trailing: React.ReactNode
  /** One DOM handle per surface, so a suite can tell an owner's row from a recipient's. */
  testId: string
}

export default function AppListRow({
  name,
  description,
  onOpen,
  columns,
  trailing,
  testId,
}: AppListRowProps): React.JSX.Element {
  return (
    <div
      data-testid={testId}
      className="relative flex items-center gap-4 px-4 py-3 border-b border-bial-border last:border-0 hover:bg-bial-bg/60 transition"
    >
      <div className="min-w-0 flex-1">
        {/* The open affordance. Its ::after covers the row, so the whole row is the target
            without the row itself being interactive. */}
        <ClampedName name={name} onOpen={onOpen} />
        <ClampedDescription text={description} onOpen={onOpen} />
      </div>

      {columns}

      {/* SIBLING of the name button, not a descendant. */}
      {trailing}
    </div>
  )
}
