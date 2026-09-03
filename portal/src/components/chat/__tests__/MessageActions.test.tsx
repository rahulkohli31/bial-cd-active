/**
 * N1 — EVERY ASSISTANT MESSAGE CARRIES A COPY ACTION, AND ONLY A COPY ACTION.
 *
 * ══ WHY THIS IS ITS OWN FILE ══
 *
 * `hideWhenRunning` is the whole difficulty, and it is a prop somebody will reasonably want to
 * set. `useActionBarFloatStatus` reads `hideWhenRunning && s.thread.isRunning` — the THREAD, not
 * the message — and a hidden Root returns `null`. Setting it would remove Copy from EVERY
 * assistant message for the whole of every turn, so a citizen watching a build could not copy the
 * plan they are reading. That directly undercuts the reason copy exists here: it is what makes
 * "build it again next week" real without any storage.
 *
 * ══ WHAT IS DELIBERATELY ABSENT ══
 *
 * No Reload, no Edit, no feedback, no More menu (which carries ExportMarkdown), no branch picker.
 * Each is driven by a capability U4's exact-equality snapshot pins to FALSE, so rendering the
 * control would be dead chrome — and the More menu would additionally offer an export nobody
 * specified. Counting the buttons is what catches one arriving.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react'
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react'
import type { FC } from 'react'

import { Thread, type ThreadComponents } from '../../assistant-ui/thread'
import { convertMessage } from '../runtime/convertMessage'
import type { ChatMessage } from '../../../utils/messageTypes'

afterEach(cleanup)

const TextPart: ThreadComponents['TextPart'] = ({ text }) => <span>{text}</span>
const ToolGroup: ThreadComponents['ToolGroup'] = ({ children }) => <div>{children}</div>
const ToolPart: ThreadComponents['ToolPart'] = () => null

const ReasoningGroup: ThreadComponents['ReasoningGroup'] = () => null

const components: ThreadComponents = { TextPart, ToolGroup, ToolPart, ReasoningGroup }

const reply = (id: string, text: string): ChatMessage => ({
  id,
  role: 'assistant',
  parts: [{ type: 'text', text }],
  seq: 1,
  createdAt: '2026-09-01T00:00:00.000Z',
})

const Harness: FC<{ messages: ChatMessage[]; isRunning?: boolean }> = ({ messages, isRunning = false }) => {
  const runtime = useExternalStoreRuntime<ChatMessage>({
    messages,
    isRunning,
    onNew: async () => undefined,
    onCancel: async () => undefined,
    convertMessage,
    unstable_capabilities: { copy: true },
  })
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread components={components} />
    </AssistantRuntimeProvider>
  )
}

const bars = () => screen.queryAllByTestId('assistant-action-bar')

describe('the action bar carries Copy, and only Copy', () => {
  it('renders exactly one button, named "Copy message"', () => {
    render(<Harness messages={[reply('a1', 'Here is the plan.')]} />)
    const buttons = bars()[0]?.querySelectorAll('button') ?? []
    // The COUNT is the assertion. Checking "Copy is present" would keep passing the day a More
    // menu — and the markdown export inside it — arrives beside it.
    expect(buttons).toHaveLength(1)
    expect(buttons[0]?.getAttribute('aria-label')).toBe('Copy message')
  })

  it('offers no Reload, Edit, feedback or branch control', () => {
    render(<Harness messages={[reply('a1', 'Here is the plan.')]} />)
    for (const name of [/regenerate|reload/i, /edit/i, /good response|bad response|feedback/i, /previous|next/i]) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
    // LIVENESS for those four absences.
    expect(screen.getByRole('button', { name: 'Copy message' })).toBeTruthy()
  })

  it('stays through a running turn — `hideWhenRunning` is NOT set', () => {
    // The one that matters. A citizen watching a build has to be able to copy the plan they are
    // reading; hiding the bar for the whole of every turn is what setting that prop would do.
    render(<Harness messages={[reply('a1', 'Here is the plan.')]} isRunning />)
    expect(screen.getByRole('button', { name: 'Copy message' })).toBeTruthy()
  })
})

describe('the accessible name does not change mid-interaction', () => {
  it('stays "Copy message" after a copy — the ICON swaps, the name does not', async () => {
    // Renaming a control while someone is standing on it is its own defect: a screen-reader user
    // hears the control they just used become a different control. The state change is announced
    // by the polite region instead.
    render(<Harness messages={[reply('a1', 'Here is the plan.')]} />)
    const button = screen.getByRole('button', { name: 'Copy message' })

    fireEvent.click(button)

    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalled())
    expect(screen.getByRole('button', { name: 'Copy message' })).toBe(button)
  })

  it('copies the message’s text', async () => {
    render(<Harness messages={[reply('a1', 'Here is the plan.')]} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy message' }))
    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith('Here is the plan.'),
    )
  })
})

describe('autohide="not-last"', () => {
  it('the newest reply keeps its bar without a hover', () => {
    // Non-default and deliberate: persistent on the latest turn, hover-revealed on history. Its
    // consequence belongs here rather than in a comment — without hover the Root returns `null`,
    // so on an older message there is no element and no attribute to query at all.
    render(<Harness messages={[reply('a1', 'older'), reply('a2', 'newest')]} />)
    // One bar, and it belongs to the last message.
    expect(bars()).toHaveLength(1)
    const messages = screen.getAllByTestId('assistant-message')
    expect(messages[messages.length - 1]?.contains(bars()[0] as Node)).toBe(true)
  })
})
