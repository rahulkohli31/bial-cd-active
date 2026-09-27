/**
 * Guards that the vendored-registry and hand-written CSS residue two earlier sweeps removed
 * stays removed.
 *
 * WHY THIS EXISTS
 *
 * Every item here was an export nobody imported, a `cva` key nobody selected, or a raw CSS rule
 * no class name reached — `tsc`/`eslint`/render tests have no opinion on any of it, so a routine
 * `shadcn add` or a careless paste can bring a whole alias set or a dead rule straight back.
 *
 * A NAMED LIST, not a reachability walk: `no-orphan-primitives.test.ts` already walks import
 * specifiers but is file-level, so it can't see an unused export inside a file something else
 * imports — the shape of every removal here. A general "no dead export" rule needs an allowlist
 * (this folder deliberately keeps `Toggle` and `buttonVariants`), and a growing allowlist is the
 * thing this file exists to avoid.
 *
 * EVERY RULE CARRIES ITS OWN LIVENESS PROBE: `forbidden.test(file) === false` is an absence
 * check, and a regex that drifted into matching nothing would pass forever, silently. Each rule
 * also runs against the exact text the sweep deleted, to prove it can still fire at all.
 *
 * NOT COVERED: no rule asserts `animate-pane-leave` is gone — `03bcba52` gave it a real caller
 * (`workspace/AppPane.tsx`), so the keyframe was kept. Its pairing with `.animate-pane-return`
 * is pinned separately by `components/workspace/__tests__/AppPane.test.tsx`.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { stripComments } from './_stripComments'

// vitest runs with cwd = the portal root (where the vitest config lives).
const ROOT = process.cwd()

/** The trailing `export { … }` block only — a name may live on as an unexported local. */
function exportBlock(source: string): string {
  return source.match(/export\s*\{[^}]*\}/)?.[0] ?? ''
}

interface Rule {
  file: string
  what: string
  /** `exports` narrows to the export block; `source` is the whole file, comments stripped. */
  scope: 'source' | 'exports'
  forbidden: RegExp
  /** Verbatim text this sweep deleted. The rule must still flag it. */
  deleted: string
}

const RULES: Rule[] = [
  {
    file: 'src/components/ui/dialog.tsx',
    what: 'the trigger, the close alias and the footer — no consumer opened or closed from a trigger',
    scope: 'source',
    forbidden: /\bDialog(?:Trigger|Close|Footer)\b/,
    deleted: 'const DialogTrigger = DialogPrimitive.Trigger',
  },
  {
    file: 'src/components/ui/dialog.tsx',
    what: 'the portal and the overlay stay as locals — DialogContent composes both, nothing outside needs them',
    scope: 'exports',
    forbidden: /\bDialog(?:Portal|Overlay)\b/,
    deleted: 'export {\n  Dialog,\n  DialogPortal,\n  DialogOverlay,\n  DialogContent,\n}',
  },
  {
    file: 'src/components/ui/popover.tsx',
    what: 'the anchor and the close alias — the publish chip anchors on its own trigger',
    scope: 'source',
    forbidden: /\bPopover(?:Anchor|Close)\b/,
    deleted: 'const PopoverAnchor = PopoverPrimitive.Anchor',
  },
  {
    file: 'src/components/ui/button.tsx',
    // `outline` is not on the list: it covers the Slot branch.
    what: 'the four cva keys no call site selects',
    scope: 'source',
    forbidden: /^\s*(?:link|sm|lg|destructive):/m,
    deleted: '        destructive:',
  },
  {
    file: 'src/index.css',
    what: 'the hand-written thin-scrollbar utility, which outlived every surface that carried the class',
    scope: 'source',
    forbidden: /\.scrollbar-thin\b/,
    deleted: '.scrollbar-thin::-webkit-scrollbar { width: 4px; }',
  },
  {
    file: 'tailwind.config.js',
    what: 'the orphan `success` brand colour (its neighbours `warning` and `danger` are both live)',
    scope: 'source',
    forbidden: /^\s*success:/m,
    deleted: "        success: '#22C55E',",
  },
  {
    file: 'src/index.css',
    what: 'the hand-written shimmer keyframe and its class — no label in this build ever wore it',
    scope: 'source',
    forbidden: /\bshimmer\b/,
    deleted: '  animation: shimmer 2.4s linear infinite;',
  },
  {
    file: 'tailwind.config.js',
    what: 'the collapsible keyframes and animations, which outlived the Radix primitive they drove',
    scope: 'source',
    forbidden: /\bcollapsible-(?:down|up)\b/,
    deleted: "        'collapsible-down': 'collapsible-down 0.2s ease-out',",
  },
]

describe('vendored and hand-written residue', () => {
  it('every name this sweep removed is still gone, and every rule still bites', () => {
    const back: string[] = []
    for (const rule of RULES) {
      const source = stripComments(readFileSync(path.join(ROOT, rule.file), 'utf8'))
      const scoped = rule.scope === 'exports' ? exportBlock(source) : source
      // A rule scoped to an export block that cannot find one is a rule covering nothing.
      if (rule.scope === 'exports') expect(scoped, `${rule.file} has no export block`).not.toBe('')
      if (rule.forbidden.test(scoped)) back.push(`${rule.file} → ${rule.what}`)

      // The liveness half — see the docblock: stops `forbidden.test(scoped) === false` passing vacuously.
      const fixture = rule.scope === 'exports' ? exportBlock(rule.deleted) : rule.deleted
      expect(
        rule.forbidden.test(fixture),
        `the rule for ${rule.file} no longer flags what it removed — it protects nothing`,
      ).toBe(true)
    }
    expect(back, `removed residue is back:\n${back.join('\n')}`).toEqual([])
  })
})
