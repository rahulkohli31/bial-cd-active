/**
 * The runtime's tests, and the most valuable one is the capability snapshot.
 *
 * A capability in this library is DERIVED from which callbacks you hand over, so one wakes up when
 * somebody adds a prop to fix an unrelated problem — `setMessages` "to fix a rerender" switches on
 * BOTH `switchToBranch` and `delete`. That is a change nothing else in the suite can see: the
 * transcript still renders, every existing assertion still passes, and a branch picker and a delete
 * action quietly become live on a surface that has no story for either.
 *
 * `toEqual`, never `toMatchObject`. `toMatchObject` passes when a key we never listed appears,
 * which is precisely the failure this exists to catch.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup, act } from '@testing-library/react'
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type AppendMessage,
} from '@assistant-ui/react'

import { ACCEPT_ATTR } from '../../../../utils/attachmentInput'
import { createAttachmentAdapter } from '../attachmentAdapter'
import { useChatRuntime, EXPECTED_CAPABILITIES, type ChatRuntimeOptions } from '../useChatRuntime'
import { convertMessage } from '../convertMessage'
import type { ChatMessage } from '../../../../utils/messageTypes'
import type { StepItem } from '../../../../utils/turnStreamApi'

afterEach(cleanup)

const step = (over: Partial<StepItem> = {}): StepItem => ({
  type: 'step',
  seq: 4,
  tool: 'bash',
  label: 'Created the visitor table',
  state: 'ok',
  hidden: false,
  ...over,
})

const text = (id: string, role: 'user' | 'assistant', body: string, seq: number): ChatMessage => ({
  id,
  role,
  parts: [{ type: 'text', text: body }],
  seq,
})

/**
 * Mount the hook and hand the runtime back. The provider is mounted because the runtime is a live
 * object with subscriptions — reading its state out of a bare hook render works, but exercising it
 * the way the thread does is a truer check for very little extra.
 */
function mountRuntime(options: Partial<ChatRuntimeOptions> = {}) {
  const captured: { runtime?: ReturnType<typeof useChatRuntime> } = {}
  const props: ChatRuntimeOptions = {
    messages: [],
    isRunning: false,
    onNew: vi.fn<(m: AppendMessage) => Promise<void>>().mockResolvedValue(undefined),
    onCancel: vi.fn<() => Promise<void>>().mockResolvedValue(undefined),
    // REQUIRED SINCE PLAN 002's U5, and required rather than optional on purpose: the
    // `attachments` capability is DERIVED from this adapter's presence, so a caller that could
    // omit it would be a caller that silently turns the library's composer box off.
    attachments: createAttachmentAdapter({ accept: ACCEPT_ATTR, staged: () => [], onRefused: () => {} }),
    ...options,
  }

  function Harness() {
    const runtime = useChatRuntime(props)
    captured.runtime = runtime
    return (
      <AssistantRuntimeProvider runtime={runtime}>
        <div data-testid="mounted" />
      </AssistantRuntimeProvider>
    )
  }

  const view = render(<Harness />)
  return { ...view, props, runtime: () => captured.runtime! }
}

describe('useChatRuntime — the server owns the transcript', () => {
  it('reports the server transcript in server order, with the server ids intact', () => {
    const messages = [
      text('srv_1_u_0', 'user', 'build me a visitor log', 1),
      text('srv_2_a_0', 'assistant', 'Here is the plan.', 2),
      text('srv_3_u_0', 'user', 'looks good', 3),
    ]
    const { runtime } = mountRuntime({ messages })

    const state = runtime().thread.getState()
    expect(state.messages).toHaveLength(3)
    expect(state.messages.map((m) => m.id)).toEqual(['srv_1_u_0', 'srv_2_a_0', 'srv_3_u_0'])
    expect(state.messages.map((m) => m.role)).toEqual(['user', 'assistant', 'user'])
  })

  it('AE43: a reply of text plus a tool call renders identically live and from a reload', () => {
    // The same fixture supplied two ways — as a live assembly and as what the reload projection
    // produces. The assertion is on the converted messages, because that IS what the thread draws.
    const parts: ChatMessage['parts'] = [
      { type: 'text', text: 'I added the table.' },
      { type: 'step', step: step() },
    ]
    const live = mountRuntime({
      messages: [{ id: 'srv_5_a_0', role: 'assistant', parts, seq: 5 }],
    })
    const liveState = live.runtime().thread.getState().messages
    cleanup()

    const reloaded = mountRuntime({
      messages: [
        {
          id: 'srv_5_a_0',
          role: 'assistant',
          parts: parts.map((p) => ({ ...p })),
          seq: 5,
          createdAt: '2026-09-01T00:00:00Z',
        },
      ],
    })
    const reloadedState = reloaded.runtime().thread.getState().messages

    expect(liveState.map((m) => m.content)).toEqual(reloadedState.map((m) => m.content))
    expect(liveState.map((m) => m.id)).toEqual(reloadedState.map((m) => m.id))
  })

  it('flows isRunning straight through to thread.isRunning', () => {
    // Omitting it is NOT neutral: `thread.isRunning` then falls back to a last-message-status
    // heuristic, which is a different question with a different answer.
    const messages = [text('srv_1_u_0', 'user', 'hi', 1)]
    const idle = mountRuntime({ messages, isRunning: false })
    expect(idle.runtime().thread.getState().isRunning).toBe(false)
    cleanup()

    const running = mountRuntime({ messages, isRunning: true })
    expect(running.runtime().thread.getState().isRunning).toBe(true)
  })

  it('throws on a duplicate id rather than letting the runtime drop a turn', () => {
    // React logs every error thrown during render to console.error, so without this the suite's
    // output carries two full stack traces for a test that is PASSING. Silenced narrowly, around
    // this one render, rather than globally — a swallowed console.error is how a real unexpected
    // warning goes unnoticed everywhere else.
    const quiet = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      expect(() =>
        mountRuntime({
          messages: [
            text('srv_1_a_0', 'assistant', 'one', 1),
            text('srv_1_a_0', 'assistant', 'two', 1),
          ],
        }),
      ).toThrow(/srv_1_a_0/)
    } finally {
      quiet.mockRestore()
    }
  })
})

describe('useChatRuntime — the library mints exactly one id, and we do not let it', () => {
  it('appends an optimistic assistant message when a turn starts with a USER message last', () => {
    // Pinning the LIBRARY's behaviour, not ours — this is the trap, and it must be visible in a
    // test so the send path's sequencing requirement is not folklore.
    const { runtime } = mountRuntime({
      messages: [text('srv_1_u_0', 'user', 'build it', 1)],
      isRunning: true,
    })

    const messages = runtime().thread.getState().messages
    expect(messages).toHaveLength(2)
    expect(messages[1]?.role).toBe('assistant')
    expect(messages[1]?.id).not.toBe('srv_1_u_0')
  })

  it('mints nothing when a server-owned assistant message is already last', () => {
    // The send path's job, stated as an assertion: get a server-owned assistant message in place
    // at the instant isRunning flips true, and identity stays the server's for the whole turn.
    const { runtime } = mountRuntime({
      messages: [
        text('srv_1_u_0', 'user', 'build it', 1),
        { id: 'srv_2_a_0', role: 'assistant', parts: [], seq: 2 },
      ],
      isRunning: true,
    })

    const messages = runtime().thread.getState().messages
    expect(messages).toHaveLength(2)
    expect(messages.map((m) => m.id)).toEqual(['srv_1_u_0', 'srv_2_a_0'])
  })
})

describe('R51a — the capability list, pinned by exact equality', () => {
  it('registers exactly three capabilities: cancel, copy and attachments', () => {
    const { runtime } = mountRuntime()

    expect(runtime().thread.getState().capabilities).toEqual(EXPECTED_CAPABILITIES)
  })

  it('the written list has THREE true entries, and each one is load-bearing', () => {
    // Stated separately because of a specific failure mode: anyone writing this test from a
    // shorter phrasing ("everything off except copy") gets a red suite, and the tempting fixes
    // are both wrong — dropping `onCancel` silently deletes R55's stop path, and dropping the
    // attachments adapter silently turns the library's composer box off, since its add control,
    // its chip list and its dropzone are ALL gated on that capability (plan 002, U5).
    const trueOnes = Object.entries(EXPECTED_CAPABILITIES)
      .filter(([, v]) => v)
      .map(([k]) => k)
      .sort()

    expect(trueOnes).toEqual(['attachments', 'cancel', 'unstable_copy'])
  })

  it('★ attachments is registered BY the adapter, and the adapter is OURS', () => {
    // The capability is derived from the adapter's presence, so "it is on" and "we supplied it"
    // are the same fact — which is what keeps the library rendering a chip rather than deciding
    // which content is re-sent. Mutation receipt: drop `adapters` from the runtime options and
    // this goes red together with the exact-equality snapshot above.
    const { runtime } = mountRuntime()
    expect(runtime().thread.getState().capabilities.attachments).toBe(true)
  })

  it('cancel is registered BY onCancel — pressing stop reaches our handler', () => {
    const onCancel = vi.fn<() => Promise<void>>().mockResolvedValue(undefined)
    const { runtime } = mountRuntime({
      messages: [text('srv_1_u_0', 'user', 'build it', 1)],
      isRunning: true,
      onCancel,
    })

    expect(runtime().thread.getState().capabilities.cancel).toBe(true)
    act(() => {
      runtime().thread.cancelRun()
    })
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('THE MUTANT: adding setMessages makes the comparison fail, and names what woke up', () => {
    // The guard's teeth. One extra prop, two capabilities — this is the exact change someone
    // makes to "fix a rerender", and without this test it lands green.
    const capabilities = withSetMessages()

    expect(capabilities).not.toEqual(EXPECTED_CAPABILITIES)
    expect(capabilities.switchToBranch).toBe(true)
    expect(capabilities.delete).toBe(true)
  })
})

/**
 * A runtime built the way it must NOT be — the library reached for directly, with the one extra
 * prop added — so the mutant test compares against a real runtime's answer rather than against a
 * hand-written object that could drift from the library's actual derivation.
 */
function withSetMessages() {
  let captured: ReturnType<typeof useExternalStoreRuntime<ChatMessage>> | undefined
  function Harness() {
    captured = useExternalStoreRuntime<ChatMessage>({
      messages: [],
      isRunning: false,
      onNew: async () => {},
      onCancel: async () => {},
      convertMessage,
      unstable_capabilities: { copy: true },
      setMessages: () => {},
    })
    return null
  }
  render(<Harness />)
  const capabilities = captured!.thread.getState().capabilities
  cleanup()
  return capabilities
}
