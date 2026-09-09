import * as React from "react"
import { ChevronLeft, ChevronRight } from "lucide-react"
import { DayPicker } from "react-day-picker"

import { cn } from "@/lib/utils"

/**
 * shadcn/ui `calendar` — a thin wrapper over `react-day-picker`, hand-authored for THIS build.
 *
 * WHY NOT THE REGISTRY BLOCK. It is written for Tailwind v4 and this portal is 3.4.17, so a
 * paste would ship classes the build does not produce; they render as NOTHING, with no error and
 * no failing test, because jsdom computes no styles. It also styles a stock calendar, and the
 * boards draw a 302px-wide one with 27px cells inside a date popover. Every class below was
 * checked against a real `npx tailwindcss` build of `tailwind.config.js`.
 *
 * NOTHING HERE KNOWS A DATE RULE. No floor, no ceiling, no "today": the caller passes
 * `disabled`, `startMonth` and `endMonth`, and those come off the wire (R13). A calendar that
 * worked out its own bounds would put a browser clock in Bangalore against a server clock in UTC
 * and offer a date the next read refuses.
 *
 * THE THREE FORMATTERS ARE SPELLED OUT, not left to a locale, for the reason `ConnectorRow.tsx`
 * gives at its own month list: en-GB and en-IN abbreviate September as `Sept` under current CLDR
 * and en-US reorders the parts, so the one form the board specifies is not any runtime's default
 * and a suite that pinned it would be pinning the machine it ran on.
 */

/** The caption's month, in full — `September 2026`, as `DateRange` draws it. */
const MONTH_NAMES = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
]

/**
 * The column headers, keyed by `Date#getDay()` — so Sunday is index 0 even though the grid
 * starts on Monday. The board draws single letters: `M T W T F S S`.
 */
const WEEKDAY_LETTERS = ["S", "M", "T", "W", "T", "F", "S"]

const FORMATTERS = {
  formatCaption: (month: Date): string =>
    `${MONTH_NAMES[month.getMonth()]} ${month.getFullYear()}`,
  formatWeekdayName: (weekday: Date): string => WEEKDAY_LETTERS[weekday.getDay()] ?? "",
  formatDay: (date: Date): string => String(date.getDate()),
}

/**
 * The nav chevrons, at the board's 12px. `react-day-picker`'s own `Chevron` renders a 24px
 * polygon; the buttons around it carry the colour, and lucide strokes `currentColor`, so the
 * greyed next-month chevron the board draws is the button's `aria-disabled:` arm rather than a
 * second colour passed down here.
 */
function CalendarChevron({
  orientation,
  className,
}: {
  orientation?: "up" | "down" | "left" | "right"
  className?: string
}): React.JSX.Element {
  const Icon = orientation === "right" ? ChevronRight : ChevronLeft
  return <Icon size={12} strokeWidth={2} className={className} aria-hidden />
}

export type CalendarProps = React.ComponentProps<typeof DayPicker> & {
  /**
   * An element whose text explains why some dates are unavailable, wired as the month grid's
   * `aria-describedby`. Its one consumer is the date popover's amber note: a screen reader
   * should hear WHY a date cannot be picked, not only that it cannot.
   */
  gridDescribedBy?: string
}

function Calendar({
  className,
  classNames,
  gridDescribedBy,
  ...props
}: CalendarProps): React.JSX.Element {
  // MEMOISED, not an inline literal. `react-day-picker` keys its component map on identity, so a
  // fresh object every render would unmount and remount the whole table — losing keyboard focus
  // mid-navigation, which is the one thing the grid is here to support.
  const components = React.useMemo(
    () => ({
      Chevron: CalendarChevron,
      MonthGrid: (gridProps: React.ComponentProps<"table">) => (
        <table {...gridProps} aria-describedby={gridDescribedBy} />
      ),
    }),
    [gridDescribedBy],
  )

  return (
    <DayPicker
      className={cn("w-full", className)}
      components={components}
      formatters={FORMATTERS}
      classNames={{
        months: "relative flex flex-col",
        month: "w-full",
        // The nav sits over the caption row rather than above it: the board puts
        // `September 2026` on the left and both chevrons on the right of ONE line.
        nav: "absolute right-0 top-0 flex items-center gap-2",
        button_previous:
          "inline-flex h-3 w-3 items-center justify-center text-canvas-placeholder transition-colors hover:text-primary-900 aria-disabled:cursor-default aria-disabled:text-canvas-grip aria-disabled:hover:text-canvas-grip",
        button_next:
          "inline-flex h-3 w-3 items-center justify-center text-canvas-placeholder transition-colors hover:text-primary-900 aria-disabled:cursor-default aria-disabled:text-canvas-grip aria-disabled:hover:text-canvas-grip",
        month_caption: "mb-[7px] flex h-3 items-center",
        caption_label: "text-[11px] font-bold text-primary-900",
        month_grid: "w-full border-collapse",
        weekdays: "flex",
        weekday:
          "flex h-[22px] flex-1 items-center justify-center text-[9.5px] font-extrabold text-canvas-label",
        weeks: "",
        week: "mt-px flex gap-0.5",
        day: "h-[27px] flex-1 p-0 text-[11px] font-semibold text-primary-900",
        day_button:
          "flex h-full w-full items-center justify-center rounded-[7px] transition-colors hover:bg-primary-50 disabled:cursor-not-allowed disabled:hover:bg-transparent",
        // The range's interior tint sits on the CELL so it runs edge to edge; the two endpoints
        // put the solid teal on the button inside, which is what gives them their own radius.
        selected: "bg-primary-50 text-primary-dark",
        range_middle: "",
        range_start:
          "rounded-l-[7px] [&>button]:bg-primary [&>button]:font-extrabold [&>button]:text-white",
        range_end:
          "rounded-r-[7px] [&>button]:bg-primary [&>button]:font-extrabold [&>button]:text-white",
        disabled: "font-normal text-canvas-sha",
        outside: "text-canvas-label",
        hidden: "invisible",
        today: "",
        focused: "",
        ...classNames,
      }}
      {...props}
    />
  )
}

export { Calendar }
