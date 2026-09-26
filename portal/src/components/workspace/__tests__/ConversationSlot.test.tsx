/**
 * The conversation slot: one home for the conversation body mount, and hide-not-unmount.
 *
 * Deliberately NOT re-asserted here — each has its own owner:
 *  - the shared draft across a reload and a sibling round trip —
 *    `components/chat/__tests__/Composer.test.tsx`;
 *  - draft/scroll surviving a hide/show cycle — `ProjectWorkspace.test.tsx` ("keeps the rail
 *    MOUNTED while collapsed"), the assertion that discriminates a CSS hide from an unmount;
 *  - a route change unmounting the conversation — it does, deliberately: the router owns which
 *    conversation is mounted, and what survives a project↔chat move is the draft and the app
 *    pane, not the component.
 *
 * The one conversation body is stubbed — mounting it, and nothing else, is the whole subject.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ConversationSlot, { type MountedConversation } from '../ConversationSlot'
import { HIDDEN_BUT_MOUNTED } from '../hiddenSubtree'

// ONE STUB, because there is one body. It reports the props it was handed INCLUDING the kind: a
// stub that printed only `chatId` could not tell "not passed" from "passed and ignored". The
// kind IS passed — for one declaration (does this surface want the app pane seen?) rather than
// for a body.
vi.mock('../../chat/ConversationSurface', () => ({
  default: (props: Record<string, unknown>) => (
    <div data-testid="conversation-body" data-kind={String(props.kind)}>
      <button type="button">a conversation control</button>
      {String(props.chatId)}
    </div>
  ),
}))

const noop = () => {}

const conversation = (over: Partial<MountedConversation> = {}): MountedConversation => ({
  chatId: 'chat-1',
  kind: 'plan',
  projectId: 'p1',
  project: null,
  projectHasSavedBuild: null,
  ...over,
})

const renderSlot = (over?: Partial<MountedConversation>, hidden = false) =>
  render(
    <MemoryRouter>
      <ConversationSlot conversation={conversation(over)} hidden={hidden} onProjectUpdate={noop} />
    </MemoryRouter>,
  )

const slot = () => screen.getByTestId('conversation-slot')

afterEach(() => cleanup())

describe('ConversationSlot — one body, whatever the kind', () => {
  // FLIPPED, NOT DELETED. These three cases assert that the per-kind branch is gone — the
  // mechanical form of that requirement's surface half — which is worth more than deleting them would be.
  it('mounts the same body for both kinds', () => {
    renderSlot({ kind: 'build' })
    expect(screen.getByTestId('conversation-body')).toBeTruthy()

    cleanup()
    renderSlot({ kind: 'plan' })
    expect(screen.getByTestId('conversation-body')).toBeTruthy()
  })

  it('hands the resolved conversation through, INCLUDING its kind', () => {
    // INVERTED DELIBERATELY: the surface cannot branch on what it is never given, so this only
    // proves the visibility declaration is passed through. The sibling tests hold the rest — one
    // BODY for both kinds, and the same DOM node across a kind change.
    renderSlot({ kind: 'build', chatId: 'build-7' })
    const body = screen.getByTestId('conversation-body')
    expect(body.textContent).toContain('build-7')
    expect(body.getAttribute('data-kind')).toBe('build')

    cleanup()
    renderSlot({ kind: 'plan', chatId: 'plan-7' })
    expect(screen.getByTestId('conversation-body').getAttribute('data-kind')).toBe('plan')
  })

  it('changing kind does NOT swap or remount the body — it is one component', () => {
    // The discriminating assertion is node IDENTITY. "There is still a body" would pass against a
    // slot that unmounted one component and mounted an identical-looking other; only the same DOM
    // node proves nothing was torn down — which is what carries a live turn across the change.
    function Switchable() {
      const [kind, setKind] = useState<'plan' | 'build'>('plan')
      return (
        <MemoryRouter>
          <button type="button" onClick={() => setKind('build')}>to builder</button>
          <ConversationSlot conversation={conversation({ kind })} onProjectUpdate={noop} />
        </MemoryRouter>
      )
    }
    render(<Switchable />)
    const body = screen.getByTestId('conversation-body')

    fireEvent.click(screen.getByRole('button', { name: 'to builder' }))

    expect(screen.getByTestId('conversation-body')).toBe(body)
  })
})

describe('ConversationSlot — hidden means mounted, out of reach, and out of the accessibility tree', () => {
  it('a hidden conversation is still in the document and still the same element', () => {
    // The distinction IS the requirement. A hidden conversation keeps its stream, its scroll
    // position and its draft precisely because it is never unmounted; the moment hiding becomes
    // unmounting, the requirement is a sentence in a document rather than a property of the code.
    function Toggle() {
      const [hidden, setHidden] = useState(false)
      return (
        <MemoryRouter>
          <button type="button" onClick={() => setHidden(!hidden)}>toggle</button>
          <ConversationSlot conversation={conversation()} hidden={hidden} onProjectUpdate={noop} />
        </MemoryRouter>
      )
    }
    render(<Toggle />)
    const body = screen.getByTestId('conversation-body')

    fireEvent.click(screen.getByRole('button', { name: 'toggle' }))

    expect(screen.getByTestId('conversation-body')).toBe(body) // the SAME node, not a replacement
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
    expect(screen.getByText('a conversation control')).toBeTruthy()
    // …and out of the accessibility tree, which is exactly what a role query cannot see.
    expect(screen.queryByRole('button', { name: 'a conversation control' })).toBeNull()
  })

  it('a visible conversation carries neither the class nor the attribute', () => {
    renderSlot()
    expect(slot().className).not.toContain('invisible')
    expect(slot().getAttribute('aria-hidden')).toBe('false')
  })
})
