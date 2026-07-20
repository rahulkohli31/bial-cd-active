// jsdom does not implement ResizeObserver. The assistant-ui Thread viewport
// (useSizeHandle, for auto-scroll-to-bottom tracking) calls `new ResizeObserver(...)`
// on mount, so any test that renders <Thread> needs at least a no-op stand-in.
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}

// jsdom also doesn't implement Element.scrollTo — Thread's auto-scroll-to-bottom
// effect (useThreadViewportAutoScroll) calls it on a rAF tick, which can fire
// after a test has already finished and torn down its render.
if (typeof Element !== 'undefined' && typeof Element.prototype.scrollTo !== 'function') {
  Element.prototype.scrollTo = function scrollTo() {}
}
