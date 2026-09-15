/**
 * WHY THIS EXISTS — the portal's scrollbar styling is asserted against the STYLESHEET SOURCE,
 * because nothing in the unit environment can evaluate it.
 *
 * WHY NOT A DOM TEST. jsdom implements no layout and renders no scrollbar at all. A test that
 * mounted a page and asked how wide the bar was would report the same answer — nothing — whether
 * the rules were present, mangled, or deleted outright. What can be checked honestly here is the
 * source text, so every rule below is a pure function over a CSS string.
 *
 * WHY THE `::-webkit-scrollbar` ABSENCE IS ASSERTED, and it is the point of this file. Those
 * pseudo-elements are what every search result recommends, and beside `scrollbar-width` they read
 * as a harmless fallback for older engines. They are not — `index.css`'s own comment carries the
 * measurement — and because the standard pair is set on `*`, a webkit block anywhere in this sheet
 * is unreachable: dead code that looks like careful cross-browser work, which is the hardest kind
 * to argue away in review. So the guard is here rather than in the argument.
 *
 * SOURCE ORDER IS THE MECHANISM, which is why it is asserted — the same claim
 * `reducedMotion.test.ts` makes about its own block, for the same reason. This is a bare
 * universal selector at the lowest specificity there is; inside an `@layer`, or above `@tailwind
 * utilities`, Tailwind's own reset wins and the styling quietly does nothing.
 *
 * EACH RULE IS ASSERTED TWICE: once against the real file, once against a fixture KNOWN to break
 * it. Without the second, a regex that stops matching passes for ever and protects nothing.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { nestingDepthAt } from './_cssNesting'
import { stripComments } from './_stripComments'

// `import.meta.url` is a jsdom http URL under this vitest config, so anchor on the cwd — the same
// anchor `reducedMotion.test.ts` uses, and vitest runs with `portal/` as the cwd.
const ROOT = process.cwd()
const CSS_FILE = 'src/index.css'
const CSS = readFileSync(path.resolve(ROOT, CSS_FILE), 'utf8')

/** The declarations the styling needs, as `[human name, pattern]`, matched against the rules that
 *  target the universal selector — the only selector that reaches every scroller. */
const REQUIRED: ReadonlyArray<readonly [string, RegExp]> = [
  ['a thinner bar than the platform default', /scrollbar-width:thin/],
  // The negative lookahead is the whole point of this one: `scrollbar-color: transparent
  // transparent` satisfies "two tokens, second is transparent" while painting no thumb at all.
  ['a neutral thumb over an invisible track', /scrollbar-color:(?!transparent)[^;]+transparent/],
]

/** Every rule in a stylesheet, as `[selector list, declaration body]`, with all whitespace gone so
 *  a formatter cannot change any answer below. */
function rulesIn(css: string): Array<[string, string]> {
  const source = stripComments(css).replace(/\s+/g, '')
  return [...source.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, selectors, body]) => [selectors, body])
}

/** Declaration bodies of every rule naming `selector` as a WHOLE entry in its selector list, so a
 *  compound selector is never mistaken for the bare one it contains. */
function declarationsFor(css: string, selector: string): string {
  return rulesIn(css)
    .filter(([selectors]) => selectors.split(',').includes(selector))
    .map(([, body]) => `${body};`)
    .join('')
}

function missingDeclarations(css: string): string[] {
  const universal = declarationsFor(css, '*')
  return REQUIRED.filter(([, pattern]) => !pattern.test(universal)).map(([name]) => name)
}

/**
 * Selector list of every rule declaring either standard scrollbar property.
 *
 * There must be exactly one, and `declarationsFor` is why: it concatenates the bodies of all
 * rules matching a selector, so a later `* { scrollbar-width: auto }` would leave the first
 * rule's `thin` still matching every assertion above while the cascade quietly handed every
 * scroller in the app back to the platform default. Counting the rules is what closes that.
 */
function scrollbarRuleSelectors(css: string): string[] {
  return rulesIn(css)
    .filter(([, body]) => /scrollbar-(?:width|color):/.test(body))
    .map(([selectors]) => selectors)
}

/** Selectors in the sheet that reach for a `::-webkit-scrollbar*` pseudo-element. */
function webkitScrollbarSelectors(css: string): string[] {
  return rulesIn(css)
    .flatMap(([selectors]) => selectors.split(','))
    .filter((s) => s.includes('::-webkit-scrollbar'))
}

/** Offsets, in the comment-stripped source, of `@tailwind utilities` and of the scrollbar rule —
 *  plus the brace depth at that rule, where anything but 0 means it is inside a layer. */
function placement(css: string): { utilitiesAt: number; startsAt: number; nestingDepth: number } {
  const source = stripComments(css)
  const utilitiesAt = source.indexOf('@tailwind utilities')
  if (utilitiesAt < 0) throw new Error('no `@tailwind utilities` in the stylesheet')

  const startsAt = source.search(/(^|[\s,}])\*\s*\{[^{}]*scrollbar-width/)
  if (startsAt < 0) throw new Error('no `* { scrollbar-width: … }` rule in the stylesheet')

  return { utilitiesAt, startsAt, nestingDepth: nestingDepthAt(source, startsAt) }
}

// ------------------------------------------------------------------------------------------
// Fixtures: minimal stylesheets that are correct except for the one thing each is named for.
// ------------------------------------------------------------------------------------------

const RULE = '* { scrollbar-width: thin; scrollbar-color: grey transparent; }'
const HEALTHY = `@tailwind base;\n@tailwind utilities;\n\nbody { color: red; }\n\n${RULE}\n`
const FIXTURE_NO_COLOUR = HEALTHY.replace(' scrollbar-color: grey transparent;', '')
const FIXTURE_OFF_THE_UNIVERSAL = HEALTHY.replace('* { scrollbar-width', 'body { scrollbar-width')
const FIXTURE_INVISIBLE_THUMB = HEALTHY.replace('grey transparent', 'transparent transparent')
const FIXTURE_SECOND_UNIVERSAL_RULE = `${HEALTHY}\n* { scrollbar-width: auto; }\n`
const FIXTURE_WITH_DEAD_WEBKIT_BLOCK = `${HEALTHY}\n::-webkit-scrollbar { width: 10px; }\n::-webkit-scrollbar-thumb { background: grey; }\n`
const FIXTURE_BEFORE_UTILITIES = `@tailwind base;\n\n${RULE}\n\n@tailwind utilities;\n`
const FIXTURE_INSIDE_A_LAYER = `@tailwind base;\n@tailwind utilities;\n\n@layer utilities {\n${RULE}\n}\n`

describe('every scroll surface in the portal gets the thin bar, not the platform default', () => {
  it('sets both properties, on the universal selector that reaches every scroller', () => {
    expect(missingDeclarations(CSS)).toEqual([])
  })

  it('reads the real rule, not an empty string', () => {
    // Guards the reader itself: a selector match that silently stopped working would report
    // "nothing missing" against a stylesheet with no scrollbar styling in it at all.
    expect(declarationsFor(CSS, '*')).toMatch(/scrollbar/)
    expect(missingDeclarations('body { color: red; }')).toEqual(REQUIRED.map(([name]) => name))
  })

  it('MUTANT — dropping the colour is caught', () => {
    expect(missingDeclarations(HEALTHY)).toEqual([])
    expect(missingDeclarations(FIXTURE_NO_COLOUR)).toEqual([
      'a neutral thumb over an invisible track',
    ])
  })

  it('exactly one rule declares them, so nothing later can quietly hand them back', () => {
    expect(scrollbarRuleSelectors(CSS)).toEqual(['*'])
  })

  it('MUTANT — a second universal rule returning scrollbars to the platform is caught', () => {
    expect(scrollbarRuleSelectors(HEALTHY)).toEqual(['*'])
    expect(scrollbarRuleSelectors(FIXTURE_SECOND_UNIVERSAL_RULE)).toEqual(['*', '*'])
    // And the reason the count is needed at all: the declaration assertions stay green through
    // the override, because they read every matching rule's body as one string.
    expect(missingDeclarations(FIXTURE_SECOND_UNIVERSAL_RULE)).toEqual([])
  })

  it('MUTANT — a thumb painted transparent is caught', () => {
    expect(missingDeclarations(FIXTURE_INVISIBLE_THUMB)).toEqual([
      'a neutral thumb over an invisible track',
    ])
  })

  it('MUTANT — moving the pair off the universal selector is caught', () => {
    // `scrollbar-width` does not inherit, so on `body` it reaches `body` and nothing else. Both
    // declarations travel together in one rule, so both go missing — which is the point: the
    // tidier-looking selector costs every scroller in the app, not one property.
    expect(missingDeclarations(FIXTURE_OFF_THE_UNIVERSAL)).toEqual(REQUIRED.map(([name]) => name))
  })
})

describe('no `::-webkit-scrollbar` rule may join them — the standard pair suppresses every one', () => {
  it('the stylesheet carries none', () => {
    expect(webkitScrollbarSelectors(CSS)).toEqual([])
  })

  it('MUTANT — a stylesheet that adds one is caught', () => {
    expect(webkitScrollbarSelectors(HEALTHY)).toEqual([])
    expect(webkitScrollbarSelectors(FIXTURE_WITH_DEAD_WEBKIT_BLOCK)).toEqual([
      '::-webkit-scrollbar',
      '::-webkit-scrollbar-thumb',
    ])
  })
})

describe('source order is the mechanism, so it is asserted', () => {
  it('the rule sits after `@tailwind utilities`', () => {
    const { startsAt, utilitiesAt } = placement(CSS)
    expect(startsAt).toBeGreaterThan(utilitiesAt)
  })

  it('and outside every `@layer`', () => {
    expect(placement(CSS).nestingDepth).toBe(0)
  })

  it('MUTANT — a stylesheet that puts it above the utilities is caught', () => {
    const healthy = placement(HEALTHY)
    expect(healthy.startsAt).toBeGreaterThan(healthy.utilitiesAt)

    const broken = placement(FIXTURE_BEFORE_UTILITIES)
    expect(broken.startsAt).toBeLessThan(broken.utilitiesAt)
  })

  it('MUTANT — a stylesheet that wraps it in a layer is caught', () => {
    expect(placement(HEALTHY).nestingDepth).toBe(0)
    expect(placement(FIXTURE_INSIDE_A_LAYER).nestingDepth).toBe(1)
  })
})
