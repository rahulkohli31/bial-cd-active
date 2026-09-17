import { useCallback, useEffect, useRef, useState } from 'react'
import type { PointerEvent } from 'react'

/**
 * The navigation's three inputs, and the one answer they produce.
 *
 * THE RAIL IS THE RESTING STATE. The navigation is always on screen as a narrow column of icons
 * and grows to its labelled width when the pointer arrives — so the destinations are never gone,
 * only quiet. That is the shape the owner asked for, and it replaces the previous split where a
 * list route carried a permanently docked panel with no way to close it and an application route
 * carried no panel at all.
 *
 * COLLAPSE IS A CONCLUSION, NOT A STATE. It is derived from three independent facts rather than
 * stored, because storing it means three writers racing to set one boolean:
 *
 *   - `pinned`   — a deliberate preference, and the only one that survives a reload.
 *   - `hovered`  — the pointer is over the navigation.
 *   - `menuOpen` — a menu the navigation opened is on screen. It PORTALS OUT of the nav, so the
 *                  pointer moving to it reads as a pointer LEAVING, and without this latch the
 *                  rail collapses out from under the menu the person is reaching for.
 *
 * THE MENU LATCH HOLDS THE WIDTH IT FOUND, IT DOES NOT FORCE THE PANEL OPEN. Written as a third
 * term in the OR, it did the latter: pressing the profile avatar on a collapsed rail swept the
 * whole panel open behind the menu, which is a lot of movement to answer a click that was aimed
 * at one control. Opening a menu is not a request to see the navigation — it is a request to see
 * the menu — so the width freezes at whatever it was when the menu opened and thaws when it
 * closes.
 */

const PIN_KEY = 'bial:nav-pinned'

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
    return window.localStorage.getItem(PIN_KEY) === '1'
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
    onPointerEnter: (event: PointerEvent<HTMLElement>) => void
    onPointerLeave: () => void
  }
  /** Handed to any menu the navigation opens, so reaching for it does not close it. */
  setMenuOpen: (open: boolean) => void
}

export function useNavRail(): NavRail {
  const [pinned, setPinned] = useState(readPin)
  const [hovered, setHovered] = useState(false)
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

  const onPointerEnter = useCallback(
    (event: PointerEvent<HTMLElement>) => {
      clearClose()
      // ARRIVING AT THE PROFILE IS NOT ARRIVING AT THE NAVIGATION. The avatar sits inside the
      // rail, so reaching it means crossing the rail — which opened the whole panel on the way to
      // a control that was already visible and already pressable. Entering THROUGH the quiet zone
      // leaves the width alone.
      //
      // Only the ENTRY is filtered, never a later move: this fires once, when the pointer crosses
      // into the aside. A pointer that came in over the destinations and then travelled down to
      // the profile has already opened the panel and keeps it open, because this does not run
      // again on the way down.
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

  const togglePin = useCallback(() => {
    setPinned((was) => {
      const next = !was
      try {
        window.localStorage.setItem(PIN_KEY, next ? '1' : '0')
      } catch {
        // The preference is lost on reload; the toggle still works for this visit.
      }
      // Unpinning under a pointer that is still over the nav must not slam it shut — the
      // pointer is there, so `hovered` keeps it open until the pointer actually leaves.
      return next
    })
  }, [])

  const live = !pinned && !hovered

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
    hoverProps: { onPointerEnter, onPointerLeave },
    setMenuOpen,
  }
}

export const NAV_RAIL_PX = 56
export const NAV_PANEL_PX = 248
