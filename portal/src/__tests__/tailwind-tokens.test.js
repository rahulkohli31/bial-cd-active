import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'

/**
 * An invented Tailwind token renders INVISIBLY, and no test that reads text content will ever
 * notice. `bg-bial-primary` shipped in the #83 dialog: the class does not exist, so the button
 * had no background, `text-white` painted white on a white card, and the primary action was
 * gone. Every unit test still passed — `getByRole` found it, `toBeEnabled()` was true, the
 * textContent matched. It was caught by looking at a screenshot from a live browser.
 *
 * jsdom computes no Tailwind styles, so a `getComputedStyle` assertion cannot close this in the
 * unit suite. What CAN be checked cheaply is the namespace: `bial-*` is the project's own custom
 * colour family, it is small and closed, and a reference outside it is always a typo.
 */
const ROOT = path.resolve(__dirname, '..')
const CONFIG = path.resolve(__dirname, '../../tailwind.config.js')

function sourceFiles(dir) {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry)
    if (statSync(full).isDirectory()) return entry === '__tests__' ? [] : sourceFiles(full)
    return /\.(jsx?|tsx?)$/.test(entry) ? [full] : []
  })
}

describe('tailwind custom tokens', () => {
  it('every bial-* class used in src/ actually exists in the config', () => {
    const config = readFileSync(CONFIG, 'utf8')
    const declared = new Set(
      (config.match(/bial:\s*\{([\s\S]*?)\}/)?.[1].match(/([a-zA-Z]+):/g) ?? []).map((k) =>
        k.replace(':', ''),
      ),
    )
    expect(declared.size, 'could not read the bial namespace out of tailwind.config.js').toBeGreaterThan(0)

    const offenders = []
    for (const file of sourceFiles(ROOT)) {
      const text = readFileSync(file, 'utf8')
      for (const [, token] of text.matchAll(/\b(?:bg|text|border|ring|fill|stroke)-bial-([a-z][a-z-]*)/g)) {
        const base = token.split('/')[0]
        if (!declared.has(base)) offenders.push(`${path.relative(ROOT, file)} → bial-${base}`)
      }
    }
    expect(offenders, `unknown bial-* tokens (they render as NOTHING):\n${offenders.join('\n')}`).toEqual([])
  })
})
