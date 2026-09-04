/**
 * THE FIFTH LINK OF THE REMOVAL TRACE, made mechanical.
 *
 * This repo's convention for removing a behaviour has five links: the surface · the navigation
 * payloads · the consumers and imports · the tests-become-inertness-guards · and the human-facing
 * copy, INCLUDING COMMENTS. Wave 3 completed four. The fifth is where fifty present-tense-false
 * sentences then sat — four of them inside `ConversationSurface.tsx` describing itself as
 * `BuilderPage`, one in `App.tsx` actively wrong about routing, and a docstring in the backend
 * telling the next reader that a branch retires with the relay while a shipping endpoint depended
 * on it.
 *
 * A comment cannot be asserted, so this does the one thing that can be: it insists that a mention
 * of something DELETED reads as history. A retired name may appear — explaining why code is
 * shaped the way it is often requires naming what it replaced — but the sentence around it has to
 * say the thing is gone. "`BuilderPage` relays them to the harness" fails. "`BuilderPage` was the
 * page that matched, and it was destroyed on every project switch" passes.
 *
 * WHY A MARKER LIST RATHER THAN REAL PROSE ANALYSIS: this has to be cheap enough to stay, and
 * wrong in the harmless direction. A present-tense sentence that happens to contain "legacy"
 * slips through; that is a miss, not a false alarm. A guard that cried wolf would be deleted
 * within a month and the next wholesale deletion would leave this link undone again.
 *
 * PRODUCTION FILES ONLY. Test files legitimately name what they retired: `twoPageEra-retired`
 * and `relaunch-chain-retired` exist to pin that something is GONE, so a retirement guard's own
 * filename and prose would trip every marker in the list below. (This used to add "and seventeen
 * suites in `pages/__tests__` are still named after the page they no longer render" — that was
 * true until the rename sweep in this same branch made it zero.)
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'

const SRC_ROOT = path.resolve(process.cwd(), 'src')

/** Deleted by the two-page retirement (#170) and this change. Unambiguous identifiers only —
 *  a generic word like "relay" appears in live contexts and would only produce noise. */
const RETIRED = [
  'ChatPage',
  'BuilderPage',
  'useClaudeAPI',
  'fetchClaudeStream',
  'BuildProgress',
  '/v1/claude',
] as const

/** Past-tense markers. Deliberately generous: a miss is cheaper than a false alarm. */
const HISTORICAL = [
  'used to',
  'was ',
  'were ',
  'had ',
  'died',
  'dies with',
  'deleted',
  'retired',
  'removed',
  'gone',
  'no longer',
  'until',
  'before',
  'predates',
  'legacy',
  'old ',
  'since',
  're-homed',
  'replaced',
  'gained',
  'gave',
  'gets this',
  'gone with',
]

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry)
    if (statSync(full).isDirectory()) return entry === '__tests__' ? [] : walk(full)
    return /\.(ts|tsx|js|jsx)$/.test(entry) && !/\.test\./.test(entry) ? [full] : []
  })
}

/** The mention's line plus two either side — a comment sentence rarely fits on one line. */
function windowAround(lines: string[], index: number): string {
  return lines
    .slice(Math.max(0, index - 2), index + 3)
    .join(' ')
    .toLowerCase()
}

describe('retired names read as history', () => {
  it('no production file mentions a retired name in the present tense', () => {
    const offenders: string[] = []
    for (const file of walk(SRC_ROOT)) {
      const rel = path.relative(SRC_ROOT, file)
      if (rel === path.join('__tests__', 'retired-names-are-past-tense.test.ts')) continue
      const lines = readFileSync(file, 'utf8').split('\n')
      lines.forEach((line, i) => {
        for (const name of RETIRED) {
          if (!line.includes(name)) continue
          const context = windowAround(lines, i)
          if (!HISTORICAL.some((marker) => context.includes(marker))) {
            offenders.push(`${rel}:${i + 1}: ${name} — ${line.trim().slice(0, 90)}`)
          }
        }
      })
    }
    expect(offenders).toEqual([])
  })

  it('the guard can actually fail — a present-tense mention is reported', () => {
    // Mutation-proofing. If the marker list ever grew to match everything, the check above
    // would be green forever and this file would be worse than nothing.
    const present = ['// BuilderPage relays them to the harness.']
    const historical = ['// BuilderPage was the page that matched, and it was deleted.']
    const flags = (lines: string[]) =>
      !HISTORICAL.some((m) => windowAround(lines, 0).includes(m)) && lines[0].includes('BuilderPage')
    expect(flags(present)).toBe(true)
    expect(flags(historical)).toBe(false)
  })
})
