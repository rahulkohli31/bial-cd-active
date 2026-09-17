import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { Menu } from 'lucide-react'
import NavPanel from './NavPanel'
import {
  DURATION,
  EDGE_ZONE_PX,
  GRACE_MS,
  LAYOUT_EASE,
  NAV_WIDTH_PX,
  REST_MS,
  STACKED_BELOW_PX,
  SUMMON_SPRING,
} from '../../lib/motion'

/**
 * THE NAVIGATION INSIDE AN APPLICATION: not on screen at all, and three gestures bring it back.
 *
 * A menu button in the toolbar is the PRIMARY route and the only visible one. Navigation that can
 * only be found by hovering an invisible strip measurably costs discoverability, so the edge zone
 * and `⌘\` are accelerators for people who learn them, never the only door.
 *
 * THE TIMINGS ARE HOVER-INTENT FIGURES, NOT TASTE (`lib/motion.ts` holds them). The chat panel
 * begins at the very edge this zone sits on, so the pointer crosses it constantly: opening on
 * contact would open it by accident several times a minute, and the grace period on the way out
 * is why a wobble of the hand does not slam it shut.
 *
 * IT ARRIVES OVER THE CHAT AND NOTHING REFLOWS, which is what makes it safe to summon
 * mid-sentence — the panes keep their measured widths and the message being read does not move.
 * Pinning is the one case that hands width back, and it does so as ONE shared layout move.
 *
 * THE EDGE ZONE IS NOT INSTALLED WHEN THE CHAT IS HIDDEN, and that is a condition on adding the
 * listener rather than a branch inside it. An 8px strip sitting over someone's application
 * waiting to be rested on is exactly what that layout exists to avoid; the button and the
 * shortcut still work.
 *
 * WHILE HIDDEN THE ZONE MUST NOT SWALLOW CLICKS. It is `pointer-events: none` until a pointer is
 * resting in it, so the chat's own left margin stays clickable through it.
 */

type RevealState = 'hidden' | 'reaching' | 'open'

interface NavRevealValue {
  /** True on the routes where the navigation is hidden rather than docked. */
  hideable: boolean
  open: boolean
  pinned: boolean
  openNav: () => void
  closeNav: () => void
  toggleNav: () => void
  togglePin: () => void
  /** Reported by the workspace: with the chat away, the edge zone is not installed at all. */
  setChatHidden: (hidden: boolean) => void
}

const NavRevealContext = createContext<NavRevealValue | null>(null)

/** The reveal controls, for the toolbar's menu button. Null outside a hideable route. */
export function useNavReveal(): NavRevealValue | null {
  return useContext(NavRevealContext)
}

/**
 * Below the stacking threshold the pointer is not the input device: no edge zone exists, and the
 * panel is a drawer. Read here rather than from a Tailwind class because three behaviours branch
 * on it, not just a style — and a class cannot tell a listener not to install itself.
 */
export function useStackedViewport(): boolean {
  const [stacked, setStacked] = useState(
    () => typeof window !== 'undefined' && window.innerWidth < STACKED_BELOW_PX,
  )
  useEffect(() => {
    const query = window.matchMedia(`(max-width: ${STACKED_BELOW_PX - 1}px)`)
    const sync = () => setStacked(query.matches)
    sync()
    query.addEventListener('change', sync)
    return () => query.removeEventListener('change', sync)
  }, [])
  return stacked
}

/**
 * The visible, primary route to the navigation — the one gesture that does not have to be
 * learned. It opens with NO DELAY: the 300ms rest belongs to the edge zone alone, where it exists
 * to tell intent from a pass-by. A button press is already the intent.
 */
export function NavMenuButton({ className = '' }: { className?: string }) {
  const reveal = useNavReveal()
  if (!reveal) return null
  return (
    <button
      type="button"
      onClick={reveal.toggleNav}
      data-nav-door=""
      data-testid="nav-menu-button"
      aria-expanded={reveal.open}
      aria-label="Open the navigation"
      title="Navigation · ⌘\"
      className={`flex items-center justify-center rounded-md p-1.5 text-primary-900 transition hover:bg-surface-muted outline-none focus-visible:ring-2 focus-visible:ring-primary ${className}`}
    >
      <Menu size={16} />
    </button>
  )
}

const PIN_KEY = 'bial:nav-pinned'

/** Remembered per person, in the same spirit as the rail width: a preference about a screen, not
 *  a property of an application. Throw-wrapped because `localStorage` genuinely throws (Safari
 *  private mode, blocked site data) and a preference nobody can save must not fail a render. */
function readPinned(): boolean {
  try {
    return window.localStorage.getItem(PIN_KEY) === '1'
  } catch {
    return false
  }
}

function writePinned(pinned: boolean): void {
  try {
    window.localStorage.setItem(PIN_KEY, pinned ? '1' : '0')
  } catch {
    // A preference that cannot be stored is not a failure worth showing anybody.
  }
}

interface Props {
  /** False on the list routes, where the panel is docked and none of this applies. */
  hideable: boolean
  children: ReactNode
}

export default function NavReveal({ hideable, children }: Props) {
  const [state, setState] = useState<RevealState>('hidden')
  const [pinned, setPinned] = useState(readPinned)
  const [chatHidden, setChatHidden] = useState(false)
  const stacked = useStackedViewport()
  const restTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const graceTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Where focus was when the panel opened, so `Esc` can put it back — which is the composer in
  // almost every case, and a keyboard user dumped on `<body>` has lost their place entirely.
  const returnFocusTo = useRef<HTMLElement | null>(null)
  // Only `Esc` asks for the restore. A panel that closed because the pointer wandered off did
  // not take anybody's focus with it, and moving the caret on them would be the rude half of a
  // courtesy.
  const restoreOnExit = useRef(false)

  /**
   * HOW THIS PANEL CAME TO BE OPEN, which decides how lightly it may leave.
   *
   * A panel summoned by resting at the screen edge is a GUESS at intent, and it withdraws when
   * the pointer moves away — that is the whole bargain of the gesture. A panel opened by pressing
   * the button or the shortcut is not a guess, and dismissing it because the pointer drifted
   * toward the work is the product taking back something a person asked for. Both used the same
   * pointer-leave, so an intentional open closed itself 400ms after the press.
   */
  const openedBy = useRef<'gesture' | 'deliberate'>('gesture')
  const panelRef = useRef<HTMLElement | null>(null)

  const clearTimers = useCallback(() => {
    if (restTimer.current) { clearTimeout(restTimer.current); restTimer.current = null }
    if (graceTimer.current) { clearTimeout(graceTimer.current); graceTimer.current = null }
  }, [])

  /** Where focus was at the moment the panel opened — whichever gesture opened it. */
  const rememberFocus = useCallback(() => {
    if (document.activeElement instanceof HTMLElement) {
      returnFocusTo.current = document.activeElement
    }
  }, [])

  const openNav = useCallback(() => {
    clearTimers()
    // ONLY WHAT WAS FOCUSED BEFORE THE PANEL OPENED. `openNav` is also what a navigation item's
    // own focus calls, to hold the panel open while the keyboard is inside it — so recording
    // unconditionally would overwrite the citizen's real place with a panel item that is about to
    // be unmounted, and `Esc` would hand focus to nothing.
    if (state !== 'open') {
      rememberFocus()
      openedBy.current = 'deliberate'
    }
    setState('open')
  }, [clearTimers, rememberFocus, state])

  const closeNav = useCallback(() => {
    clearTimers()
    setState('hidden')
  }, [clearTimers])

  const togglePin = useCallback(() => {
    setPinned((was) => {
      writePinned(!was)
      return !was
    })
    // THE FLOAT IS SETTLED HERE, NOT LEFT RUNNING UNDERNEATH. Pinning is reached from inside the
    // floating panel, so without this the reveal stays "open" behind the docked one — invisible,
    // and waiting: the next unpin would drop a floating panel over the application, unasked.
    closeNav()
  }, [closeNav])

  // EVERY DOOR GOES THROUGH `openNav`, which is what records where focus was. Escape's promise is
  // to put a keyboard user back where they were, and they are as likely to have opened the panel
  // with the button or the shortcut as by tabbing into it — a door that opened by setting the
  // state directly would return them to `<body>`.
  //
  // While the panel is docked there is nothing to reveal, so the toggle undocks — the same answer
  // `⌘\` already gives, rather than a button that appears to do nothing.
  const toggleNav = useCallback(() => {
    if (pinned) { togglePin(); return }
    if (state === 'open') closeNav()
    else openNav()
  }, [pinned, togglePin, state, closeNav, openNav])

  useEffect(() => () => clearTimers(), [clearTimers])

  // `⌘\` / `Ctrl+\` from anywhere inside an application.
  useEffect(() => {
    if (!hideable) return undefined
    const onKey = (e: KeyboardEvent) => {
      if (e.key === '\\' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        toggleNav()
        return
      }
      if (e.key === 'Escape' && state === 'open') {
        // ASK FOR THE RESTORE, DO NOT PERFORM IT HERE. The panel is still on screen for the
        // length of its exit, and focus may be inside it — calling `focus()` now hands it back
        // for a moment and then loses it again to `<body>` when the panel is removed underneath.
        // `onExitComplete` is the only point at which the panel is genuinely gone.
        restoreOnExit.current = true
        closeNav()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [hideable, state, toggleNav, closeNav])

  // THE EDGE ZONE'S LISTENER, INSTALLED CONDITIONALLY. Not installed at all when the navigation
  // is docked, when it is pinned, when the chat is hidden, or below the stacking threshold —
  // four different reasons the gesture should not exist, all answered by not listening rather
  // than by branching inside a handler that has already fired.
  /**
   * A PRESS OUTSIDE CLOSES IT, which is the gesture people reach for first and the one this panel
   * did not answer. Above the stacking threshold there is no backdrop — deliberately, so the panel
   * never swallows a click meant for the work underneath — and with nothing else listening, a
   * click away simply landed and left the panel sitting over the application.
   *
   * A LISTENER RATHER THAN A BACKDROP, so the press still reaches whatever it was aimed at. The
   * door is excluded by name: it toggles on click, and letting this close on the preceding
   * pointerdown would make the button reopen what it was pressed to close.
   */
  useEffect(() => {
    if (!hideable || state !== 'open') return undefined
    const onPress = (e: PointerEvent) => {
      const target = e.target instanceof Element ? e.target : null
      if (target === null) return
      if (panelRef.current?.contains(target) === true) return
      if (target.closest('[data-nav-door]') !== null) return
      closeNav()
    }
    document.addEventListener('pointerdown', onPress)
    return () => document.removeEventListener('pointerdown', onPress)
  }, [hideable, state, closeNav])

  const zoneLive = hideable && !pinned && !chatHidden && !stacked

  // AND A REACH ALREADY UNDER WAY IS ABANDONED WITH IT. Dropping the listener stops new intent
  // but not a timer already armed — so a pointer resting at the edge when the chat collapses, or
  // when the pin goes down, would still open the panel a moment later over a screen that no
  // longer has an edge to reach from.
  useEffect(() => {
    if (!zoneLive) clearTimers()
  }, [zoneLive, clearTimers])

  useEffect(() => {
    if (!zoneLive) return undefined
    const onMove = (e: PointerEvent) => {
      const inZone = e.clientX <= EDGE_ZONE_PX
      if (inZone) {
        if (restTimer.current || state === 'open') return
        setState('reaching')
        restTimer.current = setTimeout(() => {
          restTimer.current = null
          openedBy.current = 'gesture'
          // THIS DOOR RECORDS WHERE FOCUS WAS TOO. Escape arms the restore for ANY open, so a
          // panel summoned at the edge without recording anything left the restore pointing at
          // whatever the last BUTTON press had stored — and Escape then pulled the caret out of
          // the composer and put it on a control the person never touched.
          rememberFocus()
          setState('open')
        }, REST_MS)
        return
      }
      // A pointer that crosses without stopping opens nothing: the rest timer is cancelled the
      // moment it leaves, which is the whole difference between intent and a pass-by.
      if (restTimer.current) {
        clearTimeout(restTimer.current)
        restTimer.current = null
        setState((was) => (was === 'reaching' ? 'hidden' : was))
      }
    }
    document.addEventListener('pointermove', onMove)
    return () => document.removeEventListener('pointermove', onMove)
  }, [zoneLive, state, rememberFocus])

  const holdOpen = useCallback(() => {
    if (graceTimer.current) { clearTimeout(graceTimer.current); graceTimer.current = null }
  }, [])

  const leaveWithGrace = useCallback(() => {
    // Nothing to withdraw: this one was asked for.
    if (openedBy.current === 'deliberate') return
    if (graceTimer.current) clearTimeout(graceTimer.current)
    graceTimer.current = setTimeout(() => {
      graceTimer.current = null
      setState('hidden')
    }, GRACE_MS)
  }, [])

  // PINNED IS A LAYOUT CHANGE, NOT A FLOAT: the panel joins the row rather than covering it.
  const docked = hideable && pinned && !stacked

  const value = useMemo<NavRevealValue>(
    () => ({
      hideable,
      // WHETHER A PANEL IS ON SCREEN, not whether the floating one is. A docked panel is as
      // present as a floating one, and the menu button's `aria-expanded` is read aloud — reporting
      // "collapsed" over a panel the reader can see is the one answer that is simply false.
      open: docked || state === 'open',
      pinned,
      openNav,
      closeNav,
      toggleNav,
      togglePin,
      setChatHidden,
    }),
    [hideable, docked, state, pinned, openNav, closeNav, toggleNav, togglePin],
  )

  const panel = (
    <NavPanel
      pinned={pinned}
      onTogglePin={togglePin}
      onNavigate={closeNav}
      onItemFocus={openNav}
    />
  )

  return (
    <NavRevealContext.Provider value={value}>
      {/*
        ONE TREE SHAPE FOR BOTH STATES, AND THE ROW IS ALWAYS THE ROW. React reconciles by position
        and element type, so a docked branch that returned a root of its own would unmount the whole
        subtree beside it on every pin — and what is beside it is a running application whose
        preview is an iframe. An iframe React rebuilds does not resume; it reloads from its `src`.
        The panel joins and leaves the row; the work never moves out of it.
      */}
      <div className="flex h-screen overflow-hidden">
        {/* Not when stacked: below the threshold there is no room for a docked 248px column beside
            the work, so the preference is honoured where it means something and ignored where it
            cannot be. */}
        {docked && (
          <motion.aside
            layout
            transition={{ duration: DURATION.layout, ease: LAYOUT_EASE }}
            className="h-full shrink-0 border-r border-bial-border"
            data-testid="nav-docked"
          >
            {panel}
          </motion.aside>
        )}
        <div className="min-w-0 flex-1">{children}</div>
      </div>
      {zoneLive && (
        // The strip itself is decoration; the listener above is the mechanism. It is
        // `pointer-events-none` throughout so it can never take a click that belonged to the
        // chat's left margin underneath it.
        <motion.div
          aria-hidden="true"
          data-testid="nav-edge-zone"
          className="pointer-events-none fixed inset-y-0 left-0 z-30"
          style={{ width: EDGE_ZONE_PX }}
          animate={{ opacity: state === 'reaching' ? 1 : 0 }}
          transition={{ duration: DURATION.edge, ease: 'easeOut' }}
        >
          <div className="h-full w-full bg-gradient-to-r from-primary/15 to-transparent" />
        </motion.div>
      )}
      <AnimatePresence
        onExitComplete={() => {
          if (!restoreOnExit.current) return
          restoreOnExit.current = false
          returnFocusTo.current?.focus()
        }}
      >
        {hideable && !docked && state === 'open' && (
          <>
            {stacked && (
              <motion.div
                key="nav-backdrop"
                data-testid="nav-backdrop"
                className="fixed inset-0 z-40 bg-black/30"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: DURATION.backdrop }}
                onClick={closeNav}
              />
            )}
            <motion.aside
              key="nav-floating"
              ref={panelRef}
              data-testid="nav-floating"
              className="fixed inset-y-0 left-0 z-50 border-r border-bial-border shadow-2xl"
              style={{ width: NAV_WIDTH_PX }}
              initial={{ x: -NAV_WIDTH_PX, opacity: 0 }}
              animate={{ x: 0, opacity: 1 }}
              exit={{ x: -NAV_WIDTH_PX, opacity: 0, transition: { duration: DURATION.panelOut, ease: 'easeIn' } }}
              transition={SUMMON_SPRING}
              onPointerEnter={holdOpen}
              onPointerLeave={leaveWithGrace}
              onFocusCapture={holdOpen}
            >
              {panel}
            </motion.aside>
          </>
        )}
      </AnimatePresence>
    </NavRevealContext.Provider>
  )
}
