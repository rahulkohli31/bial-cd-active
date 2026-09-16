/**
 * ONE NOUN, EVERYWHERE A PERSON READS. The boards call the thing a citizen builds an
 * APPLICATION; the code calls it a project, and always will — routes, `projectId`, `data-testid`
 * handles and API fields are identifiers, and renaming those would be a re-plumbing with no
 * reader on the other end of it.
 *
 * SO THIS GUARD SCANS COPY AND NOTHING ELSE: JSX text, the four attributes that are read aloud
 * or shown on hover, and the SENTENCES that live in plain modules — the error copy a failed read
 * shows, the workspace's own lines, an audit label. A reader must be able to move between the
 * home list, the marketplace, a shared application and a workspace without the noun changing
 * under them, and a sweep is only true on the day it runs — the next hand-written heading, or the
 * next fallback message, is what this exists to catch.
 *
 * IT IS A GREP, NOT A PARSER, and that is a deliberate trade — but the trade has to be made in the
 * right direction, and the first version of it was not. Three real strings walked through: a
 * conditional label written as a ternary, a count built in a template literal, and a line of JSX
 * text a `{' '}` joiner split in two. The scans below each answer one of those; the shape of the
 * miss is always the same, which is that copy does not arrive in one syntactic piece.
 *
 * IT STILL CANNOT SEE COPY ASSEMBLED FROM VARIABLES, and that limit is honest rather than fixable
 * by a wider regex. What it can promise is that no LITERAL a person reads carries the retired noun.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
/** Comments are prose about the code, where the retired name is allowed to appear as history —
 *  `retired-names-are-past-tense.test.ts` is what holds those honest. */
import { stripComments } from './_stripComments'

const SRC_ROOT = path.resolve(process.cwd(), 'src')

/** The attributes a person actually reads: two spoken, two shown. */
const COPY_ATTRIBUTES = /(?:aria-label|title|placeholder|alt)="([^"]+)"/g

/**
 * …and the same four written as an EXPRESSION, which is how every conditional label is written:
 * `aria-label={open ? 'Hide the chat' : 'Show the chat'}`. Missing this form let the workspace's
 * own back control keep the retired noun through a whole sweep — the one control on the screen a
 * citizen presses to leave.
 */
const COPY_EXPRESSIONS = /(?:aria-label|title|placeholder|alt)=\{([^}]*)\}/g
const QUOTED = /'([^'\\\n]+)'|"([^"\\\n]+)"|`([^`$\\\n]+)`/g

/** JSX text: what sits between two tags, with no expression in it. */
const JSX_TEXT = />([^<>{}]+)</g

/**
 * A JSX expression carrying nothing but a space — `{' '}` — which is how a line of copy too long
 * for one source line is joined back together. It splits one sentence into two text nodes, and it
 * hid "Couldn't load more projects." through an entire sweep. Removed before the scan so the
 * sentence is read as the one sentence a person sees.
 */
const JSX_SPACE_JOINER = /\{\s*['"`]\s*['"`]\s*\}/g

const RETIRED_NOUN = /\bprojects?\b/i

/**
 * `promptGuardrails.ts` is EXEMPT, and it is the one file that has to be. Its strings are matched
 * against what a person TYPES — "for my side project" is a phrase a citizen writes, not a word
 * this platform says — so sweeping it would quietly stop the guardrail matching.
 */
const NOT_COPY = new Set([path.join('utils', 'promptGuardrails.ts')])

function sourceFiles(dir: string, ext: string, found: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry)
    if (statSync(full).isDirectory()) {
      // A test file narrates what it asserts, including the noun it retired.
      if (entry === '__tests__') continue
      sourceFiles(full, ext, found)
      continue
    }
    if (entry.endsWith(ext) && !NOT_COPY.has(path.relative(SRC_ROOT, full))) found.push(full)
  }
  return found
}

/**
 * EVERY quoted literal, of any length. The length floor this used to carry is what let `'project'`
 * and `'projects'` — a pluralising count in the admin table — through: copy is not always a
 * sentence, and a single word on screen is read exactly as loudly as a paragraph.
 */
const SENTENCE = /'([^'\\\n]+)'|"([^"\\\n]+)"/g

/**
 * WHAT A LITERAL LOOKS LIKE WHEN IT IS NOT COPY, and this is the whole of the calibration now that
 * the length floor is gone. A route, a storage key, a testid, a class fragment, an identifier and a
 * template hole all carry one of these characters; a sentence a person reads carries none of them.
 * Cheaper than parsing, and it costs a miss rather than a false alarm — a piece of copy that
 * happens to contain a full stop is simply not checked. It subsumes a Tailwind-specific test this
 * used to carry: every class list in this tree contains a hyphen.
 */
const NOT_A_SENTENCE = /[/.\-_${}:]/

export function sentencesIn(source: string): string[] {
  const clean = stripComments(source)
  const found: string[] = []
  for (const match of clean.matchAll(SENTENCE)) {
    const text = match[1] ?? match[2] ?? ''
    if (NOT_A_SENTENCE.test(text)) continue
    found.push(text)
  }
  return found
}

export function copyIn(source: string): string[] {
  const clean = stripComments(source).replace(JSX_SPACE_JOINER, ' ')
  const found: string[] = []
  for (const [, value] of clean.matchAll(COPY_ATTRIBUTES)) found.push(value)
  for (const [, expression] of clean.matchAll(COPY_EXPRESSIONS)) {
    for (const quote of expression.matchAll(QUOTED)) {
      const text = quote[1] ?? quote[2] ?? quote[3] ?? ''
      if (text.length > 0) found.push(text)
    }
  }
  for (const [, value] of clean.matchAll(JSX_TEXT)) {
    const text = value.trim()
    // `>` and `<` are not only tag delimiters — `=>`, `&&` and a comparison all put one in the
    // middle of code, so a naive span between them swallows whole function bodies. Copy has no
    // semicolons, no assignments and no `case` labels; that is enough to tell the two apart
    // without parsing, and erring here costs a miss rather than a false alarm.
    if (/[;=]|\bconst\b|\breturn\b|\bcase\b/.test(text)) continue
    if (text.length > 0 && /[a-z]{3}/i.test(text)) found.push(text)
  }
  return found
}

describe('the noun a person reads is "application"', () => {
  it('★ no rendered copy in the portal still says "project"', () => {
    const offences: string[] = []
    for (const file of sourceFiles(SRC_ROOT, '.tsx')) {
      for (const copy of copyIn(readFileSync(file, 'utf8'))) {
        if (RETIRED_NOUN.test(copy)) {
          offences.push(`${path.relative(SRC_ROOT, file)}: ${copy}`)
        }
      }
    }
    expect(offences, offences.join('\n')).toEqual([])
  })

  it.each(['.ts', '.tsx'])(
    '★ nor does any sentence written as a literal in a %s module',
    (ext) => {
    // THE HALF THE JSX SCAN CANNOT SEE: `setError('Could not load this project.')` renders to a
    // reader exactly like a heading does, and nothing about it is a tag or an attribute.
    const offences: string[] = []
    for (const file of sourceFiles(SRC_ROOT, ext)) {
      for (const copy of sentencesIn(readFileSync(file, 'utf8'))) {
        if (RETIRED_NOUN.test(copy)) offences.push(`${path.relative(SRC_ROOT, file)}: ${copy}`)
      }
    }
    expect(offences, offences.join('\n')).toEqual([])
    },
  )

  it('…and THAT grep can fail too', () => {
    const fixture = `const oops = 'Failed to load projects'`
    expect(sentencesIn(fixture).filter((copy) => RETIRED_NOUN.test(copy))).toEqual([
      'Failed to load projects',
    ])
  })

  it('★ catches the three shapes that walked past the first sweep', () => {
    // Every one of these is a real string this guard failed to see, found by a reviewer rather
    // than by the guard. A length floor hid the first two; the third is a literal chosen inside an
    // expression, which is where every conditional label in this tree lives.
    const named = (source: string) =>
      sentencesIn(source).filter((copy) => RETIRED_NOUN.test(copy))

    expect(named("`${n} ${n === 1 ? 'project' : 'projects'}`")).toEqual(['project', 'projects'])
    expect(named("const title = 'New project'")).toEqual(['New project'])
    expect(named("{done ? 'All set' : 'New project'}")).toEqual(['New project'])
  })

  it('★ reads a line of copy a {\' \'} joiner split across two source lines', () => {
    // THE ONE NO LITERAL SCAN CAN REACH: this is JSX text, so the joiner that wraps it for line
    // length turns one sentence a person reads into two text nodes, and the noun lands in neither.
    const fixture = `<p>Couldn’t load more projects.{' '}<button>Try again</button></p>`
    expect(copyIn(fixture).filter((copy) => RETIRED_NOUN.test(copy))).toEqual([
      'Couldn’t load more projects.',
    ])
  })

  it('leaves keys, classes and routes alone', () => {
    // The calibration that lets this guard live in a component tree: every one of these contains
    // the retired noun or looks like prose, and none of them is read by anybody.
    const fixture = `
      const KEY = 'bial.projects.view'
      const cls = 'flex items-center gap-2 text-sm text-neutral'
      const card = 'rounded-xl border border-bial-border px-3 py-2'
      navigate('/projects')
    `
    expect(sentencesIn(fixture).filter((copy) => RETIRED_NOUN.test(copy))).toEqual([])
  })

  it('…and the grep can actually fail', () => {
    // WITHOUT THIS THE TEST ABOVE IS WORTHLESS. A scan whose extraction quietly returns nothing
    // passes for ever and protects nothing — which is precisely how a copy sweep gets signed off
    // while half the screens still say the old word.
    const fixture = `
      <h1>Your projects</h1>
      <button aria-label="Delete project">x</button>
    `
    const found = copyIn(fixture).filter((copy) => RETIRED_NOUN.test(copy))
    expect(found).toEqual(['Delete project', 'Your projects'])
  })

  it('★ reads a label written as an expression, not only as a plain attribute', () => {
    // THE GAP THAT LET THE BACK CONTROL THROUGH. Every conditional label in this tree is written
    // this way, which is most of the labels worth checking.
    const fixture = `<button aria-label={far ? 'Back to projects' : 'Back to the project'} />`
    expect(copyIn(fixture).filter((copy) => RETIRED_NOUN.test(copy))).toEqual([
      'Back to projects',
      'Back to the project',
    ])
  })

  it('does not fire on an identifier, a route or a test handle', () => {
    // The other half of the calibration: this guard must be safe to keep, which means it may
    // never ask anybody to rename `projectId`, `/projects` or `app-menu-row`.
    const fixture = `
      <Route path="/projects" element={<ProjectsPage />} />
      <div data-testid="project-home" onClick={() => openProject(projectId)}>Open</div>
    `
    expect(copyIn(fixture).filter((copy) => RETIRED_NOUN.test(copy))).toEqual([])
  })
})
