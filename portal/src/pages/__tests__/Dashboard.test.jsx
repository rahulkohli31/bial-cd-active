/**
 * The welcome page is gone — this file is its INERTNESS GUARD (#158 §7 / §16.1).
 *
 * `pages/Dashboard.tsx` was a screen whose whole purpose was a button to `/projects`. Once
 * the project list carries the three summary numbers, that hop has nothing left to do, so
 * the page was deleted and `/dashboard` became a redirect.
 *
 * §16.1 is explicit that this suite should NOT simply be deleted: a removal is only real
 * when nothing can quietly bring it back. So instead of testing a component that no longer
 * exists, this asserts the module is absent and that nothing imports it. Re-adding the page
 * fails here, and has to be argued for rather than landed beside the new landing screen.
 *
 * IT CHECKS THE FILESYSTEM, not a dynamic import: Vite resolves imports at transform time,
 * so `import('../Dashboard')` inside a test is a build error rather than a rejected promise
 * — the suite would fail to load instead of reporting. Same reason
 * `jsx-deploy-retirement.test.ts` walks the tree.
 *
 * The route-level half — `/dashboard` renders the project list and no welcome page — lives
 * in `App.test.jsx`, where the route table is.
 */
import { describe, it, expect } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const PAGES = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const SRC = path.dirname(PAGES)

function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) return entry.name === 'node_modules' ? [] : walk(full)
    return /\.(ts|tsx|js|jsx)$/.test(entry.name) ? [full] : []
  })
}

describe('the welcome page stays deleted', () => {
  it('has no module file', () => {
    for (const ext of ['tsx', 'ts', 'jsx', 'js']) {
      expect(fs.existsSync(path.join(PAGES, `Dashboard.${ext}`))).toBe(false)
    }
  })

  it('is imported by nothing', () => {
    // The stale `vi.mock('./pages/Dashboard')` in App.test.jsx is exactly the kind of
    // leftover this catches: a mock of a path that no longer exists passes locally and
    // fails oddly later.
    const offenders = walk(SRC)
      .filter((file) => !file.endsWith(path.join('__tests__', 'Dashboard.test.jsx')))
      .filter((file) => /pages\/Dashboard|pages\\Dashboard/.test(fs.readFileSync(file, 'utf8')))
      .map((file) => path.relative(SRC, file))

    expect(offenders).toEqual([])
  })
})
