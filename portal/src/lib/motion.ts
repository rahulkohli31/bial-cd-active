/**
 * The portal's animation vocabulary — TWO CURVES, AND NOTHING LASTS LONGER THAN 260ms.
 *
 * `NavMotion.dc.html` is the specification and it allows exactly two: a spring for anything a
 * person summons on purpose, and one ease for whatever the layout does in response. They live
 * here rather than on each component so eleven consumers cannot drift into eleven dialects —
 * the same reason `devices.ts` is a leaf module read by two consumers rather than two lists.
 *
 * THE NUMBERS ARE ESTABLISHED HOVER-INTENT FIGURES, NOT TASTE. The chat panel begins at the very
 * left edge, so the pointer crosses the reveal zone constantly: anything shorter than `REST_MS`
 * opens the navigation by accident, anything longer reads as broken. `GRACE_MS` is why a wobble
 * of the hand does not slam it shut.
 *
 * REDUCED MOTION IS NOT HANDLED HERE. A root `MotionConfig reducedMotion="user"` in `main.tsx`
 * covers every consumer of this module at once; a per-component branch is the shape that got
 * written three times and was wrong three times (`index.css`'s guarantee docblock records it).
 */

/** Anything a person summons on purpose: the panel's slide, the drawer. ~260ms, no bounce. */
export const SUMMON_SPRING = { type: 'spring', stiffness: 320, damping: 30 } as const

/** Whatever the layout does in response: the pin's shared move, the chat's width. */
export const LAYOUT_EASE = [0.32, 0.72, 0, 1] as const

/** Durations, in seconds — `motion` takes seconds, the board is written in milliseconds. */
export const DURATION = {
  /** The edge answering the pointer. Fast enough to read as a response to the hand. */
  edge: 0.1,
  /** The panel leaving. Faster out than in. */
  panelOut: 0.18,
  /** The drawer's backdrop. */
  backdrop: 0.16,
  /** A layout change the person asked for: the pin, the chat's width. */
  layout: 0.24,
  /** The active highlight travelling between navigation items. */
  highlight: 0.2,
} as const

/** How long the pointer must rest in the edge zone before the panel opens. */
export const REST_MS = 300

/** How long the panel stays after the pointer leaves, so a wobble does not close it. */
export const GRACE_MS = 400

/** The reveal zone's width. Pointer-transparent until a pointer rests in it. */
export const EDGE_ZONE_PX = 8

/** The navigation's labelled width, wherever it is drawn — floating over an application, or
 *  docked and expanded on a list route. Named once because both forms must agree. */
export const NAV_WIDTH_PX = 248

/** The navigation's resting width on the list routes: icons, no labels. */
export const NAV_RAIL_PX = 56

/**
 * Below this the pointer is not the input device, so there is no hover zone at all and the panel
 * is a drawer over a dimmed backdrop. Tailwind's own `md` breakpoint, named once here because
 * three files branch on it and a second copy is how two of them end up disagreeing.
 */
export const STACKED_BELOW_PX = 768

export const layoutTransition = { duration: DURATION.layout, ease: LAYOUT_EASE } as const
export const highlightTransition = { duration: DURATION.highlight, ease: LAYOUT_EASE } as const
