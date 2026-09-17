import { useCallback, useEffect, useRef, useState } from 'react'
import type { FocusEvent, PointerEvent } from 'react'

/**
 * The navigation's inputs, and the one answer they produce.
 *
 * COLLAPSE IS DERIVED, NOT STORED. Three writers racing to set one boolean is how a panel ends up
 * disagreeing with itself. `pinned` is a deliberate preference and the only one that survives a
 * reload; `hovered` and `focused` each say the person is at the navigation right now, and the
 * second is what keeps it reachable without a pointer.
 *
 * `menuOpen` FREEZES THE WIDTH RATHER THAN FORCING IT OPEN. A menu the navigation opens portals
 * out of it, so the pointer moving to that menu reads as a pointer LEAVING, and without the latch
 * the rail collapses out from under the menu being reached for. Opening a menu is a request to
 * see the menu, not the navigation, so the width holds at whatever it already was.
 */

/**
 * The rail's own pin, deliberately NOT the key `NavReveal` uses for its floating panel.
 *
 * One key for both meant pinning the rail on a list route also docked a 248px column beside the
 * framed application on every application route — two different preferences about two different
 * screens, collapsed into one switch nobody asked to throw.
 */
export const NAV_RAIL_PIN_KEY = 'bial:nav-rail-pinned'

/**
 * How long the rail waits before closing behind a pointer that has left.
 *
 * Zero is the obvious value and the wrong one: the pointer crosses the boundary on its way to
 * anything else on the page, and a rail that snaps shut on that crossing reads as twitchy. Long
 * enough to forgive a pass-through, short enough that a deliberate exit still feels immediate.
 */
const CLOSE_DELAY_MS = 150

function readPin(): boolean {
  try {
    return window.localStorage.getItem(NAV_RAIL_PIN_KEY) === '1'
  } catch {
    // A browser with storage denied still gets a working navigation, unpinned.
    return false
  }
}

export interface NavRail {
  /** Icons only. The panel's width, its labels and the profile row all read this. */
  collapsed: boolean
  pinned: boolean
  togglePin: () => void
  /** Spread onto the navigation's own element — the hit area IS the rail. */
  hoverProps: {
    onPointerOver: (event: PointerEvent<HTMLElement>) => void
    onPointerLeave: () => void
  }
  /** Spread onto the same element. Without it the rail has no keyboard path: its labels and its
   *  pin only exist while expanded, and only the pointer could expand it. */
  focusProps: {
    onFocusCapture: () => void
    onBlurCapture: (event: FocusEvent<HTMLElement>) => void
  }
  /** Handed to any menu the navigation opens, so reaching for it does not close it. */
  setMenuOpen: (open: boolean) => void
}

export function useNavRail(): NavRail {
  const [pinned, setPinned] = useState(readPin)
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  /** The width the menu froze, or `null` when no menu is open. */
  const [frozen, setFrozen] = useState<boolean | null>(null)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const clearClose = useCallback(() => {
    if (closeTimer.current !== null) {
      clearTimeout(closeTimer.current)
      closeTimer.current = null
    }
  }, [])

  useEffect(() => clearClose, [clearClose])

  const onPointerOver = useCallback(
    (event: PointerEvent<HTMLElement>) => {
      clearClose()
      // ARRIVING AT THE PROFILE IS NOT ARRIVING AT THE NAVIGATION. The avatar sits inside the
      // rail, so reaching it means crossing the rail — which opened the whole panel on the way to
      // a control that was already visible and already pressable.
      //
      // THIS IS `over`, NOT `enter`, AND THAT IS THE WHOLE POINT. `enter` fires once, on the way
      // in, so filtering it left the rail stuck collapsed for as long as the pointer stayed
      // inside: the move from the profile UP to the destinations never reached the handler.
      // `over` bubbles on every element transition within the rail, so the same test re-asks the
      // question on each move and the panel opens the moment the pointer is somewhere that wants
      // it open.
      if (event.target instanceof Element && event.target.closest('[data-nav-quiet]') !== null) {
        return
      }
      setHovered(true)
    },
    [clearClose],
  )

  const onPointerLeave = useCallback(() => {
    clearClose()
    closeTimer.current = setTimeout(() => setHovered(false), CLOSE_DELAY_MS)
  }, [clearClose])

  const onFocusCapture = useCallback(() => {
    clearClose()
    setFocused(true)
  }, [clearClose])

  const onBlurCapture = useCallback((event: FocusEvent<HTMLElement>) => {
    // Focus moving between two controls INSIDE the rail is not focus leaving it, and closing on
    // that would shut the panel under the very key that walked into it.
    if (event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget)) {
      return
    }
    setFocused(false)
  }, [])

  const togglePin = useCallback(() => {
    setPinned((was) => {
      const next = !was
      try {
        window.localStorage.setItem(NAV_RAIL_PIN_KEY, next ? '1' : '0')
      } catch {
        // The preference is lost on reload; the toggle still works for this visit.
      }
      // Unpinning under a pointer that is still over the nav must not slam it shut — the
      // pointer is there, so `hovered` keeps it open until the pointer actually leaves.
      return next
    })
  }, [])

  const live = !pinned && !hovered && !focused

  const setMenuOpen = useCallback(
    (open: boolean) => {
      // Read the width at the moment of opening and hold it. `live` is this render's value,
      // which is the one the person was looking at when they pressed.
      setFrozen(open ? live : null)
    },
    [live],
  )

  return {
    collapsed: frozen ?? live,
    pinned,
    togglePin,
    hoverProps: { onPointerOver, onPointerLeave },
    focusProps: { onFocusCapture, onBlurCapture },
    setMenuOpen,
  }
}
