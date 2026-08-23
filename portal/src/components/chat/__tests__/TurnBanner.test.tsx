import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup, screen } from '@testing-library/react'
import TurnBanner from '../TurnBanner'

afterEach(cleanup)

// The banner slot is where every sentence this plan says to a citizen lands: their app was
// recovered, it could not be, the workspace could not be checked, the change did not come
// together, today's allowance is used up. All five arrive at the same moment in the same place.
describe('TurnBanner — one banner, newest wins (U7/R13)', () => {
  it('renders nothing at all when there is nothing to say', () => {
    const { container } = render(<TurnBanner text={null} />)
    // NOT an empty box. A bordered empty element above the composer is a permanent visual
    // artefact that reads as a broken UI, and it steals a row of vertical space from the
    // conversation for the whole time nothing is wrong — which is nearly all of the time.
    expect(container.firstChild).toBeNull()
  })

  it('renders the sentence it is given, announced politely', () => {
    render(<TurnBanner text="That change didn’t come together." />)
    const banner = screen.getByTestId('turn-banner')
    expect(banner.textContent).toBe('That change didn’t come together.')
    // `polite`, never `assertive`: these are endings with an action attached, not alarms.
    // `assertive` is spent on the two things that genuinely interrupt (a failed relaunch, a
    // failed save), and using it here would make those stop cutting through.
    expect(banner.getAttribute('role')).toBe('status')
    expect(banner.getAttribute('aria-live')).toBe('polite')
  })

  it('REPLACES a standing banner rather than stacking a second one', () => {
    // Two platform sentences about the same app are not additional information — the older one
    // is a contradiction. Taking a single value is what makes "newest wins" a property of the
    // type rather than a rule every call site has to remember.
    const { rerender } = render(<TurnBanner text="We brought your app back." />)
    rerender(<TurnBanner text="That change didn’t come together." />)

    expect(screen.getAllByTestId('turn-banner')).toHaveLength(1)
    expect(screen.getByTestId('turn-banner').textContent).toBe('That change didn’t come together.')
  })

  it('clears when the sentence is withdrawn', () => {
    const { rerender } = render(<TurnBanner text="We couldn’t check on your app's workspace." />)
    expect(screen.getByTestId('turn-banner')).toBeTruthy() // liveness for the absence below
    rerender(<TurnBanner text={null} />)
    expect(screen.queryByTestId('turn-banner')).toBeNull()
  })
})
