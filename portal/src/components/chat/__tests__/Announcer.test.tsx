/**
 * THE POLITE ACTIVITY REGION (R65, R66).
 *
 * ══ THE RULE THAT ACTUALLY BREAKS ══
 *
 * A live region must ALREADY EXIST IN THE DOM, EMPTY, before its text arrives. A region injected
 * together with its text is frequently not announced at all — several reader and browser
 * combinations miss it entirely. Two other files in this portal state that rule in their own
 * docblocks (`TurnBanner`, `LivePreview`), which is why it is asserted here as node identity
 * rather than as "the text appeared".
 *
 * ══ R66 IS TWO ANNOUNCEMENTS AND NO MORE ══
 *
 * The agent started working, and what a group amounted to when it sealed. NOT every step as it
 * happens. The old sr-only mirror throttled to one change per ten seconds with a flush branch —
 * that throttle was solving the wrong problem, and R66 removes the problem rather than tuning it.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, renderHook, act } from '@testing-library/react'

import Announcer, { useActivityAnnouncement } from '../Announcer'

afterEach(cleanup)

describe('the region', () => {
  it('is mounted and EMPTY before there is anything to say', () => {
    render(<Announcer message={null} />)
    const region = screen.getByTestId('activity-announcer')
    expect(region.textContent).toBe('')
    expect(region.getAttribute('role')).toBe('status')
    expect(region.getAttribute('aria-live')).toBe('polite')
  })

  it('is the SAME node once text arrives — not a new one inserted with it', () => {
    // The discriminating assertion. "The text appeared" would pass against a conditionally
    // rendered region, which is the shape that silently fails to announce.
    //
    // Mutation check: return `null` when `message` is null and this goes red.
    const { rerender } = render(<Announcer message={null} />)
    const region = screen.getByTestId('activity-announcer')

    rerender(<Announcer message="Working on your app." />)

    expect(screen.getByTestId('activity-announcer')).toBe(region)
    expect(region.textContent).toBe('Working on your app.')
  })

  it('reads the whole sentence, not only the words that changed', () => {
    render(<Announcer message="4 steps · one problem" />)
    expect(screen.getByTestId('activity-announcer').getAttribute('aria-atomic')).toBe('true')
  })

  it('is invisible — it wraps nothing that is already on screen', () => {
    // A second visible copy of a sentence already rendered elsewhere is that sentence rendered
    // twice to anything reading the DOM. This region is sr-only precisely so it can carry text
    // the visible surface does not.
    render(<Announcer message="Working on your app." />)
    expect(screen.getByTestId('activity-announcer').className).toContain('sr-only')
  })

  it('never uses assertive — that channel is reserved', () => {
    // `assertive` belongs to the things that genuinely interrupt (a refused send, a failed save).
    // Spending it on activity is what makes those stop cutting through.
    render(<Announcer message="Working on your app." />)
    expect(document.querySelector('[aria-live="assertive"]')).toBeNull()
  })
})

describe('useActivityAnnouncement — two announcements and no more (R66)', () => {
  it('says nothing at rest', () => {
    const { result } = renderHook(() => useActivityAnnouncement({ isRunning: false, sealedSummary: null }))
    expect(result.current).toBeNull()
  })

  it('announces once when a turn starts, and does not repeat while it runs', () => {
    const { result, rerender } = renderHook(
      ({ isRunning }) => useActivityAnnouncement({ isRunning, sealedSummary: null }),
      { initialProps: { isRunning: false } },
    )

    rerender({ isRunning: true })
    expect(result.current).toBe('Working on your app.')

    // Still running, many renders later: the message does not change, so nothing re-announces.
    // (`aria-atomic` re-reads the WHOLE line on any change, so a re-set would be a screen reader
    // repeating the same sentence for the length of an npm install.)
    rerender({ isRunning: true })
    rerender({ isRunning: true })
    expect(result.current).toBe('Working on your app.')
  })

  it('announces a sealed group’s summary when it seals', () => {
    const { result, rerender } = renderHook(
      ({ sealedSummary }) => useActivityAnnouncement({ isRunning: false, sealedSummary }),
      { initialProps: { sealedSummary: null as string | null } },
    )

    rerender({ sealedSummary: '9 steps' })
    expect(result.current).toBe('9 steps')
  })

  it('does NOT announce each step as it happens', () => {
    // The rule R66 exists for. A per-step region is a reader hearing nine sentences it cannot
    // keep up with; the group's own summary, once, is the useful version.
    const { result, rerender } = renderHook(
      ({ isRunning }) => useActivityAnnouncement({ isRunning, sealedSummary: null }),
      { initialProps: { isRunning: false } },
    )
    rerender({ isRunning: true })
    const afterStart = result.current

    // Steps arriving change nothing about this hook's inputs — it is not given them at all, which
    // is the mechanical form of "it cannot announce them".
    act(() => undefined)
    rerender({ isRunning: true })
    expect(result.current).toBe(afterStart)
  })

  it('a second turn announces again — the start message is per turn, not per page', () => {
    const { result, rerender } = renderHook(
      ({ isRunning, sealedSummary }) => useActivityAnnouncement({ isRunning, sealedSummary }),
      { initialProps: { isRunning: false, sealedSummary: null as string | null } },
    )
    rerender({ isRunning: true, sealedSummary: null })
    rerender({ isRunning: false, sealedSummary: '3 steps' })
    expect(result.current).toBe('3 steps')

    rerender({ isRunning: true, sealedSummary: '3 steps' })
    expect(result.current).toBe('Working on your app.')
  })
})
