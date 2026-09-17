/**
 * The token meter: THE FIGURES ARE THE REQUIREMENT, the ring is how they read at a glance.
 *
 * AMBER IS THE METER, AT ANY AMOUNT. `NavStates.dc.html` draws three worked readings — 54%, 93%
 * and 100% — and only the last is red. An intermediate "nearing" threshold is the defect this
 * pins against: at the board's own 54% example it would paint the wrong colour.
 *
 * THE ARC IS ASSERTED THROUGH `stroke-dasharray`, which is the mechanism rather than a
 * screenshot. jsdom computes no geometry, so a test that asked "does it look 54% full" would be
 * asking a question this environment cannot answer.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { MotionGlobalConfig } from 'motion/react'
import TokenRing from '../TokenRing'

// Every transition becomes an instant change of state — which is also exactly what the root
// `MotionConfig reducedMotion="user"` does under the operating-system preference, so the values
// asserted below are the settled ones either way.
MotionGlobalConfig.skipAnimations = true

afterEach(() => cleanup())

const CIRCUMFERENCE = 2 * Math.PI * 17

/** The board's own worked example. */
const HALF = { used: 537_102, limit: 1_000_000, remaining: 462_898, resetsAt: '' }
const SPENT = { used: 1_000_000, limit: 1_000_000, remaining: 0, resetsAt: '' }

/** The second circle is the swept arc; the first is its track. `SVGCircleElement` is not a
 *  global in this environment, so the guard is on presence rather than on the constructor. */
const arc = (): Element => {
  const found = screen.getByTestId('usage-meter').querySelectorAll('circle')[1]
  if (!found) throw new Error('no arc drawn')
  return found
}

describe('the meter states used-of-limit in figures, whatever the ring does', () => {
  it('writes both numbers out in full', () => {
    render(<TokenRing usage={HALF} />)
    expect(screen.getByTestId('usage-figures').textContent).toBe('537,102 / 1,000,000')
  })

  it('keeps the figures in the compact form the workspace toolbar uses', () => {
    // The row inside an application is already full, so the percentage in the middle of the ring
    // goes — but the client's requirement is the COUNTER, and it does not.
    render(<TokenRing usage={HALF} compact />)
    expect(screen.getByTestId('usage-figures').textContent).toBe('537,102 / 1,000,000')
  })

  it('uses tabular figures, so the numbers do not jitter as they are spent', () => {
    render(<TokenRing usage={HALF} />)
    expect(screen.getByTestId('usage-figures').className).toMatch(/tabular-nums/)
  })
})

describe('two colours, and no threshold between them', () => {
  it('is amber at the board\'s own 54% example', () => {
    render(<TokenRing usage={HALF} />)
    expect(arc().getAttribute('class')).toMatch(/stroke-accent/)
    expect(arc().getAttribute('class')).not.toMatch(/stroke-danger/)
  })

  it('is still amber at 93% — there is budget left', () => {
    render(<TokenRing usage={{ used: 926_480, limit: 1_000_000, remaining: 73_520, resetsAt: '' }} />)
    expect(arc().getAttribute('class')).toMatch(/stroke-accent/)
  })

  it('turns red only once the budget is actually spent, and still shows both figures', () => {
    render(<TokenRing usage={SPENT} />)
    expect(arc().getAttribute('class')).toMatch(/stroke-danger/)
    expect(screen.getByTestId('usage-figures').textContent).toBe('1,000,000 / 1,000,000')
  })
})

describe('the swept fraction is the reading', () => {
  it('sweeps used/limit of the circumference', () => {
    render(<TokenRing usage={HALF} />)
    const [swept, total] = (arc().getAttribute('stroke-dasharray') ?? '').split(' ').map(Number)
    expect(total).toBeCloseTo(CIRCUMFERENCE, 3)
    expect(swept / total).toBeCloseTo(0.537102, 5)
  })

  it('closes the ring when the budget is gone', () => {
    render(<TokenRing usage={SPENT} />)
    const [swept, total] = (arc().getAttribute('stroke-dasharray') ?? '').split(' ').map(Number)
    expect(swept).toBeCloseTo(total, 3)
  })

  it('cannot sweep past the ring when usage overshoots the limit', () => {
    // Not a state the server produces; the meter is the one element that may never draw
    // nonsense, and an arc longer than its own circumference wraps back over itself.
    render(<TokenRing usage={{ used: 1_200_000, limit: 1_000_000, remaining: 0, resetsAt: '' }} />)
    const [swept, total] = (arc().getAttribute('stroke-dasharray') ?? '').split(' ').map(Number)
    expect(swept).toBeCloseTo(total, 3)
  })

  it('draws nothing rather than NaN when the limit is zero', () => {
    render(<TokenRing usage={{ used: 0, limit: 0, remaining: 0, resetsAt: '' }} />)
    const [swept] = (arc().getAttribute('stroke-dasharray') ?? '').split(' ').map(Number)
    expect(swept).toBe(0)
    expect(screen.getByTestId('usage-figures').textContent).toBe('0 / 0')
  })
})

describe('the reading keeps its provenance', () => {
  it('names the reset, because a spent budget is a question about when it comes back', () => {
    render(<TokenRing usage={SPENT} />)
    expect(screen.getByTestId('usage-meter').getAttribute('title')).toBe(
      'Daily AI tokens used today · resets at midnight IST',
    )
  })
})
