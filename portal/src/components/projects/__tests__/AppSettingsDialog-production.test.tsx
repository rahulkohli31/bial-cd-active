/**
 * The Production tab — the two things an owner may do to a live application.
 *
 * WHAT IT DOES NOT OWN, and what this file therefore does not test: the status pill, the
 * provenance rows, the published address and Send for review. Those are `AppStatusPanel`'s, with
 * a suite of their own, and the tab now reads the state THROUGH the panel rather than asking the
 * server a second time — so the panel is stubbed here and the state is simply an input.
 *
 * THE COPY IS THE FEATURE, more than in most panels. To a citizen who did not write the code,
 * "restart" is the appliance remedy for "my app is broken" — and it is not one: it recycles the
 * revision already serving, so a fault in the application's own logic survives it exactly.
 * Getting that wrong leaves an owner pressing a button that cannot help them, with nothing on
 * screen telling them so, which is why the sentences are asserted rather than the buttons alone.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'

const h = vi.hoisted(() => ({
  restartApp: vi.fn(),
  takeAppDown: vi.fn(),
  refresh: vi.fn(),
  state: 'live_current' as string,
}))

vi.mock('../../../utils/deployApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  restartApp: h.restartApp,
  takeAppDown: h.takeAppDown,
}))

/**
 * The panel, reduced to the contract the tab depends on: it holds the read, and it hands the
 * state and a way to ask again down to whatever the tab renders at its foot.
 */
vi.mock('../AppStatusPanel', () => ({
  default: ({
    projectId,
    actions,
  }: {
    projectId: string
    actions?: (ctx: { state: string; refresh: () => Promise<void> }) => React.ReactNode
  }) => (
    <div data-testid="status-panel-stub" data-project={projectId}>
      {actions?.({ state: h.state, refresh: h.refresh })}
    </div>
  ),
}))

import ProductionTab from '../ProductionTab'
import { ApiError } from '../../../utils/apiError'

const mount = (over: Partial<React.ComponentProps<typeof ProductionTab>> = {}) =>
  render(<ProductionTab projectId="p1" {...over} />)

beforeEach(() => {
  vi.clearAllMocks()
  h.state = 'live_current'
  h.restartApp.mockResolvedValue({ deploymentId: 'd2' })
  h.takeAppDown.mockResolvedValue({ message: 'done' })
  h.refresh.mockResolvedValue(undefined)
})
afterEach(() => cleanup())

describe('a live application', () => {
  it('★ asks the server nothing of its own — the panel holds the one read', () => {
    // Two components in one tab each polling the same endpoint is how a screen comes to show two
    // answers to one question, and it is exactly what this tab used to do.
    mount()
    expect(screen.getByTestId('status-panel-stub').getAttribute('data-project')).toBe('p1')
    expect(screen.getByTestId('production-restart')).toBeTruthy()
    expect(screen.getByTestId('production-takedown')).toBeTruthy()
  })

  it('★ says restart runs the SAME version, so nobody presses it expecting a repair', () => {
    mount()
    const line = screen.getByTestId('production-restart').parentElement?.textContent ?? ''
    expect(line).toMatch(/same version again/i)
    // And it must not promise to pick up work saved since — that is the publish gate's job.
    expect(line).toMatch(/does not pick up anything you have saved since/i)
  })

  it('★ take down says what is KEPT, because the fear it answers is losing work', () => {
    mount()
    const line = screen.getByTestId('production-takedown').parentElement?.textContent ?? ''
    expect(line).toMatch(/chats, its data and its files are all kept/i)
    expect(line).toMatch(/publish again/i)
  })

  it.each([
    ['production-restart', 'restartApp'],
    ['production-takedown', 'takeAppDown'],
  ] as const)('runs %s and re-reads through the panel', async (testid, call) => {
    mount()
    fireEvent.click(screen.getByTestId(testid))
    await waitFor(() => expect(h[call]).toHaveBeenCalledWith('p1'))
    await waitFor(() => expect(h.refresh).toHaveBeenCalled())
  })

  it('tells the list when an operation settles, so its chip stops being stale', async () => {
    const onSettled = vi.fn()
    mount({ onSettled })
    fireEvent.click(screen.getByTestId('production-takedown'))
    await waitFor(() => expect(onSettled).toHaveBeenCalled())
  })

  it('★ a second press while one is in flight starts no second operation', async () => {
    // The server treats a second restart as a no-op on the same operation, which is exactly why
    // the interface must not accept the press: a click that silently does nothing is the failure
    // mode this whole plan avoids elsewhere.
    h.restartApp.mockReturnValue(new Promise(() => {}))
    mount()
    const restart = screen.getByTestId('production-restart')
    fireEvent.click(restart)
    await waitFor(() => expect(restart.getAttribute('aria-disabled')).toBe('true'))
    fireEvent.click(restart)
    fireEvent.click(restart)
    expect(h.restartApp).toHaveBeenCalledTimes(1)
  })

  it('★ and neither does the OTHER control — one operation at a time, not one per button', () => {
    // Restarting and taking down at once is two container operations racing over one revision,
    // and the server would refuse the second. The screen must not offer the race.
    h.restartApp.mockReturnValue(new Promise(() => {}))
    mount()
    fireEvent.click(screen.getByTestId('production-restart'))
    fireEvent.click(screen.getByTestId('production-takedown'))
    expect(h.takeAppDown).not.toHaveBeenCalled()
  })

  it('shows the operation is running rather than leaving the control looking idle', async () => {
    h.restartApp.mockReturnValue(new Promise(() => {}))
    mount()
    fireEvent.click(screen.getByTestId('production-restart'))
    await waitFor(() =>
      expect(screen.getByTestId('production-restart').textContent).toMatch(/Restarting/),
    )
  })
})

describe("refusals are the server's own words", () => {
  it.each([
    ['taken_offline', 'This app has been taken offline. Publish again to put it back.'],
    ['app_disabled', 'This app has been disabled by an administrator and cannot be restarted.'],
    ['deploy_in_flight', 'Something is already running for this app.'],
  ])('surfaces the stated reason for %s rather than a generic failure', async (code, message) => {
    h.restartApp.mockRejectedValue(new ApiError(message, 409, code))
    mount()
    fireEvent.click(screen.getByTestId('production-restart'))
    expect((await screen.findByTestId('production-refusal')).textContent).toContain(message)
  })

  it('lets the owner try again after a refusal — it is not a dead end', async () => {
    h.restartApp.mockRejectedValueOnce(new ApiError('busy', 409, 'deploy_in_flight'))
    mount()
    const restart = screen.getByTestId('production-restart')
    fireEvent.click(restart)
    await screen.findByTestId('production-refusal')
    expect(restart.getAttribute('aria-disabled')).toBe('false')
    fireEvent.click(restart)
    await waitFor(() => expect(h.restartApp).toHaveBeenCalledTimes(2))
  })

  it('★ still tells the list to re-read after a refusal, so the screen lands on the truth', async () => {
    // A refusal usually means the state moved under the owner — an administrator took it offline,
    // a deploy started. Leaving the surfaces showing what they showed before the press would
    // leave a citizen reading a reason that contradicts the status directly above it.
    h.restartApp.mockRejectedValue(new ApiError('taken offline', 409, 'taken_offline'))
    const onSettled = vi.fn()
    mount({ onSettled })
    fireEvent.click(screen.getByTestId('production-restart'))
    await screen.findByTestId('production-refusal')
    await waitFor(() => expect(onSettled).toHaveBeenCalled())
  })
})

describe('no control is offered where the endpoint would refuse it', () => {
  it.each([
    'draft',
    'in_review',
    'taken_offline',
    'nothing_built',
    'did_not_start',
    'switched_off',
    'starting_up',
  ])('offers neither action on a %s application', (state) => {
    h.state = state
    mount()
    // Absence PAIRED WITH LIVENESS: the tab really rendered and really mounted the panel that
    // says where the application stands, so this is the controls being withheld rather than a
    // blank tab.
    expect(screen.getByTestId('status-panel-stub')).toBeTruthy()
    expect(screen.queryByTestId('production-restart')).toBeNull()
    expect(screen.queryByTestId('production-takedown')).toBeNull()
  })

  it.each(['live_current', 'live_newer_work', 'live_drift_unknown'])(
    'offers both on a %s application',
    (state) => {
      // The paired positive: withholding on ten states is only meaningful if three states get it.
      h.state = state
      mount()
      expect(screen.getByTestId('production-restart')).toBeTruthy()
      expect(screen.getByTestId('production-takedown')).toBeTruthy()
    },
  )
})
