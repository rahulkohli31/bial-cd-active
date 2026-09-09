/**
 * The date pill on a connector row, and the one rule that decides what it says.
 *
 * IT IS THE POPOVER'S TRIGGER, not a label beside one — `PopoverTrigger asChild` over a shadcn
 * `Button` (R14), so the pill IS the control. It must be rendered inside a `<Popover>`; the row
 * owns that root, because the row is what closes it after a successful write.
 *
 * IT RENDERS THE WINDOW'S OWN SHAPE — `Last 7 days` for a preset, `1 – 30 Sep` for a fixed range.
 * The two boards disagree here: `DialogProjects` draws one project of each kind, and `DateRange`
 * ticks `Last 30 days` over a project whose chip reads dates. The owner settled it on the shape
 * rule (D7), which makes `DialogProjects` internally consistent and reads `DateRange`'s tick as
 * the board showing its controls rather than that project's stored state.
 *
 * IT RENDERS THE RESOLVED FIELDS ONLY. A project storing `1 – 30 Sep`, read in October, shows the
 * CLAMPED range, because that is what the resolver returned (R13). `window.stored` is not read
 * here at all — it crosses the wire solely so the popover knows which option to tick.
 *
 * THE ACCESSIBLE NAME NAMES ITS SUBJECT, built by the row and passed in whole. `1 – 30 Sep` on
 * its own announces nothing useful in a list of five projects.
 */
import { Calendar as CalendarIcon } from 'lucide-react'
import type { ConnectorWindow } from '../../utils/connectorApi'
import { Button } from '../ui/button'
import { PopoverTrigger } from '../ui/popover'
import { MONTHS } from './ConnectorRow'

/** One day of milliseconds, for the two places a span is counted. */
const DAY_MS = 86_400_000

/**
 * A `YYYY-MM-DD` calendar day as a LOCAL midnight `Date`, built field by field.
 *
 * NEVER `new Date('2026-09-01')`. That parses as UTC midnight, which is 31 August in every
 * timezone west of Greenwich — the grid would grey the wrong column and the chip would name the
 * wrong day. A window's bounds are calendar days, not instants, and this is the whole of how they
 * become `Date` objects anywhere in this feature.
 */
export function parseCalendarDay(day: string): Date {
  const [year, month, date] = day.split('-').map(Number)
  return new Date(year, month - 1, date)
}

/**
 * A local `Date` back to the `YYYY-MM-DD` the API takes.
 *
 * NEVER `toISOString().slice(0, 10)`, which shifts to UTC and can send yesterday: a date picked
 * at 09:00 in Bangalore is 03:30 UTC the same day, but one picked at 04:00 is the day before.
 */
export function toCalendarDay(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

/** Inclusive day count between two calendar days — `1 Sep` to `30 Sep` is 30, not 29. */
export function spanDays(from: Date, to: Date): number {
  return Math.round((to.getTime() - from.getTime()) / DAY_MS) + 1
}

/**
 * The board's `1 – 30 Sep`, widened only as far as the dates force it.
 *
 * The board never draws a range crossing a month or a year, because the one it draws does not.
 * A thirty-day window plainly can: `28 Aug – 3 Sep` needs both months, and `20 Dec 2026 –
 * 5 Jan 2027` needs both years or it reads as nonsense. So the month repeats only when it
 * changes, and the year appears only when it changes — the board's form is what a same-month
 * range produces, not a special case bolted beside two others.
 */
export function formatDayRange(from: Date, to: Date): string {
  const sameYear = from.getFullYear() === to.getFullYear()
  const sameMonth = sameYear && from.getMonth() === to.getMonth()
  const part = (date: Date, withMonth: boolean, withYear: boolean): string =>
    [
      String(date.getDate()),
      withMonth ? MONTHS[date.getMonth()] : null,
      withYear ? String(date.getFullYear()) : null,
    ]
      .filter((piece): piece is string => piece !== null)
      .join(' ')
  return `${part(from, !sameMonth, !sameYear)} – ${part(to, true, !sameYear)}`
}

/** `Last 7 days` for a preset, `1 – 30 Sep` for a fixed range — the shape rule, in one place. */
export function formatWindowLabel(window: ConnectorWindow): string {
  if (window.kind === 'relative') {
    return `Last ${window.days} ${window.days === 1 ? 'day' : 'days'}`
  }
  return formatDayRange(parseCalendarDay(window.start), parseCalendarDay(window.end))
}

export interface WindowChipProps {
  /** The RESOLVED window. Its `stored` half is deliberately not read here. */
  window: ConnectorWindow
  /**
   * The whole accessible name, composed by the row so both of its mounts read correctly —
   * `Days ORBIT reads in Terminal 2 Departures: 1 – 30 Sep` in the drill-down, and the same
   * sentence about `this project` in the rail.
   */
  accessibleName: string
}

export default function WindowChip({ window, accessibleName }: WindowChipProps): React.JSX.Element {
  return (
    <PopoverTrigger asChild>
      <Button
        type="button"
        variant="outline"
        aria-label={accessibleName}
        // `[&_svg]:size-[11px]` OVERRIDES `button.tsx`'s own `[&_svg]:size-4`, which is a CSS
        // descendant rule and therefore beats lucide's `size` prop: without this the board's
        // 11px calendar glyph would silently render at 16px. Same trap `dropdown-menu.tsx`
        // records for `[&>svg]:size-4`.
        className="h-auto flex-shrink-0 gap-1.5 whitespace-nowrap rounded-lg border-primary-100 bg-primary-50 px-[9px] py-[5px] text-[10.5px] font-bold text-primary-dark shadow-none hover:bg-primary-100 hover:text-primary-dark [&_svg]:size-[11px]"
      >
        <CalendarIcon aria-hidden />
        {formatWindowLabel(window)}
      </Button>
    </PopoverTrigger>
  )
}
