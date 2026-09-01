/**
 * Guard: every vendored shadcn/ui primitive is REACHED by something.
 *
 * This directory is where speculative vendoring accumulates. `npx shadcn add` pulls a
 * component and its Radix dependency in one command, the component is never wired to a
 * surface, and nothing ever goes red — so it ships forever. It has now happened twice:
 * U27 removed two zero-reference primitives (see `smoke.test.tsx`), and this change
 * removed five more (avatar/skeleton/tooltip added speculatively by #170, plus
 * collapsible and dropdown-menu orphaned since #82) along with four Radix packages that
 * were direct dependencies of nothing.
 *
 * A comment mentioning a component does NOT count as reaching it — `popover.tsx` cites
 * `dropdown-menu.tsx` as its style precedent, and that citation was the only surviving
 * mention of a component nothing imported. So this matches IMPORT SPECIFIERS only.
 *
 * KNOWN LIMIT, stated rather than papered over: reachability here is one hop, not
 * transitive from the app's entry point. If A imports B and nothing imports A, this
 * catches A and not B — the next run, after A is deleted, catches B. That is a slower
 * guard than a real reachability walk, and a far simpler one; the failure mode is
 * "removes the pile one layer per sweep", never "lets a new orphan in unnoticed".
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'

// vitest runs with cwd = the portal root (where the vitest config lives).
const SRC_ROOT = path.resolve(process.cwd(), 'src')
const UI_DIR = path.join(SRC_ROOT, 'components', 'ui')

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry)
    return statSync(full).isDirectory() ? walk(full) : [full]
  })
}

/** Every `.tsx` primitive directly in `components/ui/`, by bare name. */
function primitives(): string[] {
  return readdirSync(UI_DIR)
    .filter((entry) => entry.endsWith('.tsx'))
    .map((entry) => entry.replace(/\.tsx$/, ''))
    .sort()
}

/**
 * Files that IMPORT `components/ui/<name>`, by specifier — `@/components/ui/x`,
 * `./ui/x`, `../ui/x`, `../../components/ui/x`. The component's own file and its own
 * test are excluded: a primitive that only its own test imports is still an orphan, and
 * that is the exact shape this catches.
 */
function importersOf(name: string, files: string[]): string[] {
  const specifier = new RegExp(
    `from\\s+['"][^'"]*(?:components/)?ui/${name}['"]|import\\s+['"][^'"]*(?:components/)?ui/${name}['"]`,
  )
  return files
    .filter((file) => {
      const rel = path.relative(SRC_ROOT, file)
      if (rel === path.join('components', 'ui', `${name}.tsx`)) return false
      if (rel.startsWith(path.join('components', 'ui', '__tests__'))) return false
      return specifier.test(readFileSync(file, 'utf8'))
    })
    .map((file) => path.relative(SRC_ROOT, file))
}

describe('vendored ui primitives', () => {
  it('the five orphaned primitives are gone from disk, not merely unimported', () => {
    // A file still on disk is a file someone can import back — and an unused Radix
    // dependency is the half of the removal that a source-only sweep leaves behind.
    const removed = ['avatar', 'skeleton', 'tooltip', 'collapsible', 'dropdown-menu']
    expect(primitives().filter((name) => removed.includes(name))).toEqual([])

    const manifest = JSON.parse(
      readFileSync(path.resolve(process.cwd(), 'package.json'), 'utf8'),
    ) as { dependencies: Record<string, string> }
    const stillDeclared = removed.filter((name) => `@radix-ui/react-${name}` in manifest.dependencies)
    // `skeleton` has no Radix package of its own; the other four did.
    expect(stillDeclared).toEqual([])
  })

  it('every remaining primitive is imported by something outside its own test', () => {
    const files = walk(SRC_ROOT)
    const orphans = primitives().filter((name) => importersOf(name, files).length === 0)
    expect(orphans).toEqual([])
  })

  it('the guard can actually fail — a name nothing imports is reported', () => {
    // Mutation-proofing the assertion above: if `importersOf` silently matched everything
    // (a broken regex, a wrong root), the orphan check would be green forever. A component
    // name that exists nowhere must come back with no importers.
    expect(importersOf('a-primitive-that-was-never-vendored', walk(SRC_ROOT))).toEqual([])
    // ...and a name that IS imported must come back with importers, so the regex is not
    // simply matching nothing.
    expect(importersOf('button', walk(SRC_ROOT)).length).toBeGreaterThan(0)
  })
})
