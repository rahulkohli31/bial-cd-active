/**
 * WHAT THIS GUARDS, and why the old suite could not.
 *
 * `reducedMotion.test.ts` proves the stylesheet SUPPRESSES `.animate-spin`. It cannot prove what
 * is left on screen once it has, and what was left on screen is the bug: a `Loader2` is a circular
 * arrow, so stopping its animation yields a stationary loading spinner, which every reader
 * interprets as a hang rather than as an accommodation. Three components branched on the
 * preference and all three branched that way, so "we handle reduced motion" was true and the
 * product still looked dead on a Windows VM with animations off.
 *
 * So these assert the RENDERED ELEMENT in both registers, and each is paired with the mutant that
 * would otherwise let it pass: an assertion that only checks "a spinner is absent" goes green on a
 * component that renders nothing at all, which is the failure shape that shipped here before
 * (see the repo's `assert-absence-tests-false-green-on-crash` lesson) — so every absence below is
 * paired with a liveness assertion in the same test.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, act, cleanup } from '@testing-library/react'
import { BusyGlyph, WaitingLine, ELAPSED_AFTER_MS } from '../Waiting'

/** Point `matchMedia` at an answer. The suite-wide shim in `test-setup.ts` always says `false`;
 *  the whole question here is what happens when it says `true`. */
function setReducedMotion(reduce: boolean): void {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: (query: string) =>
      ({
        media: query,
        matches: reduce && query.includes('prefers-reduced-motion'),
        onchange: null,
        addEventListener: () => {},
        removeEventListener: () => {},
        addListener: () => {},
        removeListener: () => {},
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  })
}

afterEach(() => {
  setReducedMotion(false)
  vi.useRealTimers()
})

describe('BusyGlyph — the wait renders differently in the two motion registers', () => {
  it('animates when motion is allowed', () => {
    setReducedMotion(false)
    render(<BusyGlyph testId="glyph" />)
    const glyph = screen.getByTestId('glyph')
    expect(glyph.classList.contains('animate-spin')).toBe(true)
  })

  it('under reduced motion renders a DIFFERENT glyph — not the spinner with its class removed', () => {
    // The two renderings are compared against each other rather than against lucide's internal
    // class names, which are not this repo's to depend on.
    setReducedMotion(false)
    const spinning = render(<BusyGlyph />).container.innerHTML
    cleanup()

    setReducedMotion(true)
    const still = render(<BusyGlyph />).container.innerHTML

    // LIVENESS. Without this the two assertions below pass on a component that renders nothing —
    // the false-green shape this repo has shipped before.
    expect(still).not.toBe('')
    expect(spinning).toContain('animate-spin')

    // THE GUARANTEE, in two parts. No stalled animation…
    expect(still).not.toContain('animate-spin')
    // …and not merely the same spinner with the class taken off, which is exactly what the three
    // old per-component branches produced and exactly what reads as a hang.
    expect(still).not.toBe(spinning.replace(' animate-spin', '').replace('animate-spin ', ''))
  })
})

describe('WaitingLine — a long wait says how long', () => {
  it('says nothing about elapsed time before the threshold, but does say what it is doing', () => {
    vi.useFakeTimers()
    setReducedMotion(true)
    render(<WaitingLine label="Putting it away…" />)
    // Liveness first: the sentence is on screen, so the absence below means something.
    expect(screen.getByText('Putting it away…')).toBeTruthy()
    expect(screen.queryByTestId('waiting-elapsed')).toBeNull()
  })

  it('shows a live count once the wait outlives the threshold — the signal that needs no motion', () => {
    vi.useFakeTimers()
    setReducedMotion(true)
    render(<WaitingLine label="Saving it first…" />)
    act(() => {
      vi.advanceTimersByTime(ELAPSED_AFTER_MS + 1_000)
    })
    const elapsed = screen.getByTestId('waiting-elapsed')
    expect(elapsed.textContent).toBe('6s')
  })

  it('the count keeps climbing — a number that stopped would be the same lie as a frozen spinner', () => {
    vi.useFakeTimers()
    setReducedMotion(true)
    render(<WaitingLine label="Closing the other app…" />)
    act(() => {
      vi.advanceTimersByTime(ELAPSED_AFTER_MS + 1_000)
    })
    expect(screen.getByTestId('waiting-elapsed').textContent).toBe('6s')
    act(() => {
      vi.advanceTimersByTime(30_000)
    })
    // The production hand-over that prompted this ran to 76 seconds.
    expect(screen.getByTestId('waiting-elapsed').textContent).toBe('36s')
  })

  it('an inactive wait carries no clock at all', () => {
    vi.useFakeTimers()
    render(<WaitingLine label="Saving it first…" active={false} />)
    act(() => {
      vi.advanceTimersByTime(60_000)
    })
    expect(screen.getByText('Saving it first…')).toBeTruthy()
    expect(screen.queryByTestId('waiting-elapsed')).toBeNull()
  })
})

/**
 * THE COUNT MUST NEVER GO BACKWARDS, and a remount is how it did.
 *
 * `ChatThread`'s working line is dropped and re-appended on every reasoning burst of a build, so
 * the component timing the wait is destroyed and rebuilt several times inside ONE turn. Timed from
 * its own mount it restarts at zero each time, and a citizen watching a long build sees the number
 * fall — reported from production at 12s, then 9s.
 *
 * Both tests below remount deliberately, because a suite that only ever renders once cannot tell
 * the anchored clock from the self-timed one: both are green on a single mount.
 */
describe('WaitingLine — a wait that outlives its own element', () => {
  it('derives the count from `since`, so a remount mid-wait resumes instead of restarting', () => {
    vi.useFakeTimers()
    const startedAt = Date.now()
    const first = render(<WaitingLine label="Working on your app" since={startedAt} />)
    act(() => {
      vi.advanceTimersByTime(12_000)
    })
    expect(screen.getByTestId('waiting-elapsed').textContent).toBe('12s')

    // The burst ends, a tool call takes the floor, and the row is torn down and rebuilt.
    first.unmount()
    render(<WaitingLine label="Working on your app" since={startedAt} />)
    // Asserted on the FIRST paint, with no timer advanced: a clock that seeded 0 and caught up on
    // its next tick would still be wrong for the second the citizen is looking at it.
    expect(screen.getByTestId('waiting-elapsed').textContent).toBe('12s')

    act(() => {
      vi.advanceTimersByTime(4_000)
    })
    expect(screen.getByTestId('waiting-elapsed').textContent).toBe('16s')
  })

  it('without `since` a remount restarts — the dialog steps that must keep timing themselves', () => {
    // The mutant guard for the test above: if `since` were ignored and every wait simply became
    // turn-anchored, this one would fail. A hand-over step reports ITS age, not the dialog's.
    vi.useFakeTimers()
    const first = render(<WaitingLine label="Saving it first…" />)
    act(() => {
      vi.advanceTimersByTime(12_000)
    })
    expect(screen.getByTestId('waiting-elapsed').textContent).toBe('12s')

    first.unmount()
    render(<WaitingLine label="Putting it away…" />)
    act(() => {
      vi.advanceTimersByTime(ELAPSED_AFTER_MS + 1_000)
    })
    expect(screen.getByTestId('waiting-elapsed').textContent).toBe('6s')
  })
})
