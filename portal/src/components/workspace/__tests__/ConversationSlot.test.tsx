/**
 * The conversation slot (Plan A, U5).
 *
 * The slot's job is small and its claims are correspondingly narrow: one home for the kind→body
 * selection, and the hide-not-unmount treatment carried over from the builder surface's chat panel
 * so it survives Plan D's rewrite of the surfaces around it.
 *
 * WHAT IS PROVEN ELSEWHERE, AND DELIBERATELY NOT RE-ASSERTED HERE:
 *  - the shared draft, and its behaviour across a reload and a sibling round trip —
 *    `pages/__tests__/ChatPage-surface.test.tsx`, where the U1 characterization it flips also lives;
 *  - the draft surviving a hide/show cycle on the builder surface —
 *    `pages/__tests__/BuilderPage-panel.test.jsx:57`, and the scroll position at `:86`, which is
 *    the assertion that actually discriminates a CSS hide from an unmount;
 *  - that a route change still unmounts the conversation. It does, deliberately: the router owns
 *    which conversation is mounted and the slot keeps no stack of visited ones alive. What survives
 *    a project↔chat move is the draft and the app pane, not the component.
 *
 * The two page bodies are stubbed. Which body renders for which kind is the whole subject, and the
 * real ones would drag two transports, two hydration fetches and a build session into a test about
 * a ternary.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ConversationSlot, { type MountedConversation } from '../ConversationSlot'
import { HIDDEN_BUT_MOUNTED } from '../hiddenSubtree'

vi.mock('../../../pages/BuilderPage', () => ({
  default: (props: Record<string, unknown>) => (
    <div data-testid="builder-body">
      <button type="button">a builder control</button>
      {String(props.chatId)}
    </div>
  ),
}))
vi.mock('../../../pages/ChatPage', () => ({
  default: (props: Record<string, unknown>) => (
    <div data-testid="planning-body">
      <button type="button">a planning control</button>
      {String(props.chatId)}
    </div>
  ),
}))

const conversation = (over: Partial<MountedConversation> = {}): MountedConversation => ({
  chatId: 'chat-1',
  kind: 'plan',
  projectId: 'p1',
  projectName: 'VIP Movement',
  projectHasSavedBuild: null,
  ...over,
})

const renderSlot = (over?: Partial<MountedConversation>, hidden = false) =>
  render(
    <MemoryRouter>
      <ConversationSlot conversation={conversation(over)} hidden={hidden} />
    </MemoryRouter>,
  )

const slot = () => screen.getByTestId('conversation-slot')

afterEach(() => cleanup())

describe('ConversationSlot — one home for the kind branch', () => {
  it('mounts the builder body for a builder resolution and the planning body for a planning one', () => {
    renderSlot({ kind: 'build' })
    expect(screen.getByTestId('builder-body')).toBeTruthy()
    expect(screen.queryByTestId('planning-body')).toBeNull()

    cleanup()
    renderSlot({ kind: 'plan' })
    expect(screen.getByTestId('planning-body')).toBeTruthy()
    expect(screen.queryByTestId('builder-body')).toBeNull()
  })

  it('hands the resolved conversation through to whichever body it mounted', () => {
    renderSlot({ kind: 'build', chatId: 'build-7' })
    expect(screen.getByTestId('builder-body').textContent).toContain('build-7')
  })

  it('changing kind swaps the body, because the two are still two components', () => {
    // Said out loud rather than glossed: this plan MOVES the branch, it does not delete it. Claiming
    // the collapse would make R72's surface half sound delivered while a citizen still gets a
    // different React page per kind. Plan D is what makes this test meaningless.
    function Switchable() {
      const [kind, setKind] = useState<'plan' | 'build'>('plan')
      return (
        <MemoryRouter>
          <button type="button" onClick={() => setKind('build')}>to builder</button>
          <ConversationSlot conversation={conversation({ kind })} />
        </MemoryRouter>
      )
    }
    render(<Switchable />)
    expect(screen.getByTestId('planning-body')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'to builder' }))

    expect(screen.getByTestId('builder-body')).toBeTruthy()
    expect(screen.queryByTestId('planning-body')).toBeNull()
  })
})

describe('ConversationSlot — hidden means mounted, out of reach, and out of the accessibility tree', () => {
  it('a hidden conversation is still in the document and still the same element', () => {
    // The distinction IS the requirement. A hidden conversation keeps its stream, its scroll
    // position and its draft precisely because it is never unmounted; the moment hiding becomes
    // unmounting, R8a is a sentence in a document rather than a property of the code.
    function Toggle() {
      const [hidden, setHidden] = useState(false)
      return (
        <MemoryRouter>
          <button type="button" onClick={() => setHidden(!hidden)}>toggle</button>
          <ConversationSlot conversation={conversation()} hidden={hidden} />
        </MemoryRouter>
      )
    }
    render(<Toggle />)
    const body = screen.getByTestId('planning-body')

    fireEvent.click(screen.getByRole('button', { name: 'toggle' }))

    expect(screen.getByTestId('planning-body')).toBe(body) // the SAME node, not a replacement
    expect(slot().className).toContain(HIDDEN_BUT_MOUNTED)
    expect(slot().getAttribute('aria-hidden')).toBe('true')
  })

  it('uses visibility, not zero width or aria-hidden alone', () => {
    // `aria-hidden` alone left a collapsed subtree's composer, Send and attach controls
    // keyboard-reachable — a WCAG 4.1.2 violation — because zero width and overflow:hidden clip a
    // subtree visually without removing its descendants from the tab order. Only
    // `visibility:hidden` drops it from BOTH the tab order and the accessibility tree.
    renderSlot(undefined, true)

    expect(slot().className).toContain('invisible')
    expect(slot().getAttribute('aria-hidden')).toBe('true')

    // BOTH HALVES, AND THEY HAVE TO BE ASSERTED WITH DIFFERENT QUERIES.
    // Still in the DOM — without this the whole block passes against a slot that rendered nothing
    // at all, which is the assert-absence false-green in its purest form.
    expect(screen.getByText('a planning control')).toBeTruthy()
    // …and out of the accessibility tree, which is exactly what a role query cannot see.
    expect(screen.queryByRole('button', { name: 'a planning control' })).toBeNull()
  })

  it('a visible conversation carries neither the class nor the attribute', () => {
    renderSlot()
    expect(slot().className).not.toContain('invisible')
    expect(slot().getAttribute('aria-hidden')).toBe('false')
  })
})
