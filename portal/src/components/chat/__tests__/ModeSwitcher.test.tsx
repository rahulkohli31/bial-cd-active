/**
 * THE MODE SWITCH IS GONE — this file is its inertness guard (L8).
 *
 * WHAT USED TO BE HERE. `ModeSwitcher`: the one compact in-composer Ask / Plan / Write pill,
 * mounted on both `BuilderPage` and `ProjectBuilder`, fully controlled and switching a
 * conversation's `mode` via `turnStreamApi.switchMode` (chat mount) or local draft state
 * (project mount). ⌥P opened it; `MODES` was the FAQ's own source of truth for what the three
 * choices were called.
 *
 * WHY IT WENT. U1 collapsed the three-valued `ConversationKind` and the three-valued
 * `ConversationMode` it switched into ONE two-valued `ChatKind` (plan | build), fixed at chat
 * creation. There is no longer a mode to switch mid-conversation, so there is nothing left for
 * a switch to do — the server route it called is retired (see the backend's own guard,
 * `backend/tests/api/v1/conversations/test_mode_switch.py`), and `switchMode` itself is gone
 * from `turnStreamApi.ts`. A control wired to a deleted endpoint is not a smaller feature; it is
 * a dead one, so the repo's own convention
 * (`docs/solutions/conventions/cleanly-removing-dead-ui-controls-2026-06-23.md`) applies: delete
 * it, do not hide it behind a flag.
 *
 * WHY THIS FILE STAYS. Deleting the suite deletes the evidence. This walks the real source tree
 * — not a render — because the thing being proven is an ABSENCE: nothing under `portal/src`
 * still imports or mounts the component, and the component file itself does not exist. A render
 * test cannot prove either half of that; only reading the tree can.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs'
import path from 'node:path'

// vitest runs with cwd = the portal root (where vitest.config.ts lives), and import.meta.url is
// a jsdom http URL here — so anchor on cwd, not the module URL (matches jsx-deploy-retirement).
const SRC_ROOT = path.resolve(process.cwd(), 'src')

// WHAT COUNTS AS USING IT — and this is narrower than "the name appears", deliberately.
//
// The first version of this guard forbade the STRING anywhere under `src`, with an allowlist of
// four files permitted to mention it in prose. That is the wrong shape for an L8 guard: the
// convention it enforces asks every removal to leave a comment saying what used to be here and
// why it went, so a name-anywhere rule makes following the convention a test failure and the fix
// under time pressure is to delete the explanation. The allowlist could not keep up either — it
// went red the moment three suites were rewritten to say, correctly, that the control they used
// to exercise is gone.
//
// So the rule matches its own test name: an IMPORT of the module or a MOUNT of the component.
// Both are what a re-add would actually look like, and neither can be written by accident in a
// sentence about the past.
const USES_IT = [
  /from\s+['"][^'"]*ModeSwitcher['"]/, // import … from '…/ModeSwitcher'
  /require\(\s*['"][^'"]*ModeSwitcher['"]/, // require('…/ModeSwitcher')
  /import\(\s*['"][^'"]*ModeSwitcher['"]/, // a dynamic import of it
  /<ModeSwitcher[\s/>]/, // <ModeSwitcher … /> — the mount
  /vi\.mock\(\s*['"][^'"]*ModeSwitcher['"]/, // a mock standing in for it is still a dependency
]

// The guard itself writes those patterns down, so it would match every one of them.
const ALLOWLIST = new Set([path.join('components', 'chat', '__tests__', 'ModeSwitcher.test.tsx')])

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry)
    return statSync(full).isDirectory() ? walk(full) : [full]
  })
}

describe('ModeSwitcher is retired (U19)', () => {
  it('the component file is gone from disk', () => {
    expect(existsSync(path.join(SRC_ROOT, 'components', 'chat', 'ModeSwitcher.tsx'))).toBe(false)
  })

  it('no source file imports or mounts it', () => {
    const offenders: string[] = []
    let scanned = 0
    for (const file of walk(SRC_ROOT)) {
      const rel = path.relative(SRC_ROOT, file)
      if (ALLOWLIST.has(rel)) continue
      scanned += 1
      const text = readFileSync(file, 'utf8')
      if (USES_IT.some((pattern) => pattern.test(text))) offenders.push(rel)
    }
    // LIVENESS: the walk actually read the tree. An empty `offenders` proves nothing if `walk`
    // returned nothing — a wrong `SRC_ROOT` (this file's cwd assumption is a real hazard) would
    // otherwise make this guard pass forever while the component sat there mounted.
    expect(scanned).toBeGreaterThan(100)
    expect(offenders).toEqual([])
  })

  it('the guard would CATCH a re-add — each pattern matches the shape it is for', () => {
    // The half that stops this from being a guard nobody has tested. An absence test whose
    // matcher is subtly wrong reports the same clean result as a genuine absence.
    const reAdds = [
      "import { ModeSwitcher } from '../chat/ModeSwitcher'",
      "const { ModeSwitcher } = require('./ModeSwitcher')",
      "const M = await import('../../components/chat/ModeSwitcher')",
      '<ModeSwitcher value={kind} onSelect={setKind} />',
      "vi.mock('../../components/chat/ModeSwitcher', () => ({}))",
    ]
    for (const line of reAdds) {
      expect(USES_IT.some((pattern) => pattern.test(line)), line).toBe(true)
    }
    // …and prose about the retired control is NOT a re-add, which is the whole point of the
    // narrowing above.
    const prose = [
      '// The ModeSwitcher used to sit here; a chat kind is fixed at creation now.',
      "  * mounted on both `BuilderPage` and `ProjectBuilder` — see ModeSwitcher's guard.",
    ]
    for (const line of prose) {
      expect(USES_IT.some((pattern) => pattern.test(line)), line).toBe(false)
    }
  })
})
