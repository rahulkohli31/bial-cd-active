/**
 * THE BOUNDARY, AND THE ONE TIMER THAT FINDS IT.
 *
 * The pane counts seconds for the number it draws; this is the other half — the FACT that the wait
 * has outlived the platform's budget, which belongs in the workspace state beside the sentence it
 * changes. What these pin is the part that is easy to get wrong: it arms from the SERVER's instant,
 * so a tab that opens well into a start is already past the boundary rather than starting its
 * patience over, and it is a timer rather than anything hung off the poll — a stuck wait is exactly
 * the reading that stops being published.
 */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useTheWaitHasGoneOnTooLong } from '../useTheWaitHasGoneOnTooLong'
import { START_PATIENCE_MS } from '../workspaceState'
import type { PreviewState } from '../../../utils/buildSessionApi'

const reading = (over: Partial<PreviewState> = {}): PreviewState => ({
  state: 'asleep',
  alive: false,
  previewUrl: null,
  restorable: null,
  startingSince: null,
  ...over,
})

const NOW = new Date('2026-09-22T07:20:00.000Z')
const iso = (msAgo: number) => new Date(NOW.getTime() - msAgo).toISOString()

describe('useTheWaitHasGoneOnTooLong', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('is false while the wait is inside the budget, and true when the timer fires', () => {
    const { result } = renderHook(() =>
      useTheWaitHasGoneOnTooLong(reading({ state: 'starting', startingSince: iso(0) })),
    )
    expect(result.current).toBe(false)

    // One millisecond short is still inside it — the boundary is crossed, never approached.
    act(() => {
      vi.advanceTimersByTime(START_PATIENCE_MS - 1)
    })
    expect(result.current).toBe(false)

    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(result.current).toBe(true)
  })

  it('★ a tab that opens well into a start is past the boundary at once', () => {
    // THE RELOAD CASE, AND THE REASON THIS READS THE SERVER'S INSTANT. A hook that armed a fresh
    // two-minute timer on mount would give a citizen four minutes of the opening sentence for a
    // wait that was already two minutes old — and another two for every reload after that.
    const { result } = renderHook(() =>
      useTheWaitHasGoneOnTooLong(
        reading({ state: 'starting', startingSince: iso(START_PATIENCE_MS + 30_000) }),
      ),
    )
    expect(result.current).toBe(true)
  })

  it('gives an undated wait the whole budget from now, rather than guessing', () => {
    const { result } = renderHook(() =>
      useTheWaitHasGoneOnTooLong(reading({ state: 'starting' })),
    )
    expect(result.current).toBe(false)

    act(() => {
      vi.advanceTimersByTime(START_PATIENCE_MS)
    })
    expect(result.current).toBe(true)
  })

  it('claims nothing about a reading that is not a wait, however old the clock gets', () => {
    for (const state of ['alive', 'asleep'] as const) {
      const { result } = renderHook(() => useTheWaitHasGoneOnTooLong(reading({ state })))
      act(() => {
        vi.advanceTimersByTime(START_PATIENCE_MS * 3)
      })
      expect(result.current, state).toBe(false)
    }
  })

  it('★ resets the moment the wait ends, so a finished start cannot leave the flag standing', () => {
    // The map would carry a stale `true` onto the NEXT start otherwise — and that start's first
    // second would open on the sentence written for a wait that has gone wrong.
    const { result, rerender } = renderHook(
      ({ preview }: { preview: PreviewState }) => useTheWaitHasGoneOnTooLong(preview),
      { initialProps: { preview: reading({ state: 'starting', startingSince: iso(0) }) } },
    )
    act(() => {
      vi.advanceTimersByTime(START_PATIENCE_MS)
    })
    expect(result.current).toBe(true)

    rerender({ preview: reading({ state: 'alive', alive: true }) })
    expect(result.current).toBe(false)

    // A NEW start, dated from the clock as it stands now rather than from the old one's instant —
    // which is the whole reason the flag may not simply be left where the last wait put it.
    const freshlyStarted = new Date(Date.now()).toISOString()
    rerender({ preview: reading({ state: 'starting', startingSince: freshlyStarted }) })
    expect(result.current).toBe(false)
  })

  it('never lets a server clock ahead of this browser read as a wait already spent', () => {
    // A future instant would make `Date.now() - startedAt` negative; clamping to zero is what
    // keeps a skewed clock from arming the boundary either instantly or never.
    const { result } = renderHook(() =>
      useTheWaitHasGoneOnTooLong(reading({ state: 'starting', startingSince: iso(-60_000) })),
    )
    expect(result.current).toBe(false)

    act(() => {
      vi.advanceTimersByTime(START_PATIENCE_MS)
    })
    expect(result.current).toBe(true)
  })
})
