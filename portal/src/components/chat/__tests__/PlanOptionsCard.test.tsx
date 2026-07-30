/** PlanOptionsCard (U11): actionable only when newest+unresolved, resolved states render
 * settled, build_failed re-arms, and "Keep refining" resolves through the API. */

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
    mode: 'plan',
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

  it('re-arms the buttons on build_failed with the failure named', () => {
    render(
      <PlanOptionsCard
        conversationId="c1"
        item={item({ state: 'build_failed', reason: 'lock_held' })}
        onBuildIt={() => undefined}
      />
    )
    expect(screen.getByRole('alert').textContent).toContain('Another build is already running')
    const armed = screen.getByRole('button', { name: 'Build it' }) as HTMLButtonElement
    expect(armed.disabled).toBe(false)
  })

  it('an expired card is informational only', () => {
    render(<PlanOptionsCard conversationId="c1" item={item()} expired />)
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.getByText('A newer plan supersedes these options.')).toBeTruthy()
  })
})
