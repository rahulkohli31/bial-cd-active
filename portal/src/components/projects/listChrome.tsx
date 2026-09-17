import { useMemo } from 'react'
import { LayoutGrid, List as ListIcon, ChevronsLeft, ChevronsRight } from 'lucide-react'
import { DENSITY_COLS, TOGGLE_ACTIVE, type Density, type View } from '../../utils/listView'
import { Skeleton } from '../ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '../ui/toggle-group'
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from '../ui/pagination'

/**
 * The furniture every application list wears — the view and density controls, the loading
 * skeleton, and the numbered pager.
 *
 * ONE COPY, BECAUSE TWO LISTS WEAR IT. The owner's list and the shared list are two pages with
 * different rows, different filters and different empty states; what a reader operates them WITH
 * is the same, and a second copy of a pager's disabled-edge rules is how the two come to behave
 * differently on the same press. What differs per page — the rows, the counts caption, the
 * search and the filters — stays on the page.
 */

interface ViewControlsProps {
  view: View
  density: Density
  onView: (next: View) => void
  onDensity: (next: Density) => void
}

/**
 * The density control is GRID-ONLY: a list has one density, and a control that cannot change
 * anything teaches a reader to distrust the row.
 *
 * NO WRAPPER OF ITS OWN — the two toggles are returned bare, because the page owns the controls
 * row they sit in and one of the two puts a Create App button in the same cluster.
 */
export function ViewControls({ view, density, onView, onDensity }: ViewControlsProps): React.JSX.Element {
  return (
    <>
      {view === 'grid' && (
        <ToggleGroup
          type="single"
          value={density}
          onValueChange={(v) => v && onDensity(v as Density)}
          aria-label="Card size"
        >
          {(['S', 'M', 'L'] as const).map((d) => (
            <ToggleGroupItem key={d} value={d} aria-label={`${d} cards`} className={`px-2.5${TOGGLE_ACTIVE}`}>
              {d}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      )}

      <ToggleGroup
        type="single"
        value={view}
        onValueChange={(v) => v && onView(v as View)}
        aria-label="View"
      >
        <ToggleGroupItem value="list" aria-label="List view" className={TOGGLE_ACTIVE.trim()}>
          <ListIcon size={15} />
        </ToggleGroupItem>
        <ToggleGroupItem value="grid" aria-label="Grid view" className={TOGGLE_ACTIVE.trim()}>
          <LayoutGrid size={15} />
        </ToggleGroupItem>
      </ToggleGroup>
    </>
  )
}

/** Shaped like the view you are in — a card skeleton under a list flashes wrong. */
export function ListSkeleton({ view, density }: { view: View; density: Density }): React.JSX.Element {
  if (view === 'list') {
    return (
      <div className="bg-white border border-bial-border rounded-2xl overflow-hidden" aria-busy="true">
        {[0, 1, 2, 3, 4].map((i) => (
          <div key={i} className="px-4 py-3.5 border-b border-bial-border last:border-0">
            <Skeleton className="h-4 w-48 mb-2" />
            <Skeleton className="h-3 w-80" />
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className={`grid gap-4 ${DENSITY_COLS[density]}`} aria-busy="true">
      {[0, 1, 2, 3, 4, 5].map((i) => (
        <div key={i} className="bg-white border border-bial-border rounded-2xl px-5 py-4">
          <Skeleton className="h-4 w-1/2 mb-3" />
          <Skeleton className="h-3 w-3/4 mb-2" />
          <Skeleton className="h-3 w-1/4" />
        </div>
      ))}
    </div>
  )
}

interface ListPagerProps {
  /** What was ASKED for — the edges and the window centre follow the request, so the controls
   *  stay live while a page is in flight. */
  page: number
  /** What the rows on screen ANSWER to. A page that failed must not mark its own number active
   *  over the rows that succeeded. */
  activePage: number
  totalPages: number
  onGo: (page: number) => void
  /** Names the landmark, so a screen reader on a page with two pagers can tell them apart. */
  label: string
}

/**
 * First, previous, a sliding window of five, next, last.
 *
 * A SLIDING WINDOW, NOT THE FIRST FIVE. `Math.min(totalPages, 5)` renders pages 1-5 whatever page
 * you are on, so from page 6 nothing is marked active and the only way deeper is Next, repeatedly
 * — with the page being read not shown at all.
 *
 * IT WRAPS RATHER THAN OVERFLOWING: the number list reached `right: 534px` on a 390px screen with
 * only two pages, which put a horizontal scrollbar on the landing page.
 *
 * The edges are `aria-disabled` and pointer-inert rather than `disabled`, which is this portal's
 * rule throughout — a disabled control throws focus to the document body.
 */
export function ListPager({ page, activePage, totalPages, onGo, label }: ListPagerProps): React.JSX.Element {
  const pageWindow = useMemo(() => {
    const span = Math.min(5, Math.max(totalPages, 1))
    // Centre on the current page, then clamp so the window never runs past either end.
    const first = Math.min(Math.max(page - Math.floor(span / 2), 1), Math.max(totalPages - span + 1, 1))
    return Array.from({ length: span }, (_, i) => first + i)
  }, [page, totalPages])

  const atFirst = page <= 1
  const atLast = page >= totalPages
  const inert = 'pointer-events-none opacity-40'

  return (
    <Pagination className="mx-0 w-auto" aria-label={label}>
      <PaginationContent className="flex-wrap justify-end">
        {/* Jump-to-first/last: at six pages the difference is four clicks or one. */}
        <PaginationItem>
          <PaginationLink
            aria-label="First page"
            aria-disabled={atFirst}
            onClick={() => !atFirst && onGo(1)}
            className={atFirst ? inert : undefined}
          >
            <ChevronsLeft size={15} />
          </PaginationLink>
        </PaginationItem>
        <PaginationItem>
          <PaginationPrevious
            aria-disabled={atFirst}
            onClick={() => !atFirst && onGo(page - 1)}
            className={atFirst ? inert : undefined}
          />
        </PaginationItem>
        {pageWindow.map((n) => (
          <PaginationItem key={n}>
            <PaginationLink isActive={n === activePage} onClick={() => onGo(n)}>
              {n}
            </PaginationLink>
          </PaginationItem>
        ))}
        <PaginationItem>
          <PaginationNext
            aria-disabled={atLast}
            onClick={() => !atLast && onGo(page + 1)}
            className={atLast ? inert : undefined}
          />
        </PaginationItem>
        <PaginationItem>
          <PaginationLink
            aria-label="Last page"
            aria-disabled={atLast}
            onClick={() => !atLast && onGo(totalPages)}
            className={atLast ? inert : undefined}
          >
            <ChevronsRight size={15} />
          </PaginationLink>
        </PaginationItem>
      </PaginationContent>
    </Pagination>
  )
}
