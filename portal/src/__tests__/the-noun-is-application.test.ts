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
 * IT IS A GREP, NOT A PARSER, and that is a deliberate trade. It can miss copy assembled from
 * variables; it cannot fire on an identifier, because an identifier is never a bare JSX text node
 * nor the whole value of an `aria-label`. Wrong in the harmless direction.
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
 * A SENTENCE in a plain module, as opposed to a key, a class name or a route. The test is crude
 * and deliberately so: twelve characters, a space, no slash, and a first word that is not
 * dotted. It finds the error copy and the workspace's lines, and it leaves `'bial:nav-pinned'`
 * and `'text-sm font-bold'` alone.
 */
const SENTENCE = /'([^'\\\n]{12,})'|"([^"\\\n]{12,})"|`([^`$\\\n]{12,})`/g

/** Tailwind, which is the one thing in a component file that looks like a sentence and is not. */
const CLASSES = /\b(?:flex|grid|text-|bg-|border|rounded|px-|py-|mt-|mb-|gap-|w-|h-|min-|max-|hover:|focus)/

export function sentencesIn(source: string): string[] {
  const clean = stripComments(source)
  const found: string[] = []
  for (const match of clean.matchAll(SENTENCE)) {
    const text = match[1] ?? match[2] ?? match[3] ?? ''
    const [first = ''] = text.split(' ')
    if (!text.includes(' ') || text.includes('/') || first.includes('.')) continue
    if (CLASSES.test(text) || text.startsWith('bial')) continue
    found.push(text)
  }
  return found
}

export function copyIn(source: string): string[] {
  const clean = stripComments(source)
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
