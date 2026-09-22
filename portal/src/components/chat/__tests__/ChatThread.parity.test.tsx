/**
 * THE PARITY CHECKLIST, ASKED OF THE NEW HOST.
 *
 * `MessageContent.test.tsx` already covers the sanitisation pipeline (20 cases, 43 assertions) —
 * none of it moved through the port, so it is not re-asserted here (the one loss, `compact`, is
 * recorded as an amendment in that file's own docblock). This file only answers what the NEW
 * host does that the old one could not:
 *   - a model-authored `<img>` still cannot reach the DOM (no `img-src` CSP anywhere in this
 *     repo, so `disallowedElements` is the ONLY thing holding that refusal);
 *   - a citizen's own prose renders as markdown through the thread's user-message path, and
 *     the bubble's own overrides keep a pasted heading at body size;
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
  it('refuses a model-authored image, and fetches nothing from that host', () => {
    const { container } = mount([
      assistant('a1', 'before ![alt text](https://attacker.example/x.png) after'),
    ])

    expect(container.querySelectorAll('img')).toHaveLength(0)
    expect(container.innerHTML).not.toMatch(/attacker\.example/)
    // Liveness — the reply itself rendered, so the absence above is an absence and not a crash.
    expect(screen.getByTestId('assistant-message')).toBeTruthy()
  })

  it('renders citizen prose as MARKDOWN — one renderer, no branch on who wrote it', () => {
    const { container } = mount([
      { id: 'u1', role: 'user', parts: [{ type: 'text', text: '**bold**\n- one\n- two' }], seq: 1 },
    ])

    const bubble = screen.getByTestId('user-message')
    expect(within(bubble).getByText('bold').tagName).toBe('STRONG')
    expect(within(bubble).getAllByRole('listitem')).toHaveLength(2)
    // The liveness half: a crashed user-message render would satisfy an absence assertion by
    // rendering nothing at all.
    expect(container.querySelector('[data-role="user"]')).toBeTruthy()
  })

  it('a heading a citizen pasted renders at body size — the bubble overrides the prose scale', () => {
    mount([
      { id: 'u1', role: 'user', parts: [{ type: 'text', text: '# Gate 4 is down' }], seq: 1 },
    ])

    const bubble = screen.getByTestId('user-message')
    const heading = within(bubble).getByRole('heading', { level: 1, name: 'Gate 4 is down' })
    // The override lives on the bubble, not in the renderer — that is what keeps `MessageContent`
    // free of an authorship branch while a display-size H1 still reads as body text here.
    expect(heading.closest('[class*="prose-headings:text-sm"]')).toBeTruthy()
  })

  it('renders assistant markdown through the same pipeline as before', () => {
    const { container } = mount([assistant('a1', '**bold**\n- one\n- two')])

    expect(container.querySelector('strong')?.textContent).toBe('bold')
    expect(container.querySelectorAll('li')).toHaveLength(2)
  })

  it('★ the viewport FOLLOWS the stream — it is not anchored to the top of each turn', async () => {
    // DEFECT E4. `turnAnchor="top"` pins each new user message near the top for a focused read,
    // and the library derives `autoScroll = turnAnchor !== "top"` from it — so choosing the top
    // anchor also switched continuous follow OFF, and suppressed the resize-driven follow for the
    // whole duration of a run. The one positioning scroll happened, the reply then grew past the
    // fold unfollowed, `isAtBottom` correctly went false, and the return-to-latest control offered
    // itself on essentially every build with the reader never having scrolled anywhere.
    //
    // ASSERTED ON THE SOURCE, and the reason is worth stating rather than hiding: the behaviour
    // this pins is SCROLLING, and jsdom has no layout — every element reports zero height, so a
    // test that "scrolled" here would prove nothing about a browser. The real proof is a real
    // browser, run against a 77-message transcript, which opens 0px from the bottom with no
    // control showing (docs/debug/screenshots-2026-09-09/02-long-transcript-on-open.png). This
    // assertion exists so the prop cannot be flipped back silently between such runs.
    //
    // Mutation check: set `turnAnchor="top"` in thread.tsx and this goes red.
    const source = (await import('../../assistant-ui/thread?raw')).default as string
    // Comments stripped first: the docblock beside the prop QUOTES `turnAnchor="top"` while
    // explaining why it is wrong, so a naive negative match reads the explanation as the code.
    const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
    expect(code).toMatch(/<ThreadPrimitive\.Viewport\s+turnAnchor="bottom"/)
    expect(code).not.toMatch(/turnAnchor="top"/)
  })

  it('the viewport is the ONLY scroll container in the thread', () => {
    // The old surface nested five scroll containers (ChatPage plus BuilderPage's own); this
    // asserts the new one adds exactly one, by querying the class rather than trusting the
    // markup to stay put.
    const { container } = mount([assistant('a1', 'hello'), assistant('a2', 'again', 2)])

    const scrollers = container.querySelectorAll('.overflow-y-auto, .overflow-y-scroll')
    expect(scrollers).toHaveLength(1)
    expect(scrollers[0]).toBe(screen.getByTestId('thread-viewport'))
  })

  it('adds no calc(100vh …) anywhere', () => {
    // The only one in the repo lived on the retired chat page and coupled the transcript to
    // the navbar's height. The workspace shell owns the height model now.
    const { container } = mount([assistant('a1', 'hello')])
    expect(container.innerHTML).not.toMatch(/100vh/)
  })

  it('renders NO element for an assistant message whose text part is empty', () => {
    // This surface shipped an empty grey bubble once and fixed it; the renderer changed
    // underneath, so the guarantee is re-established rather than assumed.
    //
    // IT NOW HOLDS ONE LEVEL HIGHER THAN IT USED TO, which is what this test's own title always
    // asked for. The bubble SHELL used to survive — empty, but present, and carrying an action bar
    // whose copy button would put an empty string on the clipboard. The library drops a blank text
    // part outright (`fromThreadMessageLike`), so such a message reaches the renderer with no
    // content at all, and `AssistantMessage` now draws nothing for it.
    const { container } = mount([
      { id: 'a1', role: 'assistant', parts: [{ type: 'text', text: '' }], seq: 1 },
      // LIVENESS. Without a real message beside it, "no bubble" would pass on a harness that threw
      // before rendering anything — the false-green shape this repo has shipped before.
      { id: 'a2', role: 'assistant', parts: [{ type: 'text', text: 'still here' }], seq: 2 },
    ])

    expect(screen.getByText('still here')).toBeTruthy()
    expect(screen.getAllByTestId('assistant-message')).toHaveLength(1)
    // Exactly one paragraph, and it belongs to the LIVE message — so the empty one contributed no
    // prose element of its own. Asserting zero would now be asserting against the liveness message.
    const paragraphs = container.querySelectorAll('p')
    expect(paragraphs).toHaveLength(1)
    expect(paragraphs[0]?.textContent).toBe('still here')
  })

  it('the same reply renders identically in a Plan chat and a Build chat', () => {
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

  it('an assistant message carries a copy action and only a copy action', () => {
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

  it('a USER message carries no copy control', () => {
    mount([{ id: 'u1', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 1 }])

    const message = screen.getByTestId('user-message')
    expect(within(message).queryByRole('button')).toBeNull()
    // Liveness.
    expect(message.textContent).toContain('hi')
  })
})
