/**
 * The composer's half of the per-conversation guardrail: a warning, and emphatically not a gate.
 *
 * What is under test is the DISCIPLINE, not the sentence — the estimate and the wording are
 * `utils/__tests__/contextLimits.test.ts`'s. Three things have to be true here and each one has
 * already been got wrong somewhere in this codebase:
 *
 *   1. it is not a `disabled` (R45/R64 — the focus-loss defect this repo has recorded twice);
 *   2. it does not touch what the citizen has typed;
 *   3. it is announced, not shouted.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'

import { ComposerHarness } from './_composerHarness'
import Composer, { type ComposerProps } from '../Composer'

const WARNING =
  'This chat is getting long. Start a new chat soon to keep things quick — your app and everything you have built stays exactly as it is.'

afterEach(() => {
  cleanup()
  sessionStorage.clear()
})

function draw(over: Partial<ComposerProps> = {}) {
  const props: ComposerProps = {
    conversationId: 'chat-1',
    onSubmit: vi.fn().mockResolvedValue(undefined),
    isRunning: false,
    onUrgent: vi.fn(),
    ...over,
  }
  return { props, ...render(<ComposerHarness><Composer {...props} /></ComposerHarness>) }
}

describe('the getting-long warning', () => {
  it('is absent while there is nothing to warn about', () => {
    draw()
    expect(screen.queryByTestId('composer-context-warning')).toBeNull()
    // Liveness: the composer really rendered, so the absence above means absent rather than
    // "the component threw and queryBy found nothing".
    expect(screen.getByTestId('composer-input')).toBeTruthy()
  })

  it('is absent for an explicit null, not only for an omitted prop', () => {
    draw({ contextWarning: null })
    expect(screen.queryByTestId('composer-context-warning')).toBeNull()
    expect(screen.getByTestId('composer-input')).toBeTruthy()
  })

  it('shows the sentence it is handed, verbatim', () => {
    draw({ contextWarning: WARNING })
    expect(screen.getByTestId('composer-context-warning').textContent).toBe(WARNING)
  })

  it('★ disables NOTHING — the whole subtree stays interactive', () => {
    // R45/R64. A warning that blurred the textarea mid-sentence would be worse than no
    // warning, and this is the mechanical form of the rule.
    const { container } = draw({ contextWarning: WARNING })
    expect(container.querySelectorAll('[disabled]')).toHaveLength(0)
    // With something to send, Send is available — the warning is not a reason to withhold it.
    // (An EMPTY composer marks Send unavailable for its own reason, which is not this one.)
    fireEvent.change(screen.getByTestId('composer-input'), { target: { value: 'one more thing' } })
    expect(screen.getByTestId('composer-send').getAttribute('aria-disabled')).not.toBe('true')
    expect(container.querySelectorAll('[disabled]')).toHaveLength(0)
  })

  it('★ still SENDS past the threshold — the soft limit is advisory', () => {
    // The hard boundary is the server's. If the browser refused here, an administrator raising
    // someone's per-conversation max would have no effect until that user reloaded.
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    draw({ contextWarning: WARNING, onSubmit })
    fireEvent.change(screen.getByTestId('composer-input'), { target: { value: 'one more thing' } })
    fireEvent.click(screen.getByTestId('composer-send'))
    expect(onSubmit).toHaveBeenCalledTimes(1)
  })

  it('★ never destroys what the citizen has typed', () => {
    // The warning can appear mid-sentence, on the render after any keystroke.
    const { rerender, props } = draw({ contextWarning: null })
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement
    fireEvent.change(input, { target: { value: 'half a thought' } })

    rerender(<ComposerHarness><Composer {...props} contextWarning={WARNING} /></ComposerHarness>)

    expect((screen.getByTestId('composer-input') as HTMLTextAreaElement).value).toBe(
      'half a thought',
    )
    expect(screen.getByTestId('composer-context-warning')).toBeTruthy()
  })

  it('is announced politely, not assertively', () => {
    // `role="status"` is polite: it reaches a screen reader at the next pause instead of
    // interrupting someone mid-word. Nothing about a soft threshold is urgent.
    draw({ contextWarning: WARNING })
    expect(screen.getByTestId('composer-context-warning').getAttribute('role')).toBe('status')
  })

  it('is independent of the per-message character cap', () => {
    // Two unrelated limits that both contain the word "length" — the exact conflation that let
    // the conversation guardrail be recorded as redistributed into `composerCap` and lost.
    draw({ contextWarning: WARNING })
    fireEvent.change(screen.getByTestId('composer-input'), { target: { value: 'x'.repeat(10_001) } })

    // The cap's own counter and refusal appear, and the context warning is still there beside
    // them: neither suppresses the other.
    expect(screen.getByTestId('composer-counter')).toBeTruthy()
    expect(screen.getByTestId('composer-context-warning')).toBeTruthy()
  })
})
