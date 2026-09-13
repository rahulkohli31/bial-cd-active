/**
 * Guard: every vendored shadcn/ui primitive is REACHED by something. `npx shadcn add` pulls a
 * component and its Radix dependency together; if nothing ever imports it, nothing goes red, so
 * it ships forever (see also `smoke.test.tsx`, an earlier pass at the same problem).
 *
 * A comment citing a component does NOT count as reaching it, so this matches IMPORT SPECIFIERS
 * only — a stray "see also" citation was once the only surviving mention of an orphan.
 *
 * KNOWN LIMIT: reachability is one hop, not transitive. If A imports B and nothing imports A,
 * this catches A, then B on the next run after A is deleted — slower than a real reachability
 * walk, but it never lets a new orphan in unnoticed.
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
  it('the primitives removed as orphans are gone from disk, not merely unimported', () => {
    // A file still on disk can be imported back, and an unused Radix dependency is the half
    // of a removal a source-only sweep misses — hence checking both disk and package.json.
    //
    // `skeleton` and `tooltip` are deliberately NOT in this list: removed once for having no
    // consumer, they gained one later (projects-list loading state; row tooltip) and are
    // vendored again on purpose. The second test below is what actually enforces "no
    // orphans", and covers them too.
    //
    // `dropdown-menu` LEFT THIS LIST for the same reason, and the reversal is deliberate: the
    // avatar menu in `Navbar.tsx` was hand-rolled, so the primitive really was an orphan; it is
    // now that menu's implementation, which is a consumer the second test below enforces.
    const removed = ['avatar', 'collapsible']
    expect(primitives().filter((name) => removed.includes(name))).toEqual([])

    const manifest = JSON.parse(
      readFileSync(path.resolve(process.cwd(), 'package.json'), 'utf8'),
    ) as { dependencies: Record<string, string> }
    const stillDeclared = removed.filter((name) => `@radix-ui/react-${name}` in manifest.dependencies)
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
