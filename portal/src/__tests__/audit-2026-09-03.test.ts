/**
 * U13 — THE AUDIT'S RECORD, MADE MECHANICAL. Two controls, one file, because both exist for the
 * same reason: a dated sentence in a comment cannot fail, and this sweep's whole finding was that
 * prose about what is kept and why rots faster than the code it describes.
 *
 * ── 1. THE MARKER CONVENTION ──
 *
 * The 2026-09-03 residue audit refuted 136 of 380 findings, 118 of them because the code exists on
 * purpose. Those live under one greppable stem so the next sweep verifies instead of re-deriving:
 *
 *     AUDIT-<date> · canvas-divergence: <what the board says> — <what ships> — <what settles it>
 *     AUDIT-<date> · verified-alive:    intentionally retained pending verification — …
 *
 * This asserts only that the stem never appears WITHOUT a date and one of those two class names —
 * i.e. that the convention cannot decay into an untyped `TODO`, which is what every previous
 * marker scheme in this tree became. It emphatically does NOT prove a marker is still TRUE; only
 * re-verification does that, and the two divergence guards below are the shape that can.
 *
 * WHY THE `verified-alive` MARKERS SAY SO LITTLE. This repository is public. A marker naming the
 * reachability path, the external caller and the blast radius would be the more useful comment —
 * and a dozen of them under one greppable stem would compose into an indexed map of endpoints and
 * scripts that look retired but are live, published by us. So the in-code marker carries the stem,
 * the date and the fact of intentional retention, and nothing else; the detail lives in the
 * git-ignored audit record a reader reaches by grepping the stem. That is why this guard checks
 * shape rather than content.
 *
 * ── 2. THE `Removals` BOARD'S STOP ROW (D2) ──
 *
 * See its own describe below. The board and the code disagree, the code won, and the disagreement
 * is asserted so the day someone acts on the board it goes red instead of quietly shipping.
 *
 * KNOWN LIMIT, stated rather than papered over: the scan below covers a fixed extension allowlist
 * under the repository root, minus vendored and generated trees. A marker written into a file type
 * nobody writes comments in — a lockfile, a binary — escapes it. The failure mode is a missed
 * marker, never a false alarm, which is the direction a guard has to fail in to survive.
 */
import { describe, it, expect } from 'vitest'
import { existsSync, lstatSync, readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'

// vitest runs with cwd = the portal root; the markers live across the whole repository.
const REPO_ROOT = path.resolve(process.cwd(), '..')

/** This file states ill-formed examples on purpose, so it cannot be one of its own subjects. */
const SELF = path.join('portal', 'src', '__tests__', 'audit-2026-09-03.test.ts')

const SKIP_DIRS = new Set([
  '.git', 'node_modules', 'dist', 'build', 'coverage', '__pycache__', '.venv', 'venv',
  '.next', '.pytest_cache', '.ruff_cache', '.mypy_cache', 'htmlcov', 'playwright-report',
  'test-results', 'site-packages',
  // Git-ignored governance trees. `docs/` holds the plan and the findings record, both of which
  // quote the stem as a specification — scanning them would assert the convention against the
  // document that defines it.
  'docs', '.claude', '.vulcan', '.mythos',
])

const SCANNED_EXT = new Set([
  '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.py', '.conf', '.md', '.css', '.html',
  '.yml', '.yaml', '.sh', '.toml', '.txt',
])

const MAX_BYTES = 512 * 1024

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    if (SKIP_DIRS.has(entry)) return []
    const full = path.join(dir, entry)
    // `lstat`, not `stat`: a symlinked directory would otherwise be descended into, and a link
    // that pointed at an ancestor would recurse until the stack gave out. A link reads as a
    // non-directory here and then fails the extension filter, which is the right answer for a
    // scan whose subject is checked-in source.
    const stat = lstatSync(full)
    if (stat.isDirectory()) return walk(full)
    if (stat.size > MAX_BYTES) return []
    const named = /^(Dockerfile|Caddyfile)/.test(entry)
    return named || SCANNED_EXT.has(path.extname(entry)) ? [full] : []
  })
}

/** Any use of the stem at all — deliberately wider than the well-formed shape, so a marker that
 *  drifted out of the convention is still FOUND and then reported, rather than skipped. */
const STEM = /AUDIT-\d{4}-\d{2}-\d{2}/
/** The convention: a date, one of the two class names, and something after the colon. */
const WELL_FORMED = /AUDIT-\d{4}-\d{2}-\d{2} · (?:canvas-divergence|verified-alive): +\S/

interface Marker { site: string; text: string; wellFormed: boolean; cls: string | null }

/** Every marker in one source, as a pure function of its text — so the fixture below can drive
 *  the SAME extractor the repo scan uses, without needing a marker to exist on disk. */
function markersIn(rel: string, source: string): Marker[] {
  if (!STEM.test(source)) return []
  const found: Marker[] = []
  source.split('\n').forEach((line, i) => {
    if (!STEM.test(line)) return
    const cls = /· (canvas-divergence|verified-alive):/.exec(line)?.[1] ?? null
    found.push({
      site: `${rel}:${i + 1}`,
      text: line.trim(),
      wellFormed: WELL_FORMED.test(line),
      cls,
    })
  })
  return found
}

const scanned = walk(REPO_ROOT).map((file) => path.relative(REPO_ROOT, file))

function markers(): Marker[] {
  return scanned
    .filter((rel) => rel !== SELF)
    .flatMap((rel) => markersIn(rel, readFileSync(path.join(REPO_ROOT, rel), 'utf8')))
}

describe('the AUDIT-<date> marker convention', () => {
  const all = markers()

  it('never appears without a date and one of the two class names', () => {
    const malformed = all.filter((m) => !m.wellFormed).map((m) => `${m.site}: ${m.text.slice(0, 100)}`)
    expect(malformed).toEqual([])
  })

  it('the scan is not vacuously green — it reaches both halves of the repo and the extractor works', () => {
    // LIVENESS, and deliberately NOT "a marker exists". The check above is an ABSENCE check, and
    // an absence check with a broken finder is green for ever — but an earlier draft proved that
    // by asserting the backlog still had markers in it, which made this file go RED on its own
    // success: finishing the audit and removing the last marker would have failed the guard that
    // exists to police markers. So liveness is proven the two ways that stay true afterwards.
    //
    // One: the walk reaches the repository. If `REPO_ROOT` resolved somewhere else — or `walk`
    // silently returned nothing — these fail instead of passing on an empty set.
    expect(scanned.filter((rel) => rel.startsWith('portal/')).length).toBeGreaterThan(50)
    expect(scanned.filter((rel) => rel.startsWith('backend/')).length).toBeGreaterThan(50)
    expect(scanned).toContain(SELF)

    // Two: the extractor finds and CLASSIFIES a marker, driven on a fixture rather than on
    // whatever happens to be checked in. If `STEM` or the class regex drifted, this fails
    // whether or not a real marker is left anywhere in the tree.
    const found = markersIn('fixture.ts', [
      'const x = 1',
      '// AUDIT-2026-09-03 · verified-alive: intentionally retained pending verification',
      '// AUDIT-2026-09-03 · canvas-divergence: the board says X — Y ships — Z settles it',
    ].join('\n'))
    expect(found.map((m) => m.cls)).toEqual(['verified-alive', 'canvas-divergence'])
    expect(found.map((m) => m.site)).toEqual(['fixture.ts:2', 'fixture.ts:3'])
    expect(found.every((m) => m.wellFormed)).toBe(true)
  })

  it('the guard can actually fail — a stem without a class, or with an unknown one, is reported', () => {
    // Mutation-proofing the shape check. A regex that had drifted into matching everything would
    // keep the assertion above green while the convention rotted into an untyped TODO, which is
    // the exact decay this file exists to prevent.
    const shaped = (line: string) => WELL_FORMED.test(line)
    expect(shaped('# AUDIT-2026-09-03 · verified-alive: intentionally retained pending verification')).toBe(true)
    expect(shaped('# AUDIT-2026-09-03 · canvas-divergence: the board says X — Y ships')).toBe(true)
    expect(shaped('# AUDIT-2026-09-03: come back to this')).toBe(false)
    expect(shaped('# AUDIT-2026-09-03 · someday: come back to this')).toBe(false)
    expect(shaped('# AUDIT · verified-alive: intentionally retained')).toBe(false)
    expect(shaped('# AUDIT-2026-09-03 · verified-alive:')).toBe(false)
    // ...and the wide finder must still catch the malformed ones, or they would never be reported.
    expect(STEM.test('# AUDIT-2026-09-03: come back to this')).toBe(true)
  })
})

/**
 * D2 — THE STOP CONTROL SHIPS, AND THE BOARD SAYS IT SHOULD NOT.
 *
 * The `Removals` board removes "both Stop buttons", on the grounds that stopping a response is
 * being rebuilt on its own and nothing ships in its place. It does ship: `StopTurnControl` is a
 * live component, imported and rendered by the composer both kinds of chat mount. That was settled
 * in the code's favour, so the BOARD is the stale artifact and redrawing it is the fix.
 *
 * WHY A TEST AND NOT A MARKER. A marker states the disagreement; only this can notice when the
 * disagreement ends. Someone sweeping from the board would delete the component and every existing
 * Stop test would go red for the ordinary reason — "a control was removed" — with nothing saying
 * the removal contradicts a decision. This says it, in the failure message, at the moment it
 * matters. It is the pattern this repo already uses for retirements
 * (`components/ui/__tests__/no-orphan-primitives.test.ts`, `retired-names-are-past-tense.test.ts`);
 * the difference is that those pin something GONE and this pins something KEPT.
 */
describe('canvas divergence — the Removals board removes both Stop buttons', () => {
  const CHAT = path.join(process.cwd(), 'src', 'components', 'chat')
  const control = path.join(CHAT, 'StopTurnControl.tsx')

  const imports = (source: string) => /import\s+StopTurnControl[^\n]*from\s+'\.\/StopTurnControl'/.test(source)
  const renders = (source: string) => /<StopTurnControl\b/.test(source)

  it('still ships: the control exists and the composer both kinds mount imports and renders it', () => {
    expect(existsSync(control)).toBe(true)
    const composer = readFileSync(path.join(CHAT, 'Composer.tsx'), 'utf8')
    expect(imports(composer)).toBe(true)
    expect(renders(composer)).toBe(true)
  })

  it('carries the divergence marker, which is the only pointer to why it survived the sweep', () => {
    expect(/· canvas-divergence:/.test(readFileSync(control, 'utf8'))).toBe(true)
  })

  it('the guard can actually fail — an unwired composer is reported', () => {
    // Mutation-proofing. Both predicates above are presence checks against a regex; a regex that
    // matched any source at all would make this describe permanently green.
    const unwired = "import ComposerBox from './ComposerBox'\nreturn <ComposerBox />"
    expect(imports(unwired)).toBe(false)
    expect(renders(unwired)).toBe(false)
  })
})
