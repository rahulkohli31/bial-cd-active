/**
 * FOLLOWING THE NEWEST CONTENT, AND THE WAY BACK TO IT (R35a, R29a, R64).
 *
 * ══ WHAT THIS REPLACES ══
 *
 * The old transcript was pinned by brute force: a sentinel `<div>` and a
 * `scrollIntoView({behavior:'smooth'})` on EVERY `[messages]` change. That is what made the build
 * bubble read as pinned — and it also dragged a reader who had scrolled up back to the bottom on
 * every single delta, so reading the middle of a long build was impossible. The thread's own
 * viewport ships auto-scroll with a bottom-proximity check, and this is one of the places the
 * library genuinely replaces our code.
 *
 * ══ WHY THE LIBRARY'S OWN BUTTON IS NOT USED, AND WHY THAT IS THE TEST ══
 *
 * `ThreadPrimitive.ScrollToBottom` does not disappear at the bottom — it renders a `disabled`
 * button. Verified in the installed 0.15.17: `useThreadScrollToBottom` returns `null` when
 * `isAtBottom`, and `createActionButton` renders `<button disabled={props.disabled || !callback}>`.
 * A disabled control sitting in the reading line is what R64 refuses.
 *
 * So the assertions are about REACHABILITY — present or absent, and never carrying a real
 * `disabled` — rather than about visibility. That distinction is the whole reason the hook was
 * kept and the primitive's button dropped.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'

import ScrollToLatest, { scrollControlLabel } from '../ScrollToLatest'

const h = vi.hoisted(() => ({ isAtBottom: true, scrollToBottom: vi.fn() }))

// The viewport store, stubbed at the one selector this component reads. Mocking the module rather
// than mounting a whole runtime keeps this a test of the CONTROL: whether a thread scrolls is the
// library's business and is covered by its own suite.
vi.mock('@assistant-ui/react', () => ({
  useThreadViewport: (select: (s: { isAtBottom: boolean; scrollToBottom: unknown }) => unknown) =>
    select({ isAtBottom: h.isAtBottom, scrollToBottom: h.scrollToBottom }),
}))

afterEach(() => {
  cleanup()
  h.isAtBottom = true
  h.scrollToBottom.mockClear()
})

const control = () => screen.queryByTestId('scroll-to-latest')

describe('it is ABSENT at the bottom, not disabled', () => {
  it('renders nothing while the reader is already at the newest message', () => {
    h.isAtBottom = true
    const { container } = render(<ScrollToLatest isRunning={false} hasPendingOffer={false} />)
    expect(control()).toBeNull()
    // The mechanical form of "the library's button is not used": there is no element at all, so
    // there is nothing in the reading line to be disabled.
    expect(container.querySelector('[disabled]')).toBeNull()
    expect(container.innerHTML).toBe('')
  })

  it('appears once there is somewhere to go', () => {
    h.isAtBottom = false
    render(<ScrollToLatest isRunning={false} hasPendingOffer={false} />)
    expect(control()).toBeTruthy()
  })

  it('never carries a real `disabled`, in any state it can render in', () => {
    h.isAtBottom = false
    for (const [isRunning, hasPendingOffer] of [[false, false], [true, false], [false, true], [true, true]] as const) {
      const { container, unmount } = render(
        <ScrollToLatest isRunning={isRunning} hasPendingOffer={hasPendingOffer} />,
      )
      expect(container.querySelector('[disabled]')).toBeNull()
      expect(screen.getByTestId('scroll-to-latest')).toBeTruthy() // liveness for the sweep
      unmount()
    }
  })
})

describe('one control, three things to say', () => {
  it('names the offer above everything else (R29a)', () => {
    // When a pending offer has scrolled out of view this is how it stays reachable — which is what
    // lets there be NO second Build button anywhere else on the screen. The canvas's `Removals`
    // board is explicit that one never appears in the top bar.
    expect(scrollControlLabel(true, true)).toBe('Back to the plan waiting for you')
    expect(scrollControlLabel(false, true)).toBe('Back to the plan waiting for you')
  })

  it('says a reply is arriving while a turn runs (R35a)', () => {
    expect(scrollControlLabel(true, false)).toBe('A reply is arriving — jump to it')
  })

  it('is plain otherwise', () => {
    expect(scrollControlLabel(false, false)).toBe('Jump to the newest message')
  })

  it('renders the sentence it chose', () => {
    h.isAtBottom = false
    render(<ScrollToLatest isRunning hasPendingOffer={false} />)
    expect(control()?.textContent).toContain('A reply is arriving')
  })
})

describe('pressing it', () => {
  it('scrolls to the bottom, smoothly', () => {
    h.isAtBottom = false
    render(<ScrollToLatest isRunning={false} hasPendingOffer={false} />)
    control()?.click()
    expect(h.scrollToBottom).toHaveBeenCalledWith({ behavior: 'smooth' })
  })

  it('does not swallow pointer events across the whole strip', () => {
    // The wrapper spans the viewport so the pill can centre, and it must NOT eat clicks meant for
    // the transcript underneath it — only the button itself is interactive.
    h.isAtBottom = false
    const { container } = render(<ScrollToLatest isRunning={false} hasPendingOffer={false} />)
    expect((container.firstElementChild as HTMLElement).className).toContain('pointer-events-none')
    expect(control()?.className).toContain('pointer-events-auto')
  })
})
