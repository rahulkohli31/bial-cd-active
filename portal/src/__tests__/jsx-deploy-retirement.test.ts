/**
 * Retirement guard (flipped from the deleted DeployBar/compileJsx suites, APPROVAL R20).
 *
 * The JSX-era deploy path — the in-browser Babel compile, the client-supplied
 * artifact, the DeployBar — is REMOVED, not hidden. This walks the real source
 * tree and fails if any retired symbol creeps back, so a reintroduction must be
 * deliberate and reviewed rather than a drive-by import. (The dead-UI doctrine:
 * remove the affordance, keep a guard that the removed path stays inert.)
 *
 * IT NOW ALSO GUARDS THE THREE PUBLISH CONTROLS AND THE FOUR CLIENT PREDICATES that fed
 * them, retired together when publishing collapsed onto one chip reading one
 * server-computed field. The list grew in the SAME change that deleted them — a guard that
 * lags a deletion by one commit is a failure this repo has already had.
 *
 * ONE THING IS DELIBERATELY ABSENT: the publish hook's old name. It was RENAMED, not
 * retired, and banning a renamed symbol guards nothing — it only stops anyone writing the
 * old name in a comment explaining the rename.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs'
import path from 'node:path'

// vitest runs with cwd = the portal root (where vitest.config.ts lives), and
// import.meta.url is a jsdom http URL here — so anchor on cwd, not the module URL.
const SRC_ROOT = path.resolve(process.cwd(), 'src')

// The guard itself and the owner-surface inertness tests legitimately NAME the
// retired symbols; every other source file must not. `deployApi.test.ts` is on
// the list for exactly the reason `appRegistryApi.test.js` is: it asserts that
// the retired predicates are ABSENT from the real module, which it cannot do
// without saying what they were called.
const ALLOWLIST = new Set([
  path.join('__tests__', 'jsx-deploy-retirement.test.ts'),
  path.join('utils', '__tests__', 'appRegistryApi.test.js'),
  path.join('utils', '__tests__', 'deployApi.test.ts'),
])

const RETIRED_TOKENS = [
  'compileJsx',
  '@babel/standalone',
  'submitApp',
  'DeployBar',
  'DEPLOY_ENABLED',
  // The three publish controls that collapsed into one chip, and the four client-side
  // predicates whose whole job was to re-decide, in the browser, what the server had
  // already decided. `publishState` is where those answers come from now.
  'DeployControl',
  'SubmitControl',
  'PublishButton',
  'REVIEW_STATUS_ANCHOR',
  'isLive',
  'isRoutedForReview',
  'ROUTED_FAILURE_CODES',
  'stepLabel',
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
    // The three publish surfaces, and their suites — deleted, not stubbed out. A file
    // still on disk is a file someone can import back.
    for (const gone of ['DeployControl', 'SubmitControl', 'PublishButton']) {
      expect(existsSync(path.join(SRC_ROOT, 'components', `${gone}.tsx`))).toBe(false)
      expect(existsSync(path.join(SRC_ROOT, 'components', '__tests__', `${gone}.test.tsx`))).toBe(
        false,
      )
    }
  })

  it('nothing points at the review-status anchor, which no longer exists', () => {
    // Asserted on the anchor's identifier and its HREF FORM, never on the bare substring
    // `review-status` — that legitimately survives in two files this plan keeps: the admin
    // registry panel's own live region and the questionnaire's `dc-review-status`.
    const offenders: string[] = []
    for (const file of walk(SRC_ROOT)) {
      const rel = path.relative(SRC_ROOT, file)
      if (ALLOWLIST.has(rel)) continue
      if (readFileSync(file, 'utf8').includes('#review-status')) offenders.push(rel)
    }
    expect(offenders).toEqual([])
  })

  it('speaks none of the retired pipeline phase vocabulary to a citizen', () => {
    // `stepLabel` translated the pipeline's phase tokens into citizen words in the
    // browser. The whole vocabulary is DELETED rather than restyled — while a publish runs
    // the chip says "Starting up" and stops there. `Live` and `Publish again` are
    // deliberately NOT in this list: they are labels the new chip renders.
    const RETIRED_PHASES = [
      'Getting ready',
      'Packaging your app',
      'Setting up the server',
      'Starting it up',
      // The vocabulary the retired cards spoke, on surfaces that no longer exist.
      'Review &amp; approval',
      'Waiting for review',
      'Taken down',
    ]
    const offenders: string[] = []
    for (const file of walk(SRC_ROOT)) {
      const rel = path.relative(SRC_ROOT, file)
      if (ALLOWLIST.has(rel)) continue
      const text = readFileSync(file, 'utf8')
      for (const phrase of RETIRED_PHASES) {
        if (text.includes(phrase)) offenders.push(`${rel}: ${phrase}`)
      }
    }
    expect(offenders).toEqual([])
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
