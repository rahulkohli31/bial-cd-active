import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup, screen } from '@testing-library/react'
import TurnBanner from '../TurnBanner'

afterEach(cleanup)

// The banner slot is where every sentence this plan says to a citizen lands: their app was
// recovered, it could not be, the workspace could not be checked, the change did not come
// together, today's allowance is used up. All five arrive at the same moment in the same place.
describe('TurnBanner — one banner, newest wins (U7/R13)', () => {
  it('renders no visible box when there is nothing to say', () => {
    render(<TurnBanner text={null} />)
    // NOT an empty bordered box. A permanent visual artefact above the composer reads as broken
    // UI and steals a row from the conversation for the whole time nothing is wrong — which is
    // nearly all of the time.
    expect(screen.queryByTestId('turn-banner')).toBeNull()
    // LIVENESS for that absence, and the property it is paired with: the live region IS there.
    expect(document.querySelector('[role="status"]')).toBeTruthy()
  })

  it('keeps the live region mounted so the announcement actually lands', () => {
    // ★ Inserting a region together with its text announces inconsistently — several reader and
    // browser combinations miss it entirely — so the element has to be in the accessibility tree
    // BEFORE the text arrives. The preview pane keeps a permanent region for exactly this
    // reason, and a banner that rendered itself into existence with its sentence would have made
    // this file's own promise quietly false.
    //
    // Mutation check: return `null` when there is no text and the first assertion goes red.
    const { rerender } = render(<TurnBanner text={null} />)
    const region = document.querySelector('[role="status"]')
    expect(region).toBeTruthy()
    expect(region?.textContent).toBe('')

    rerender(<TurnBanner text="That change didn’t come together." />)

    // The SAME element, now carrying text — not a new one inserted alongside it.
    expect(document.querySelectorAll('[role="status"]')).toHaveLength(1)
    expect(document.querySelector('[role="status"]')?.textContent).toBe(
      'That change didn’t come together.',
    )
  })

  it('announces politely, never assertively', () => {
    render(<TurnBanner text="We brought your app back." />)
    // These are endings with an action attached, not alarms. `assertive` is spent on this page
    // for the two things that genuinely interrupt (a failed relaunch, a failed save), and using
    // it here would make those stop cutting through.
    expect(document.querySelector('[role="status"]')?.getAttribute('aria-live')).toBe('polite')
    expect(document.querySelector('[aria-live="assertive"]')).toBeNull()
  })

  it('shows the newest sentence and nothing of the one it replaced', () => {
    // The stacking risk U7 names is that two platform sentences about the same app are on screen
    // together — the older one is not extra information, it is a contradiction. This asserts the
    // OLD text is gone, which a component that appended would fail; asserting only "there is one
    // banner" could not fail for a component with a single string prop.
    const { rerender } = render(<TurnBanner text="We brought your app back." />)
    rerender(<TurnBanner text="That change didn’t come together." />)

    expect(screen.getByTestId('turn-banner').textContent).toBe('That change didn’t come together.')
    expect(screen.queryByText(/brought your app back/i)).toBeNull()
  })

  it('clears when the sentence is withdrawn', () => {
    const { rerender } = render(<TurnBanner text="We couldn’t check on your app's workspace." />)
    expect(screen.getByTestId('turn-banner')).toBeTruthy() // liveness for the absence below
    rerender(<TurnBanner text={null} />)
    expect(screen.queryByTestId('turn-banner')).toBeNull()
  })
})

// U24 — "who to ask for more" has to be CLICKABLE, or it is a string the citizen retypes.
describe('an address in a platform sentence', () => {
  // ★ THIS IS THE SURFACE THE SENTENCE ACTUALLY LANDS ON. The at-limit copy also renders inside
  // the build-progress panel, but a plain Write turn never opens one — so the panel's own mailto
  // rendering is unreachable for exactly the citizen who has just run out of budget. Testing the
  // linkifier in isolation passes whether or not anything ever hands it the sentence; this fails
  // if the banner stops doing so.
  it('renders as a real mailto anchor', () => {
    render(
      <TurnBanner text="Today's budget is used up. If you need more before then, ask support@bial.example." />,
    )

    const link = screen.getByRole('link', { name: 'support@bial.example' })
    expect(link.getAttribute('href')).toBe('mailto:support@bial.example')
  })

  it('leaves a sentence with no address byte-identical', () => {
    const plain = 'Your workspace had been reset, so we are putting your app back.'
    render(<TurnBanner text={plain} />)

    expect(screen.getByTestId('turn-banner').textContent).toBe(plain)
    // LIVENESS: the linkifier really did run over this text and chose to add nothing.
    expect(screen.queryByRole('link')).toBeNull()
  })
})
