/**
 * R36's WALL IS AT THE CONVERTER, NOT AT THE DRAW SITE.
 *
 * ══ WHY THAT PLACEMENT IS THE REQUIREMENT ══
 *
 * A diagnostic frame carries a developer half — the source, and the compiler's own title — whose
 * schema docstring records that "safe to render verbatim" is the sentence that once produced a
 * stack trace under a file-path title in a citizen's chat. That half is still on the wire, because
 * the agent is the party that can act on it.
 *
 * `StepItem.detail.args` / `detail.result` are NOT on the wire: `StepDetail` was removed from the
 * server outright (`services/messages/projection.py` now pins its emitted field set in a test) and
 * `StepItem` here has no `detail` to copy. The converter's silence about those fields is therefore
 * belt and braces rather than the only guard — which is worth stating plainly, because an earlier
 * draft of this docblock claimed the opposite and would have had the next reader believe the wall
 * was load-bearing where it is not, and merely tidy where it is.
 *
 * A promise at the component ("this row only renders the label") is only as good as the next
 * person editing that component. A converter that never copies the field means the expander (R33)
 * has NOTHING TO LEAK even if someone later renders every field a part holds. That is why the
 * assertions below are made on the CONVERTED OBJECT as well as on the rendered tree: the first is
 * the guarantee, the second is only its symptom.
 *
 * Covers AE34.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'

import { convertMessage, convertPart } from '../convertMessage'
import ActivityRow from '../../ActivityRow'
import type { ChatMessage, MessagePart } from '../../../../utils/messageTypes'

/** Platform-internal text of the three kinds that have actually reached a browser. */
const INTERNAL = {
  path: '/workspace/app/(dashboard)/page.tsx',
  argv: 'npx --yes prisma migrate deploy --schema /workspace/prisma/schema.prisma',
  stack: 'at Object.<anonymous> (/workspace/.next/server/app/page.js:12:5)',
}

/** A step part carrying every internal field the wire is known to send. */
const loadedStep = (): MessagePart => ({
  type: 'step',
  step: {
    type: 'step',
    seq: 4,
    tool: 'run_command',
    label: 'Updated the home page',
    state: 'ok',
    hidden: false,
    // Not in `StepItem`'s declared shape, and that is the point: the wire is not typed, and the
    // converter has to drop what it was never told about rather than pass it through.
    ...({ detail: { args: INTERNAL.argv, result: INTERNAL.stack, path: INTERNAL.path } } as object),
  },
})

const message = (parts: MessagePart[]): ChatMessage => ({
  id: 'm1',
  role: 'assistant',
  parts,
  seq: 1,
  createdAt: '2026-09-01T00:00:00.000Z',
})

describe('the converted part carries the label and the state, and NOTHING else', () => {
  it('drops `detail` entirely — asserted on the object, which is the guarantee', () => {
    const part = convertPart(loadedStep())
    expect(part).toBeTruthy()
    expect(part).toMatchObject({ type: 'tool-call', toolName: 'activity' })

    // The KEY SET, not a handful of absences. A test naming `detail` alone would keep passing the
    // day the wire renames it, which is exactly how this class of leak returns.
    const args = (part as { args: Record<string, unknown> }).args
    expect(Object.keys(args).sort()).toEqual(['label', 'state'])

    // …and no `result` or `artifact` rides on the part beside the args.
    expect(Object.keys(part as object).sort()).toEqual(['args', 'toolCallId', 'toolName', 'type'])
  })

  it('drops the RAW TOOL NAME too — an unrecognised command must never reach the screen as argv', () => {
    // `tool` is `run_command` here. The server's classifier computes the friendly label and fails
    // closed; this is the second wall, and it is what stops a command line rendering as a step.
    const args = (convertPart(loadedStep()) as { args: Record<string, unknown> }).args
    expect(args.label).toBe('Updated the home page')
    expect(JSON.stringify(args)).not.toContain('run_command')
  })

  it('none of the internal text survives conversion, in any field', () => {
    const converted = JSON.stringify(convertMessage(message([loadedStep()])))
    for (const [name, value] of Object.entries(INTERNAL)) {
      expect(converted, `${name} survived the converter`).not.toContain(value)
    }
    // LIVENESS: the step DID convert — the absences above describe a redaction, not a drop.
    expect(converted).toContain('Updated the home page')
  })
})

describe('…and none of it reaches the DOM either — the symptom half', () => {
  it('expanding a row puts no internal text on screen', () => {
    const part = convertPart(loadedStep()) as unknown as Record<string, unknown>
    const Row = ActivityRow as (props: Record<string, unknown>) => JSX.Element
    const { container } = render(<Row {...part} />)

    for (const value of Object.values(INTERNAL)) {
      expect(container.textContent).not.toContain(value)
    }
    expect(screen.getByText('Updated the home page')).toBeTruthy()
    // No `<pre>` anywhere: the shape that carried the stack trace, gone by construction.
    expect(container.querySelector('pre')).toBeNull()
    cleanup()
  })
})

describe('the parts with no rendered form', () => {
  it('a diagnostic’s developer half is never mapped into a part at all', () => {
    // A diagnostic is not a `MessagePart` — it arrives as a turn FRAME, and the surface takes only
    // its citizen-facing sentence when it turns one into a row. There is no converter path that
    // could carry the developer half, which is stated here so the absence is deliberate rather
    // than accidental.
    const parts: MessagePart['type'][] = ['build', 'build_in_progress', 'plan_options']
    expect(parts).not.toContain('diagnostic')
  })

  it('`build`, `build_in_progress` and `plan_options` convert to nothing', () => {
    expect(convertPart({ type: 'build', status: 'ended', sessionId: 's1' } as MessagePart)).toBeNull()
    expect(convertPart({ type: 'build_in_progress', sessionId: 's1' } as MessagePart)).toBeNull()
    expect(
      convertPart({ type: 'plan_options', item: { toolCallId: 'c1', state: 'pending' } } as MessagePart),
    ).toBeNull()
  })

  it('a message made only of them converts to empty content, not to a missing message', () => {
    // Dropping a PART is not dropping a MESSAGE. The message still exists with empty content, and
    // the thread renders no element for it — which is what lets the surface decide separately
    // whether such a row belongs in the transcript at all.
    const converted = convertMessage(message([{ type: 'build_in_progress', sessionId: 's1' } as MessagePart]))
    expect(converted.id).toBe('m1')
    expect(converted.content).toEqual([])
  })
})
