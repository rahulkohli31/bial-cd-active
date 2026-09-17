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
 * …and the same text when what PRECEDES it is an expression rather than a tag:
 * `{busy ? <Spinner/> : null} Create application`. Every button in this tree whose label follows a
 * conditional glyph is written that way, and it is how "Create project" survived on the primary
 * button of the very dialog whose title a sweep had just corrected — the two sat 300px apart.
 *
 * A closing brace is not rare in code, so this one reads with a stricter eye than the scan above.
 * Anything carrying a bracket, a colon, a pipe or a keyword is code, and code is read by nobody.
 */
const JSX_TEXT_AFTER_EXPRESSION = /\}([^<>{}]+)</g
const LOOKS_LIKE_CODE = /[();:|&?[\]]|\b(?:const|let|return|case|function|interface|export|import|extends|as)\b/

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
 * …and the backticked ones, which is where the longest copy in this product lives: anything built
 * around a count or a name is a template literal, and the delete dialog's four cascade sentences —
 * the most consequential prose here — are all of them. Read with the holes blanked, because the
 * sentence a person sees is the prose around them.
 */
const TEMPLATE = /`([^`\\]*)`/g
const HOLE = /\$\{[^{}]*\}/g

/** Tailwind, which is the one thing in a component file that looks like a sentence and is not. */
const CLASSES =
  /\b(?:flex|grid|text-|bg-|border|rounded|px-|py-|mt-|mb-|gap-|w-|h-|min-|max-|hover:|focus)/

/**
 * WHETHER A LITERAL IS SOMETHING A PERSON READS, decided on SHAPE rather than on punctuation.
 *
 * The punctuation blacklist this replaces rejected any literal containing a full stop — which is
 * most copy in the product — so the guard was quietly reading a fraction of what it claimed to,
 * and a walkthrough found sentences on screen that it had passed. A blacklist that grows to cover
 * keys eventually covers prose too; shape does not drift that way:
 *
 *   - a route is never read aloud, and it announces itself with a leading slash;
 *   - a class list is never read, and Tailwind names itself;
 *   - ONE TOKEN WITH NO SPACE is an identifier, a key or a testid — `bial:nav-pinned`,
 *     `app-menu-row`, `projectId` — UNLESS it is a bare word, because a bare word is how a
 *     pluralising count is written: `count === 1 ? 'project' : 'projects'`;
 *   - everything else is prose, and gets read.
 */
function isCopy(text: string): boolean {
  if (text.startsWith('/')) return false
  if (CLASSES.test(text)) return false
  if (!/\s/.test(text)) return /^[A-Za-z]+$/.test(text)
  return true
}

export function sentencesIn(source: string): string[] {
  const clean = stripComments(source)
  const found: string[] = []
  for (const match of clean.matchAll(SENTENCE)) {
    const text = match[1] ?? match[2] ?? ''
    if (isCopy(text)) found.push(text)
  }
  for (const [, literal] of clean.matchAll(TEMPLATE)) {
    const text = literal.replace(HOLE, ' ').trim()
    if (text.length > 0 && isCopy(text)) found.push(text)
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
  for (const [, value] of clean.matchAll(JSX_TEXT_AFTER_EXPRESSION)) {
    const text = value.trim()
    if (LOOKS_LIKE_CODE.test(text)) continue
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

  it('★ reads a sentence built around a count, which is where the gravest copy here lives', () => {
    // FOUND BY A WALKTHROUGH, NOT BY THIS GUARD. Anything phrased around a number is a template
    // literal, and the delete dialog's cascade — the sentence a person reads immediately before an
    // irreversible one — is four of them. The holes are blanked; the prose around them is the copy.
    const fixture = 'const s = `This deletes the project and all ${n} chat${n === 1 ? "" : "s"}.`'
    expect(sentencesIn(fixture).filter((copy) => RETIRED_NOUN.test(copy))).toEqual([
      'This deletes the project and all   chat .',
    ])
  })

  it('★ reads a button label that follows an expression rather than a tag', () => {
    // The shape no `>`-anchored scan can reach. It is how every button with a conditional glyph is
    // written, and it kept "Create project" on the primary button of a dialog titled "Create App".
    const fixture = `<button>{busy ? <Glyph/> : null} Create project</button>`
    expect(copyIn(fixture).filter((copy) => RETIRED_NOUN.test(copy))).toEqual(['Create project'])
  })

  it('★ a sentence is still read when it ends in a full stop', () => {
    // THE REGRESSION THIS GUARD SHIPPED ONCE. Calibrating on punctuation rather than shape meant
    // any literal containing a `.` was skipped — which is most copy — so the scan stayed green
    // while reading a fraction of what it claimed to. A blacklist written to exclude keys
    // eventually excludes prose; the test is what the literal LOOKS like, not what it contains.
    const fixture = `setError('Could not load this project.')`
    expect(sentencesIn(fixture).filter((copy) => RETIRED_NOUN.test(copy))).toEqual([
      'Could not load this project.',
    ])
  })

  it('leaves keys, classes and routes alone', () => {
    // The calibration that lets this guard live in a component tree: every one of these contains
    // the retired noun or looks like prose, and none of them is read by anybody.
    const fixture = `
      const KEY = 'bial.projects.view'
      const cls = 'flex items-center gap-2 text-sm text-neutral'
      // A hyphen is a word boundary, so a class TOKEN carrying the noun reads as the noun —
      // which is the whole reason Tailwind is named here rather than left to the shape test.
      const card = 'rounded-xl border border-project-accent px-3 py-2'
      const handle = 'project-home'
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
