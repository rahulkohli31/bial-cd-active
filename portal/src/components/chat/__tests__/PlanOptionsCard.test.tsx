/** PlanOptionsCard: actionable only when newest+unresolved, resolved states render settled,
 * and "Keep refining" resolves through the API.
 *
 * The re-arm case is now an INERTNESS GUARD rather than a deleted test (L8): the card had a
 * third settled state, `build_failed`, that put the failure's name in an alert and turned the
 * buttons back on. The guard is what fails if someone re-adds it. */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PlanOptionsCard } from '../PlanOptionsCard'
import * as api from '../../../utils/turnStreamApi'
import type { PlanOptionsItem } from '../../../utils/turnStreamApi'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function item(overrides: Partial<PlanOptionsItem> = {}): PlanOptionsItem {
  return {
    type: 'plan_options',
    seq: 5,
    toolCallId: 'opt-1',
    state: 'pending',
    ...overrides,
  }
}

describe('PlanOptionsCard', () => {
  it('renders both buttons for a pending card and resolves refine via the API', async () => {
    const resolved = vi
      .spyOn(api, 'resolvePlanOptions')
      .mockResolvedValue({ state: 'refine', alreadyResolved: false })
    const onRefined = vi.fn()
    render(
      <PlanOptionsCard
        conversationId="c1"
        item={item()}
        onBuildIt={() => undefined}
        onRefined={onRefined}
      />
    )
    fireEvent.click(screen.getByRole('button', { name: 'Keep refining' }))
    await waitFor(() => expect(onRefined).toHaveBeenCalledWith('opt-1'))
    expect(resolved).toHaveBeenCalledWith('c1', 'opt-1')
  })

  it('routes Build it through the caller and surfaces a failure without resolving', async () => {
    const onBuildIt = vi.fn().mockRejectedValue(new Error('lock held'))
    render(<PlanOptionsCard conversationId="c1" item={item()} onBuildIt={onBuildIt} />)
    fireEvent.click(screen.getByRole('button', { name: 'Build it' }))
    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toContain('could not be started')
    )
    // The buttons stay armed — never resolved-with-no-build.
    const buildButton = screen.getByRole('button', { name: 'Build it' }) as HTMLButtonElement
    expect(buildButton.disabled).toBe(false)
  })

  it('renders settled states without buttons', () => {
    const { rerender } = render(
      <PlanOptionsCard conversationId="c1" item={item({ state: 'refine' })} />
    )
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.getByText('You kept refining this plan.')).toBeTruthy()
    rerender(<PlanOptionsCard conversationId="c1" item={item({ state: 'build' })} />)
    expect(screen.getByText('Build started from this plan.')).toBeTruthy()
  })

  it('has no third settled state and no re-arm arm', () => {
    // AN INERTNESS GUARD over the retired `build_failed` state (L8). A press that fails now
    // records NOTHING — the offer is answered only once the new chat's turn is running — so a
    // card can no longer be burned, and a mechanism for un-burning one would be a second, staler
    // account of a failure the caller has already reported in full.
    //
    // Asserted over the SOURCE, not over a render, because that is the only form the claim can
    // take once the state is unrepresentable: `item({ state: 'build_failed' })` does not
    // type-check any more, so a test that renders it cannot be written to fail.
    const source = PlanOptionsCard.toString()
    expect(source).not.toContain('build_failed')
    expect(source).not.toContain('FAILURE_COPY')
    // …and the settled states that DO exist still render settled, with nothing actionable.
    const { rerender } = render(
      <PlanOptionsCard conversationId="c1" item={item({ state: 'build' })} onBuildIt={() => undefined} />
    )
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    rerender(
      <PlanOptionsCard conversationId="c1" item={item({ state: 'refine' })} onBuildIt={() => undefined} />
    )
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('an expired card is informational only', () => {
    render(<PlanOptionsCard conversationId="c1" item={item()} expired />)
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.getByText('A newer plan supersedes these options.')).toBeTruthy()
  })
})
