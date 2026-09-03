/**
 * Is this element's text ACTUALLY clipped? Measured after layout, never guessed from length —
 * character heuristics are wrong at every breakpoint — and re-measured whenever the element
 * resizes, because a column that widens un-clips text that was clipped a moment ago.
 *
 * SHARED because `ProjectRow` and `ProjectCard` both needed it and used to carry byte-identical
 * copies (round-4 review) — a silent duplication, unlike `offset_pagination.py`'s honestly
 * disclosed `clean_page` copy on the backend.
 *
 * RE-MEASURES ON `document.fonts.ready` TOO, not just on resize. Manrope loads with
 * `display=swap`, so a measurement taken before the swap can pin `clipped` wrong for the
 * element's whole lifetime — and `ResizeObserver` does not fire for a content-only width
 * change with an unchanged box (the element's box does not move; only what fits inside it
 * does).
 */
import { useCallback, useEffect, useRef, useState } from 'react'

export function useClipped<T extends HTMLElement>(text: string | null) {
  const ref = useRef<T>(null)
  const [clipped, setClipped] = useState(false)

  const measure = useCallback(() => {
    const el = ref.current
    if (el) setClipped(el.scrollWidth > el.clientWidth)
  }, [])

  useEffect(() => {
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    if (ref.current) observer.observe(ref.current)
    return () => observer.disconnect()
  }, [measure, text])

  useEffect(() => {
    // Guarded rather than assumed: jsdom does not implement the Font Loading API at all in
    // some versions, and a real browser without it simply has no swap to chase.
    const ready = typeof document !== 'undefined' ? document.fonts?.ready : undefined
    if (!ready) return
    let cancelled = false
    ready.then(() => {
      if (!cancelled) measure()
    })
    return () => {
      cancelled = true
    }
  }, [measure])

  return { ref, clipped }
}
