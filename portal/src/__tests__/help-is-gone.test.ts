/**
 * HELP IS GONE, AND THE HARD PART OF REMOVING A CONTROL IS NEVER THE JSX.
 *
 * This repo's own scar is the shape being guarded against: a Help page that went on describing a
 * button removed months earlier, because a deletion touched the component and nothing else. So
 * the check is not "is the file gone" — `tsc` already answers that — it is whether the PRODUCT
 * still names a capability it no longer has: a route, a module, a link, or a sentence telling
 * somebody to go there.
 *
 * SOURCE TEXT, NOT A RENDER. A copy string that names Help is a defect whether or not any test
 * happens to mount the screen carrying it, and jsdom cannot mount every screen.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs'
import path from 'node:path'
import { stripComments } from './_stripComments'

const ROOT = process.cwd()
const SRC = path.resolve(ROOT, 'src')

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry)
    if (statSync(full).isDirectory()) return sourceFiles(full)
    return /\.(jsx?|tsx?)$/.test(entry) ? [full] : []
  })
}

const rel = (file: string) => path.relative(ROOT, file).split(path.sep).join('/')

/** This file, which spells every banned form on purpose so the searches can be proved to fire.
 *  Exempted the same way the connector-naming guard exempts its one catalogue. */
const THIS_FILE = 'src/__tests__/help-is-gone.test.ts'

/**
 * The route, the module, and a link to either. Deliberately NOT a bare `/help/i`: "helper",
 * "this helps" and "help you name it" are ordinary English that appears all over the product and
 * has nothing to do with the deleted page. What is banned is the ADDRESS and the MODULE.
 */
const HELP_ADDRESS = /(['"`])\/help(\/[^'"`]*)?\1|\bHelpPage\b|\bhelp-page\b/

/** Copy that sends a person somewhere that no longer exists. */
const HELP_SIGNPOST = /\b(?:see|visit|read|check|open|go to|on)\s+(?:the\s+)?Help\b/i

function offenders(pattern: RegExp): string[] {
  return sourceFiles(SRC)
    .map(rel)
    .filter((name) => name !== THIS_FILE)
    .filter((name) => pattern.test(stripComments(readFileSync(path.resolve(ROOT, name), 'utf8'))))
    .sort()
}

describe('the Help page and every way to reach it are gone', () => {
  it('no module, route or link names it', () => {
    expect(offenders(HELP_ADDRESS)).toEqual([])
  })

  it('no copy anywhere tells a person to go there', () => {
    expect(offenders(HELP_SIGNPOST)).toEqual([])
  })

  it('the page and its test are off disk', () => {
    expect(existsSync(path.join(SRC, 'pages', 'HelpPage.tsx'))).toBe(false)
    expect(existsSync(path.join(SRC, 'pages', '__tests__', 'HelpPage.test.tsx'))).toBe(false)
  })

  it('MUTANT — the searches actually fire on text that violates them', () => {
    // Without this a regex that stopped matching would report a clean tree for ever, which is
    // precisely how the original stale reference survived.
    expect(HELP_ADDRESS.test(`<Route path="/help" element={<HelpPage />} />`)).toBe(true)
    expect(HELP_ADDRESS.test(`navigate('/help')`)).toBe(true)
    expect(HELP_SIGNPOST.test('See the Help page for the full list.')).toBe(true)
    // …and pass the ordinary English the product is full of.
    expect(HELP_ADDRESS.test('const helper = () => {}')).toBe(false)
    expect(HELP_ADDRESS.test('It helps to name the app first.')).toBe(false)
    expect(HELP_SIGNPOST.test('We can help you name it.')).toBe(false)
  })

  it('the scan reads a real tree, not an empty one', () => {
    // A wrong root would make every assertion above vacuously true.
    expect(sourceFiles(SRC).length).toBeGreaterThan(100)
  })
})
