/**
 * THE HEADING ON BIAL CHAT IS A GREETING, AND IT CHANGES.
 *
 * Thirty lines across four bands of the citizen's own clock, one picked on mount. `*word*` marks
 * the single word set italic in the brand teal — the only colour on that screen — and `{name}` is
 * filled from the signed-in profile.
 *
 * NINE OF THE THIRTY CARRY NO NAME, and that is two jobs in one list: it is the fallback for a
 * profile with no display name, and it is what stops the personalisation becoming a tic. A surface
 * that says "Rohith" every single time reads as a product performing familiarity rather than one
 * that knows who is signed in.
 *
 * EVERYTHING HERE IS A PURE FUNCTION OVER ITS ARGUMENTS — the hour, the name and the previous pick
 * all arrive as parameters rather than being read from a clock, a profile or storage. That is what
 * makes the band boundaries and the parser testable without freezing time or mounting a DOM; the
 * two `sessionStorage` helpers at the foot are the only exception and they are the only thing in
 * the module that can fail.
 */

/** The four bands, by the hour on the citizen's own clock. */
export type Band = 'morning' | 'afternoon' | 'evening' | 'night'

export interface Greeting {
  /** The band this line belongs to, or `any` for the two that fit whatever the hour is. */
  band: Band | 'any'
  /** `*word*` wraps the accented word; `{name}` is optional and absent from nine of the thirty. */
  headline: string
  /** One short line under it. Never a second sentence — the heading carries the screen. */
  sub: string
}

export const GREETINGS: readonly Greeting[] = [
  { band: 'morning', headline: '*Morning*, {name}.', sub: "What's first today?" },
  { band: 'morning', headline: '*Good morning*.', sub: "The day's wide open." },
  { band: 'morning', headline: '*Early start*, {name}.', sub: "Let's make it count." },
  { band: 'morning', headline: 'A *fresh* day, {name}.', sub: 'Where do you want to begin?' },
  { band: 'morning', headline: '*Morning*.', sub: 'Tell me what you need.' },
  { band: 'morning', headline: '*Back at it*, {name}.', sub: 'Pick up where you left off.' },
  { band: 'morning', headline: '*First light*, {name}.', sub: "What's on for today?" },

  { band: 'afternoon', headline: '*Afternoon*, {name}.', sub: 'What can I take off your plate?' },
  { band: 'afternoon', headline: '*Good afternoon*.', sub: 'Ask me anything.' },
  { band: 'afternoon', headline: '*Half way* there, {name}.', sub: "What's next?" },
  { band: 'afternoon', headline: '*Midday*, {name}.', sub: "Let's get something done." },
  { band: 'afternoon', headline: 'Still *going*, {name}.', sub: 'What do you need?' },
  { band: 'afternoon', headline: '*Afternoon*.', sub: "Tell me what you're working on." },
  { band: 'afternoon', headline: '*Ready* when you are, {name}.', sub: "Type naturally — I'll follow." },

  { band: 'evening', headline: '*Evening*, {name}.', sub: 'Quick task before you sign off?' },
  { band: 'evening', headline: '*Good evening*.', sub: "What's left to clear?" },
  { band: 'evening', headline: '*Winding down*, {name}?', sub: 'One more thing, maybe.' },
  { band: 'evening', headline: '*Evening*.', sub: 'Ask me anything.' },
  { band: 'evening', headline: '*Last stretch*, {name}.', sub: 'What can I help finish?' },
  { band: 'evening', headline: '*Clear skies*, {name}.', sub: "What's the last thing on your list?" },
  { band: 'evening', headline: 'On the *evening* shift, {name}?', sub: 'Tell me what you need.' },

  { band: 'night', headline: '*Still up*, {name}?', sub: "I'm here." },
  { band: 'night', headline: 'A *late* one, {name}.', sub: 'What are we working on?' },
  { band: 'night', headline: '*Night* shift.', sub: 'Ask away.' },
  { band: 'night', headline: '*Burning* the midnight oil?', sub: "Let's make it quick." },
  { band: 'night', headline: '*Quiet* hours, {name}.', sub: 'Good time to think.' },
  { band: 'night', headline: '*Still here*.', sub: 'So am I.' },
  { band: 'night', headline: 'It is *late*, {name}.', sub: 'What do you need?' },

  // The two that fit any hour, so no band is ever down to its own seven.
  { band: 'any', headline: '*Hey* {name}.', sub: "Tell me what you need. I'll figure out the rest." },
  { band: 'any', headline: 'Ready to *roll*, {name}.', sub: 'Type naturally — I understand context.' },
]

/** The band an hour falls in. Night wraps midnight, which is why it is the fallthrough. */
export function bandFor(hour: number): Band {
  if (hour >= 5 && hour < 12) return 'morning'
  if (hour >= 12 && hour < 17) return 'afternoon'
  if (hour >= 17 && hour < 21) return 'evening'
  return 'night'
}

/** Every line that fits this hour and this profile. Never empty: each band keeps at least two
 *  lines that need no name, which is what makes the pick below safe to index. */
export function candidates(hour: number, name: string | null): readonly Greeting[] {
  const band = bandFor(hour)
  return GREETINGS.filter(
    (g) => (g.band === band || g.band === 'any') && (name !== null || !g.headline.includes('{name}')),
  )
}

export interface GreetingRequest {
  /** The hour on the citizen's own clock, 0–23. */
  hour: number
  /** Their first name, or `null` when the profile carries none — which narrows the set to nine. */
  name: string | null
  /** The headline shown last time, so a refresh never repeats it. */
  exclude?: string | null
  /** Injected so a test can choose a line rather than assert about chance. */
  random?: () => number
}

export function pickGreeting({ hour, name, exclude = null, random = Math.random }: GreetingRequest): Greeting {
  const fits = candidates(hour, name)
  // THE EXCLUSION MUST NOT BE ABLE TO EMPTY THE SET. A profile with no name and a band down to two
  // lines would otherwise leave nothing to pick from on the second refresh, and repeating the
  // previous line is a far smaller fault than rendering no heading at all.
  const fresh = fits.filter((g) => g.headline !== exclude)
  const pool = fresh.length > 0 ? fresh : fits
  return pool[Math.floor(random() * pool.length) % pool.length]
}

export interface HeadlineSegment {
  text: string
  /** True for the one word the screen paints teal and italic. */
  accent: boolean
}

/**
 * The headline, split for rendering — never assembled as markup.
 *
 * Odd-numbered chunks of a `*`-split are the ones that were wrapped, which is the whole of the
 * parsing rule. Empty chunks are dropped so a headline opening on its accent does not render a
 * blank text node before it.
 */
export function headlineParts(headline: string, name: string | null): HeadlineSegment[] {
  return headline
    .split('*')
    .map((chunk, index) => ({ text: chunk.replaceAll('{name}', name ?? ''), accent: index % 2 === 1 }))
    .filter((segment) => segment.text.length > 0)
}

/** The first word of a display name, or `null` — which is the signal to use a nameless line. */
export function firstName(displayName: string | null | undefined): string | null {
  const first = (displayName ?? '').trim().split(/\s+/)[0]
  return first.length > 0 ? first : null
}

const LAST_KEY = 'bial_chat_greeting'

/**
 * The previous pick, so the next one differs. `sessionStorage` rather than `localStorage` because
 * "don't repeat what I just saw" is a property of this sitting, and both accessors are guarded
 * because storage genuinely throws rather than degrading — a greeting is not worth a blank screen.
 */
export function lastGreeting(): string | null {
  try {
    return sessionStorage.getItem(LAST_KEY)
  } catch {
    return null
  }
}

export function rememberGreeting(headline: string): void {
  try {
    sessionStorage.setItem(LAST_KEY, headline)
  } catch {
    // Nothing remembered this sitting; the next pick may repeat, and that is the whole cost.
  }
}
