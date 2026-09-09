/**
 * `DateRange`, as a control: the four options, the month grid and its two disabled ends, the
 * amber note, and the summary that moves with the selection.
 *
 * THE FIXTURE CONNECTOR IS NOT THE REAL ONE, for the reason `IntegrationsDialog.test.tsx` gives:
 * every string the popover says about a connector comes off the wire, and a component with the
 * real name compiled into it would still pass a suite that asserted the real name. `ORBIT` keeps
 * this suite able to tell the difference — and the same goes for the number in the amber note,
 * which is derived from the window's own bounds and is deliberately NOT 30 in one test here.
 *
 * SEPTEMBER 2026 IS THE MONTH THROUGHOUT, and its real shape is used: 1 September 2026 is a
 * TUESDAY. The board draws it on a Monday, which the fidelity register already records as a
 * drawing rather than a calendar — `react-day-picker` renders the true one, and this suite
 * asserts against the true one.
 *
 * EVERY ABSENCE ASSERTION IS PAIRED. A component that crashed on mount would satisfy "the summary
 * did not change" perfectly, so each of those tests also asserts something positive rendered.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'

import type { ConnectorWindow } from '../../../utils/connectorApi'
import { Popover, PopoverTrigger } from '../../ui/popover'
import WindowPopover from '../WindowPopover'

afterEach(cleanup)

/** A thirty-day September, ticked on `Last 30 days` — the board's own state. */
const thirtyDays: ConnectorWindow = {
  kind: 'relative',
  start: '2026-09-01',
  end: '2026-09-30',
  days: 30,
  clamped: false,
  earliestDate: '2026-09-01',
  latestDate: '2026-09-30',
  stored: { days: 30, start: null, end: null },
}

/**
 * A SIXTEEN-day window, so both bounds fall inside one visible month and both ends of the grid
 * can be asserted. It also proves the option list is derived: sixteen days cannot offer thirty.
 */
const sixteenDays: ConnectorWindow = {
  kind: 'absolute',
  start: '2026-09-05',
  end: '2026-09-20',
  days: 16,
  clamped: false,
  earliestDate: '2026-09-05',
  latestDate: '2026-09-20',
  stored: { days: null, start: '2026-09-05', end: '2026-09-20' },
}

function open(
  window: ConnectorWindow,
  onApply: (choice: unknown) => Promise<void> = () => Promise.resolve(),
  connectorName = 'ORBIT',
): { onCancel: ReturnType<typeof vi.fn> } {
  const onCancel = vi.fn()
  render(
    <Popover open>
      <PopoverTrigger>days</PopoverTrigger>
      <WindowPopover
        connectorName={connectorName}
        window={window}
        onApply={onApply}
        onCancel={onCancel}
      />
    </Popover>,
  )
  return { onCancel }
}

/** The `<td>` for one calendar day, by the `data-day` react-day-picker stamps on it. */
function cell(day: string): HTMLElement {
  const found = document.querySelector(`[data-day="${day}"]`)
  if (!(found instanceof HTMLElement)) throw new Error(`no cell for ${day}`)
  return found
}

/** …and the button inside it, which is what carries the real `disabled` attribute. */
function dayButton(day: string): HTMLButtonElement {
  const button = cell(day).querySelector('button')
  if (!(button instanceof HTMLButtonElement)) throw new Error(`no button for ${day}`)
  return button
}

function summary(): string {
  const footer = document.querySelector('[data-testid="window-popover"] .ml-auto')?.parentElement
  return footer?.textContent ?? ''
}

describe('WindowPopover', () => {
  it('renders the board’s title, subtitle and four options, with the stored preset ticked', () => {
    open(thirtyDays)

    expect(screen.getByText('Which days should this app read?')).toBeTruthy()
    expect(
      screen.getByText(
        'Only for building. Once it is published, the board reads whatever dates the person looking at it picks.',
      ),
    ).toBeTruthy()

    const options = screen.getAllByRole('radio').map((node) => node.textContent)
    expect(options).toEqual(['Last 7 days', 'Last 14 days', 'Last 30 days', 'Pick dates'])
    expect(screen.getByRole('radio', { name: 'Last 30 days' }).getAttribute('aria-checked')).toBe(
      'true',
    )
    expect(screen.getByRole('radio', { name: 'Last 7 days' }).getAttribute('aria-checked')).toBe(
      'false',
    )
  })

  it('offers only the presets the connector’s own retention covers', () => {
    open(sixteenDays)

    // Sixteen days of retention cannot serve `Last 30 days`, and the server would refuse it.
    const options = screen.getAllByRole('radio').map((node) => node.textContent)
    expect(options).toEqual(['Last 7 days', 'Last 14 days', 'Pick dates'])
  })

  it('starts the week on Monday', () => {
    open(thirtyDays)

    const headers = Array.from(document.querySelectorAll('th')).map((node) => node.textContent)
    expect(headers).toEqual(['M', 'T', 'W', 'T', 'F', 'S', 'S'])
  })

  it('names the connector and its retention in the amber note, from the window’s own bounds', () => {
    open(sixteenDays, () => Promise.resolve(), 'ORBIT')

    const note = document.getElementById(
      document.querySelector('table')?.getAttribute('aria-describedby') ?? '',
    )
    expect(note?.textContent).toBe(
      'ORBIT keeps 16 days available while you build. Earlier dates are greyed out.',
    )
  })

  it('describes the grid by the amber note, so a reader hears why a date is unavailable', () => {
    open(thirtyDays)

    const grid = document.querySelector('table')
    const describedBy = grid?.getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    expect(document.getElementById(describedBy ?? '')?.textContent).toContain('available while you build')
  })

  it('disables dates before the floor AND after the ceiling, and a click on either does nothing', () => {
    open(sixteenDays)

    // The liveness half: the days inside the window really rendered and really are pickable.
    expect(dayButton('2026-09-05').hasAttribute('disabled')).toBe(false)
    expect(dayButton('2026-09-20').hasAttribute('disabled')).toBe(false)

    const before = dayButton('2026-09-04')
    const after = dayButton('2026-09-21')
    expect(before.hasAttribute('disabled')).toBe(true)
    expect(after.hasAttribute('disabled')).toBe(true)

    const was = summary()
    fireEvent.click(before)
    fireEvent.click(after)
    expect(summary()).toBe(was)
    expect(was).toContain('5 – 20 Sep · 16 days')
  })

  it('pre-selects the CLAMPED range, and ticks Pick dates for a range that was hand-picked', () => {
    // Stored `1 – 30 Jun`, read in September: the resolver moved it, and this is what came back.
    const clamped: ConnectorWindow = {
      kind: 'absolute',
      start: '2026-09-01',
      end: '2026-09-30',
      days: 30,
      clamped: true,
      earliestDate: '2026-09-01',
      latestDate: '2026-09-30',
      stored: { days: null, start: '2026-06-01', end: '2026-06-30' },
    }
    open(clamped)

    expect(screen.getByRole('radio', { name: 'Pick dates' }).getAttribute('aria-checked')).toBe(
      'true',
    )
    // The grid shows what the RESOLVER returned, not the June pair the row still stores.
    expect(summary()).toContain('1 – 30 Sep · 30 days')
    expect(cell('2026-09-01').getAttribute('data-selected')).toBe('true')
    expect(document.querySelector('[data-day="2026-06-01"]')).toBeNull()
  })

  it('sends a preset as {kind: relative, days} and previews it from the server’s own ceiling', async () => {
    const onApply = vi.fn<(choice: unknown) => Promise<void>>().mockResolvedValue(undefined)
    open(thirtyDays, onApply)

    fireEvent.click(screen.getByRole('radio', { name: 'Last 7 days' }))

    // The preview runs BACK from `latestDate` (2026-09-30), never from a browser clock.
    expect(summary()).toContain('24 – 30 Sep · 7 days')

    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1))
    expect(onApply).toHaveBeenCalledWith({ kind: 'relative', days: 7 })
  })

  it('builds an absolute range from two dates, and the summary moves with the selection', async () => {
    const onApply = vi.fn<(choice: unknown) => Promise<void>>().mockResolvedValue(undefined)
    open(thirtyDays, onApply)

    fireEvent.click(dayButton('2026-09-08'))
    // One end picked: the footer says what is still owed rather than inventing a range.
    expect(summary()).toContain('Pick the last day.')
    expect(screen.getByRole('radio', { name: 'Pick dates' }).getAttribute('aria-checked')).toBe(
      'true',
    )

    fireEvent.click(dayButton('2026-09-12'))
    expect(summary()).toContain('8 – 12 Sep · 5 days')

    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1))
    expect(onApply).toHaveBeenCalledWith({
      kind: 'absolute',
      start: '2026-09-08',
      end: '2026-09-12',
    })
  })

  it('keeps itself open with the server’s message when Apply is refused', async () => {
    const onApply = vi
      .fn<(choice: unknown) => Promise<void>>()
      .mockRejectedValue(new Error('You do not have access to ORBIT yet.'))
    open(thirtyDays, onApply)

    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await screen.findByRole('alert')
    expect(screen.getByRole('alert').textContent).toBe('You do not have access to ORBIT yet.')
    // Still open, and still showing its controls: a refusal that closed the popover would leave
    // the citizen looking at an unchanged chip with no idea why.
    expect(screen.getByRole('radio', { name: 'Last 30 days' })).toBeTruthy()
  })

  it('cancels without writing', () => {
    const onApply = vi.fn<(choice: unknown) => Promise<void>>().mockResolvedValue(undefined)
    const { onCancel } = open(thirtyDays, onApply)

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onApply).not.toHaveBeenCalled()
  })
})
