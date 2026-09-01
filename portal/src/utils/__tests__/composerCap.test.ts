/**
 * The browser's character cap (R42, R42a, R43).
 *
 * ══ WHY THE COUNTING RULE IS THE WHOLE TEST ══
 *
 * The server counts characters as Python counts them — one per Unicode CODE POINT. JavaScript's
 * `String.length` counts UTF-16 code UNITS, so anything above U+FFFF counts twice. A counter that
 * used `String.length` would drift from the server silently, by more the more emoji a citizen
 * types, and near a limit a wrong number is worse than no number at all.
 *
 * So the astral-plane cases below are not exotica. They are the one behaviour that separates this
 * module from a one-line `text.length`, and every one of them fails against that one-liner.
 */
import { describe, it, expect } from 'vitest'

import {
  COUNTER_VISIBLE_WITHIN,
  MAX_COMPOSER_CHARS,
  capState,
  countCharacters,
} from '../composerCap'

/** One astral-plane code point: `String.length` says 2, a code-point count says 1. */
const EMOJI = '🚀'

describe('countCharacters counts the way the server counts', () => {
  it('counts a plain string one per character', () => {
    expect(countCharacters('')).toBe(0)
    expect(countCharacters('gate cleaning log')).toBe(17)
  })

  it('counts an astral-plane character ONCE, where String.length counts it twice', () => {
    // Mutation check: replace the implementation with `text.length` and this goes red — it is the
    // only assertion in the file that can tell the two apart on a short string.
    expect(EMOJI.length).toBe(2) // the trap, stated out loud
    expect(countCharacters(EMOJI)).toBe(1)
    expect(countCharacters(`${EMOJI}${EMOJI}${EMOJI}`)).toBe(3)
  })

  it('counts a combining mark separately — agreement with the server, not with the eye', () => {
    // "é" as `e` + U+0301 is TWO characters to the server and two here. That is not what a reader
    // would guess by counting glyphs, and it is the right answer anyway: the number has to mean
    // what the thing enforcing the limit means.
    expect(countCharacters('é')).toBe(2)
    expect(countCharacters('é')).toBe(1) // the precomposed form is one, in both places
  })
})

describe('capState — nothing is ever cut', () => {
  it('is silent and permissive on an ordinary message', () => {
    const state = capState('add a column for the gate number')
    expect(state.over).toBe(false)
    expect(state.showCounter).toBe(false)
    expect(state.message).toBeNull()
  })

  it('stays silent right up to the counter threshold, then shows the number', () => {
    const justBelow = 'x'.repeat(MAX_COMPOSER_CHARS - COUNTER_VISIBLE_WITHIN - 1)
    expect(capState(justBelow).showCounter).toBe(false)
    // Exactly AT the threshold the counter appears — the boundary is asserted rather than
    // approached, because an off-by-one here is invisible in use.
    const atThreshold = 'x'.repeat(MAX_COMPOSER_CHARS - COUNTER_VISIBLE_WITHIN)
    expect(capState(atThreshold).showCounter).toBe(true)
    expect(capState(atThreshold).over).toBe(false)
  })

  it('is not over AT the cap, and is over one character past it', () => {
    const exactly = 'x'.repeat(MAX_COMPOSER_CHARS)
    expect(capState(exactly).over).toBe(false)
    expect(capState(exactly).message).toBeNull()

    const oneMore = 'x'.repeat(MAX_COMPOSER_CHARS + 1)
    expect(capState(oneMore).over).toBe(true)
  })

  it('says nothing has been cut, and names the number to shorten to', () => {
    // The copy is the requirement, not decoration: someone whose paste was silently truncated
    // believes their whole specification went in, and the app that gets built is missing the half
    // nobody knows was dropped. The sentence has to say the text is still there.
    const message = capState('x'.repeat(MAX_COMPOSER_CHARS + 1)).message
    expect(message).toMatch(/nothing has been cut/i)
    expect(message).toContain(MAX_COMPOSER_CHARS.toLocaleString())
    expect(message).toMatch(/send it in two/i)
  })

  it('measures the cap in code points too — an emoji message is not cut short by half', () => {
    // 6,000 rockets is 6,000 characters to the server and 12,000 UTF-16 units to `String.length`.
    // Under a `text.length` implementation this citizen would be refused at 5,000 rockets with
    // half their message apparently missing and no explanation that made sense.
    const rockets = EMOJI.repeat(6_000)
    expect(rockets.length).toBe(12_000)
    expect(capState(rockets).count).toBe(6_000)
    expect(capState(rockets).over).toBe(false)
  })

  it('reports the count it would SHOW, not the raw length', () => {
    const mixed = `${EMOJI}${EMOJI}abc`
    expect(capState(mixed).count).toBe(5)
  })
})

describe('the gap to the server is deliberate and wide (R42a)', () => {
  it('stops six times below the server refusal, so the two never disagree in front of a citizen', () => {
    // The server REFUSES (does not trim) at 64,000 with a 422 and nothing stored. Six times the
    // headroom means a message that passes here cannot plausibly be refused there. Pinning the
    // relationship rather than the number is what makes this survive Plan B moving the server's.
    const SERVER_REFUSES_AT = 64_000
    expect(MAX_COMPOSER_CHARS * 6).toBeLessThanOrEqual(SERVER_REFUSES_AT)
  })
})
