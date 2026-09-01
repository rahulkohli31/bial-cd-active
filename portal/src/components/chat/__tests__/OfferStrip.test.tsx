/**
 * THE PLAN OFFER, AS A STRIP ON THE COMPOSER (R29, R29a, R45, R64, R51a).
 *
 * The three decisions this file exists to hold, because each is one someone would reasonably
 * undo:
 *
 *  D2 — A SPENT STRIP STAYS, AND STAYS PRESSABLE. The first press answers the tool call; that is
 *       unavoidable. Pressing again is an ordinary request that creates another Build chat.
 *       "Only one offer is live" is about which one blocks the composer, never about which one a
 *       citizen may press.
 *  D3 — THE BROWSER NEVER POSTS THE PLAN TEXT BACK. The server reads it from the offering tool
 *       call's own message. A browser-supplied body would let a stale second tab write stale
 *       requirements into a permanent first message.
 *  D4 — IDEMPOTENCY WITHOUT STORAGE, and its honest boundary. The minted id lives in a ref, so a
 *       double press and a retry collide on the primary key and the server hands back the chat
 *       that already exists — and a RELOAD is out of reach, which is asserted rather than hidden.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

import OfferStrip, { BUILD_LABEL, KEEP_PLANNING_LABEL, type OfferStripProps } from '../OfferStrip'

afterEach(cleanup)

function draw(over: Partial<OfferStripProps> = {}) {
  const props: OfferStripProps = {
    toolCallId: 'call-1',
    conversationId: 'chat-1',
    spent: false,
    onBuild: vi.fn().mockResolvedValue(undefined),
    onKeepPlanning: vi.fn().mockResolvedValue(undefined),
    onFailed: vi.fn(),
    ...over,
  }
  return { props, ...render(<OfferStrip {...props} />) }
}

const build = () => screen.getByTestId('offer-build')
const keepPlanning = () => screen.getByTestId('offer-keep-planning')

describe('the two buttons, and the words on them', () => {
  it('reads as a pair: each label names the mode the press puts you in', () => {
    // Client call, 2026-08-31 (R-15). NOT "Build it", NOT "Keep refining" — which the client found
    // confusing — and NOT the canvas's older "Not yet — keep talking". The same two words appear
    // in the model-facing copy, or the agent tells a citizen to press a button that does not
    // exist, so they are pinned here as constants rather than as inline strings.
    draw()
    expect(build().textContent).toContain(BUILD_LABEL)
    expect(keepPlanning().textContent).toContain(KEEP_PLANNING_LABEL)
    expect(BUILD_LABEL).toBe('Build this plan')
    expect(KEEP_PLANNING_LABEL).toBe('Keep planning')
  })

  it('there are exactly TWO, because one would be a dead end', () => {
    // A locked box behind a single "Build this plan" leaves anyone who wanted to change something
    // with nowhere to go. The second button is what makes the gate survivable.
    const { container } = draw()
    expect(container.querySelectorAll('button')).toHaveLength(2)
  })

  it('never renders a real `disabled`, in any state (R45, R64)', () => {
    // `disabled` on the focused element blurs it to `document.body`. Both buttons carry
    // `aria-disabled` while a press is in flight instead — affordance, not enforcement.
    const { container } = draw({ spent: true })
    expect(container.querySelector('[disabled]')).toBeNull()
  })
})

describe('the strip IS its tool call id', () => {
  it('renders nothing at all without one, rather than a dead button', () => {
    const { container } = draw({ toolCallId: null })
    expect(container.innerHTML).toBe('')
    // LIVENESS for that emptiness: the same component with an id renders.
    cleanup()
    draw()
    expect(screen.getByTestId('offer-strip')).toBeTruthy()
  })

  it('does nothing on a press with no conversation to answer in', () => {
    const onBuild = vi.fn()
    draw({ conversationId: null, onBuild })
    fireEvent.click(build())
    expect(onBuild).not.toHaveBeenCalled()
  })
})

describe('Build this plan', () => {
  it('sends the conversation, the tool call and a minted chat id — and NO plan text (D3)', async () => {
    const onBuild = vi.fn().mockResolvedValue(undefined)
    draw({ onBuild })

    fireEvent.click(build())
    await waitFor(() => expect(onBuild).toHaveBeenCalled())

    const handoff = onBuild.mock.calls[0][0]
    // Asserted on the KEYS, not just the values: an extra field carrying the plan is exactly the
    // thing D3 forbids, and checking only that the three expected fields are right would not see
    // a fourth one arrive.
    expect(Object.keys(handoff).sort()).toEqual(['conversationId', 'newChatId', 'toolCallId'])
    expect(handoff.conversationId).toBe('chat-1')
    expect(handoff.toolCallId).toBe('call-1')
    // A UUIDv7 — the id becomes a conversation's primary key and ADR-0006 wants v7, so the shape
    // is pinned even though the value cannot be.
    expect(handoff.newChatId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  })

  it('a double press carries the SAME minted id — one plan, one build chat (D4)', async () => {
    // Two presses that minted two ids would be two build chats for one plan, and the citizen would
    // be looking at the second, empty one. The ref is what makes the second press collide on the
    // primary key so the server answers with the chat that already exists.
    let release: (() => void) | undefined
    const onBuild = vi.fn().mockImplementation(() => new Promise<void>((r) => { release = r }))
    draw({ onBuild })

    fireEvent.click(build())
    await waitFor(() => expect(onBuild).toHaveBeenCalledTimes(1))
    fireEvent.click(build()) // while the first is still in flight — absorbed by `busy`
    expect(onBuild).toHaveBeenCalledTimes(1)

    release?.()
    await waitFor(() => expect(build().getAttribute('aria-disabled')).toBe('false'))
    fireEvent.click(build()) // a genuine retry, after the first settled
    await waitFor(() => expect(onBuild).toHaveBeenCalledTimes(2))

    expect(onBuild.mock.calls[1][0].newChatId).toBe(onBuild.mock.calls[0][0].newChatId)
  })

  it('a FRESH mount mints a new id — R28’s reload clause, undelivered on purpose', async () => {
    // A ref dies with the page. The only thing that would survive a reload is a local record,
    // which D2 forbids, so after a reload a fresh press-session mints a new id and creates a
    // SECOND build chat. That is the honest boundary of R28, and it is asserted here rather than
    // discovered in production — closing it needs storage, which is a decision nobody has taken.
    const onBuild = vi.fn().mockResolvedValue(undefined)
    draw({ onBuild })
    fireEvent.click(build())
    await waitFor(() => expect(onBuild).toHaveBeenCalledTimes(1))
    const first = onBuild.mock.calls[0][0].newChatId

    cleanup() // the reload
    draw({ onBuild })
    fireEvent.click(build())
    await waitFor(() => expect(onBuild).toHaveBeenCalledTimes(2))

    expect(onBuild.mock.calls[1][0].newChatId).not.toBe(first)
  })

  it('a failed handoff leaves the reader where they are, told, with the strip pressable (R29)', async () => {
    const onBuild = vi.fn().mockRejectedValue(new Error('nope'))
    const onFailed = vi.fn()
    draw({ onBuild, onFailed })

    fireEvent.click(build())
    await waitFor(() => expect(onFailed).toHaveBeenCalled())
    expect(onFailed.mock.calls[0][0]).toMatch(/nothing has changed/i)

    // Pressable again — the press did not burn the offer, and a spent-looking strip after a
    // failure is how "a burned card with no build behind it" happens.
    await waitFor(() => expect(build().getAttribute('aria-disabled')).toBe('false'))
    fireEvent.click(build())
    await waitFor(() => expect(onBuild).toHaveBeenCalledTimes(2))
  })
})

describe('Keep planning', () => {
  it('answers the call with the tool call id and nothing else', async () => {
    const onKeepPlanning = vi.fn().mockResolvedValue(undefined)
    draw({ onKeepPlanning })

    fireEvent.click(keepPlanning())
    await waitFor(() => expect(onKeepPlanning).toHaveBeenCalledWith('call-1'))
  })

  it('reports a failure rather than leaving the reader with a button that did nothing', async () => {
    const onFailed = vi.fn()
    draw({ onKeepPlanning: vi.fn().mockRejectedValue(new Error('nope')), onFailed })

    fireEvent.click(keepPlanning())
    await waitFor(() => expect(onFailed).toHaveBeenCalled())
    expect(onFailed.mock.calls[0][0]).toMatch(/try again/i)
  })
})

describe('a spent strip (D2)', () => {
  it('stays on screen, is marked spent, and STILL issues a request when pressed', async () => {
    const onBuild = vi.fn().mockResolvedValue(undefined)
    draw({ spent: true, onBuild })

    const strip = screen.getByTestId('offer-strip')
    expect(strip.getAttribute('data-spent')).toBe('true')
    // The whole of D2 in one assertion: spent is a TREATMENT, not a disablement.
    fireEvent.click(build())
    await waitFor(() => expect(onBuild).toHaveBeenCalledTimes(1))
  })

  it('a live strip is not marked spent', () => {
    draw()
    expect(screen.getByTestId('offer-strip').getAttribute('data-spent')).toBe('false')
  })
})
