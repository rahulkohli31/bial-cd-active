/**
 * THE ORDER A TURN IS READ IN, AND THE STATUS THAT SAYS IT IS THINKING.
 *
 * Two properties, both of them about what the citizen sees rather than about any one module:
 *
 *  1. A turn that wrote, acted, and wrote again renders in THAT order — and the same order
 *     whether the page was reloaded or not. The live path and the reload path build their
 *     `ChatMessage`s differently (one streaming message carrying every part, versus one message
 *     per stored item) and both arrive at this thread, so this is the only place the two can be
 *     put side by side and compared as DOM. Until the narration drop was deleted a turn could
 *     hold at most one block of text and it was always last, so this ordering was unreachable.
 *
 *  2. The working status appears when the model is reasoning and never carries its content. It
 *     is driven by a reasoning part that has NO FIELD for reasoning text — "status only" is a
 *     property of the type rather than a promise someone has to keep. What the converter hands
 *     the library is a constant the platform wrote (`REASONING_STATUS_TEXT`), because the
 *     library drops a literally empty reasoning part; the last case here pins that nothing but
 *     the status line reaches the DOM either way.
 *
 * Asserted on the ORDER of rendered elements, never on presence: every one of these cases passes
 * a presence check today with the parts in the wrong order, which is exactly the failure the
 * plan this file belongs to exists to prevent.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'

import ChatThread from '../ChatThread'
import type { ChatMessage, MessagePart } from '../../../utils/messageTypes'
import type { StepItem } from '../../../utils/turnStreamApi'

afterEach(cleanup)

const step = (seq: number, label: string): StepItem => ({
  type: 'step',
  seq,
  tool: 'read_file',
  label,
  state: 'ok',
  hidden: false,
})

function mount(messages: ChatMessage[], isRunning = false) {
  return render(
    <div style={{ height: 600 }}>
      <ChatThread
        messages={messages}
        isRunning={isRunning}
        onNew={vi.fn().mockResolvedValue(undefined)}
        onCancel={vi.fn().mockResolvedValue(undefined)}
      />
    </div>,
  )
}

/**
 * The turn under test, as a sequence of parts: prose, a step, prose, a step, prose.
 *
 * One fixture feeds both shapes below, so a divergence can only come from the shapes themselves
 * and never from two hand-written turns that quietly differ.
 */
const TURN: MessagePart[] = [
  { type: 'text', text: 'Looking at what you already have.' },
  { type: 'step', step: step(1, 'Looking at your visitor screen') },
  { type: 'text', text: 'It is there but nothing saves yet.' },
  { type: 'step', step: step(2, 'Building your visitor screen') },
  { type: 'text', text: 'Now it remembers every visitor.' },
]

/** The LIVE shape: one streaming message carrying every part of the turn. */
const live = (): ChatMessage[] => [{ id: 'a1', role: 'assistant', parts: TURN, seq: 1 }]

/**
 * The RELOAD shape: one message per projected item, which is what `messagesFromProjection`
 * produces from the stored rows. The ids follow its composite-key convention.
 */
const reloaded = (): ChatMessage[] =>
  TURN.map((part, index) => ({
    id: `srv_1_${part.type}_${index}`,
    role: 'assistant' as const,
    parts: [part],
    seq: 1,
  }))

/** Stands for an activity group in a reading order — see `readingOrder`. */
const ACTIVITY = '«activity»'

/**
 * What the thread drew, in order — a paragraph as its own text, an activity group as a marker.
 *
 * Read off the rendered tree rather than off the fixtures, because the question is what a person
 * looking at the screen reads top to bottom.
 *
 * A GROUP IS A MARKER RATHER THAN ITS LABEL, deliberately. The group is collapsed by default, so
 * its rows are not in the DOM at all, and the summary line its trigger shows is the workspace
 * plan's copy rather than this one's — asserting on that wording here would make this file go
 * red the next time somebody rewords a summary, which has nothing to do with the order parts
 * arrive in. Where the group SITS is what this file is about.
 */
function readingOrder(container: HTMLElement): string[] {
  const nodes = container.querySelectorAll(
    '[data-testid="assistant-message"] p, [data-testid="activity-group"]',
  )
  return Array.from(nodes)
    .map((node) =>
      node.getAttribute('data-testid') === 'activity-group'
        ? ACTIVITY
        : (node.textContent ?? '').trim(),
    )
    .filter((text) => text !== '')
}

describe('a turn reads the same whether or not the page was reloaded', () => {
  it('renders prose and steps interleaved, in the order they were written', () => {
    const { container } = mount(live())
    expect(readingOrder(container)).toEqual([
      'Looking at what you already have.',
      ACTIVITY,
      'It is there but nothing saves yet.',
      ACTIVITY,
      'Now it remembers every visitor.',
    ])
  })

  it('produces the identical reading order from the reload shape', () => {
    const liveRender = mount(live())
    const liveOrder = readingOrder(liveRender.container)
    cleanup()

    const reloadRender = mount(reloaded())
    expect(readingOrder(reloadRender.container)).toEqual(liveOrder)
  })

  it('groups two adjacent steps as ONE activity group, and prose between them as two', () => {
    // The board's sealing rule, and it has only been reachable since prose stopped being held.
    // `groupPartByType` coalesces ADJACENT tool-call parts, so what decides the number of groups
    // is whether anything was written between them — which is a fact about the turn, not a
    // setting. Two assertions rather than one: the same fixture minus its middle paragraph must
    // produce ONE group, or this proves nothing about the paragraph.
    const withProse = mount([{ id: 'a1', role: 'assistant', parts: TURN, seq: 1 }])
    expect(withProse.container.querySelectorAll('[data-testid="activity-group"]')).toHaveLength(2)
    cleanup()

    const adjacent = mount([
      {
        id: 'a1',
        role: 'assistant',
        parts: [TURN[1], TURN[3]] as MessagePart[],
        seq: 1,
      },
    ])
    expect(adjacent.container.querySelectorAll('[data-testid="activity-group"]')).toHaveLength(1)
  })
})

describe('the working status — that the agent is thinking, never what about', () => {
  it('renders one status line for a content-free reasoning part', () => {
    mount([{ id: 'a1', role: 'assistant', parts: [{ type: 'reasoning' }], seq: 1 }], true)

    const status = screen.getByTestId('working-status')
    expect(status.textContent).toBe('Working on your app')
    // ONE line, not one per part: the surface synthesises exactly one while the flag is true,
    // and a second would mean the status was being driven by something that repeats.
    expect(screen.getAllByTestId('working-status')).toHaveLength(1)
  })

  it('shows the status ABOVE the activity group on a turn that is also running steps', () => {
    // The grouping is HIERARCHICAL — reasoning and tool-call parts share a chain-of-thought
    // parent and get separate children — so both render, and the status comes first because the
    // model thought before it acted. A comment in `ChatThread` used to claim the activity group
    // covered the status on any turn that ran a tool; it does not, and this is that correction.
    const { container } = mount(
      [
        {
          id: 'a1',
          role: 'assistant',
          parts: [{ type: 'reasoning' }, TURN[1]] as MessagePart[],
          seq: 1,
        },
      ],
      true,
    )

    const rendered = Array.from(
      container.querySelectorAll('[data-testid="working-status"], [data-testid="activity-group"]'),
    ).map((node) => node.getAttribute('data-testid'))
    expect(rendered).toEqual(['working-status', 'activity-group'])
  })

  it('carries no reasoning text into the DOM, because the part has nowhere to hold any', () => {
    // The structural half of the guarantee. There is no field on `ReasoningPart` for text, and
    // the converter hands the library an EMPTY reasoning part — so this is not "the renderer
    // chooses not to draw it", it is "there is nothing to draw". The status line is the entire
    // rendered content of the message.
    const { container } = mount([{ id: 'a1', role: 'assistant', parts: [{ type: 'reasoning' }], seq: 1 }], true)

    const message = container.querySelector('[data-testid="assistant-message"]')
    expect(message?.textContent?.trim()).toBe('Working on your app')
  })

  it('shows no status at all once the reasoning part is gone', () => {
    // The surface drops the part the moment the server clears the flag, so the status has to be
    // absent on a turn that carries only prose. Paired with a liveness assertion, because an
    // empty transcript would satisfy the absence for the wrong reason.
    mount([{ id: 'a1', role: 'assistant', parts: [TURN[0]] as MessagePart[], seq: 1 }])

    expect(screen.queryByTestId('working-status')).toBeNull()
    expect(screen.getByTestId('assistant-message').textContent).toContain('Looking at what you already have.')
  })
})
