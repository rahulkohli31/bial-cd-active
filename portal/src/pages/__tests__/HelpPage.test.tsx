/**
 * The Help FAQ, reconciled against the code it describes.
 *
 * #157 C rewrote four answers that had gone quietly false: an invented Staff ID login, a
 * "no hard limit" that ignored the daily token cap, a footer link that pointed back at this
 * very page, and advice to attach a file type the composer had stopped accepting. None of
 * them broke anything — they just stopped being true, and nothing noticed, because HelpPage
 * had no test file at all and prose has no compiler.
 *
 * So these tests do not check the wording. They check that each answer still AGREES with the
 * thing it claims to describe — the real mode list, the real attachment allowlist, the real
 * feature flag — which is the axis these strings actually drifted on.
 *
 * They assert on the exported FAQS data rather than a render, on purpose. `AccordionItem`
 * renders its answer as `{open && ...}`, so against a collapsed accordion every negative
 * assertion here would pass with nothing in the DOM. That is not hypothetical: the #157
 * browser harness passed a full sweep of "this false claim is gone" checks for exactly that
 * reason before the accordions were expanded. Data cannot go vacuous that way.
 */
import { describe, it, expect } from 'vitest'
import { FAQS } from '../HelpPage'
import { MODES } from '../../components/chat/ModeSwitcher'
import { ALLOWED_MEDIA_TYPES, PPTX_MEDIA_TYPE } from '../../utils/attachmentInput'
import { DECK_ATTACHMENTS_ENABLED } from '../../config/features'

/** The answer to the FAQ whose question matches — fails loudly rather than returning
 *  `undefined` and letting a `.toMatch` on nothing decide the test. */
function answerTo(pattern: RegExp): string {
  const hit = FAQS.find((f) => pattern.test(f.q))
  expect(hit, `no FAQ question matching ${pattern}`).toBeTruthy()
  return hit!.a
}

describe('the FAQ answers are non-empty prose', () => {
  it('every entry has a question and a substantial answer', () => {
    // The liveness guard for everything below: if FAQS were empty or malformed, the
    // negative assertions in the other blocks would all pass on nothing.
    expect(FAQS.length).toBeGreaterThan(5)
    for (const { q, a } of FAQS) {
      expect(q.length).toBeGreaterThan(10)
      expect(a.length).toBeGreaterThan(40)
    }
  })
})

describe('the "Start Chat" answer agrees with the real mode list', () => {
  const answer = answerTo(/Start Chat/)

  it('names every mode the composer actually offers', () => {
    // Additive drift is the failure being pinned: a fourth mode would otherwise leave this
    // answer confidently describing three.
    for (const { label } of MODES) {
      expect(answer, `the FAQ never mentions the ${label} mode`).toContain(label)
    }
    expect(MODES.length).toBe(3) // if this changes, the answer's "three modes" does too
    expect(answer).toMatch(/three modes/i)
  })

  it('does not claim a build never starts — in Write it starts immediately', () => {
    // THE BUG this pins. The first rewrite opened "It opens a chat — it does not start a
    // build," which is true in Ask and Plan and false in Write, where ProjectBuilder's own
    // helper copy promises "it gets built right away — no plan step."
    expect(answer).not.toMatch(/does not start a build/i)
    expect(answer).toMatch(/no plan step|straight away|right away|immediately/i)
  })

  it('still says Plan is the default, because it is', () => {
    expect(MODES[1].value).toBe('plan') // ModeSwitcher's own sticky default
    expect(answer).toMatch(/defaults to Plan/i)
  })
})

describe('the attachment answer agrees with the real allowlist', () => {
  const answer = answerTo(/files can I attach/)

  it('promises only formats the composer actually accepts', () => {
    // Both directions. The one-way version of this test is what let ".pptx" survive in the
    // copy after the composer stopped taking it (#157 B2).
    const offersPptx = ALLOWED_MEDIA_TYPES.includes(PPTX_MEDIA_TYPE)
    expect(offersPptx).toBe(DECK_ATTACHMENTS_ENABLED)
    if (offersPptx) {
      expect(answer).toMatch(/PowerPoint|\.pptx/i)
    } else {
      expect(answer).not.toMatch(/PowerPoint|\.pptx/i)
    }
  })

  it('names the formats that are accepted at every flag setting', () => {
    for (const [type, phrase] of [
      ['application/pdf', /PDF/],
      ['text/csv', /CSV/],
      ['image/png', /PNG/],
    ] as const) {
      expect(ALLOWED_MEDIA_TYPES).toContain(type)
      expect(answer).toMatch(phrase)
    }
  })
})

describe('the corrected answers stay corrected', () => {
  it('does not offer a Staff ID login the sign-in page has never had', () => {
    // LoginPage has no text input at all — Microsoft SSO is the only way in.
    const answer = answerTo(/Who can use/)
    expect(answer).not.toMatch(/staff id|BIAL-X/i)
    expect(answer).toMatch(/Microsoft/i)
  })

  it('does not claim an unlimited allowance, and does not say only builds stop', () => {
    // "No hard limit" was true of app COUNT and false of the daily token cap, which
    // `enforce_daily_limit` applies to every AI action — Ask and Plan included.
    const answer = answerTo(/limit to how many apps/)
    expect(answer).not.toMatch(/no hard limit/i)
    expect(answer).toMatch(/80%/) // the real amber threshold (Navbar: pct >= 80)
    expect(answer).toMatch(/every AI action|not just builds/i)
  })

  it('does not send the reader back to this page for support', () => {
    // The old answer pointed at the portal footer link, which runs navigate('/help').
    const answer = answerTo(/Who do I contact/)
    expect(answer).not.toMatch(/footer/i)
    expect(answer).toMatch(/@/) // a real address, not a signpost
  })
})
