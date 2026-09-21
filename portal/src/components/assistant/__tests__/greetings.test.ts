/**
 * THE GREETING SET, AND THE RULES THAT KEEP IT HONEST.
 *
 * Everything under test is a pure function over its arguments — the hour and the name are passed
 * in, never read from a clock or a profile — so none of this needs frozen time or a mounted DOM.
 * The two storage helpers are the exception and are tested last, including the throw.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'

import {
  GREETINGS,
  bandFor,
  candidates,
  firstName,
  headlineParts,
  lastGreeting,
  pickGreeting,
  rememberGreeting,
} from '../greetings'

const BANDS = ['morning', 'afternoon', 'evening', 'night'] as const
/** One hour inside each band, for the sweeps that only care which band they are in. */
const HOURS = { morning: 9, afternoon: 14, evening: 18, night: 23 } as const

describe('the bands are the citizen own clock, and they meet without a gap', () => {
  it.each([
    [0, 'night'],
    [4, 'night'],
    [5, 'morning'],
    [11, 'morning'],
    [12, 'afternoon'],
    [16, 'afternoon'],
    [17, 'evening'],
    [20, 'evening'],
    [21, 'night'],
    [23, 'night'],
  ])('%i:00 is %s', (hour, band) => {
    expect(bandFor(hour)).toBe(band)
  })

  it('every hour of the day lands in exactly one band, and night is the one that wraps', () => {
    const hours = Array.from({ length: 24 }, (_, h) => bandFor(h))
    expect(hours).toHaveLength(24)
    expect(new Set(hours)).toEqual(new Set(BANDS))
    // The wrap is the only thing a range check could get wrong twice over: 23:00 and 00:00 are
    // the same band despite sitting at opposite ends of the number line.
    expect(bandFor(23)).toBe(bandFor(0))
  })
})

describe('the set itself', () => {
  it('is thirty lines, and each marks exactly one accent word', () => {
    expect(GREETINGS).toHaveLength(30)
    for (const greeting of GREETINGS) {
      expect(
        greeting.headline.split('*').length - 1,
        `"${greeting.headline}" must wrap exactly one word in asterisks`,
      ).toBe(2)
      expect(greeting.sub.length).toBeGreaterThan(0)
    }
  })

  it('nine of the thirty use no name at all', () => {
    // Both a fallback and a rationing: a surface that says the name every single time reads as a
    // product performing familiarity rather than one that knows who is signed in.
    expect(GREETINGS.filter((g) => !g.headline.includes('{name}'))).toHaveLength(9)
  })

  it('★ every band keeps at least two nameless lines, which is what makes the pick safe', () => {
    // `pickGreeting` indexes its pool directly and drops the previous line from it. Two is the
    // floor that keeps that subtraction from emptying the set for a profile with no display name
    // — the narrowest case there is. Drop a band to one and the guard inside the pick is all that
    // stands between a refresh and a screen with no heading on it.
    for (const band of BANDS) {
      expect(candidates(HOURS[band], null).length, `${band} has too few nameless lines`).toBeGreaterThanOrEqual(2)
    }
  })
})

describe('what a citizen is offered depends on the hour and on whether we know their name', () => {
  it.each(BANDS)('%s draws on its own seven plus the two that fit any hour', (band) => {
    const pool = candidates(HOURS[band], 'Asha')
    expect(pool).toHaveLength(9)
    expect(pool.every((g) => g.band === band || g.band === 'any')).toBe(true)
  })

  it('a profile with no display name is never offered a line that needs one', () => {
    for (const band of BANDS) {
      for (const greeting of candidates(HOURS[band], null)) {
        expect(greeting.headline).not.toContain('{name}')
      }
    }
  })
})

describe('picking one', () => {
  it('is driven by the injected source, at both ends of its range', () => {
    const pool = candidates(9, 'Asha')
    expect(pickGreeting({ hour: 9, name: 'Asha', random: () => 0 })).toEqual(pool[0])
    expect(pickGreeting({ hour: 9, name: 'Asha', random: () => 0.999 })).toEqual(pool[pool.length - 1])
  })

  it('does not fall off the end when the source returns exactly 1', () => {
    // `Math.random()` is documented as [0, 1) and a stub is not. An off-by-one here renders
    // `undefined.headline` on the one screen this module exists for.
    expect(pickGreeting({ hour: 9, name: 'Asha', random: () => 1 })).toEqual(candidates(9, 'Asha')[0])
  })

  it('★ never repeats the line the citizen just saw — for any band, named or not', () => {
    for (const band of BANDS) {
      for (const name of ['Asha', null]) {
        for (const previous of candidates(HOURS[band], name)) {
          // Every position in the pool, so the exclusion is proved for the first and last too.
          for (const roll of [0, 0.34, 0.67, 0.999]) {
            const picked = pickGreeting({
              hour: HOURS[band],
              name,
              exclude: previous.headline,
              random: () => roll,
            })
            expect(picked.headline, `${band}/${name ?? 'no name'} repeated after ${previous.headline}`).not.toBe(
              previous.headline,
            )
          }
        }
      }
    }
  })

  it('ignores a remembered line that no longer fits the hour', () => {
    // The stored line is from this sitting, not this band — a citizen who was here at nine and
    // comes back at two has a morning headline in storage, and it must not narrow the afternoon.
    expect(candidates(14, 'Asha')).toHaveLength(9)
    const picked = pickGreeting({ hour: 14, name: 'Asha', exclude: '*Morning*, {name}.', random: () => 0 })
    expect(picked).toEqual(candidates(14, 'Asha')[0])
  })
})

describe('the headline is split for rendering, never assembled as markup', () => {
  it('marks the wrapped word and nothing else', () => {
    expect(headlineParts('*Evening*, {name}.', 'Asha')).toEqual([
      { text: 'Evening', accent: true },
      { text: ', Asha.', accent: false },
    ])
    expect(headlineParts('Ready to *roll*, {name}.', 'Asha')).toEqual([
      { text: 'Ready to ', accent: false },
      { text: 'roll', accent: true },
      { text: ', Asha.', accent: false },
    ])
  })

  it('drops the empty chunk a leading accent produces rather than rendering a blank node', () => {
    expect(headlineParts('*Morning*.', null).map((s) => s.text)).toEqual(['Morning', '.'])
  })

  it('★ no placeholder and no asterisk survives into anything a citizen reads', () => {
    for (const greeting of GREETINGS) {
      for (const name of ['Asha', null]) {
        const rendered = headlineParts(greeting.headline, name)
          .map((s) => s.text)
          .join('')
        expect(rendered, greeting.headline).not.toContain('{name}')
        expect(rendered, greeting.headline).not.toContain('*')
      }
      // Exactly one accented word per line, which is what keeps one colour on that screen.
      expect(headlineParts(greeting.headline, 'Asha').filter((s) => s.accent)).toHaveLength(1)
    }
  })
})

describe('the name is the first word of the profile, or nothing at all', () => {
  it.each([
    ['Asha Rao', 'Asha'],
    ['  Priya   Nair  ', 'Priya'],
    ['Rohith', 'Rohith'],
    ['', null],
    ['   ', null],
    [null, null],
    [undefined, null],
  ])('%p → %p', (input, expected) => {
    expect(firstName(input)).toBe(expected)
  })
})

describe('the previous pick is remembered, and storage failing is not an outage', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    sessionStorage.clear()
  })

  it('round-trips', () => {
    expect(lastGreeting()).toBeNull()
    rememberGreeting('*Evening*, {name}.')
    expect(lastGreeting()).toBe('*Evening*, {name}.')
  })

  it('★ survives storage that throws, which it genuinely does in a private window', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage is blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage is blocked')
    })
    // Paired with liveness below: both calls returning quietly must mean "handled", not "never ran".
    expect(() => rememberGreeting('*Morning*.')).not.toThrow()
    expect(lastGreeting()).toBeNull()
    expect(Storage.prototype.setItem).toHaveBeenCalled()
    expect(Storage.prototype.getItem).toHaveBeenCalled()
  })
})
