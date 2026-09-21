/**
 * WHY THIS EXISTS — the portal's reduced-motion guarantee is asserted against the STYLESHEET
 * SOURCE, because nothing in the unit environment can evaluate it.
 *
 * WHY NOT A DOM TEST. jsdom parses `@media` rules but evaluates none of them, and no Tailwind
 * utility exists in the unit environment at all. A test that mounted a page and asserted "nothing
 * animates" would therefore pass on an EMPTY page, on a page whose spinners all spin, and on a
 * build where the suppression block was deleted outright — the exact shape of guard that reports
 * green while the product regresses. What can be checked honestly here is the source text, so
 * every rule below is a pure function over a CSS or JS string.
 *
 * WHAT WAS ACTUALLY BROKEN. Thirty-odd `.animate-spin` / `.animate-pulse` / `.animate-bounce`
 * sites ignored the operating-system preference while `tailwind.config.js` stated in prose that
 * `index.css` was "where every other one in this build is suppressed too". Nothing was broken
 * except the sentence, and the sentence is what stopped anyone from looking. So the docblocks are
 * asserted here alongside the CSS: a future edit that re-falsifies them goes red.
 *
 * SOURCE ORDER IS THE MECHANISM, WHICH IS WHY IT IS ASSERTED. The block overrides Tailwind's own
 * `.animate-spin` purely because it sits AFTER `@tailwind utilities` at equal specificity and
 * outside any `@layer`. A tidy-up that moved it into a layer would keep every selector assertion
 * green while silently restoring every spinner — invisible in the same way that bug was.
 *
 * EACH RULE IS ASSERTED TWICE: once against the real file, once against a fixture KNOWN to break
 * it. Without the second, a parser whose regex stops matching passes for ever and protects
 * nothing.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'
import { nestingDepthAt } from './_cssNesting'
import { stripComments } from './_stripComments'

// `import.meta.url` is a jsdom http URL under this vitest config, so anchor on the cwd — the same
// anchor `nginx-apps-routing.test.ts` uses, and vitest runs with `portal/` as the cwd.
const ROOT = process.cwd()
const CSS_FILE = 'src/index.css'
const CONFIG_FILE = 'tailwind.config.js'
const ENTRY_FILE = 'src/main.tsx'
const CSS = readFileSync(path.resolve(ROOT, CSS_FILE), 'utf8')
const CONFIG = readFileSync(path.resolve(ROOT, CONFIG_FILE), 'utf8')
const ENTRY = readFileSync(path.resolve(ROOT, ENTRY_FILE), 'utf8')

/** The seven selectors the block must neutralise. The first two are the app pane; the next three
 *  are every perpetual wait in the product, the bug named above; the last two are BIAL Chat's
 *  backdrop, which drifts and flies for as long as that screen is open — the same class of
 *  never-ending motion, arriving as a decoration rather than as a wait. */
const REQUIRED_SELECTORS = [
  '.animate-pane-leave',
  '.animate-pane-return',
  '.animate-spin',
  '.animate-pulse',
  '.animate-bounce',
  '.chat-mote',
  '.chat-plane',
] as const

/** The other mechanism: a subscribed `matchMedia` boolean, for the places that swap the ELEMENT
 *  rather than the motion. `the tree agrees` below is what keeps that naming honest.
 *
 *  IT IS ONE FILE NOW, AND THAT IS THE FIX. It used to be three — `OfferStrip`,
 *  `StopTurnControl`, `ToolActivityLine` — each carrying its own copy of the branch, and all
 *  three carrying the SAME defect: they dropped `animate-spin` and kept the `Loader2`, so under
 *  the preference the product rendered a motionless spinner, which reads as a hang rather than as
 *  an accommodation. Three copies of a branch is how three copies of one bug happen. The branch
 *  now lives once, in `ui/Waiting.tsx`, and every wait in the portal goes through it. */
const HOOK_CONSUMERS = ['Waiting'] as const

/** The third mechanism: a root `MotionConfig`, which is the only layer that reaches `motion`'s
 *  JS-driven animation. `index.css`'s block cannot see it and the hook swaps an element rather
 *  than a motion, so without this the library would ignore the preference while both docblocks
 *  claimed full coverage — the exact falsehood this file exists to prevent. `the tree agrees`
 *  below proves the config is really wired rather than merely written about. */
const ROOT_MOTION_CONFIG = 'MotionConfig'

// ------------------------------------------------------------------------------------------
// A very small CSS reader. Comments are stripped first: this stylesheet's prose quotes the very
// selectors and at-rules being counted, and a parser that read them would find the block inside
// its own description.
// ------------------------------------------------------------------------------------------

interface ReduceMotionBlock {
  /** Declaration body of the `@media (prefers-reduced-motion: reduce)` rule. */
  body: string
  /** Offset of the `@media` at-rule, in the comment-stripped source. */
  startsAt: number
  /** Offset of `@tailwind utilities`, in the same string, so the two are comparable. */
  utilitiesAt: number
  /** Open braces still unclosed where the block starts. Anything but 0 means it is nested. */
  nestingDepth: number
}

function reduceMotionBlock(css: string): ReduceMotionBlock {
  const source = stripComments(css)
  const utilitiesAt = source.indexOf('@tailwind utilities')
  if (utilitiesAt < 0) throw new Error('no `@tailwind utilities` in the stylesheet')

  const opener = /@media[^{}]*prefers-reduced-motion[^{}]*\{/.exec(source)
  if (!opener) throw new Error('no `@media (prefers-reduced-motion: reduce)` block in the stylesheet')
  const startsAt = opener.index
  const bodyFrom = startsAt + opener[0].length

  let depth = 1
  let cursor = bodyFrom
  while (cursor < source.length && depth > 0) {
    const c = source[cursor]
    if (c === '{') depth += 1
    else if (c === '}') depth -= 1
    cursor += 1
  }
  if (depth !== 0) throw new Error('the reduce-motion block is never closed')

  return {
    body: source.slice(bodyFrom, cursor - 1),
    startsAt,
    utilitiesAt,
    nestingDepth: nestingDepthAt(source, startsAt),
  }
}

/** Selectors the block actually turns OFF — a selector listed on a rule that does not set
 *  `animation: none` has been disarmed just as surely as one that was deleted. */
function suppressedSelectors(body: string): Set<string> {
  const found = new Set<string>()
  const rule = /([^{}]+)\{([^{}]*)\}/g
  let match: RegExpExecArray | null
  while ((match = rule.exec(body)) !== null) {
    if (!/(^|;)animation:none(;|$)/.test(match[2].replace(/\s+/g, ''))) continue
    for (const selector of match[1].split(',')) {
      const trimmed = selector.trim()
      if (trimmed) found.add(trimmed)
    }
  }
  return found
}

function missingSelectors(css: string): string[] {
  const suppressed = suppressedSelectors(reduceMotionBlock(css).body)
  return REQUIRED_SELECTORS.filter((selector) => !suppressed.has(selector))
}

/** The one comment in a file that documents the guarantee. Selected by content rather than by
 *  position so reordering a file does not silently select a different comment. */
function motionDocblock(text: string): string {
  const comments = text.match(/\/\*[\s\S]*?\*\//g) ?? []
  const documenting = comments.filter((c) => c.includes('usePrefersReducedMotion'))
  if (documenting.length !== 1) {
    throw new Error(`expected exactly 1 reduced-motion docblock, found ${documenting.length}`)
  }
  return documenting[0]
}

/** What a docblock must name to be true: both mechanisms, everything this block covers, and the
 *  explicit statement that there is no third. */
function unnamedInDocblock(docblock: string): string[] {
  const required = [
    'usePrefersReducedMotion',
    ...HOOK_CONSUMERS,
    ROOT_MOTION_CONFIG,
    '.animate-spin',
    '.animate-pulse',
    '.animate-bounce',
  ]
  const missing = required.filter((name) => !docblock.includes(name))
  if (!/no fourth/i.test(docblock)) missing.push('the "no fourth mechanism" statement')
  return missing
}

/** Every non-test source file that CALLS the hook. */
function hookConsumersInTree(): string[] {
  const walk = (dir: string): string[] =>
    readdirSync(dir).flatMap((entry) => {
      const full = path.join(dir, entry)
      if (statSync(full).isDirectory()) return entry === '__tests__' ? [] : walk(full)
      return /\.(jsx?|tsx?)$/.test(entry) ? [full] : []
    })
  return walk(path.resolve(ROOT, 'src'))
    .filter((file) => /usePrefersReducedMotion\s*\(\s*\)/.test(stripComments(readFileSync(file, 'utf8'))))
    .map((file) => path.basename(file).replace(/\.[jt]sx?$/, ''))
    .sort()
}

// ------------------------------------------------------------------------------------------
// Fixtures: minimal stylesheets that are correct except for the one thing each is named for.
// ------------------------------------------------------------------------------------------

const RULE = `  ${REQUIRED_SELECTORS.join(',\n  ')} {\n    animation: none;\n  }`
const BLOCK = `@media (prefers-reduced-motion: reduce) {\n${RULE}\n}`
const HEALTHY_FIXTURE = `@tailwind base;\n@tailwind utilities;\n\nbody { color: red; }\n\n${BLOCK}\n`
const FIXTURE_MISSING_A_SELECTOR = HEALTHY_FIXTURE.replace('  .animate-spin,\n', '')
const FIXTURE_BLOCK_BEFORE_UTILITIES = `@tailwind base;\n\n${BLOCK}\n\n@tailwind utilities;\n`
const FIXTURE_BLOCK_INSIDE_A_LAYER = `@tailwind base;\n@tailwind utilities;\n\n@layer utilities {\n${BLOCK}\n}\n`

describe('the reduce-motion block suppresses every animation utility the portal waits with', () => {
  it('names all five selectors and sets `animation: none` on each', () => {
    expect(missingSelectors(CSS)).toEqual([])
  })

  it('reads the real block, not an empty one', () => {
    // Guards the parser itself: a regex that stopped matching would report "nothing missing".
    expect(suppressedSelectors(reduceMotionBlock(CSS).body).size).toBeGreaterThanOrEqual(
      REQUIRED_SELECTORS.length,
    )
  })

  it('MUTANT — a stylesheet with one selector deleted is caught', () => {
    expect(missingSelectors(HEALTHY_FIXTURE)).toEqual([])
    expect(missingSelectors(FIXTURE_MISSING_A_SELECTOR)).toEqual(['.animate-spin'])
  })
})

describe('source order is the mechanism, so it is asserted', () => {
  it('the block sits after `@tailwind utilities`', () => {
    // Equal specificity: `.animate-spin { animation: none }` beats Tailwind's own `.animate-spin`
    // only by coming later in the sheet.
    const { startsAt, utilitiesAt } = reduceMotionBlock(CSS)
    expect(startsAt).toBeGreaterThan(utilitiesAt)
  })

  it('the block is nested inside nothing — no `@layer`, no other at-rule', () => {
    expect(reduceMotionBlock(CSS).nestingDepth).toBe(0)
  })

  it('MUTANT — a stylesheet with the block moved above `@tailwind utilities` is caught', () => {
    const healthy = reduceMotionBlock(HEALTHY_FIXTURE)
    expect(healthy.startsAt).toBeGreaterThan(healthy.utilitiesAt)

    const moved = reduceMotionBlock(FIXTURE_BLOCK_BEFORE_UTILITIES)
    expect(moved.startsAt).toBeLessThan(moved.utilitiesAt)
    // The five selectors survive the move untouched — which is exactly why the order assertion
    // has to exist separately.
    expect(missingSelectors(FIXTURE_BLOCK_BEFORE_UTILITIES)).toEqual([])
  })

  it('MUTANT — a stylesheet with the block wrapped in `@layer utilities` is caught', () => {
    expect(reduceMotionBlock(HEALTHY_FIXTURE).nestingDepth).toBe(0)
    expect(reduceMotionBlock(FIXTURE_BLOCK_INSIDE_A_LAYER).nestingDepth).toBe(1)
    expect(missingSelectors(FIXTURE_BLOCK_INSIDE_A_LAYER)).toEqual([])
  })
})

describe('both docblocks name the mechanisms that exist', () => {
  it.each([
    [CSS_FILE, CSS],
    [CONFIG_FILE, CONFIG],
  ])('%s names both mechanisms, every consumer, and no third', (_file, text) => {
    expect(unnamedInDocblock(motionDocblock(text))).toEqual([])
  })

  it('the tree agrees: the root config the docblocks name is really wired', () => {
    // The docblocks now claim a mechanism that lives in a THIRD file, so claiming it is not the
    // same as having it. Asserted against the entry module rather than against a render: the
    // preference is a media query jsdom evaluates for nothing, so a mounted assertion here would
    // be the empty-page pass this whole file exists to avoid.
    expect(ENTRY).toContain(ROOT_MOTION_CONFIG)
    expect(ENTRY).toMatch(/reducedMotion=("user"|\{'user'\}|\{"user"\})/)
  })

  it('MUTANT — an entry that drops the root config, or asks for the wrong mode, is caught', () => {
    // `reducedMotion="never"` is the dangerous mutant: it type-checks, renders, and silently
    // opts the whole product OUT of the preference while both docblocks still promise it.
    // `replaceAll`, for the reason the docblock mutants below spell out: `main.tsx` names the
    // mode in its own prose as well as in the JSX, so mutating only the first occurrence leaves
    // the real one standing and the mutant passes without having mutated anything.
    const stripped = ENTRY.replaceAll(ROOT_MOTION_CONFIG, 'SomeOtherProvider')
    expect(stripped).not.toContain(ROOT_MOTION_CONFIG)
    expect(ENTRY.replaceAll('reducedMotion="user"', 'reducedMotion="never"')).not.toMatch(
      /reducedMotion=("user"|\{'user'\}|\{"user"\})/,
    )
  })

  it('the tree agrees: exactly the consumers the docblocks name', () => {
    // This is what keeps the prose true rather than merely well-written. A second consumer, or a
    // renamed one, goes red here and the docblocks get corrected with it — and a SECOND consumer
    // is now itself the smell, since the whole point of `ui/Waiting.tsx` is that the branch is
    // written once.
    expect(hookConsumersInTree()).toEqual([...HOOK_CONSUMERS])
  })

  it('MUTANT — a docblock that drops a consumer, or the "no third" statement, is caught', () => {
    const real = motionDocblock(CSS)
    // `replaceAll`, not `replace`: the docblock names the module more than once (the path, the
    // primitive, `WaitingLine`), so mutating only the first occurrence leaves the name still
    // present and the mutant passes for the wrong reason — a mutant that does not mutate proves
    // nothing about the assertion it is meant to be testing.
    expect(unnamedInDocblock(real.replaceAll('Waiting', 'SomeOtherThing'))).toEqual(['Waiting'])
    expect(unnamedInDocblock(real.replaceAll(ROOT_MOTION_CONFIG, 'SomeProvider'))).toEqual([
      ROOT_MOTION_CONFIG,
    ])
    expect(unnamedInDocblock(real.replace(/THERE IS NO FOURTH/i, 'THERE IS ONE MORE'))).toEqual([
      'the "no fourth mechanism" statement',
    ])
  })
})
