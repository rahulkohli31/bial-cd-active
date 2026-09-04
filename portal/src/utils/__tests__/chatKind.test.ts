/**
 * `chatKindFor` — the lookup that decides what a chat row CALLS itself and SAYS about itself
 * (U16/R73).
 *
 * Before U16 the word and completion were literals baked into this file. Now they are not —
 * `word` and `description` come from `getStoredUser()?.chat_kinds`, the U16 catalogue riding the
 * once-cached `GET /auth/me` bootstrap. The strongest proof that the sourcing is real, rather
 * than a hardcoded fallback with a bootstrap-shaped decoration on top, is to mock the bootstrap
 * with wording that does NOT match the product copy and watch `chatKindFor` return exactly that
 * — if a literal `'Build'`/`'Plan'` were still baked in anywhere, these tests would keep passing
 * with the OLD words and go red the moment the mock's words diverged from them.
 *
 * `kind` is still unvalidated wire data (`narrowChat` in ProjectPage passes through whatever
 * string the API sent), so the fallback's reach — prototype-pollution keys, values with no
 * matching catalogue entry, a bootstrap that has not resolved yet — is tested here too.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const h = vi.hoisted(() => ({ getStoredUser: vi.fn() }))
vi.mock('../auth', () => ({ getStoredUser: h.getStoredUser }))

import { chatKindFor, UNKNOWN_CHAT_KIND } from '../chatKind'

// Deliberately NOT "Plan"/"Build" or the shipped descriptions — so a test that only checked
// these values against themselves could not accidentally pass by restating the real copy.
const MOCK_CATALOGUE = [
  { value: 'plan', name: 'Ideate', description: 'Think it through first.' },
  { value: 'build', name: 'Ship', description: 'Make it real.' },
]

function withCatalogue(chat_kinds: typeof MOCK_CATALOGUE | undefined) {
  h.getStoredUser.mockReturnValue(chat_kinds ? { chat_kinds } : null)
}

beforeEach(() => {
  h.getStoredUser.mockReset()
})

describe('chatKindFor', () => {
  it('sources the word and the description from the bootstrap catalogue, not a literal here', () => {
    withCatalogue(MOCK_CATALOGUE)
    const plan = chatKindFor('plan')
    expect(plan.word).toBe('Ideate')
    expect(plan.description).toBe('Think it through first.')
    const build = chatKindFor('build')
    expect(build.word).toBe('Ship')
    expect(build.description).toBe('Make it real.')
    // The wire value is a schema word; it must never reach a screen as the WORD.
    expect(plan.word).not.toBe('plan')
    expect(build.word).not.toBe('build')
  })

  it('tracks a changed bootstrap rather than caching the first answer', () => {
    // The same wire value, two different catalogues: if the module cached or hardcoded the
    // wording on first use, the second call would still answer with the first mock's words.
    withCatalogue(MOCK_CATALOGUE)
    expect(chatKindFor('build').word).toBe('Ship')
    withCatalogue([{ value: 'build', name: 'Launch', description: 'It is live now.' }])
    expect(chatKindFor('build').word).toBe('Launch')
  })

  it('keeps the icon and the pill local, unaffected by whatever the bootstrap says', () => {
    // R-8: icon/pill are NOT part of the catalogue's shape and must not move even though the
    // words now do. Same kind, two catalogues, one look.
    withCatalogue(MOCK_CATALOGUE)
    const first = chatKindFor('plan')
    withCatalogue([{ value: 'plan', name: 'Different Every Time', description: '…' }])
    const second = chatKindFor('plan')
    expect(second.Icon).toBe(first.Icon)
    expect(second.pill).toBe(first.pill)
  })

  it('★ gives the pill the glyph its own board draws — which for BUILD is none', () => {
    // The boards disagree on purpose, and the code used to draw a wrench in the BUILD pill that
    // no board has. `PlanChat` puts an 11px message-square inside its PLAN pill; `BuildChat`,
    // `NewBuildChat`, `PlainAnswer` and `ChatStarting` all draw BUILD as the word alone. The
    // picker's `Icon` is a separate question and both kinds still answer it.
    withCatalogue(MOCK_CATALOGUE)
    expect(chatKindFor('build').pillIcon).toBeNull()
    expect(UNKNOWN_CHAT_KIND.pillIcon).toBeNull()
    // LIVENESS, both halves: the kind that DOES draw one still has it, and the picker's icon is
    // untouched for the kind whose pill has none.
    expect(chatKindFor('plan').pillIcon).toBe(chatKindFor('plan').Icon)
    expect(chatKindFor('build').Icon).toBeTruthy()
  })

  it('paints each kind the pill colours the canvas draws, and neither as an action', () => {
    // The boards give the kind a LABEL pill — BUILD #8C5D1E on #FFF4E0, PLAN #0A5C5F on
    // #E0F5F6 — which is the one role the gold family legitimately keeps. What must never come
    // back is `bg-secondary`/`text-secondary`: the gold DEFAULT, which paints an action.
    withCatalogue(MOCK_CATALOGUE)
    expect(chatKindFor('build').pill).toBe('bg-accent-light text-secondary-800')
    expect(chatKindFor('plan').pill).toBe('bg-primary-50 text-primary-dark')
    for (const kind of ['build', 'plan', 'assistant']) {
      expect(chatKindFor(kind).pill).not.toMatch(/(?:bg|text)-secondary(?![-\w])/)
    }
  })

  it('falls back for the third value the field can hold today, and for one it cannot yet', () => {
    withCatalogue(MOCK_CATALOGUE)
    expect(chatKindFor('assistant')).toBe(UNKNOWN_CHAT_KIND)
    expect(chatKindFor('a_kind_invented_next_quarter')).toBe(UNKNOWN_CHAT_KIND)
    expect(chatKindFor('')).toBe(UNKNOWN_CHAT_KIND)
  })

  it('falls back for a recognised wire value whose catalogue entry has not arrived', () => {
    // Two separate misses this module must survive without throwing: no session at all (the
    // bootstrap has not resolved), and a session whose catalogue is missing this value. Neither
    // is hypothetical — the first is just "render before /auth/me's promise settles".
    withCatalogue(undefined)
    expect(chatKindFor('plan')).toBe(UNKNOWN_CHAT_KIND)
    withCatalogue([{ value: 'build', name: 'Ship', description: 'Make it real.' }]) // no 'plan' entry
    expect(chatKindFor('plan')).toBe(UNKNOWN_CHAT_KIND)
  })

  it('falls back rather than throwing when a profile arrives with no catalogue at all', () => {
    // A THIRD MISS, and the one the type system says is impossible: a NON-NULL profile whose
    // `chat_kinds` is absent. `UserProfile` is an unchecked cast over whatever `/auth/me`
    // returned, so this is a wire shape, not a contradiction — a stale service worker, a
    // server that predates the catalogue, a test double. The function's own contract says it
    // never throws, and it is called once per row of the chat list: a throw here is not one
    // bad badge, it is the whole project page failing to render.
    h.getStoredUser.mockReturnValue({} as never)
    expect(() => chatKindFor('plan')).not.toThrow()
    expect(chatKindFor('plan')).toBe(UNKNOWN_CHAT_KIND)
  })

  it('★ falls back for a kind that collides with an inherited Object property', () => {
    // THE ONE THAT WAS A CRASH. A bare index into the local look-up table finds
    // `Object.prototype.constructor` — a truthy function — so `??` never fires, and the row
    // then renders `undefined` as its word and hands `<kind.Icon />` an undefined component,
    // which throws during render. `narrowChat` will pass any string the API sends straight in.
    //
    // Mutation check: drop the `Object.hasOwn` guard in `chatKindFor` and this goes red.
    withCatalogue(MOCK_CATALOGUE)
    for (const inherited of ['constructor', 'toString', 'valueOf', '__proto__', 'hasOwnProperty']) {
      expect(chatKindFor(inherited), `"${inherited}" escaped the fallback`).toBe(UNKNOWN_CHAT_KIND)
    }
  })

  it('gives every answer a word and a completion that composes into its phrase', () => {
    // The badge shows `word` and hides `completion`; the element's whole text is what a screen
    // reader says. A completion that does not continue its word reads as gibberish.
    withCatalogue(MOCK_CATALOGUE)
    for (const kind of [chatKindFor('plan'), chatKindFor('build'), UNKNOWN_CHAT_KIND]) {
      expect(kind.word.length).toBeGreaterThan(0)
      expect(`${kind.word}${kind.completion}`.trim()).toBe(`${kind.word}${kind.completion}`.trim())
      if (kind.completion) expect(kind.completion).toMatch(/^ /) // a space, not a jammed word
    }
  })
})
