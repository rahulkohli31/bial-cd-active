/**
 * FOLLOWING THE NEWEST CONTENT, AND THE WAY BACK TO IT (R35a, R29a).
 *
 * ══ WHAT THIS REPLACES ══
 *
 * The old transcript was pinned by brute force: a sentinel `<div>` and a
 * `scrollIntoView({behavior:'smooth'})` on EVERY `[messages]` change. That is what made the build
 * bubble read as pinned, and it also dragged a reader who had scrolled up back to the bottom on
 * every single delta — so reading the middle of a long build was impossible. It is gone. The
 * thread's own viewport ships auto-scroll with a bottom-proximity check, and this is one of the
 * places the library genuinely replaces our code.
 *
 * ══ WHY THE LIBRARY'S OWN BUTTON IS NOT USED ══
 *
 * `ThreadPrimitive.ScrollToBottom` does NOT disappear at the bottom — it renders a `disabled`
 * button. Verified: `useThreadScrollToBottom` returns `null` when `isAtBottom`, and
 * `createActionButton` renders `<button disabled={props.disabled || !callback}>`. A disabled
 * control sitting in the reading line is exactly what R64 refuses. So the hook is kept and the
 * primitive's button is dropped: the control is rendered ONLY while there is somewhere to go, and
 * the tests assert reachability — present or absent, and never carrying a real `disabled` —
 * rather than visibility.
 *
 * ══ ONE CONTROL, TWO REASONS TO PRESS IT ══
 *
 * R35a: while a turn runs and the reader is scrolled up, it says a reply is arriving.
 * R29a: when a pending offer's message has scrolled out of view, it names the offer and takes the
 *       reader back to it — so the offer stays reachable WITHOUT a second way to start a build
 *       appearing somewhere else on the screen. The canvas's `Removals` board is explicit that no
 *       second Build button exists anywhere, least of all in the top bar.
 */
import type { FC } from 'react'
import { ArrowDown } from 'lucide-react'
import { useThreadViewport } from '@assistant-ui/react'

export interface ScrollToLatestProps {
  /** A turn is running — the control says a reply is arriving (R35a's second clause). */
  isRunning: boolean
  /** A pending offer exists and is out of view — the control names it (R29a). */
  hasPendingOffer: boolean
}

/** The three things it can say, in the platform's register. */
export function scrollControlLabel(isRunning: boolean, hasPendingOffer: boolean): string {
  if (hasPendingOffer) return 'Back to the plan waiting for you'
  if (isRunning) return 'A reply is arriving — jump to it'
  return 'Jump to the newest message'
}

const ScrollToLatest: FC<ScrollToLatestProps> = ({ isRunning, hasPendingOffer }) => {
  const isAtBottom = useThreadViewport((s) => s.isAtBottom)
  const scrollToBottom = useThreadViewport((s) => s.scrollToBottom)

  // Rendered only while there is somewhere to go. Not `disabled` at the bottom — ABSENT.
  if (isAtBottom) return null

  const label = scrollControlLabel(isRunning, hasPendingOffer)

  return (
    <div className="pointer-events-none flex justify-center pb-2">
      <button
        type="button"
        onClick={() => scrollToBottom({ behavior: 'smooth' })}
        data-testid="scroll-to-latest"
        className="pointer-events-auto inline-flex items-center gap-1.5 rounded-full border border-bial-border bg-white px-3 py-1.5 text-xs font-semibold text-tertiary shadow-sm transition hover:border-primary hover:text-primary"
      >
        <ArrowDown size={13} aria-hidden="true" />
        {label}
      </button>
    </div>
  )
}

export default ScrollToLatest
