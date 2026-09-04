/**
 * Guard: the vendored-registry and hand-written CSS residue this sweep removed stays removed.
 *
 * NOTHING ELSE CAN GO RED WHEN IT COMES BACK, which is how all of it arrived. Every item below
 * was an EXPORT nobody imported, a `cva` key nobody selected, or a raw CSS rule no class name
 * reached — so neither `tsc` nor `eslint` nor any render test has an opinion about it. One
 * `npx shadcn@latest add` of a component that lists `dialog` or `popover` in its
 * registryDependencies restores the whole alias set and the four button variants in a diff that
 * looks like a routine upgrade, and a thin-scrollbar rule is one paste away from being beside
 * the scroller that replaced it.
 *
 * WHY A NAMED LIST RATHER THAN A REACHABILITY WALK. `components/ui/__tests__/no-orphan-primitives.test.ts`
 * already walks import specifiers, and it is deliberately FILE-level — it cannot see an unused
 * export inside a file something else imports, which is the shape of every removal here. The
 * obvious generalisation, "no primitive exports a name nothing imports", is not available: this
 * folder deliberately keeps two such names (`toggle.tsx`'s `Toggle`, whose file is imported for
 * its variants, and `button.tsx`'s `buttonVariants`, which the registry convention exports and
 * the file itself uses), so the general rule would need an allowlist — and an allowlist that
 * grows is the thing it was written to prevent. A list of what ONE sweep removed cannot rot that
 * way: it is either still true or it is red.
 *
 * MUTATION-CHECKED, BECAUSE A SOURCE-SCANNING RULE THAT MATCHES NOTHING PASSES FOR EVER. Each
 * rule is asserted twice — once against the real file, and once against the exact text that was
 * deleted from it, which it must still flag.
 *
 * COMMENTS ARE NOT SOURCE. `dialog.tsx`, `popover.tsx` and `button.tsx` each record what they
 * dropped and why, naming the removed identifiers so the next re-copy is recognised for what it
 * is. Those sentences are the point of the removal, not a violation of it — same treatment, and
 * the same `://` guard, as `tailwind-tokens.test.js`.
 *
 * WHAT THIS DOES NOT COVER, STATED RATHER THAN IMPLIED. The plan behind this sweep also asked
 * for a rule that no CSS rule anywhere emits `animate-pane-leave`. That was true at the audited
 * base and is false here: `03bcba52` gave the class a caller (`workspace/AppPane.tsx`, holding
 * the column open through `paneExit.ts`), so the keyframe was kept and there is nothing to
 * assert. Its pairing with `.animate-pane-return` under `prefers-reduced-motion` is already
 * pinned by `components/workspace/__tests__/AppPane.test.tsx`, which reads the stylesheet rule
 * AND checks that both classes are applied by the two components.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'

// vitest runs with cwd = the portal root (where the vitest config lives).
const ROOT = process.cwd()

/** `//` is only a comment when it is not part of a `://` scheme — `index.css` opens on a font URL. */
function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

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
    what: 'the four cva keys no call site selects (`outline` is NOT one of them — it covers the Slot branch)',
    scope: 'source',
    forbidden: /^\s*(?:destructive|link|sm|lg):/m,
    deleted: '        lg: "h-10 rounded-md px-8",',
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

      // The teeth: the same rule, against the exact text that was deleted.
      const fixture = rule.scope === 'exports' ? exportBlock(rule.deleted) : rule.deleted
      expect(
        rule.forbidden.test(fixture),
        `the rule for ${rule.file} no longer flags what it removed — it protects nothing`,
      ).toBe(true)
    }
    expect(back, `removed residue is back:\n${back.join('\n')}`).toEqual([])
  })
})
