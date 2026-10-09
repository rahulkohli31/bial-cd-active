/**
 * The live reply both chat pages draw. Pinned here because the two pages share it, and a change
 * that suits one of them reorders or drops the other's steps.
 */
import { describe, it, expect } from 'vitest'

import { appendText, putStep, streamingParts, type LiveTurn } from '../liveTurnParts'
import type { StepItem } from '../../../utils/turnStreamApi'

const step = (label: string, state: StepItem['state'] = 'pending', hidden = false): StepItem => ({
  type: 'step',
  seq: 0,
  tool: 'run_python',
  label,
  state,
  hidden,
})

const newTurn = (): LiveTurn => ({ parts: [], working: false })

describe('a live turn keeps the order it happened in', () => {
  it('a finished step replaces its started one in place, and only a new one counts as new', () => {
    const turn = newTurn()
    expect(putStep(turn, 'call-1', step('Running the analysis'))).toBe(true)
    appendText(turn, 'Done.', true)
    expect(putStep(turn, 'call-1', step('Running the analysis', 'ok'))).toBe(false)

    expect(streamingParts(turn)).toEqual([
      { type: 'step', step: step('Running the analysis', 'ok') },
      { type: 'text', text: 'Done.' },
    ])
  })

  it('prose after a step opens its own block even when the frame says to continue', () => {
    const turn = newTurn()
    appendText(turn, 'Let me add', true)
    appendText(turn, ' that up.', false)
    putStep(turn, 'call-1', step('Running the analysis', 'ok'))
    appendText(turn, 'It totals 4,210.', false)

    expect(streamingParts(turn)).toEqual([
      { type: 'text', text: 'Let me add that up.' },
      { type: 'step', step: step('Running the analysis', 'ok') },
      { type: 'text', text: 'It totals 4,210.' },
    ])
  })
})

describe('the streaming message', () => {
  it('drops hidden steps, carries the working line at the tail, and ends on a text part', () => {
    const turn = newTurn()
    appendText(turn, 'Reading it now.', true)
    putStep(turn, 'call-1', step('Reading budget.xlsx', 'ok', true))
    putStep(turn, 'call-2', step('Running the analysis'))
    turn.working = true

    expect(streamingParts(turn)).toEqual([
      { type: 'text', text: 'Reading it now.' },
      { type: 'step', step: step('Running the analysis') },
      { type: 'reasoning' },
      { type: 'text', text: '' },
    ])
  })

  it('is a new array every time, so the runtime sees each change', () => {
    const turn = newTurn()
    appendText(turn, 'Hello.', true)
    expect(streamingParts(turn)).not.toBe(streamingParts(turn))
  })
})
