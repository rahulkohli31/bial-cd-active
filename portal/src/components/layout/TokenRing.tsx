import { motion } from 'motion/react'
import type { UsageToday } from '../../utils/usage'
import { DURATION, LAYOUT_EASE } from '../../lib/motion'

/**
 * The daily token budget, as a filling ring with the figures beside it.
 *
 * THE FIGURES ARE THE REQUIREMENT; THE RING IS THE SHAPE. The client asked for a counter that
 * never stops being visible, so used-of-limit stays written out in full next to the arc — the
 * arc is how it reads at a glance, not a replacement for the numbers.
 *
 * TWO COLOURS, NO INTERMEDIATE THRESHOLD. Amber at any remaining budget, red once it is spent.
 * `NavStates.dc.html` draws 54% and 93% both amber and only 100% red, and the old meter's 80%
 * "nearing" branch is what that contradicts: amber IS the meter, and danger is kept for a budget
 * that is actually gone.
 *
 * THE ARC IS `stroke-dasharray` ON A ROTATED CIRCLE, which is what makes it animatable by a
 * single number and readable at 38px. Reduced motion is the root `MotionConfig`'s job, not this
 * component's — under the preference the arc lands on its final value without sweeping.
 */

/** The board's geometry: a 42-unit box, r=17, 4px stroke. */
const RADIUS = 17
const CIRCUMFERENCE = 2 * Math.PI * RADIUS

interface Props {
  usage: UsageToday
  /** The compact form for the workspace toolbar, where the row is already full. */
  compact?: boolean
  /**
   * The collapsed rail: the arc and the percentage inside it, with no figures beside it.
   *
   * THE COUNTER STILL NEVER DISAPPEARS, which is the client's actual requirement. A 56px column
   * cannot hold "412,000 / 1,000,000", so the reading that survives the collapse is the percent
   * written inside the arc — a number, on screen, at every width. The full figures come back the
   * moment the navigation opens, and the title carries them meanwhile.
   */
  rail?: boolean
}

export default function TokenRing({ usage, compact = false, rail = false }: Props) {
  // A zero limit would make every reading NaN. It is not a state the server produces, but the
  // division is here and the meter is the one element that may never render nonsense.
  const fraction = usage.limit > 0 ? Math.min(1, usage.used / usage.limit) : 0
  const spent = usage.remaining <= 0
  const percent = Math.round(fraction * 100)
  const size = rail ? 34 : compact ? 30 : 38
  // TOKENS, NEVER A SECOND NAME FOR A COLOUR THE RAMP ALREADY OWNS. Amber is `accent`, whose
  // only other use in this product is the unsaved dot on Save; red is `danger`, and the spent
  // figure takes the status ramp's own red ink.
  const arcStroke = spent ? 'stroke-danger' : 'stroke-accent'

  return (
    <div
      className={
        rail
          ? 'flex justify-center select-none mb-2.5'
          : compact
            ? 'flex items-center gap-2 select-none'
            : 'flex items-center gap-2.5 mx-3 mb-2.5 px-3 py-2.5 border border-bial-border rounded-xl select-none'
      }
      title={
        rail
          ? `${usage.used.toLocaleString('en-US')} / ${usage.limit.toLocaleString('en-US')} tokens today · resets at midnight IST`
          : 'Daily AI tokens used today · resets at midnight IST'
      }
      data-testid="usage-meter"
    >
      <svg width={size} height={size} viewBox="0 0 42 42" aria-hidden="true">
        <circle cx="21" cy="21" r={RADIUS} className="fill-none stroke-bial-border" strokeWidth="4" />
        <motion.circle
          cx="21"
          cy="21"
          r={RADIUS}
          className={`fill-none ${arcStroke}`}
          strokeWidth="4"
          strokeLinecap="round"
          transform="rotate(-90 21 21)"
          initial={false}
          animate={{ strokeDasharray: `${fraction * CIRCUMFERENCE} ${CIRCUMFERENCE}` }}
          transition={{ duration: DURATION.layout, ease: LAYOUT_EASE }}
        />
        {!compact && (
          <text
            x="21"
            y="24.5"
            textAnchor="middle"
            fontSize="11"
            fontWeight="700"
            className={spent ? 'fill-status-red-fg' : 'fill-primary-900'}
          >
            {percent}%
          </text>
        )}
      </svg>
      <div className={`leading-tight ${rail ? 'hidden' : ''}`}>
        {/* Tabular figures: the two numbers sit under each other as the budget is spent, and a
            counter that reflows every few thousand tokens reads as a glitch. */}
        <div
          className={`text-[11px] font-bold tabular-nums whitespace-nowrap ${
            spent ? 'text-danger' : 'text-primary-900'
          }`}
          data-testid="usage-figures"
        >
          {usage.used.toLocaleString('en-US')} / {usage.limit.toLocaleString('en-US')}
        </div>
        <div className="text-[10px] font-medium text-neutral">tokens today</div>
      </div>
    </div>
  )
}
