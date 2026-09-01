import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'

/**
 * An invented Tailwind token renders INVISIBLY, and no test that reads text content will ever
 * notice. `bg-bial-primary` shipped in the #83 dialog: the class does not exist, so the button
 * had no background, `text-white` painted white on a white card, and the primary action was
 * gone. Every unit test still passed — `getByRole` found it, `toBeEnabled()` was true, the
 * textContent matched. It was caught by looking at a screenshot from a live browser.
 *
 * jsdom computes no Tailwind styles, so a `getComputedStyle` assertion cannot close this in the
 * unit suite. What CAN be checked cheaply is the SOURCE TEXT, and this file holds every rule of
 * that kind. There are four now; the first is the original, the other three arrived with the
 * chat-surface rebuild (Plan D, U1) because porting v4 registry sources into a v3 build makes
 * the same failure reachable three new ways.
 *
 *   1. `bial-*` — the project's own colour family. A reference outside it is always a typo.
 *   2. TAILWIND v4 SYNTAX. The portal is on 3.4.17 and the assistant-ui / shadcn registry
 *      sources we port are authored for v4. Every token below is valid v4 and produces
 *      NOTHING on 3.4.17 — no build error, no console warning, no failing test.
 *   3. `--color-*` VARIABLE REFERENCES. v4 names its theme variables `--color-foreground`;
 *      this portal defines `--foreground`. `var(--color-foreground)` resolves to nothing and
 *      the declaration is dropped, so the element paints transparent.
 *   4. R68 — SUB-BODY-SIZE ARBITRARY FONT SIZES, scoped (see IN_SCOPE below).
 *
 * EACH RULE IS A PURE FUNCTION OVER SOURCE TEXT, and is asserted twice: once across the real
 * tree, and once against a fixture string that is KNOWN to violate it. Without the second, a
 * rule whose regex never matches anything passes for ever and protects nothing — which is the
 * failure mode a source-scanning guard is most prone to.
 */
const ROOT = path.resolve(__dirname, '..')
const CONFIG = path.resolve(__dirname, '../../tailwind.config.js')

function sourceFiles(dir) {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry)
    if (statSync(full).isDirectory()) return entry === '__tests__' ? [] : sourceFiles(full)
    return /\.(jsx?|tsx?)$/.test(entry) ? [full] : []
  })
}

/** Repo-relative, POSIX-separated, so an assertion message reads the same on Windows. */
const rel = (file) => path.relative(ROOT, file).split(path.sep).join('/')

/**
 * Comments are not source, and here that distinction has teeth rather than being tidiness.
 * `ui/pagination.tsx`'s docblock QUOTES the v4 tokens it deliberately avoided
 * (`border-primary!`, `bg-transparent!`) so the next author knows why the file is written the
 * way it is — and the first run of the trailing-`!` rule flagged exactly that prose. Reporting
 * it would push someone to delete the one comment in the tree that explains the trap.
 *
 * `//` is only treated as a comment when it is not part of a `://` scheme, so the font `@import`
 * URL and every https link survive.
 */
function stripComments(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

describe('tailwind custom tokens', () => {
  it('every bial-* class used in src/ actually exists in the config', () => {
    const config = readFileSync(CONFIG, 'utf8')
    const declared = new Set(
      (config.match(/bial:\s*\{([\s\S]*?)\}/)?.[1].match(/([a-zA-Z]+):/g) ?? []).map((k) =>
        k.replace(':', ''),
      ),
    )
    expect(declared.size, 'could not read the bial namespace out of tailwind.config.js').toBeGreaterThan(0)

    const offenders = []
    for (const file of sourceFiles(ROOT)) {
      const text = readFileSync(file, 'utf8')
      for (const [, token] of text.matchAll(/\b(?:bg|text|border|ring|fill|stroke)-bial-([a-z][a-z-]*)/g)) {
        const base = token.split('/')[0]
        if (!declared.has(base)) offenders.push(`${rel(file)} → bial-${base}`)
      }
    }
    expect(offenders, `unknown bial-* tokens (they render as NOTHING):\n${offenders.join('\n')}`).toEqual([])
  })
})

// ─────────────────────────────────────────────────────────────────────────────────────────
// Rule 2 — Tailwind v4 syntax
//
// Every entry was read out of a real registry source during the component survey
// (`.claude/scratch/component-decisions.md`, the v4→v3 rewrite table). These are the tokens
// actually present in `thread`, `tool-group`, `tool-fallback` and `attachment` — not a
// speculative list of everything v4 added.
// ─────────────────────────────────────────────────────────────────────────────────────────
const V4_ONLY = [
  {
    name: 'data-open: / data-closed: (v4 Radix shorthand)',
    re: /\bgroup-data-(?:open|closed)\/[a-z-]+:|(?<![[\w-])data-(?:open|closed):/g,
    v3: 'data-[state=open]: / data-[state=closed]:',
  },
  {
    name: 'duration-(--var) (v4 CSS-variable shorthand)',
    re: /\bduration-\(--[^)]+\)/g,
    v3: 'duration-[var(--x)]',
  },
  {
    name: 'animation-duration-* (no v3 utility at all)',
    re: /\banimation-duration-[([]/g,
    v3: '[animation-duration:var(--x)]',
  },
  {
    name: 'blur-in-[…] (tw-animate-css, not tailwindcss-animate@1.0.7)',
    re: /\bblur-in-\[/g,
    v3: 'drop it — this build has no plugin that defines it',
  },
  {
    name: '@container queries',
    re: /(?<![\w@])@container\b/g,
    v3: 'v4 only — restructure, or add the container-queries plugin deliberately',
  },
  {
    name: 'color-mix(…)',
    re: /\bcolor-mix\(/g,
    v3: 'an hsl() token with an opacity modifier, e.g. bg-foreground/30',
  },
  { name: 'wrap-break-word', re: /\bwrap-break-word\b/g, v3: 'break-words' },
  { name: 'not-last: variant', re: /\bnot-last:/g, v3: '[&:not(:last-child)]:' },
  {
    name: 'trailing-! important (v4 order)',
    // v3 wants the bang LEADING: `!ring-0`, never `ring-0!`. `ui/pagination.tsx`'s own
    // docstring records this repo hitting the trap once already.
    re: /(?<=[\s"'`{])[a-z][a-z0-9:/[\]().-]*[a-z0-9\])]!(?=[\s"'`}])/g,
    v3: 'move the ! to the front: !ring-0',
  },
  {
    name: 'fractional spacing (v4 arbitrary-free .5 steps)',
    // v3 defines 0.5/1.5/2.5/3.5 and nothing above; `pb-7.5` and `-mb-7.5` are v4.
    re: /(?<![\w-])-?[pm][trblxy]?-(?:[4-9]|[1-9]\d+)\.5(?![\w.])/g,
    v3: 'an arbitrary value, e.g. pb-[1.875rem]',
  },
]

/** @returns {string[]} one description per v4 token found in `text`. */
function findV4Tokens(text) {
  const hits = []
  for (const { name, re, v3 } of V4_ONLY) {
    for (const [hit] of text.matchAll(re)) hits.push(`${hit.trim()}   [${name}] → use ${v3}`)
  }
  return hits
}

/** @returns {string[]} one description per `var(--color-*)` reference found in `text`. */
function findColorVarRefs(text) {
  return [...text.matchAll(/var\(\s*--color-[\w-]+/g)].map(
    ([hit]) => `${hit}  (this portal declares --foreground, not --color-foreground)`,
  )
}

const R68_MIN_PX = 12

/** @returns {string[]} one description per arbitrary font size below the body ramp. */
function findSubBodyFontSizes(text) {
  const hits = []
  for (const [hit, value, unit] of text.matchAll(/\btext-\[(\d+(?:\.\d+)?)(px|rem|em)\]/g)) {
    const px = unit === 'px' ? Number(value) : Number(value) * 16
    if (px < R68_MIN_PX) hits.push(`${hit} (${px}px, floor is ${R68_MIN_PX}px)`)
  }
  return hits
}

describe('tailwind v4 syntax never reaches this v3.4.17 build', () => {
  it('no source file under src/ uses a v4-only token', () => {
    const offenders = []
    for (const file of sourceFiles(ROOT)) {
      const text = stripComments(readFileSync(file, 'utf8'))
      for (const hit of findV4Tokens(text)) offenders.push(`${rel(file)} → ${hit}`)
    }
    expect(
      offenders,
      'Tailwind v4 syntax in a v3.4.17 build. These compile to NOTHING and no other test\n' +
        `can see it, because jsdom computes no styles:\n${offenders.join('\n')}`,
    ).toEqual([])
  })

  it('flags a fixture using each v4 token, and passes its v3 equivalent', () => {
    // The teeth. Every rule above must reject something, or it is decoration.
    expect(findV4Tokens('<div className="data-open:opacity-100" />')).toHaveLength(1)
    expect(findV4Tokens('<div className="duration-(--tool-group-duration)" />')).toHaveLength(1)
    expect(findV4Tokens('<div className="animation-duration-[--x]" />')).toHaveLength(1)
    expect(findV4Tokens('<div className="blur-in-[2px]" />')).toHaveLength(1)
    expect(findV4Tokens('<div className="@container/thread" />')).toHaveLength(1)
    expect(findV4Tokens('<div className="bg-[color-mix(in_oklab,var(--x)_30%,transparent)]" />')).toHaveLength(1)
    expect(findV4Tokens('<div className="wrap-break-word" />')).toHaveLength(1)
    expect(findV4Tokens('<div className="not-last:border-b" />')).toHaveLength(1)
    expect(findV4Tokens('<div className="ring-0! bg-black/50!" />')).toHaveLength(2)
    expect(findV4Tokens('<div className="pb-7.5 -mb-7.5" />')).toHaveLength(2)

    // The v3 spellings of the same intent are clean — otherwise the rule would block the fix.
    expect(findV4Tokens('<div className="data-[state=open]:opacity-100" />')).toEqual([])
    expect(findV4Tokens('<div className="duration-[var(--tool-group-duration)]" />')).toEqual([])
    expect(findV4Tokens('<div className="!ring-0 !bg-black/50" />')).toEqual([])
    expect(findV4Tokens('<div className="break-words px-2.5 pb-3.5 [&:not(:last-child)]:border-b" />')).toEqual([])
    // And an ordinary line of prose is not a v4 token. `data-testid` in particular sits one
    // character away from the `data-open:` rule and appears on almost every element we write.
    expect(findV4Tokens('<p data-testid="composer-gate-note">Sent! 100% done.</p>')).toEqual([])
  })

  it('no source file references a --color-* variable', () => {
    const offenders = []
    for (const file of sourceFiles(ROOT)) {
      const text = stripComments(readFileSync(file, 'utf8'))
      for (const hit of findColorVarRefs(text)) offenders.push(`${rel(file)} → ${hit}`)
    }
    expect(
      offenders,
      `--color-* references resolve to nothing here and paint the element transparent:\n${offenders.join('\n')}`,
    ).toEqual([])
  })

  it('flags a fixture using var(--color-foreground), and passes var(--foreground)', () => {
    expect(findColorVarRefs('style={{ color: "var(--color-foreground)" }}')).toHaveLength(1)
    expect(findColorVarRefs('className="bg-[var(--color-card)]"')).toHaveLength(1)
    expect(findColorVarRefs('style={{ color: "hsl(var(--foreground))" }}')).toEqual([])
  })

  it('walks src/ only — it never descends into node_modules', () => {
    // The `content` glob in tailwind.config.js deliberately scans
    // `node_modules/streamdown/dist`, so "scan everything Tailwind scans" would pull a
    // megabyte of minified dist through these regexes and report v4 tokens we do not own.
    // The walk is rooted at src/ and terminates; this pins that it stays that way.
    const files = sourceFiles(ROOT)
    expect(files.length).toBeGreaterThan(50)
    expect(files.filter((f) => f.includes('node_modules'))).toEqual([])
  })
})

/**
 * R68 — nothing a person reads is smaller than the platform's body size.
 *
 * SCOPE IS DELIBERATELY A LIST, NOT THE WHOLE TREE. Sub-body-size text is widespread on
 * `main` (admin tables, the navbar's token meter, project cards) and R68 is a chat-surface
 * requirement, not a portal-wide restyle. So the rule covers exactly "the files this plan
 * authors or modifies": the whole ported tree, plus each chat file as the unit that owns it
 * opens it.
 *
 * ADD YOUR FILE HERE WHEN YOUR UNIT OPENS IT. That is the growth the plan describes — a file
 * enters scope in the same commit that raises its type, so the guard is never committed red
 * and never quietly stops covering something.
 *
 * Named scale steps are not flagged: `text-xs` and up are the platform's own ramp and moving
 * between them is a design decision, not a typo. What this catches is the ARBITRARY value —
 * `text-[11px]` — which is how a component ends up below the ramp without anyone choosing it.
 */
const IN_SCOPE = [
  'components/assistant-ui/', // every ported registry source, for the life of the plan
]

function inR68Scope(file) {
  const r = rel(file)
  return IN_SCOPE.some((entry) => (entry.endsWith('/') ? r.startsWith(entry) : r === entry))
}

describe('R68 — no text below the platform body size on the chat surface', () => {
  it('no in-scope file sets an arbitrary font size below the body ramp', () => {
    const offenders = []
    for (const file of sourceFiles(ROOT).filter(inR68Scope)) {
      const text = stripComments(readFileSync(file, 'utf8'))
      for (const hit of findSubBodyFontSizes(text)) offenders.push(`${rel(file)} → ${hit}`)
    }
    expect(
      offenders,
      `R68: text a citizen reads, below the platform body size:\n${offenders.join('\n')}`,
    ).toEqual([])
  })

  it('flags text-[11px] and text-[10px], and passes text-sm and text-xs', () => {
    expect(findSubBodyFontSizes('<span className="text-[11px] text-neutral" />')).toHaveLength(1)
    expect(findSubBodyFontSizes('<span className="text-[10px]" />')).toHaveLength(1)
    expect(findSubBodyFontSizes('<span className="text-[0.6875rem]" />')).toHaveLength(1)
    expect(findSubBodyFontSizes('<span className="text-sm" />')).toEqual([])
    expect(findSubBodyFontSizes('<span className="text-xs" />')).toEqual([])
    expect(findSubBodyFontSizes('<span className="text-[12px] text-[1rem]" />')).toEqual([])
  })

  it('the scope list only names files that exist', () => {
    // A path that has been renamed or deleted silently stops being covered, and the rule
    // above would keep passing while covering less. This is the mutant guard for the list.
    const present = sourceFiles(ROOT).map(rel)
    const missing = IN_SCOPE.filter((entry) =>
      entry.endsWith('/') ? !present.some((p) => p.startsWith(entry)) : !present.includes(entry),
    )
    // A directory entry may legitimately be empty while the plan has not created it yet;
    // a named FILE that has vanished is always a hole in the guard.
    expect(missing.filter((m) => !m.endsWith('/'))).toEqual([])
  })
})
