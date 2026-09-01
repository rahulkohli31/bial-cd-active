/**
 * The seam's own tests. What they pin, in order of how expensive the failure is:
 *
 *  - the same fixture converts identically whether it arrived live or on reload (AE43);
 *  - a step carries LABEL AND STATE and nothing else (R36's wall, asserted on the object);
 *  - a duplicate id throws here rather than silently losing a turn inside the runtime;
 *  - the predicate that decides whether the library mints an id of its own.
 */
import { describe, it, expect } from 'vitest'

import {
  assertUniqueIds,
  convertMessage,
  convertPart,
  hasUpcomingMessage,
  type ActivityArgs,
} from '../convertMessage'
import type { ChatMessage } from '../../../../utils/messageTypes'
import type { StepItem } from '../../../../utils/turnStreamApi'

const step = (over: Partial<StepItem> = {}): StepItem => ({
  type: 'step',
  seq: 4,
  tool: 'bash',
  label: 'Created the visitor table',
  state: 'ok',
  hidden: false,
  ...over,
})

const argsOf = (part: ReturnType<typeof convertPart>): ActivityArgs =>
  (part as unknown as { args: ActivityArgs }).args

describe('convertMessage — parts', () => {
  it('maps prose to a text part', () => {
    expect(convertPart({ type: 'text', text: 'Here is your app.' })).toEqual({
      type: 'text',
      text: 'Here is your app.',
    })
  })

  it('maps a step to a tool-call part carrying only the label and the state', () => {
    const part = convertPart({ type: 'step', step: step() })

    expect(part).toEqual({
      type: 'tool-call',
      toolCallId: 'step-4',
      toolName: 'activity',
      args: { label: 'Created the visitor table', state: 'ok' },
    })
  })

  it('maps pending to running, so the group reports itself running', () => {
    expect(argsOf(convertPart({ type: 'step', step: step({ state: 'pending' }) })).state).toBe(
      'running',
    )
    expect(argsOf(convertPart({ type: 'step', step: step({ state: 'failed' }) })).state).toBe(
      'failed',
    )
  })

  it('gives a step a tool call id that does not move between deltas', () => {
    // If it moved, the group would see a NEW part on every delta: the live count would climb
    // while the same single step re-rendered.
    const first = convertPart({ type: 'step', step: step({ state: 'pending' }) })
    const settled = convertPart({ type: 'step', step: step({ state: 'ok' }) })

    expect((first as unknown as { toolCallId: string }).toolCallId).toBe('step-4')
    expect((settled as unknown as { toolCallId: string }).toolCallId).toBe('step-4')
  })

  it('renders no element for the parts that are not transcript prose', () => {
    expect(convertPart({ type: 'build_in_progress', sessionId: 's1' })).toBeNull()
    expect(
      convertPart({
        type: 'plan_options',
        item: { type: 'plan_options', seq: 2, toolCallId: 'tc-1', state: 'pending' },
      }),
    ).toBeNull()
  })
})

describe('R36 — the wall is the converter, not a promise at the draw site', () => {
  it('drops every platform-internal field a step frame may carry', () => {
    // The wire's step frame carries `detail.args` and `detail.result`, redacted and clipped but
    // PRESENT, and the diagnostic frame carries a developer half whose own schema records that
    // "safe to render verbatim" once produced a stack trace under a file path in a citizen's
    // chat. A part that never holds the field cannot leak it, however the expander is written.
    const leaky = {
      ...step(),
      detail: { args: '/srv/app/.env AZURE_KEY=abc', result: 'Traceback (most recent call last)' },
      internalMarker: '<<<BIAL_INTERNAL>>>',
    } as unknown as StepItem

    const part = convertPart({ type: 'step', step: leaky })
    const serialised = JSON.stringify(part)

    expect(serialised).not.toMatch(/detail/)
    expect(serialised).not.toMatch(/AZURE_KEY/)
    expect(serialised).not.toMatch(/Traceback/)
    expect(serialised).not.toMatch(/BIAL_INTERNAL/)
    // And the liveness half: the row still says what happened.
    expect(argsOf(part).label).toBe('Created the visitor table')
  })

  it('never forwards the raw tool name, so an unrecognised command cannot reach the screen', () => {
    const part = convertPart({
      type: 'step',
      step: step({ tool: 'bash', label: 'Checked the workspace' }),
    })

    expect(JSON.stringify(part)).not.toMatch(/bash/)
    expect(argsOf(part).label).toBe('Checked the workspace')
  })
})

describe('convertMessage — identity is the server’s', () => {
  it('carries the server id through, so the array index is never the fallback', () => {
    const message: ChatMessage = {
      id: 'srv_7_a_3',
      role: 'assistant',
      parts: [{ type: 'text', text: 'done' }],
      seq: 7,
    }

    expect(convertMessage(message).id).toBe('srv_7_a_3')
  })

  it('AE43: the same reply converts identically live and on reload', () => {
    // The live assembly and `messagesFromProjection` both produce a `ChatMessage`; ONE converter
    // takes both. Identical output is therefore a property of the shape, not a rule anyone has to
    // remember — which is what makes "one surface, both kinds, live and reloaded" true.
    const parts: ChatMessage['parts'] = [
      { type: 'text', text: 'I added the table.' },
      { type: 'step', step: step() },
    ]
    const live: ChatMessage = { id: 'srv_9_a_0', role: 'assistant', parts, seq: 9 }
    const reloaded: ChatMessage = {
      id: 'srv_9_a_0',
      role: 'assistant',
      parts: parts.map((p) => ({ ...p })),
      seq: 9,
      createdAt: '2026-09-01T00:00:00Z',
    }

    expect(convertMessage(live)).toEqual(convertMessage(reloaded))
  })

  it('keeps a message whose parts all drop, with empty content', () => {
    // Dropping a PART is not dropping a MESSAGE. The message survives with no content and the
    // thread renders no element for it — which is different from the message disappearing and
    // taking its id (and therefore its position) with it.
    const converted = convertMessage({
      id: 'srv_3_g_0',
      role: 'assistant',
      parts: [{ type: 'build_in_progress', sessionId: 's1' }],
      seq: 3,
    })

    expect(converted.id).toBe('srv_3_g_0')
    expect(converted.content).toEqual([])
  })
})

describe('assertUniqueIds — the duplicate that would silently cost a turn', () => {
  it('throws, naming the offending id', () => {
    const dup: ChatMessage[] = [
      { id: 'srv_1_a_0', role: 'assistant', parts: [{ type: 'text', text: 'one' }], seq: 1 },
      { id: 'srv_1_a_0', role: 'assistant', parts: [{ type: 'text', text: 'two' }], seq: 1 },
    ]

    // The runtime's own behaviour is to keep the LAST occurrence and console.warn — a lost turn
    // whose only evidence is a console nobody has open.
    expect(() => assertUniqueIds(dup)).toThrow(/srv_1_a_0/)
  })

  it('passes a well-formed transcript', () => {
    expect(() =>
      assertUniqueIds([
        { id: 'srv_1_u_0', role: 'user', parts: [], seq: 1 },
        { id: 'srv_2_a_0', role: 'assistant', parts: [], seq: 2 },
      ]),
    ).not.toThrow()
  })
})

describe('hasUpcomingMessage — the one id the library mints', () => {
  it('is true when a turn starts with a USER message last', () => {
    // This is the case that breaks server-owned identity: the runtime appends an optimistic
    // assistant message with an id we do not control. The send path's job is to make sure a
    // server-owned assistant message is already last at the instant isRunning flips true.
    const messages: ChatMessage[] = [{ id: 'u1', role: 'user', parts: [], seq: 1 }]

    expect(hasUpcomingMessage(true, messages)).toBe(true)
  })

  it('is false once a server-owned assistant message is last', () => {
    const messages: ChatMessage[] = [
      { id: 'u1', role: 'user', parts: [], seq: 1 },
      { id: 'srv_2_a_0', role: 'assistant', parts: [], seq: 2 },
    ]

    expect(hasUpcomingMessage(true, messages)).toBe(false)
  })

  it('is false when nothing is running, whatever is last', () => {
    expect(hasUpcomingMessage(false, [{ id: 'u1', role: 'user', parts: [], seq: 1 }])).toBe(false)
  })

  it('is true on an empty transcript while running', () => {
    // No last message at all is not an assistant message, so the library would mint one here too.
    expect(hasUpcomingMessage(true, [])).toBe(true)
  })
})
