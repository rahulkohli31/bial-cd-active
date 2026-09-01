/**
 * R55 — the relocated stop control.
 *
 * The pairing rule this file follows throughout: EVERY ABSENCE ASSERTION IS PAIRED WITH A
 * LIVENESS ASSERTION. `queryByTestId(...)` returning null also passes when the component threw,
 * so "the control is gone" and "the whole surface crashed" are the same green without a second
 * assertion proving something still rendered.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'

import StopTurnControl from '../StopTurnControl'

afterEach(cleanup)

const CHAT = 'c-1'
const TURN = 't-9'

function setup(overrides: Partial<React.ComponentProps<typeof StopTurnControl>> = {}) {
  const props = {
    running: true,
    resolveTarget: () => ({ conversationId: CHAT, turnId: TURN }),
    onStopTurn: vi.fn<(c: string, t: string) => Promise<void>>().mockResolvedValue(undefined),
    onStopSession: vi.fn<() => Promise<void>>().mockResolvedValue(undefined),
    onStopFailed: vi.fn<(m: string) => void>(),
    ...overrides,
  }
  // A sibling stands in for the composer: it is what the liveness assertions read, and it is
  // what makes "the control is absent" mean something other than "nothing rendered".
  render(
    <div>
      <textarea aria-label="Message" defaultValue="" />
      <StopTurnControl {...props} />
    </div>,
  )
  return props
}

const stopButton = () => screen.queryByTestId('stop-turn')

describe('StopTurnControl', () => {
  it('stops the LIVE TURN with the active conversation id and that turn id', async () => {
    const { onStopTurn, onStopSession } = setup()

    fireEvent.click(screen.getByTestId('stop-turn'))

    await waitFor(() => expect(onStopTurn).toHaveBeenCalledWith(CHAT, TURN))
    expect(onStopSession).not.toHaveBeenCalled()
  })

  it('stops a LEGACY BUILD SESSION when there is no turn id', async () => {
    // The arm discriminates on whether a turn id exists — a transport fact, not a chat kind.
    const { onStopTurn, onStopSession } = setup({ resolveTarget: () => null })

    fireEvent.click(screen.getByTestId('stop-turn'))

    await waitFor(() => expect(onStopSession).toHaveBeenCalledTimes(1))
    expect(onStopTurn).not.toHaveBeenCalled()
  })

  it('is absent when no turn is running, and the composer is still there and typeable', () => {
    setup({ running: false })

    expect(stopButton()).toBeNull()
    // The liveness half. Without it this test passes just as well when the render threw.
    const box = screen.getByLabelText('Message')
    fireEvent.change(box, { target: { value: 'still typing' } })
    expect((box as HTMLTextAreaElement).value).toBe('still typing')
  })

  it('issues ONE stop request when pressed twice quickly', async () => {
    let release: () => void = () => {}
    const onStopTurn = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          release = resolve
        }),
    )
    setup({ onStopTurn })

    fireEvent.click(screen.getByTestId('stop-turn'))
    fireEvent.click(screen.getByTestId('stop-turn'))

    expect(onStopTurn).toHaveBeenCalledTimes(1)
    release()
  })

  it('carries aria-disabled and a reason while stopping, and NOT a real disabled', async () => {
    // The second half is the assertion that matters. `disabled` on a focused control blurs it to
    // document.body, which is the exact failure R64 exists to forbid, and it is invisible to any
    // test that only checks the control "looks unavailable".
    let release: () => void = () => {}
    setup({
      onStopTurn: vi.fn(
        () =>
          new Promise<void>((resolve) => {
            release = resolve
          }),
      ),
    })

    const button = screen.getByTestId('stop-turn')
    fireEvent.click(button)

    await waitFor(() => expect(button.getAttribute('aria-disabled')).toBe('true'))
    expect(button.hasAttribute('disabled')).toBe(false)
    expect((button as HTMLButtonElement).disabled).toBe(false)
    expect(button.getAttribute('title')).toBeTruthy()

    release()
  })

  it('keeps its accessible name as Stop while the request is in flight', async () => {
    // Renaming a control mid-interaction is the defect U15 avoids on the copy button. The glyph
    // and the title carry the in-flight state; the word does not move.
    let release: () => void = () => {}
    setup({
      onStopTurn: vi.fn(
        () =>
          new Promise<void>((resolve) => {
            release = resolve
          }),
      ),
    })

    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy()
    fireEvent.click(screen.getByTestId('stop-turn'))
    await waitFor(() =>
      expect(screen.getByTestId('stop-turn').getAttribute('aria-disabled')).toBe('true'),
    )
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy()

    release()
  })

  it('reports a failed stop and returns to a pressable state', async () => {
    // A citizen must not be left holding a dead button. Both halves are asserted: the sentence
    // exists (U9 routes it to the assertive slot), and the control works again afterwards.
    const onStopTurn = vi
      .fn<(c: string, t: string) => Promise<void>>()
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValue(undefined)
    const { onStopFailed } = setup({ onStopTurn })

    fireEvent.click(screen.getByTestId('stop-turn'))

    await waitFor(() => expect(onStopFailed).toHaveBeenCalledTimes(1))
    expect(vi.mocked(onStopFailed).mock.calls[0]?.[0]).toMatch(/could not stop/i)

    await waitFor(() =>
      expect(screen.getByTestId('stop-turn').getAttribute('aria-disabled')).toBe('false'),
    )
    fireEvent.click(screen.getByTestId('stop-turn'))
    await waitFor(() => expect(onStopTurn).toHaveBeenCalledTimes(2))
  })

  it('leaves the composer typeable while the control is on screen (R45 with R55)', () => {
    setup()

    expect(screen.getByTestId('stop-turn')).toBeTruthy()
    const box = screen.getByLabelText('Message')
    expect((box as HTMLTextAreaElement).disabled).toBe(false)
    fireEvent.change(box, { target: { value: 'one more thing' } })
    expect((box as HTMLTextAreaElement).value).toBe('one more thing')
  })
})

describe('StopTurnControl reads its target at press time, not at render time', () => {
  it('stops the turn that is live WHEN PRESSED, not the one live when it rendered', async () => {
    // The regression this shape exists to prevent. `BuilderPage` keeps the live turn id in a ref
    // whose own comment records that a handler closing over a render-time value stops the wrong
    // turn. A plain `turnId` PROP would have moved that bug up one layer and made it silent.
    let live = 't-first'
    const onStopTurn = vi.fn<(c: string, t: string) => Promise<void>>().mockResolvedValue(undefined)
    render(
      <StopTurnControl
        running
        resolveTarget={() => ({ conversationId: CHAT, turnId: live })}
        onStopTurn={onStopTurn}
        onStopSession={vi.fn<() => Promise<void>>().mockResolvedValue(undefined)}
        onStopFailed={vi.fn()}
      />,
    )

    // The turn rolls over with NO re-render — exactly what a ref write inside the stream reader
    // does.
    live = 't-second'
    fireEvent.click(screen.getByTestId('stop-turn'))

    await waitFor(() => expect(onStopTurn).toHaveBeenCalledWith(CHAT, 't-second'))
  })
})
