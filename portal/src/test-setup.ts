/**
 * THE ONE PLACE THE TEST ENVIRONMENT IS TAUGHT WHAT JSDOM DOES NOT IMPLEMENT.
 *
 * Before this file the portal had NO `setupFiles` key at all, and the gap showed: `scrollIntoView`
 * was stubbed per-file in seventeen files, `MarketplacePage.test.tsx` stubbed all four of Radix
 * Select's requirements with a comment saying the shim lived there *because there was nowhere
 * else to put it*, and `ResizeObserver`, `IntersectionObserver` and `navigator.clipboard` had zero
 * occurrences anywhere in `src/` — not because nothing needed them, but because nothing that
 * needed them could be rendered at all.
 *
 * ── WHAT IS DELIBERATELY NOT DONE HERE ──
 *
 * The seventeen per-file `scrollIntoView` stubs are NOT removed. Removing them is a mechanical
 * sweep with one real risk — a file that relied on a differently-shaped stub — and folding it in
 * here would mean a red suite could be either this file or that sweep. The blast radius of adding
 * `setupFiles` is kept to "things that were previously impossible".
 *
 * ── EVERY DEFAULT IS TODAY'S BEHAVIOUR ──
 *
 * A shim that changes what tests observe is a shim that rewrites the suite. `matchMedia` reports
 * `matches: false`, so `usePrefersReducedMotion` keeps returning "animate" exactly as it does now
 * when the function is absent entirely. The observers do nothing rather than firing synthetic
 * callbacks. Nothing here makes an assertion pass that would otherwise fail.
 */
// No `jest-dom` import: the suite asserts with plain vitest matchers throughout, and adding the
// package here would put a new dependency in front of every one of the 103 existing test files
// to serve none of them.
import { beforeEach, vi } from 'vitest'

// ── Observers ────────────────────────────────────────────────────────────────────────────────
// jsdom implements neither. assistant-ui's thread viewport uses IntersectionObserver for its
// at-bottom check and ResizeObserver for auto-grow; Radix uses ResizeObserver for positioning.
// Both are inert on purpose: a shim that synthesised entries would be inventing layout in an
// environment that has none, and every test reading a real at-bottom state would be reading a
// number this file made up.
class InertObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): [] {
    return []
  }
}

Object.defineProperty(globalThis, 'ResizeObserver', {
  writable: true,
  configurable: true,
  value: InertObserver,
})

Object.defineProperty(globalThis, 'IntersectionObserver', {
  writable: true,
  configurable: true,
  value: class extends InertObserver {
    readonly root = null
    readonly rootMargin = ''
    readonly thresholds: readonly number[] = []
  },
})

// ── matchMedia ───────────────────────────────────────────────────────────────────────────────
// `removeEventListener` is as load-bearing as `addEventListener`: `usePrefersReducedMotion`
// subscribes on mount and unsubscribes on unmount, so a shim missing the second throws during
// CLEANUP — which surfaces as an unrelated later test failing, the least diagnosable shape there
// is. The legacy `addListener`/`removeListener` pair is included because Radix and older
// libraries still feature-detect on it.
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  configurable: true,
  value: (query: string): MediaQueryList =>
    ({
      media: query,
      matches: false,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList,
})

// ── Element methods jsdom leaves undefined ───────────────────────────────────────────────────
// Radix Select calls all four while opening: it captures the pointer to track a drag-select, and
// scrolls the highlighted item into view. Without them the component throws before it renders,
// which is why three test files stub them by hand today. Plan F adds a Radix Select to the
// history filter; three lines here are the difference between that landing and its implementer
// re-deriving a per-file stub from a comment that will by then be out of date.
Element.prototype.scrollIntoView ??= function scrollIntoView() {}
Element.prototype.hasPointerCapture ??= function hasPointerCapture() {
  return false
}
Element.prototype.setPointerCapture ??= function setPointerCapture() {}
Element.prototype.releasePointerCapture ??= function releasePointerCapture() {}
// The FIFTH, added when the thread's viewport arrived (U17): `useThreadViewportAutoScroll` calls
// `scrollTo` from a `requestAnimationFrame` callback, so its absence surfaces as an UNCAUGHT
// exception rather than a failing assertion — the test that provoked it has usually already
// passed, and the message names a library file nobody edited. jsdom implements `scrollTo` on
// `window` but not on `Element`, which is exactly the shape of gap this file exists to close.
Element.prototype.scrollTo ??= function scrollTo() {}

// ── Clipboard ────────────────────────────────────────────────────────────────────────────────
// A SPY, not a stub, and reset between tests. N1's copy button is the only consumer and its
// failure path is a requirement, not a nicety: clipboard writes reject on insecure origins and
// under a denied permission, and R65 says the citizen is told. A shim that can only succeed
// makes the half that matters untestable.
//
// It is re-installed in `beforeEach` rather than once at module scope because a test that calls
// `mockRejectedValueOnce` and then throws would otherwise leave the rejection armed for whichever
// test ran next.
beforeEach(() => {
  Object.defineProperty(navigator, 'clipboard', {
    writable: true,
    configurable: true,
    value: {
      writeText: vi.fn<(text: string) => Promise<void>>().mockResolvedValue(undefined),
      readText: vi.fn<() => Promise<string>>().mockResolvedValue(''),
    },
  })
})
