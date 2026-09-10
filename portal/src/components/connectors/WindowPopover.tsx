/**
 * `DateRange`, whole: the four options, the month grid, the amber note and the resolved summary.
 *
 * IT IS THE POPOVER'S BODY, NOT ITS ROOT. `ProjectConnectorRow` owns the `<Popover>` and the
 * chip that triggers it, because the row is what closes it after a write the row made. This file
 * renders the `PopoverContent` and nothing outside it.
 *
 * BOTH BOUNDS COME FROM THE SERVER AND NOTHING HERE COMPUTES A DATE RULE (R13). `earliestDate`
 * and `latestDate` arrive on the window; the grid disables everything before the first and after
 * the last, and the nav is bounded by the same pair — which is why the board draws its next-month
 * chevron greyed. There is no `new Date()` in this file. A browser clock in Bangalore and a
 * server clock in UTC are 5½ hours apart, and `today - 29` in the browser is the one thing R13
 * exists to forbid.
 *
 * THE ONE PIECE OF ARITHMETIC, AND WHY IT IS NOT THAT. Ticking `Last 7 days` moves the grid's
 * highlight and the footer summary to the seven days ending at `latestDate`. That is a PREVIEW of
 * a pick, anchored on the server's own `latestDate` — never on a browser clock — and it is thrown
 * away the moment `Apply` answers: the write sends `{kind: 'relative', days: 7}` and the chip
 * re-renders from what the resolver returned. The board draws exactly this state, `Last 30 days`
 * ticked over a highlighted 1–30 Sep.
 *
 * THE AMBER NOTE NAMES THE CONNECTOR AND THE NUMBER, AND BOTH ARE DATA (R18). The name is
 * `displayName` off the wire; the number is the span between the two bounds, which is the
 * connector's own retention by construction (the resolver sets `earliest = latest - (retention -
 * 1)`, where `latest` is the connector's freshness ceiling and NOT today — the lake is loaded the
 * following morning, so the newest readable day runs a day or more behind). Neither is written
 * down here, so a second connector that keeps a week says `a week` without a line of this file
 * changing.
 *
 * WHAT THE BOARD'S COPY DOES NOT COVER: the note explains the FLOOR only, and this grid also
 * disables dates after `latestDate` — which is the ceiling, a day or more before today, not today
 * itself. The sentence is the board's, verbatim, and is not extended here —
 * R1 makes that copy binding in substance and an addition is the owner's call, not an
 * implementer's. It is wired as the grid's `aria-describedby` so a screen reader hears the
 * available span rather than only that a date is unavailable.
 */
import { useId, useState } from 'react'
import { Check, Loader2 } from 'lucide-react'
import type { DateRange } from 'react-day-picker'
import type { ConnectorWindow, WindowChoice } from '../../utils/connectorApi'
import { Button } from '../ui/button'
import { Calendar } from '../ui/calendar'
import { PopoverContent } from '../ui/popover'
import { ToggleGroup, ToggleGroupItem } from '../ui/toggle-group'
import { formatDayRange, parseCalendarDay, spanDays, toCalendarDay } from './WindowChip'

const TITLE = 'Which days should this app read?'
const SUBTITLE =
  'Only for building. Once it is published, the board reads whatever dates the person looking at it picks.'
/** The last option, which is not a day count — a sentinel rather than a number. */
const PICK = 'pick'

/**
 * The presets `DateRange` draws, filtered to what this connector can actually serve.
 *
 * A MIRROR OF THE SERVER'S `_offered_days`, DELIBERATELY, and the server is still the enforcer —
 * a hand-crafted `days` earns a `422 unsupported_window` whatever this list says. Offering all
 * three regardless would put `Last 30 days` in front of a connector that keeps seven and make
 * the refusal the citizen's first news of it. The cap is not hard-coded: it is the span between
 * the two bounds the window already carries.
 */
function offeredDays(maxDays: number): number[] {
  const withinRetention = [7, 14, 30].filter((days) => days <= maxDays)
  // A connector keeping less than a week still offers exactly its retention — an empty list
  // would leave the citizen a switch and no range to switch it to.
  return withinRetention.length > 0 ? withinRetention : [maxDays]
}

/** `n` days from a local calendar date, built field by field so a DST boundary cannot shift it. */
function shiftDays(date: Date, days: number): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days)
}

export interface WindowPopoverProps {
  /** The connector's own name, off the wire. The amber note is the only thing that reads it. */
  connectorName: string
  /** The RESOLVED window. Its bounds drive the grid; its `stored` half picks the ticked option. */
  window: ConnectorWindow
  /**
   * Writes the choice. REJECTS on failure, and the popover then keeps itself open with the
   * server's own message showing — a refusal the citizen can act on is worth more than a closed
   * popover and an unchanged chip they have to notice for themselves.
   */
  onApply: (choice: WindowChoice) => Promise<void>
  /** `Cancel`, Escape and an outside press all land here. Nothing is written. */
  onCancel: () => void
}

export default function WindowPopover({
  connectorName,
  window,
  onApply,
  onCancel,
}: WindowPopoverProps): React.JSX.Element {
  const noteId = useId()
  const earliest = parseCalendarDay(window.earliestDate)
  const latest = parseCalendarDay(window.latestDate)
  const maxDays = spanDays(earliest, latest)
  const offered = offeredDays(maxDays)

  /**
   * WHICH OPTION IS TICKED COMES FROM `stored`; WHAT THE GRID HIGHLIGHTS COMES FROM THE RESOLVED
   * PAIR. That is the whole of how the two halves of a window are used, and it is what makes a
   * clamped range pre-select the clamped dates while still showing it was a hand-picked range
   * rather than a preset.
   */
  const [choice, setChoice] = useState<string>(
    window.stored.days !== null && offered.includes(window.stored.days)
      ? String(window.stored.days)
      : PICK,
  )
  const [range, setRange] = useState<DateRange | undefined>({
    from: parseCalendarDay(window.start),
    to: parseCalendarDay(window.end),
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const from = range?.from
  const to = range?.to
  const complete = from !== undefined && to !== undefined
  const summary = complete
    ? `${formatDayRange(from, to)} · ${spanDays(from, to)} days`
    : 'Pick the last day.'
  const canApply = (choice !== PICK || complete) && !saving

  const apply = async (): Promise<void> => {
    // A real guard, not decoration: `aria-disabled` says so without doing so, and a keyboard
    // Enter reaches this the same way a press does.
    if (!canApply) return
    const next: WindowChoice | null =
      choice === PICK
        ? complete
          ? { kind: 'absolute', start: toCalendarDay(from), end: toCalendarDay(to) }
          : null
        : { kind: 'relative', days: Number(choice) }
    if (next === null) return
    setSaving(true)
    setError(null)
    try {
      await onApply(next)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setSaving(false)
    }
  }

  return (
    <PopoverContent
      align="start"
      data-testid="window-popover"
      onEscapeKeyDown={onCancel}
      onInteractOutside={onCancel}
      // Keep the popover clear of the window edge when Radix has to shift or flip it.
      collisionPadding={12}
      // `p-0 w-[302px]`: the board's own width, and each band owns its padding because the
      // divider above the grid and the amber note are inset differently from the footer.
      //
      // THE HEIGHT CAP IS NOT COSMETIC — it is what keeps `Apply` reachable. This popover is a
      // fixed ~520px tall, and the chip that opens it sits high in the rail, so on a 900px
      // window the footer landed 75px BELOW the fold (rendered, focusable, and impossible to
      // click), while on an 800px window Radix flipped it upward and the title and presets went
      // off the TOP at y=-105. Both were found in a real browser; jsdom computes no layout, so
      // no unit test could have seen either. Capping at Radix's own available-height variable and
      // scrolling the body — with the footer pinned outside the scroller — means the window can
      // be any height and the two controls that commit or abandon the choice are always on screen.
      //
      // Tailwind 3 spells this `max-h-[var(--x)]`; the current shadcn registry's `max-h-(--x)`
      // is v4-only syntax this build cannot parse, exactly as `dropdown-menu.tsx` records.
      className="flex max-h-[var(--radix-popover-content-available-height)] w-[302px] flex-col overflow-hidden rounded-[13px] border-bial-border p-0 shadow-[0_18px_44px_rgba(16,24,40,.18)]"
    >
      {/* The scrolling body. `min-h-0` is what lets a flex child actually shrink below its
          content height — without it the cap above would be ignored and nothing would scroll. */}
      <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="px-[15px] pt-[13px]">
        <p className="m-0 text-xs font-extrabold text-primary-900">{TITLE}</p>
        <p className="m-0 mt-[5px] text-[10.5px] leading-[1.5] text-neutral">{SUBTITLE}</p>
      </div>

      <ToggleGroup
        type="single"
        value={choice}
        aria-label={TITLE}
        onValueChange={(next) => {
          // Radix lets a single-select group be emptied by re-pressing the chosen item. There is
          // no "no window" state to fall into, so a second press on the ticked option is a no-op.
          if (next === '') return
          setChoice(next)
          if (next !== PICK) setRange({ from: shiftDays(latest, -(Number(next) - 1)), to: latest })
        }}
        className="mt-[11px] flex w-full flex-col items-stretch justify-start gap-px px-[15px]"
      >
        {offered.map((days) => (
          <ToggleGroupItem
            key={days}
            value={String(days)}
            className="h-auto w-full justify-between rounded-[7px] px-[9px] py-1.5 text-[11.5px] font-semibold text-neutral data-[state=on]:bg-primary-50 data-[state=on]:font-bold data-[state=on]:text-primary-dark data-[state=on]:shadow-none [&_svg]:size-3"
          >
            Last {days} days
            {choice === String(days) && (
              <Check strokeWidth={2.4} className="text-primary-dark" aria-hidden />
            )}
          </ToggleGroupItem>
        ))}
        <ToggleGroupItem
          value={PICK}
          className="h-auto w-full justify-between rounded-[7px] px-[9px] py-1.5 text-[11.5px] font-semibold text-neutral data-[state=on]:bg-primary-50 data-[state=on]:font-bold data-[state=on]:text-primary-dark data-[state=on]:shadow-none [&_svg]:size-3"
        >
          Pick dates
          {choice === PICK && <Check strokeWidth={2.4} className="text-primary-dark" aria-hidden />}
        </ToggleGroupItem>
      </ToggleGroup>

      <div className="mx-[15px] mt-[11px] border-t border-bial-border pt-[11px]">
        <Calendar
          mode="range"
          gridDescribedBy={noteId}
          selected={range}
          onSelect={(next) => {
            setRange(next)
            // Touching the grid IS picking dates. Leaving `Last 30 days` ticked over a range the
            // citizen just drew would make `Apply` send something other than what they see.
            setChoice(PICK)
          }}
          // AN ARRAY, NOT `{before, after}` — the object form is `react-day-picker`'s
          // DateInterval, which matches the days BETWEEN the two and would disable exactly the
          // span the connector serves. Two matchers, one per end.
          disabled={[{ before: earliest }, { after: latest }]}
          // A press on a complete range STARTS A NEW ONE. `react-day-picker`'s default instead
          // drags the nearer endpoint, so somebody arriving on `1 – 30 Sep` and wanting `8 – 12
          // Sep` gets `1 – 8 Sep` on their first click and can never reach the range they meant.
          // The board's option is `Pick dates`, plural and from scratch.
          resetOnSelect
          startMonth={earliest}
          endMonth={latest}
          defaultMonth={parseCalendarDay(window.end)}
          weekStartsOn={1}
        />
      </div>

      <div
        id={noteId}
        className="mx-[15px] mt-[11px] rounded-lg border border-canvas-noteedge bg-status-amber-bg px-2.5 py-2"
      >
        <p className="m-0 text-[10.5px] leading-[1.5] text-status-amber-fg">
          {connectorName} keeps <b className="font-extrabold">{maxDays} days</b> available while
          you build. Earlier dates are greyed out.
        </p>
      </div>

      {error !== null && (
        <div
          role="alert"
          className="mx-[15px] mt-[11px] rounded-lg border border-red-200 bg-red-50 px-2.5 py-2"
        >
          <p className="m-0 text-[10.5px] leading-[1.5] text-red-600">{error}</p>
        </div>
      )}

      </div>

      {/* Pinned: the footer never scrolls out of reach. */}
      <div className="flex flex-shrink-0 items-center gap-2 border-t border-bial-border/60 px-[15px] pb-3.5 pt-3">
        <span className="text-[10.5px] text-neutral">{summary}</span>
        <span className="ml-auto inline-flex gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={onCancel}
            className="h-auto rounded-lg border-bial-border bg-white px-3 py-1.5 text-[11.5px] font-semibold text-neutral shadow-none hover:bg-white hover:text-primary-900"
          >
            Cancel
          </Button>
          <Button
            type="button"
            aria-disabled={!canApply}
            onClick={() => void apply()}
            className="h-auto gap-1.5 rounded-lg px-3.5 py-1.5 text-[11.5px] font-bold shadow-none aria-disabled:opacity-60 [&_svg]:size-3"
          >
            {saving && <Loader2 className="animate-spin" aria-hidden />}
            Apply
          </Button>
        </span>
      </div>
    </PopoverContent>
  )
}
