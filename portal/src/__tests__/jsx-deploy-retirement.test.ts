/**
 * Retirement guard (flipped from the deleted DeployBar/compileJsx suites, APPROVAL R20).
 *
 * The JSX-era deploy path — the in-browser Babel compile, the client-supplied
 * artifact, the DeployBar — is REMOVED, not hidden. This walks the real source
 * tree and fails if any retired symbol creeps back, so a reintroduction must be
 * deliberate and reviewed rather than a drive-by import. (The dead-UI doctrine:
 * remove the affordance, keep a guard that the removed path stays inert.)
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs'
import path from 'node:path'

// vitest runs with cwd = the portal root (where vitest.config.ts lives), and
// import.meta.url is a jsdom http URL here — so anchor on cwd, not the module URL.
const SRC_ROOT = path.resolve(process.cwd(), 'src')

// The guard itself and the owner-surface inertness test legitimately NAME the
// retired symbols; every other source file must not.
const ALLOWLIST = new Set([
  path.join('__tests__', 'jsx-deploy-retirement.test.ts'),
  path.join('utils', '__tests__', 'appRegistryApi.test.js'),
])

const RETIRED_TOKENS = [
  'compileJsx',
  '@babel/standalone',
  'submitApp',
  'DeployBar',
  'DEPLOY_ENABLED',
] as const

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry)
    return statSync(full).isDirectory() ? walk(full) : [full]
  })
}

describe('JSX-era deploy retirement', () => {
  it('the retired modules are gone from disk', () => {
    expect(existsSync(path.join(SRC_ROOT, 'components', 'DeployBar.jsx'))).toBe(false)
    expect(existsSync(path.join(SRC_ROOT, 'utils', 'compileJsx.js'))).toBe(false)
  })

  it('no source file references a retired symbol', () => {
    const offenders: string[] = []
    for (const file of walk(SRC_ROOT)) {
      const rel = path.relative(SRC_ROOT, file)
      if (ALLOWLIST.has(rel)) continue
      const text = readFileSync(file, 'utf8')
      for (const token of RETIRED_TOKENS) {
        if (text.includes(token)) offenders.push(`${rel}: ${token}`)
      }
    }
    expect(offenders).toEqual([])
  })
})
