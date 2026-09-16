import { MoreHorizontal, ExternalLink, Trash2 } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from '../ui/dropdown-menu'

/**
 * The `⋯` menu an application carries in the list and on its tile — ONE component, so the two
 * views cannot come to offer different actions on the same application.
 *
 * IT IS A SIBLING OF THE NAME BUTTON, NEVER A DESCENDANT. The row's open affordance is a real
 * `<button>` whose stretched `::after` covers the whole row; a menu trigger inside it would be a
 * button inside a button. The caller lifts this above that overlay with `z-10`, exactly as the
 * bare delete control it replaces was lifted.
 *
 * ALWAYS VISIBLE, NEVER HOVER-ONLY. The tile's delete control used to appear on hover, which is
 * an affordance a keyboard has no route to and a touch screen never triggers at all.
 *
 * WHAT IS NOT HERE YET. Restart and Take down arrive with the endpoints they call, and Settings
 * arrives with the dialog it opens; a menu item that does nothing is a defect rather than a
 * placeholder, which is why each lands with its own unit rather than being stubbed now.
 */

export interface AppRowMenuProps {
  /** Named in the trigger's accessible label, so a page of menus is not a page of "More". */
  appName: string
  onOpen: () => void
  onDelete: () => void
  /** Distinguishes the row's menu from the tile's in the DOM — one testid each. */
  where: 'row' | 'tile'
}

export default function AppRowMenu({ appName, onOpen, onDelete, where }: AppRowMenuProps) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          data-testid={`app-menu-${where}`}
          aria-label={`More actions for ${appName}`}
          className="relative z-10 flex h-[26px] w-[26px] flex-shrink-0 items-center justify-center rounded-lg text-neutral transition hover:bg-surface-muted hover:text-primary-900 data-[state=open]:bg-surface-muted data-[state=open]:text-primary-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
        >
          <MoreHorizontal size={16} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="min-w-[200px] rounded-md border-bial-border bg-white p-1 shadow-lg"
      >
        <DropdownMenuItem
          onSelect={onOpen}
          className="gap-2 rounded-sm px-2 py-1.5 text-sm text-primary-900 focus:bg-surface-muted"
        >
          <ExternalLink size={15} />
          Open
        </DropdownMenuItem>
        {/* A hairline, not the vendored `DropdownMenuSeparator`: the primitive was trimmed to
            the four parts this portal uses, and re-vendoring a fifth for one rule is a wider
            change than the rule is worth. The board draws the same 1px line. */}
        <div role="separator" className="-mx-1 my-1 h-px bg-bial-border" />
        <DropdownMenuItem
          onSelect={onDelete}
          className="gap-2 rounded-sm px-2 py-1.5 text-sm text-danger focus:bg-red-50 focus:text-danger"
        >
          <Trash2 size={15} />
          Delete
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
