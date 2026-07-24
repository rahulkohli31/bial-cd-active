/**
 * ToolActivityGroup (Mode B) — the collapsible group of friendly activity lines (F3/U3). What this
 * pins:
 *  - the header is a real <button aria-expanded> controlling the step list;
 *  - ≥3 steps group into one collapsible <ol> of Mode-A rows;
 *  - a failed row conveys failure by TEXT (a visually-hidden "failed"), never colour alone;
 *  - prefers-reduced-motion suppresses the spinner animation, yet the collapse state still changes.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { ToolActivityGroup, type ToolActivityGroupStep } from '../ToolActivityGroup'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function mockReducedMotion(reduce: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: reduce,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

const threeSteps: ToolActivityGroupStep[] = [
  { seq: 1, label: "Building your app's main page", state: 'ok' },
  { seq: 2, label: 'Setting up the tools your app needs', state: 'ok' },
  { seq: 3, label: 'Making sure everything fits together', state: 'failed' },
]

describe('ToolActivityGroup (Mode B)', () => {
  it('renders a real <button aria-expanded> header', () => {
    const { container } = render(
      <ToolActivityGroup summaryLabel="Building your app" running steps={threeSteps} />,
    )
    expect(container.querySelector('button[aria-expanded]')).toBeTruthy()
  })

  it('groups ≥3 steps into one collapsible list of rows', () => {
    render(<ToolActivityGroup summaryLabel="Building your app" running steps={threeSteps} />)
    const list = screen.getByRole('list', { name: /activity/i })
    expect(within(list).getAllByRole('listitem')).toHaveLength(3)
    expect(list.textContent).toContain('Making sure everything fits together')
  })

  it('conveys the failed state by text (sr-only "failed"), not glyph colour alone', () => {
    render(<ToolActivityGroup summaryLabel="Building your app" running steps={threeSteps} />)
    expect(screen.getAllByText('failed').length).toBeGreaterThanOrEqual(1)
  })

  it('reduced motion suppresses the spinner but the collapse state still changes', () => {
    mockReducedMotion(true)
    const startedSteps: ToolActivityGroupStep[] = [
      { seq: 1, label: 'Setting up the tools your app needs', state: 'started' },
      { seq: 2, label: "Building your app's main page", state: 'ok' },
      { seq: 3, label: 'Making sure everything fits together', state: 'ok' },
    ]
    const { container } = render(
      <ToolActivityGroup
        summaryLabel="Building your app"
        summaryState="started"
        running
        steps={startedSteps}
      />,
    )
    // No animation despite a running/started state…
    expect(container.querySelector('.animate-spin')).toBeNull()
    // …yet the control still works: toggling flips aria-expanded (the state change occurs).
    const header = screen.getByRole('button')
    expect(header.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(header)
    expect(header.getAttribute('aria-expanded')).toBe('false')
  })
})
