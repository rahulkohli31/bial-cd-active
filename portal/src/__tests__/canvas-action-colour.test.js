import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'

/**
 * THE ACTION COLOUR IS TEAL, AND NOTHING ELSE PAINTS AN ACTION (plan 002, U1).
 *
 * Across the 41 boards of `docs/ux-canvas/`, #0D7377 fills every primary action without
 * exception — Send for review, Launch Application, Try again, Build this plan, the composer's
 * send control, Create project. The brand gold #D9A036 occurs exactly ONCE in 3,100+ literal
 * hexes, as a `:root` declaration nothing uses; the brand orange #F5A623 appears on exactly two
 * controls, the header token meter and the 6px unsaved dot. The implementation had inverted all
 * three: three primary actions filled gold, the Plan/Build control filled orange, and the one
 * place the canvas puts amber TEXT had no amber at all.
 *
 * WHY THIS IS A SOURCE-TEXT TEST AND NOT A RENDER TEST. jsdom computes no Tailwind styles, so a
 * `getComputedStyle` assertion cannot tell gold from teal — every DOM assertion passes either
 * way. The same reasoning `tailwind-tokens.test.js` sets out in its own docblock. What can be
 * checked cheaply is which class names exist in the tree, so that is what this checks.
 *
 * THE ALLOWLISTS ARE THE POINT. Neither token is banned outright — each has a real board role,
 * and a blanket ban would be a rule nobody could keep. What is banned is the DEFAULT of each
 * being used as a surface, anywhere outside the sites named below.
 */
const ROOT = path.resolve(__dirname, '..')

function sourceFiles(dir) {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry)
    if (statSync(full).isDirectory()) return entry === '__tests__' ? [] : sourceFiles(full)
    return /\.(jsx?|tsx?)$/.test(entry) ? [full] : []
  })
}

const rel = (file) => path.relative(ROOT, file).split(path.sep).join('/')

/** Comments are not source — they are where the reasons live, and several of them quote the
 *  very class names these rules forbid so the next author knows why. Same treatment, and the
 *  same `://` guard, as `tailwind-tokens.test.js`. */
function stripComments(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

/**
 * Every `bg-secondary` / `bg-secondary-N` / `shadow-secondary` / `ring-secondary`, plus the
 * hover forms. `text-secondary-800` is deliberately NOT matched: #8C5D1E on #FFF4E0 is the
 * canvas's BUILD pill, a label the boards draw in exactly that pair.
 */
const GOLD_SURFACE = /\b(?:hover:|focus-visible:|active:|data-\[state=on\]:)?(?:bg|shadow|ring|border)-secondary(?:-\d{2,3})?(?![-\w])/g

/** The same, for the brand orange. `bg-accent-light` (#FFF4E0) is a separate token and a real
 *  board colour — the BUILD pill's ground — so the trailing `(?![-\w])` excludes it. A plain
 *  `\b` would NOT: `-` is a non-word character, so `\b` matches happily inside `bg-accent-light`
 *  and inside `text-secondary-800`, and both rules would fire on the two pairs the boards draw. */
const ORANGE_SURFACE = /\b(?:hover:|focus-visible:|active:|data-\[state=on\]:)?(?:bg|shadow|ring|border)-accent(?![-\w])/g

/**
 * The gold family's board role is a LABEL, never a fill. One site survives, on a screen the
 * canvas does not cover at all.
 */
const GOLD_ALLOWED = {
  // "Staff Internal Portal", the login panel's eyebrow. A label, not an action, and no board
  // draws the login screen. It uses `secondary-800` rather than the DEFAULT because white on
  // #D9A036 is 2.34:1.
  'pages/LoginPage.tsx': ['bg-secondary-800'],
}

/** The canvas uses `accent` on exactly two controls, and one of them lives in this tree. */
const ORANGE_ALLOWED = {
  // The header token meter's fill. The board's own worked example draws it amber at 54%.
  'components/layout/Navbar.tsx': ['bg-accent'],
}

function offenders(pattern, allowed) {
  const found = []
  for (const file of sourceFiles(ROOT)) {
    const name = rel(file)
    const permitted = allowed[name] ?? []
    for (const hit of stripComments(readFileSync(file, 'utf8')).match(pattern) ?? []) {
      if (!permitted.includes(hit)) found.push(`${name} → ${hit}`)
    }
  }
  return found
}

describe('U1 — teal is the action colour', () => {
  it('paints no surface with the brand gold outside its one label site', () => {
    expect(
      offenders(GOLD_SURFACE, GOLD_ALLOWED),
      'The canvas fills every primary action #0D7377 and paints #D9A036 on nothing. Use `bg-primary`.',
    ).toEqual([])
  })

  it('paints no surface with the brand orange outside the token meter', () => {
    expect(
      offenders(ORANGE_SURFACE, ORANGE_ALLOWED),
      'The canvas uses #F5A623 on the token meter and the 6px unsaved dot, and nowhere else — never as a hover, a selected state, or a button.',
    ).toEqual([])
  })

  /**
   * WITHOUT THIS, A REGEX THAT MATCHES NOTHING PASSES FOR EVER. Both rules are asserted against
   * a fixture that is KNOWN to violate them, so the guard cannot rot into a no-op the way a
   * source-scanning rule most easily does.
   */
  it('the rules actually fire on text that violates them', () => {
    const bad = `
      <button className="bg-secondary hover:bg-secondary-600 shadow-secondary/30" />
      <Toggle className="data-[state=on]:bg-accent hover:bg-accent" />
    `
    expect(bad.match(GOLD_SURFACE)).toEqual(['bg-secondary', 'hover:bg-secondary-600', 'shadow-secondary'])
    expect(bad.match(ORANGE_SURFACE)).toEqual(['data-[state=on]:bg-accent', 'hover:bg-accent'])
    // …and pass the two things that are legitimately on the boards.
    expect('bg-accent-light text-secondary-800'.match(GOLD_SURFACE)).toBeNull()
    expect('bg-accent-light text-secondary-800'.match(ORANGE_SURFACE)).toBeNull()
  })
})
