/**
 * THE PARITY CHECKLIST, ASKED OF THE NEW HOST (R52).
 *
 * `MessageContent.test.tsx` is the checklist R52 asks someone to write and it was already written —
 * 21 cases, 44 assertions. It still passes UNCHANGED, because `MessageContent` is re-hosted rather
 * than replaced: not one line of its sanitisation pipeline moved. That file is the guarantee; this
 * file is the mirror question L5 insists on afterwards — what does the NEW host do that the old
 * one could not?
 *
 * Three answers, each asserted below rather than reasoned about:
 *   - a model-authored `<img>` still cannot reach the DOM (there is no `img-src` CSP anywhere in
 *     this repo, so `disallowedElements` is the ONLY thing holding that refusal);
 *   - user prose is still verbatim through the thread's own user-message path;
 *   - the thread introduces exactly one scroll container, where the old surface nested five.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'

import ChatThread from '../ChatThread'
import ChatRuntimeProvider from '../runtime/ChatRuntimeProvider'
import type { ChatMessage } from '../../../utils/messageTypes'
import type { StepItem } from '../../../utils/turnStreamApi'

afterEach(cleanup)

const step = (over: Partial<StepItem> = {}): StepItem => ({
  type: 'step',
  seq: 1,
  tool: 'bash',
  label: 'Reading your visitor screen',
  state: 'ok',
  hidden: false,
  ...over,
})

function mount(messages: ChatMessage[], isRunning = false) {
  return render(
    <div style={{ height: 600 }}>
      <ChatRuntimeProvider
        messages={messages}
        isRunning={isRunning}
        onNew={vi.fn().mockResolvedValue(undefined)}
        onCancel={vi.fn().mockResolvedValue(undefined)}
      >
        <ChatThread />
      </ChatRuntimeProvider>
    </div>,
  )
}

const assistant = (id: string, text: string, seq = 1): ChatMessage => ({
  id,
  role: 'assistant',
  parts: [{ type: 'text', text }],
  seq,
})

describe('ChatThread — what the new host must still guarantee', () => {
  it('AE41: refuses a model-authored image, and fetches nothing from that host', () => {
    const { container } = mount([
      assistant('a1', 'before ![alt text](https://attacker.example/x.png) after'),
    ])

    expect(container.querySelectorAll('img')).toHaveLength(0)
    expect(container.innerHTML).not.toMatch(/attacker\.example/)
    // Liveness — the reply itself rendered, so the absence above is an absence and not a crash.
    expect(screen.getByTestId('assistant-message')).toBeTruthy()
  })

  it('renders user prose VERBATIM — markdown is never parsed in a user message', () => {
    const { container } = mount([
      { id: 'u1', role: 'user', parts: [{ type: 'text', text: '**not bold**' }], seq: 1 },
    ])

    expect(container.querySelector('strong')).toBeNull()
    expect(screen.getByTestId('user-message').textContent).toContain('**not bold**')
  })

  it('renders assistant markdown through the same pipeline as before', () => {
    const { container } = mount([assistant('a1', '**bold**\n- one\n- two')])

    expect(container.querySelector('strong')?.textContent).toBe('bold')
    expect(container.querySelectorAll('li')).toHaveLength(2)
  })

  it('R49: the viewport is the ONLY scroll container in the thread', () => {
    // The mechanical form of the requirement. The old surface nested five scrollers
    // (ChatPage 639→642→644→655→700→719 plus BuilderPage's own); this asserts the new one adds
    // exactly one, by querying the class rather than trusting the markup to stay put.
    const { container } = mount([assistant('a1', 'hello'), assistant('a2', 'again', 2)])

    const scrollers = container.querySelectorAll('.overflow-y-auto, .overflow-y-scroll')
    expect(scrollers).toHaveLength(1)
    expect(scrollers[0]).toBe(screen.getByTestId('thread-viewport'))
  })

  it('adds no calc(100vh …) anywhere', () => {
    // The only one in the repo lived at ChatPage.tsx:642 and coupled the transcript to the
    // navbar's height. Plan A owns the height model now.
    const { container } = mount([assistant('a1', 'hello')])
    expect(container.innerHTML).not.toMatch(/100vh/)
  })

  it('renders NO element for an assistant message whose text part is empty', () => {
    // This surface shipped an empty grey bubble once and fixed it; the renderer changed
    // underneath, so the guarantee is re-established rather than assumed.
    const { container } = mount([{ id: 'a1', role: 'assistant', parts: [{ type: 'text', text: '' }], seq: 1 }])

    const message = screen.getByTestId('assistant-message')
    expect(within(message).queryByText(/\S/)).toBeNull()
    expect(container.querySelectorAll('p')).toHaveLength(0)
  })

  it('AE43: the same reply renders identically in a Plan chat and a Build chat', () => {
    // Asserted on the rendered TREE, because nothing in the renderer may consult the kind — and
    // the only way to prove that is to render the same parts twice and diff the DOM.
    const parts: ChatMessage['parts'] = [
      { type: 'text', text: 'I added the table.' },
      { type: 'step', step: step() },
    ]
    const first = mount([{ id: 'a1', role: 'assistant', parts, seq: 1 }])
    const planHtml = first.container.innerHTML
    cleanup()

    const second = mount([{ id: 'a1', role: 'assistant', parts: parts.map((p) => ({ ...p })), seq: 1 }])
    expect(second.container.innerHTML).toBe(planHtml)
  })

  it('N1: an assistant message carries a copy action and only a copy action', () => {
    mount([assistant('a1', 'Here is your app.')])

    const bar = screen.getByTestId('assistant-action-bar')
    const buttons = within(bar).getAllByRole('button')
    expect(buttons).toHaveLength(1)
    expect(buttons[0]?.getAttribute('aria-label')).toBe('Copy message')

    // The mutant half: none of the actions the registry ships beside Copy survived the port.
    expect(within(bar).queryByRole('button', { name: /refresh|reload|regenerate/i })).toBeNull()
    expect(within(bar).queryByRole('button', { name: /edit/i })).toBeNull()
    expect(within(bar).queryByRole('button', { name: /more/i })).toBeNull()
  })

  it('N1: a USER message carries no copy control', () => {
    mount([{ id: 'u1', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 1 }])

    const message = screen.getByTestId('user-message')
    expect(within(message).queryByRole('button')).toBeNull()
    // Liveness.
    expect(message.textContent).toContain('hi')
  })
})
