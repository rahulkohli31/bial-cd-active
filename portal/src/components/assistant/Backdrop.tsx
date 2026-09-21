/**
 * THE SKY BEHIND BIAL CHAT — three layers, all decoration, none of it interactive.
 *
 * A wash settling into the bottom-right corner, a drifting field of motes with real depth, and two
 * aircraft on long shallow climbs. The whole layer is `aria-hidden` and `pointer-events-none`: it
 * says nothing and it catches nothing, so the composer keeps every click on the screen.
 *
 * THE WASH IS IN ONE CORNER AND NOT IN THE MIDDLE, which is the difference between depth and a
 * halo: a bright pool at the centre of a blue-grey ground leaves a near-white middle against blue
 * corners with a visible ring between the two. Its colour is `#E2E8F0`, the hairline the portal
 * already draws on every card, so nothing new enters the palette.
 *
 * THE GEOMETRY IS RANDOM BUT NOT RANDOMISED. A seeded generator run once at module scope gives one
 * field for the life of the bundle: `Math.random` here would reshuffle the sky under any re-render,
 * and a backdrop that twitches when a sibling sets state only shows itself in front of an audience.
 *
 * EVERYTHING THAT MOVES IS A CSS ANIMATION, which is load-bearing rather than incidental: it puts
 * all of it behind the stylesheet's reduce-motion block with no branch in this file.
 */
import type { CSSProperties } from 'react'

/** A top-down airliner, nose up in its own box. `.chat-plane-glyph` turns it to face its path. */
const AIRLINER =
  'M 12 1.6 L 13.5 2.4 L 14 8 L 22.4 13.2 L 22.4 15.2 L 14 12.8 L 14 18.2 ' +
  'L 17.2 20.4 L 17.2 21.9 L 12 20.6 L 6.8 21.9 L 6.8 20.4 L 10 18.2 ' +
  'L 10 12.8 L 1.6 15.2 L 1.6 13.2 L 10 8 L 10.5 2.4 Z'

/** Deterministic, and seeded once. Nothing here needs cryptographic randomness — it needs the
 *  same answer twice. */
function seeded(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

interface Mote {
  key: string
  /** `chat-mote-far` blurs a hair; `chat-mote-haze` is the soft back wall. */
  variant: string
  style: CSSProperties
}

/**
 * THREE DEPTHS PLUS A BACK WALL. The near motes are bigger, darker and quicker than the far ones,
 * which is the whole of the depth: a field of identical specks reads as noise on the glass rather
 * than as air with something in it.
 *
 * THEY START SCATTERED THROUGH THE WHOLE FIELD, not bunched below it. Negative animation delays
 * already spread them across the cycle, so starting them off-screen buys nothing while it costs
 * the one state that matters — under a reduced-motion preference, where the animation is dropped
 * and every mote stands wherever the stylesheet left it.
 */
function field(): Mote[] {
  const random = seeded(7)
  const between = (lo: number, hi: number) => lo + random() * (hi - lo)
  const motes: Mote[] = []

  for (let i = 0; i < 46; i += 1) {
    const roll = random()
    const depth: { variant: string; size: number; ink: number; seconds: number } =
      roll < 0.4
        ? { variant: 'chat-mote-far', size: between(1.2, 1.8), ink: between(0.1, 0.15), seconds: between(30, 38) }
        : roll < 0.8
          ? { variant: '', size: between(2, 2.8), ink: between(0.17, 0.23), seconds: between(22, 29) }
          : { variant: '', size: between(3.2, 4.4), ink: between(0.26, 0.34), seconds: between(15, 21) }
    motes.push({
      key: `m${i}`,
      variant: depth.variant,
      style: {
        left: `${between(1, 99).toFixed(1)}%`,
        bottom: `${between(-6, 100).toFixed(1)}%`,
        width: `${depth.size.toFixed(1)}px`,
        height: `${depth.size.toFixed(1)}px`,
        animationDuration: `${depth.seconds.toFixed(1)}s`,
        animationDelay: `-${(random() * depth.seconds).toFixed(1)}s`,
        '--mote-ink': depth.ink.toFixed(3),
        '--mote-drift': `${between(-55, 55).toFixed(0)}px`,
      } as CSSProperties,
    })
  }

  for (let i = 0; i < 5; i += 1) {
    const seconds = between(38, 52)
    const size = between(9, 15)
    motes.push({
      key: `h${i}`,
      variant: 'chat-mote-haze',
      style: {
        left: `${between(5, 95).toFixed(1)}%`,
        bottom: `${between(-6, 100).toFixed(1)}%`,
        width: `${size.toFixed(0)}px`,
        height: `${size.toFixed(0)}px`,
        animationDuration: `${seconds.toFixed(0)}s`,
        animationDelay: `-${(random() * seconds).toFixed(0)}s`,
        '--mote-drift': `${between(-30, 30).toFixed(0)}px`,
      } as CSSProperties,
    })
  }

  return motes
}

const MOTES = field()

/**
 * One aircraft: an element for the crossing, one for the climb, and the glyph at a fixed heading.
 * The two animations share a duration and a delay so the composition traces a single arc; the
 * geometry and the reason it is built this way are in `index.css`.
 */
function Aircraft({ lane }: { lane: 'a' | 'b' }) {
  return (
    <span className={`chat-plane chat-plane-${lane}`}>
      <span className="chat-plane-lift">
        <svg className="chat-plane-glyph" viewBox="0 0 24 24" width="19" height="19">
          <path d={AIRLINER} />
        </svg>
      </span>
    </span>
  )
}

export default function Backdrop() {
  return (
    // The layer itself is plain utilities: a test can read a class list, and cannot read a
    // stylesheet rule jsdom never evaluates.
    <div
      aria-hidden="true"
      data-testid="assistant-backdrop"
      className="pointer-events-none absolute inset-0 overflow-hidden"
    >
      <div className="chat-sky-corner" />
      {MOTES.map((mote) => (
        <span key={mote.key} className={`chat-mote ${mote.variant}`} style={mote.style} />
      ))}
      {/* Two, at very different periods, each waiting off screen for as long as it takes to
          cross — so there is usually one in the air, sometimes none, and never a formation. */}
      <Aircraft lane="a" />
      <Aircraft lane="b" />
    </div>
  )
}
