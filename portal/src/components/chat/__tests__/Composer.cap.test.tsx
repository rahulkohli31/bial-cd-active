/**
 * THE CAP AS A CITIZEN MEETS IT (R42, R43).
 *
 * `composerCap.test.ts` pins the arithmetic. This pins the promise the arithmetic is for: over the
 * cap the text STAYS — all of it — and Send is marked unavailable with one line saying why.
 *
 * The two are separate files on purpose. A counting rule that is right in isolation and truncates
 * in the UI is the exact defect issue #156 is about: someone whose paste was quietly cut believes
 * their whole specification went in, and the app that gets built is missing the half nobody knows
 * was dropped.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'

import Composer, { type ComposerProps } from '../Composer'
import { COUNTER_VISIBLE_WITHIN, MAX_COMPOSER_CHARS } from '../../../utils/composerCap'

afterEach(() => {
  cleanup()
  sessionStorage.clear()
})

function draw(over: Partial<ComposerProps> = {}) {
  const props: ComposerProps = {
    conversationId: 'chat-1',
    onSubmit: vi.fn().mockResolvedValue(undefined),
    isRunning: false,
    onUrgent: vi.fn(),
    ...over,
  }
  return render(<Composer {...props} />)
}

const box = () => screen.getByTestId('composer-input') as HTMLTextAreaElement
const counter = () => screen.queryByTestId('composer-counter')
const type = (text: string) => fireEvent.change(box(), { target: { value: text } })

describe('nothing is ever cut', () => {
  it('holds every character of an over-cap paste', () => {
    draw()
    const huge = 'x'.repeat(MAX_COMPOSER_CHARS + 500)
    type(huge)
    // The WHOLE string, not "roughly the right length". A truncating implementation would leave a
    // value of exactly MAX_COMPOSER_CHARS and pass any looser assertion.
    expect(box().value).toBe(huge)
  })

  it('declares no `maxLength`, which is the attribute that would do the cutting', () => {
    draw()
    expect(box().hasAttribute('maxLength')).toBe(false)
  })

  it('refuses to SEND it, and says the text has not been cut', () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    draw({ onSubmit })
    type('x'.repeat(MAX_COMPOSER_CHARS + 1))

    expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/nothing has been cut/i)
    // Enforced in the handler, not by the attribute: Enter is the way most sends happen.
    fireEvent.keyDown(box(), { key: 'Enter' })
    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByTestId('composer-send').getAttribute('aria-disabled')).toBe('true')
  })

  it('never renders a real `disabled` while over the cap', () => {
    // The state most likely to tempt one, so it is swept here as well as in `Composer.test.tsx`.
    const { container } = draw()
    type('x'.repeat(MAX_COMPOSER_CHARS + 1))
    expect(container.querySelector('[disabled]')).toBeNull()
  })
})

describe('R43 — the counter is silent until it is useful, and then exact', () => {
  it('shows nothing on an ordinary message', () => {
    draw()
    type('add a column for the gate number')
    // A permanent counter on a near-empty box is noise. Its absence here is the requirement.
    expect(counter()).toBeNull()
  })

  it('appears within the threshold, and reads as count / cap', () => {
    draw()
    type('x'.repeat(MAX_COMPOSER_CHARS - COUNTER_VISIBLE_WITHIN))
    const shown = counter()
    expect(shown).toBeTruthy()
    expect(shown?.textContent).toContain((MAX_COMPOSER_CHARS - COUNTER_VISIBLE_WITHIN).toLocaleString())
    expect(shown?.textContent).toContain(MAX_COMPOSER_CHARS.toLocaleString())
  })

  it('counts CODE POINTS, so an emoji message is not reported at twice its length', () => {
    // The whole reason the count is not `String.length`: the server counts code points, and a
    // number that disagrees with the thing enforcing the limit is worse than no number.
    // 9,500 rockets is 9,500 characters and 19,000 UTF-16 units.
    draw()
    type('🚀'.repeat(9_500))
    expect(counter()?.textContent).toContain((9_500).toLocaleString())
    expect(counter()?.textContent).not.toContain((19_000).toLocaleString())
    // …and it is under the cap, so Send is free.
    expect(screen.getByTestId('composer-send').getAttribute('aria-disabled')).toBe('false')
  })

  it('goes back to silence when the text is shortened again', () => {
    draw()
    type('x'.repeat(MAX_COMPOSER_CHARS))
    expect(counter()).toBeTruthy() // liveness for the absence below
    type('short again')
    expect(counter()).toBeNull()
    expect(screen.queryByTestId('composer-gate-note')).toBeNull()
  })
})
