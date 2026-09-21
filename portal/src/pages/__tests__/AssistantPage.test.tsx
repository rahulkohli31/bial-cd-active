/**
 * BIAL CHAT — the screen, mounted whole.
 *
 * THE COMPOSER UNDER TEST IS THE REAL ONE. Nothing about the library, the runtime or the box is
 * doubled here, because the claim this page makes is precisely that it mounts the genuine
 * assistant-ui composer with nothing behind it. A test against a stub would pass on a drawing.
 *
 * THE GREETING IS RANDOM, AND THE ASSERTIONS ARE WRITTEN FOR THAT rather than around it: most
 * check membership of the set the page is allowed to draw from, and the two that need one exact
 * line pin the clock and the source of chance instead of hoping.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { MotionGlobalConfig } from 'motion/react'

const h = vi.hoisted(() => ({ getStoredUser: vi.fn() }))
vi.mock('../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getStoredUser: h.getStoredUser,
}))

import AssistantPage from '../AssistantPage'
import { GREETINGS, headlineParts } from '../../components/assistant/greetings'

MotionGlobalConfig.skipAnimations = true

const CITIZEN = { email: 'asha@bial.aero', display_name: 'Asha Rao', isAdmin: false }

const heading = () => screen.getByTestId('assistant-greeting')
const box = () => screen.getByTestId('composer-input') as HTMLTextAreaElement
const send = () => screen.getByTestId('composer-send')
const sky = () => screen.getByTestId('assistant-backdrop')

/** Every headline the page may draw, already rendered for this name — which is what "it came from
 *  the set, and the placeholder was filled" means as one assertion. */
const rendered = (name: string | null) =>
  GREETINGS.filter((g) => name !== null || !g.headline.includes('{name}')).map((g) =>
    headlineParts(g.headline, name)
      .map((segment) => segment.text)
      .join(''),
  )

beforeEach(() => {
  vi.clearAllMocks()
  h.getStoredUser.mockReturnValue(CITIZEN)
  sessionStorage.clear()
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('the heading is a greeting, and it is the only colour on the screen', () => {
  it('draws one of the thirty, with the placeholder filled and the markers gone', () => {
    render(<AssistantPage />)
    const text = heading().textContent ?? ''
    expect(rendered('Asha')).toContain(text)
    expect(text).not.toContain('{name}')
    expect(text).not.toContain('*')
  })

  it('paints exactly one word, and it is a real element rather than markup', () => {
    render(<AssistantPage />)
    const accents = heading().querySelectorAll('em')
    expect(accents).toHaveLength(1)
    expect(accents[0].className).toContain('text-primary')
    expect((accents[0].textContent ?? '').length).toBeGreaterThan(0)
  })

  it('uses the first word of the display name, at the hour the citizen is actually at', () => {
    // The one place the clock and the source of chance are both pinned, so the exact line is
    // known: 09:00 is the morning band, and roll 0 is its first candidate.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date(2026, 8, 21, 9, 30, 0))
    vi.spyOn(Math, 'random').mockReturnValue(0)

    render(<AssistantPage />)
    expect(heading().textContent).toBe('Morning, Asha.')
    expect(heading().querySelector('em')?.textContent).toBe('Morning')
  })

  it.each([
    ['a profile with no display name', { ...CITIZEN, display_name: null }],
    ['no cached profile at all', null],
  ])('★ %s is never given a line that needs one', (_case, user) => {
    // The failure this guards is not a crash — it is the heading reading "Morning, ." or
    // "Morning, {name}.", which looks like the product does not know who signed in.
    h.getStoredUser.mockReturnValue(user)
    render(<AssistantPage />)
    const text = heading().textContent ?? ''
    expect(rendered(null)).toContain(text)
    expect(text).not.toMatch(/,\s*\./)
  })

  it('★ does not repeat itself on the next visit in the same sitting', () => {
    render(<AssistantPage />)
    const first = heading().textContent
    cleanup()

    render(<AssistantPage />)
    expect(heading().textContent).not.toBe(first)
    // Liveness: the second render drew a real heading rather than nothing at all, which would
    // also satisfy "not the same".
    expect(rendered('Asha')).toContain(heading().textContent ?? '')
  })
})

describe('the composer is the library own, and it will not send', () => {
  it('mounts the real box, its dropzone and its attachment control', () => {
    render(<AssistantPage />)
    expect(screen.getByTestId('composer-dropzone')).toBeTruthy()
    expect(screen.getByTestId('composer-attach')).toBeTruthy()
    expect(box().tagName).toBe('TEXTAREA')
  })

  it('wears the portal own not-yet treatment, and says when in one line', () => {
    render(<AssistantPage />)
    expect(send().getAttribute('aria-disabled')).toBe('true')
    // The pale ground the boards reserve for "you may not send", not a greyed-out control.
    expect(send().className).toContain('bg-canvas-sendoff')
    expect(send().getAttribute('aria-label')).toMatch(/sending switches on/i)
    expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/^Preview —/)
  })

  it('★ the note is painted in the grey that passes contrast on this page ground', () => {
    // `text-neutral` is 4.44:1 on #F0F4F8 and fails AA; it passes everywhere else because the
    // composer almost always sits on white. This screen is the first to put it on the platform
    // ground, and the swap is the whole reason `noteClassName` exists.
    render(<AssistantPage />)
    const note = screen.getByTestId('composer-gate-note')
    expect(note.className).toContain('text-status-grey-fg')
    expect(note.className).not.toContain('text-neutral')
  })

  it('★ takes what is typed, refuses to send it, and loses none of it', () => {
    const { container } = render(<AssistantPage />)
    fireEvent.change(box(), { target: { value: 'how many stands are free tonight' } })
    expect(box().value).toBe('how many stands are free tonight')

    // Both doors: the control, and the key that bypasses it.
    fireEvent.click(send())
    fireEvent.keyDown(box(), { key: 'Enter' })

    expect(box().value).toBe('how many stands are free tonight')
    // Nothing on this screen is ever really `disabled` — that blurs the focused element mid
    // sentence, which is the defect the whole composer is built around avoiding.
    expect(container.querySelector('[disabled]')).toBeNull()
  })
})

describe('the sky behind it is decoration and nothing else', () => {
  it('says nothing to a screen reader and catches no pointer', () => {
    render(<AssistantPage />)
    expect(sky().getAttribute('aria-hidden')).toBe('true')
    expect(sky().className).toContain('pointer-events-none')
  })

  it('draws the whole field: the corner, the motes and two aircraft', () => {
    render(<AssistantPage />)
    expect(sky().querySelector('.chat-sky-corner')).toBeTruthy()
    expect(sky().querySelectorAll('.chat-mote')).toHaveLength(51)
    expect(sky().querySelectorAll('.chat-plane')).toHaveLength(2)
  })

  it('★ is the same field on every mount, not a reshuffle', () => {
    // `Math.random` here would move every speck whenever a sibling set state — a backdrop that
    // twitches under a keystroke, which only ever shows up in front of an audience.
    const positions = () =>
      [...sky().querySelectorAll('.chat-mote')].map((node) => (node as HTMLElement).style.left)

    render(<AssistantPage />)
    const first = positions()
    cleanup()
    render(<AssistantPage />)

    expect(positions()).toEqual(first)
    // Liveness: a generator that returned one value for everything would also be "identical".
    expect(new Set(first).size).toBeGreaterThan(40)
  })
})
