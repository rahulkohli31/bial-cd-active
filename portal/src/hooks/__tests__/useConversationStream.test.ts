/**
 * useConversationStream (U10): snapshot-replaces / tail-appends state composition, the
 * in-place step replacement by toolCallId, terminal outcomes, and the one-shot cursor
 * resume on a truncated socket.
 */

import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useConversationStream } from '../useConversationStream'
import * as api from '../../utils/turnStreamApi'
import type { TurnFrame } from '../../utils/turnStreamApi'

afterEach(() => {
  vi.restoreAllMocks()
})

function scriptedRead(script: Array<{ frames: TurnFrame[]; outcome: api.StreamOutcome }>) {
  let call = 0
  return vi
    .spyOn(api, 'readTurnStream')
    .mockImplementation(async ({ onFrame }: api.ReadStreamOptions) => {
      const step = script[Math.min(call, script.length - 1)]
      call += 1
      for (const frame of step.frames) onFrame(frame)
      return step.outcome
    })
}

const SNAPSHOT: TurnFrame = {
  type: 'snapshot',
  seq: 2,
  turnId: 't1',
  turnStatus: 'running',
  items: [{ type: 'user_text', seq: 1, text: 'hi' } as never],
  textSoFar: 'hello ',
  steps: [],
}

describe('useConversationStream', () => {
  it('composes snapshot + deltas + steps and settles on the terminal', async () => {
    scriptedRead([
      {
        outcome: 'completed',
        frames: [
          SNAPSHOT,
          { type: 'text_delta', seq: 3, text: 'world' },
          {
            type: 'step',
            seq: 4,
            toolCallId: 'call-1',
            phase: 'started',
            item: {
              type: 'step',
              seq: 0,
              mode: 'ask',
              tool: 'read_file',
              label: 'Read app/page.tsx',
              state: 'pending',
              hidden: true,
              detail: {},
            },
          },
          {
            type: 'step',
            seq: 5,
            toolCallId: 'call-1',
            phase: 'finished',
            item: {
              type: 'step',
              seq: 0,
              mode: 'ask',
              tool: 'read_file',
              label: 'Read app/page.tsx',
              state: 'ok',
              hidden: true,
              detail: { result: 'export default …' },
            },
          },
          { type: 'turn_ended', seq: 6, turnId: 't1', status: 'completed' },
        ],
      },
    ])
    const { result } = renderHook(() => useConversationStream('c1'))
    act(() => result.current.attach())
    await waitFor(() => expect(result.current.state.status).toBe('completed'))
    expect(result.current.state.text).toBe('hello world')
    expect(result.current.state.items).toHaveLength(1)
    // The finished step REPLACED its started card — one slot, resolved.
    expect(result.current.state.steps).toHaveLength(1)
    expect(result.current.state.steps[0].state).toBe('ok')
  })

  it('ignores unknown frame types without breaking the stream', async () => {
    scriptedRead([
      {
        outcome: 'completed',
        frames: [
          SNAPSHOT,
          { type: 'shiny_future_frame', seq: 3 } as TurnFrame,
          { type: 'turn_ended', seq: 4, turnId: 't1', status: 'completed' },
        ],
      },
    ])
    const { result } = renderHook(() => useConversationStream('c1'))
    act(() => result.current.attach())
    await waitFor(() => expect(result.current.state.status).toBe('completed'))
    expect(result.current.state.text).toBe('hello ')
  })

  it('surfaces the in-band error message alongside the failed terminal', async () => {
    scriptedRead([
      {
        outcome: 'completed',
        frames: [
          SNAPSHOT,
          { type: 'error', seq: 3, message: 'The assistant hit a problem.' },
          { type: 'turn_ended', seq: 4, turnId: 't1', status: 'failed' },
        ],
      },
    ])
    const { result } = renderHook(() => useConversationStream('c1'))
    act(() => result.current.attach())
    await waitFor(() => expect(result.current.state.status).toBe('failed'))
    expect(result.current.state.errorMessage).toBe('The assistant hit a problem.')
  })

  it('resumes ONCE with its cursor on a truncated socket, then errors on a repeat', async () => {
    const read = scriptedRead([
      { outcome: 'truncated', frames: [SNAPSHOT, { type: 'text_delta', seq: 3, text: 'wor' }] },
      { outcome: 'truncated', frames: [] },
    ])
    const { result } = renderHook(() => useConversationStream('c1'))
    act(() => result.current.attach())
    await waitFor(() => expect(result.current.state.status).toBe('error'))
    expect(read).toHaveBeenCalledTimes(2)
    const resume = read.mock.calls[1][0]
    expect(resume.cursor).toBe(3) // the last applied seq
    expect(resume.turnId).toBe('t1')
  })

  it('marks the stream stalled when the watchdog trips', async () => {
    scriptedRead([{ outcome: 'stalled', frames: [SNAPSHOT] }])
    const { result } = renderHook(() => useConversationStream('c1'))
    act(() => result.current.attach())
    await waitFor(() => expect(result.current.state.status).toBe('stalled'))
  })

  it('send() starts the turn then subscribes; stop() names the turn', async () => {
    const started = vi.spyOn(api, 'startTurn').mockResolvedValue({ turnId: 't42' })
    const stopped = vi.spyOn(api, 'stopTurn').mockResolvedValue('stopping')
    scriptedRead([
      {
        outcome: 'completed',
        frames: [{ type: 'turn_ended', seq: 1, turnId: 't42', status: 'completed' }],
      },
    ])
    const { result } = renderHook(() => useConversationStream('c1'))
    await act(async () => {
      await result.current.send({ text: 'build me a thing' })
    })
    expect(started).toHaveBeenCalledWith('c1', { text: 'build me a thing' })
    await waitFor(() => expect(result.current.state.status).toBe('completed'))
    await act(async () => {
      await result.current.stop()
    })
    expect(stopped).toHaveBeenCalledWith('c1', 't42')
  })
})
