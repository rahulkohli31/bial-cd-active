/**
 * THE TWO-PAGE ERA IS OVER — the inertness guard (L8, Plan D U17).
 *
 * ══ WHAT USED TO BE HERE, AND WHY IT IS ONE FILE NOW ══
 *
 * Eleven suites, 144 blocks, all pinning behaviour of modules this unit deleted:
 *
 *   BuildProgress.test.tsx            50  the pinned build-progress card
 *   ChatPage-sendpath.test.jsx        24  the legacy relay send, `useClaudeAPI` mocked at every case
 *   ChatPage-dragdrop.test.jsx        13  drag-and-drop onto the planning page's own composer
 *   ChatPage-surface.test.tsx          7  the planning page's context-length guardrail
 *   ChatPage-launchbuilder.test.jsx    4  the Launch Builder modal and its summarize step
 *   useClaudeAPI-retry.test.js        21  the legacy stream reader's timeouts and retries
 *   useClaudeAPI-estimate.test.js     10  `estimateConversationTokens`, incl. deck parts
 *   useClaudeAPI-suspended.test.js     3  the suspended-account arm of the legacy fetch
 *   contextLimits.test.js              5  `getContextLimits`
 *   buildSystemPrompt.test.js          2  that the legacy hook exported what it exported
 *   PlanOptionsCard.test.tsx           5  the in-transcript plan card
 *
 * NOT ONE OF THEM DESCRIBES A CAPABILITY THAT STILL EXISTS. That is the test for whether a suite
 * converts or dies: a suite whose subject was REPLACED gets re-pointed at the replacement, and a
 * suite whose subject was REMOVED becomes an assertion of absence. These are the second kind —
 * `useClaudeAPI` has no successor (the turn stream is a different transport with its own suites),
 * the planning page has no successor (one surface serves both kinds), and the plan card was
 * replaced by a control on the composer with its own file.
 *
 * WHERE THE BEHAVIOUR THEY DESCRIBED IS PINNED NOW, so this is a redistribution rather than a
 * loss — and so a reader can check the claim:
 *
 *   the send discipline        → `BuilderPage-projectfirst`, `-composer`, `-persistence`
 *                                (persist-before-stream, the N8 both-bubble rollback, the gate)
 *   drag-and-drop + the draft  → `components/chat/__tests__/Composer.test.tsx`
 *   the length guardrail       → `utils/__tests__/composerCap.test.ts`
 *   the plan offer             → `components/chat/__tests__/OfferStrip.test.tsx`
 *   the stream reader          → `utils/__tests__/turnStreamApi.test.ts`
 *   the build narrative        → `components/chat/__tests__/ActivityGroup.test.tsx`, and the
 *                                at-limit half moved WITH ITS FUNCTION to
 *                                `utils/__tests__/turnNarrative.test.ts`
 *
 * The counts above are named in the PR so the suite arithmetic reconciles rather than being waved
 * through: 144 blocks removed here, plus 6 in U13's `attachmentInput-deck.test.js`, against the
 * new files listed above.
 */
import { describe, it, expect } from 'vitest'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const THIS_FILE = fileURLToPath(import.meta.url)
const SRC_ROOT = path.resolve(path.dirname(THIS_FILE), '../..')

/** The modules this unit deleted, by the path anything importing them would have used. */
const DELETED = [
  'pages/BuilderPage.tsx',
  'pages/ChatPage.tsx',
  'hooks/useClaudeAPI.ts',
  'components/chat/BuildProgress.tsx',
  'components/chat/PlanOptionsCard.tsx',
  'components/AttachmentLightbox.tsx',
]

/** Every source file, so the import sweep below reads the shipped tree rather than a guess. */
function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules') continue
      sourceFiles(full, out)
    } else if (/\.(ts|tsx|js|jsx)$/.test(entry.name)) {
      out.push(full)
    }
  }
  return out
}

describe('the deleted modules are gone and nothing reaches for them (L8)', () => {
  it('none of the six files exists', () => {
    for (const rel of DELETED) {
      expect(existsSync(path.join(SRC_ROOT, rel)), `${rel} still exists`).toBe(false)
    }
    // THE LIVENESS HALF, and it is not decoration: every assertion above is an absence, and a
    // `SRC_ROOT` pointing at the wrong directory would make all six pass while proving nothing.
    expect(existsSync(path.join(SRC_ROOT, 'components/chat/ConversationSurface.tsx'))).toBe(true)
  })

  it('no file imports any of them, under any spelling', () => {
    // This file NAMES all six in its own docblock and inventory, so it must exclude itself or the
    // sweep reports the guard as the violation.
    const files = sourceFiles(SRC_ROOT).filter((f) => f !== THIS_FILE)
    const offenders: string[] = []
    for (const file of files) {
      const source = readFileSync(file, 'utf8')
      for (const rel of DELETED) {
        const name = path.basename(rel).replace(/\.(tsx?|jsx?)$/, '')
        // Matches `from '…/Name'`, `require('…/Name')`, `import('…/Name')` and `vi.mock('…/Name')`
        // — a mock standing in for a deleted module is still a dependency on it, and is exactly
        // how a suite goes on passing against a module that no longer ships.
        const reach = new RegExp(`(from|require\\(|import\\(|vi\\.mock\\()\\s*['"][^'"]*/${name}['"]`)
        if (reach.test(source)) offenders.push(`${path.relative(SRC_ROOT, file)} → ${name}`)
      }
    }
    expect(offenders).toEqual([])
  })

  it('no `kind ===` comparison survives under pages/ — R72, mechanically', () => {
    // ChatRoute still RESOLVES a kind and hands it to the slot; what has stopped is anything
    // branching on it. The slot's own file is checked too, because that is where the branch this
    // unit deleted actually lived.
    for (const rel of ['pages', 'components/workspace']) {
      for (const file of sourceFiles(path.join(SRC_ROOT, rel))) {
        if (file.includes('__tests__')) continue
        const source = readFileSync(file, 'utf8')
        // The KIND VALUES, not the word `kind` beside a `===`. A bare `kind\s*===\s*['"]` also
        // matches `typeof row.kind === 'string'`, which is a type guard on a wire field and has
        // nothing to do with branching on which sort of chat this is — a guard that reports it
        // teaches the next reader to stop believing the guard.
        expect(
          /\bkind\s*===\s*['"](plan|build|builder)['"]/.test(source),
          `${path.relative(SRC_ROOT, file)} branches on a chat's kind`,
        ).toBe(false)
      }
    }
    // LIVENESS: the kind is still resolved, so the absences above mean "nothing branches on it"
    // rather than "the concept was deleted and the sweep found nothing to look at".
    const route = readFileSync(path.join(SRC_ROOT, 'pages/ChatRoute.tsx'), 'utf8')
    expect(route).toMatch(/kindFromServer/)
  })
})
