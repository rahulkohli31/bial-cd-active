/**
 * `chatKindFor` — the lookup that decides what a chat row CALLS itself (U1/R16).
 *
 * The plan said this module needed no spec of its own, on the grounds that a test over one record
 * per kind could only restate the literal. That was right about the literals and wrong about the
 * LOOKUP: `kind` is unvalidated wire data (`narrowChat` in ProjectPage passes through whatever
 * string the API sent), and a plain object lookup answers for keys nobody put in it. So what is
 * tested here is only the part that is not a restatement — the fallback's reach.
 */
import { describe, it, expect } from 'vitest'
import { chatKindFor, CHAT_KINDS, UNKNOWN_CHAT_KIND } from '../chatKind'

describe('chatKindFor', () => {
  it('answers the two kinds that exist, in the citizen’s words and never the storage value', () => {
    expect(chatKindFor('builder').word).toBe('Build')
    expect(chatKindFor('planning').word).toBe('Plan')
    // The wire value is a schema word; it must never reach a screen.
    for (const kind of Object.values(CHAT_KINDS)) {
      expect(kind!.word).not.toMatch(/builder|planning|assistant/i)
    }
  })

  it('falls back for the third value the field can hold today, and for one it cannot yet', () => {
    expect(chatKindFor('assistant')).toBe(UNKNOWN_CHAT_KIND)
    expect(chatKindFor('a_kind_invented_next_quarter')).toBe(UNKNOWN_CHAT_KIND)
    expect(chatKindFor('')).toBe(UNKNOWN_CHAT_KIND)
  })

  it('★ falls back for a kind that collides with an inherited Object property', () => {
    // THE ONE THAT WAS A CRASH. A bare `CHAT_KINDS[kind]` finds `Object.prototype.constructor` —
    // a truthy function — so `??` never fires, and the row then renders `undefined` as its word
    // and hands `<kind.Icon />` an undefined component, which throws during render. `narrowChat`
    // will pass any string the API sends straight into here.
    //
    // Mutation check: drop the `Object.hasOwn` guard and this goes red.
    for (const inherited of ['constructor', 'toString', 'valueOf', '__proto__', 'hasOwnProperty']) {
      expect(chatKindFor(inherited), `"${inherited}" escaped the fallback`).toBe(UNKNOWN_CHAT_KIND)
    }
  })

  it('gives every record a word and a completion that composes into its phrase', () => {
    // The badge shows `word` and hides `completion`; the element's whole text is what a screen
    // reader says. A completion that does not continue its word reads as gibberish.
    for (const kind of [...Object.values(CHAT_KINDS), UNKNOWN_CHAT_KIND]) {
      expect(kind!.word.length).toBeGreaterThan(0)
      expect(`${kind!.word}${kind!.completion}`.trim()).toBe(`${kind!.word}${kind!.completion}`.trim())
      if (kind!.completion) expect(kind!.completion).toMatch(/^ /) // a space, not a jammed word
    }
  })
})
