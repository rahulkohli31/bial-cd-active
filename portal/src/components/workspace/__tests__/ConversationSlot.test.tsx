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
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import ConversationSlot, { type MountedConversation } from '../ConversationSlot'
import { HIDDEN_BUT_MOUNTED } from '../hiddenSubtree'
import { WorkspaceChannelProvider, createWorkspaceChannel } from '../workspaceChannel'

vi.mock('../../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/auth')>()),
  getStoredUser: () => ({
    chat_kinds: [
      { value: 'plan', name: 'Plan', description: 'Shape a plan first.' },
      { value: 'build', name: 'Build', description: 'Change the live app.' },
    ],
  }),
}))

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

describe('ConversationSlot — the "All chats" strip', () => {
  function Where() {
    const location = useLocation()
    return (
      <>
        <span data-testid="where">{location.pathname + location.search}</span>
        <span data-testid="where-state">{JSON.stringify(location.state ?? null)}</span>
      </>
    )
  }

  /** The slot under a channel the route has named, at a chat address reached with `state`. */
  function renderInWorkspace({
    over,
    state,
    chatTitle = 'Out-time — what should it record?',
    paneVisible = true,
  }: {
    over?: Partial<MountedConversation>
    state?: unknown
    chatTitle?: string | null
    paneVisible?: boolean
  } = {}) {
    const channel = createWorkspaceChannel()
    channel.heading.set({ projectId: 'p1', projectName: 'Visitor Log', chatTitle, chatKind: 'plan' })
    channel.visible.set(paneVisible)
    return render(
      <WorkspaceChannelProvider value={channel}>
        <MemoryRouter initialEntries={[{ pathname: '/chat/chat-1', state }]}>
          <Routes>
            <Route
              path="/chat/:chatId"
              element={<ConversationSlot conversation={conversation(over)} onProjectUpdate={noop} />}
            />
            <Route path="*" element={<Where />} />
          </Routes>
        </MemoryRouter>
      </WorkspaceChannelProvider>,
    )
  }

  const strip = () => screen.getByTestId('all-chats-strip')
  const back = () => screen.getByRole('link', { name: 'Back to all chats' })

  it('★ heads a project chat with the way back, the chat\'s title and its kind, above the body', () => {
    renderInWorkspace()

    expect(strip().textContent).toContain('All chats')
    expect(screen.getByTestId('all-chats-strip-title').textContent).toBe('Out-time — what should it record?')
    expect(screen.getByTestId('chat-kind-chip').textContent).toBe('Plan chat')
    expect(slot().firstElementChild).toBe(strip())
    expect(screen.getByTestId('conversation-body')).toBeTruthy()
  })

  it('★ returns to the list it was opened from, tab, search, sort and page included, never by a history step', () => {
    renderInWorkspace({ state: { chatList: '?kind=plan&q=sign&page=2' } })

    expect(back().getAttribute('href')).toBe('/projects/p1/chats?kind=plan&q=sign&page=2')
    fireEvent.click(back())
    expect(screen.getByTestId('where').textContent).toBe('/projects/p1/chats?kind=plan&q=sign&page=2')
    expect(JSON.parse(screen.getByTestId('where-state').textContent ?? 'null')).toEqual({ returnedFrom: 'chat-1' })
  })

  it('carries no menu: a chat is renamed and deleted from the list, not from its own header', () => {
    renderInWorkspace()
    expect(back()).toBeTruthy()
    expect(screen.queryByRole('button', { name: /more actions/i })).toBeNull()
  })

  it('returns to the fresh list from a chat opened any other way', () => {
    renderInWorkspace()
    expect(back().getAttribute('href')).toBe('/projects/p1/chats')
  })

  it('calls a chat with no title yet New chat', () => {
    renderInWorkspace({ chatTitle: null })
    expect(screen.getByTestId('all-chats-strip-title').textContent).toBe('New chat')
  })

  it('keeps a long title to one line, whole in its tooltip, and the chip whole beside it', () => {
    const long = 'A very long chat title that goes on well past the width of any rail this strip will sit in'
    renderInWorkspace({ chatTitle: long })
    const title = screen.getByTestId('all-chats-strip-title')
    expect(title.getAttribute('title')).toBe(long)
    expect(title.className).toMatch(/\btruncate\b/)
    expect(screen.getByTestId('chat-kind-chip').className).toMatch(/\bflex-shrink-0\b/)
  })

  it('★ a chat opened from the list takes focus onto its title; one opened another way leaves focus be', () => {
    renderInWorkspace({ state: { chatList: '' } })
    expect(document.activeElement).toBe(screen.getByTestId('all-chats-strip-title'))

    cleanup()
    renderInWorkspace()
    expect(document.activeElement).not.toBe(screen.getByTestId('all-chats-strip-title'))
  })

  it('keeps to the transcript\'s measure when the chat has the window to itself', () => {
    renderInWorkspace({ paneVisible: false })
    expect(strip().firstElementChild?.className).toMatch(/\bmax-w-thread\b/)

    cleanup()
    renderInWorkspace({ paneVisible: true })
    expect(strip().firstElementChild?.className).not.toMatch(/\bmax-w-thread\b/)
  })

  it('a chat with no project has no strip, and its body still renders', () => {
    renderInWorkspace({ over: { projectId: null } })
    expect(screen.getByTestId('conversation-body')).toBeTruthy()
    expect(screen.queryByTestId('all-chats-strip')).toBeNull()
  })
})
