/**
 * The presentational half of a grid tile: name, description, one trailing control beside the
 * name, and whatever facts the surface puts in the foot. The owner's grid and the shared grid
 * both compose it, so the two cannot drift into different tiles.
 *
 * `trailing` IS REQUIRED, HAS NO DEFAULT, AND NOTHING IN HERE ASKS WHO IS LOOKING — the same
 * permission-boundary guarantee `AppListRow` carries, and for the same reason: the owner passes
 * a `⋯` menu, a recipient passes Open, and a future edit cannot hand a recipient the menu by
 * forgetting a branch that does not exist.
 *
 * The tile is a plain container (no `role="button"`). The open affordance is a real `<button>`
 * on the title whose stretched `::after` covers the whole tile, and the trailing control is a
 * SIBLING layered above it — never an interactive descendant of another interactive element.
 * Native buttons carry Enter and Space for free, so no key handler belongs here.
 */
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '../ui/tooltip'
import { useClipped } from '../../hooks/useClipped'
import { Card } from '../ui/card'

/**
 * The tile's name: clipped with an ellipsis, and revealed in full on hover ONLY when it is
 * really clipped. Measured on the inner span rather than the button, because that is the
 * element `truncate` acts on — the button is as wide as the tile either way.
 *
 * The span, not the button, also keeps `overflow:hidden` off the button so its stretched
 * `::after` still covers the tile.
 */
function NameButton({ name, onOpen }: { name: string; onOpen: () => void }): React.JSX.Element {
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

export interface AppTileProps {
  name: string
  description: string | null
  onOpen: () => void
  /** REQUIRED, and with no owner-aware branch behind it: see the module docblock. */
  trailing: React.ReactNode
  /** The foot: the facts this surface shows under the description, laid out by the caller
   *  because the two lists carry different ones. */
  foot: React.ReactNode
  /** One DOM handle per surface, so a suite can tell an owner's tile from a recipient's. */
  testId: string
}

export default function AppTile({
  name,
  description,
  onOpen,
  trailing,
  foot,
  testId,
}: AppTileProps): React.JSX.Element {
  const hasDescription = typeof description === 'string' && description.trim().length > 0

  return (
    // shadcn `Card` is the tile — the surface (border, radius, background) comes from the
    // primitive so a grid tile here and a card anywhere else cannot drift apart. The layout and
    // hover behaviour stay local, because they belong to THIS tile.
    <Card
      data-testid={testId}
      className="group relative flex flex-col gap-3 rounded-2xl px-5 py-4 hover:border-primary/40 hover:shadow-sm transition font-manrope"
    >
      <div className="flex items-start justify-between gap-3">
        <h3 className="min-w-0 flex-1">
          <NameButton name={name} onOpen={onOpen} />
        </h3>
        {/* ALWAYS VISIBLE, never `opacity-0 group-hover:opacity-100` as the bare delete control
            it replaced was: a hover-only control has no keyboard route and no touch route. */}
        {trailing}
      </div>

      {hasDescription ? (
        <p className="text-xs text-neutral leading-relaxed line-clamp-2">{description}</p>
      ) : (
        <p className="text-xs text-neutral/70 italic">No description yet</p>
      )}

      <div className="mt-auto pt-1">{foot}</div>
    </Card>
  )
}
