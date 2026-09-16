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
import { render, screen, cleanup, fireEvent, waitFor, act } from '@testing-library/react'

const h = vi.hoisted(() => ({
  restartApp: vi.fn(),
  takeAppDown: vi.fn(),
  refresh: vi.fn(),
  state: 'live_current' as string,
  failureCode: null as string | null,
  hasServingRow: true,
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
    actions?: (ctx: {
      state: string
      failureCode: string | null
      hasServingRow: boolean
      refresh: () => Promise<void>
    }) => React.ReactNode
  }) => (
    <div data-testid="status-panel-stub" data-project={projectId}>
      {actions?.({
        state: h.state,
        failureCode: h.failureCode,
        hasServingRow: h.hasServingRow,
        refresh: h.refresh,
      })}
    </div>
  ),
}))

import ProductionTab from '../ProductionTab'
import { ApiError } from '../../../utils/apiError'

const mount = (over: Partial<React.ComponentProps<typeof ProductionTab>> = {}) =>
  render(<ProductionTab projectId="p1" appName="Ramp Ops" {...over} />)

beforeEach(() => {
  vi.clearAllMocks()
  h.state = 'live_current'
  h.failureCode = null
  h.hasServingRow = true
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

  it('runs production-restart and re-reads through the panel', async () => {
    mount()
    fireEvent.click(screen.getByTestId('production-restart'))
    await waitFor(() => expect(h.restartApp).toHaveBeenCalledWith('p1'))
    await waitFor(() => expect(h.refresh).toHaveBeenCalled())
  })

  it('runs production-takedown once confirmed, and re-reads through the panel', async () => {
    // Restart interrupts the application for a moment; a take-down ends it for everyone until
    // somebody publishes again. Only one of the two is asked about, and it is this one.
    mount()
    fireEvent.click(screen.getByTestId('production-takedown'))
    fireEvent.click(screen.getByTestId('take-down-confirm'))
    await waitFor(() => expect(h.takeAppDown).toHaveBeenCalledWith('p1'))
    await waitFor(() => expect(h.refresh).toHaveBeenCalled())
  })

  it('★ asks before it ends the application, and names it — the same question the list asks', async () => {
    // TWO DOORS TO ONE ACT. The row menu on the list asks; this button did the same thing on a
    // single press. A question that only one door puts is a question people learn to ignore.
    mount()
    fireEvent.click(screen.getByTestId('production-takedown'))

    expect(screen.getByText(/Take “Ramp Ops” out of production\?/)).toBeTruthy()
    expect(h.takeAppDown).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('take-down-cancel'))
    await waitFor(() => expect(screen.queryByTestId('take-down-confirm')).toBeNull())
    expect(h.takeAppDown).not.toHaveBeenCalled()
    // Liveness: the control is still there to press again.
    expect(screen.getByTestId('production-takedown')).toBeTruthy()
  })

  it('tells the list when an operation settles, so its chip stops being stale', async () => {
    const onSettled = vi.fn()
    mount({ onSettled })
    fireEvent.click(screen.getByTestId('production-takedown'))
    fireEvent.click(screen.getByTestId('take-down-confirm'))
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

  it('★ …including two presses inside ONE tick, which is what a ref buys over state', async () => {
    // THE HALF THE TEST ABOVE CANNOT SEE. It waits for the button to go disabled before pressing
    // again, so a guard held in React state passes it — state is what disabled the button. A
    // key-repeat on a focused button, or any tight double-click, lands both presses against the
    // SAME render, where a state read through that render's closure still says "nothing pending"
    // and two container operations start on one revision.
    h.restartApp.mockReturnValue(new Promise(() => {}))
    mount()
    const restart = screen.getByTestId('production-restart')

    // BOTH EVENTS IN ONE BATCH. React 18 flushes a discrete click synchronously, so two separate
    // `fireEvent` calls DO re-render in between and a state guard survives them — which is why
    // the test above cannot see this. Wrapping both in one `act` is the faithful model of two
    // events arriving in the same task, and there the second handler still reads the first
    // render's `pending`.
    act(() => {
      fireEvent.click(restart)
      fireEvent.click(restart)
    })

    expect(h.restartApp).toHaveBeenCalledTimes(1)
    // Liveness beside the count: the first press really did start, so this is a guard rather than
    // a render that never wired the handler at all.
    await waitFor(() => expect(restart.getAttribute('aria-disabled')).toBe('true'))
  })

  it('★ and neither does the OTHER control — one operation at a time, not one per button', () => {
    // Restarting and taking down at once is two container operations racing over one revision,
    // and the server would refuse the second. The screen must not offer the race.
    h.restartApp.mockReturnValue(new Promise(() => {}))
    mount()
    fireEvent.click(screen.getByTestId('production-restart'))
    fireEvent.click(screen.getByTestId('production-takedown'))
    fireEvent.click(screen.getByTestId('take-down-confirm'))
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
  ])('offers neither action on a %s application with nothing serving', (state) => {
    h.state = state
    h.hasServingRow = false
    mount()
    // Absence PAIRED WITH LIVENESS: the tab really rendered and really mounted the panel that
    // says where the application stands, so this is the controls being withheld rather than a
    // blank tab.
    expect(screen.getByTestId('status-panel-stub')).toBeTruthy()
    expect(screen.queryByTestId('production-restart')).toBeNull()
    expect(screen.queryByTestId('production-takedown')).toBeNull()
  })

  it('★ offers Take down on a live application whose NEXT version is in review', () => {
    // SUBMITTING FOR REVIEW DOES NOT STOP THE VERSION ALREADY SERVING. The lifecycle arm simply
    // outranks the deployment row when the state is named, so this reads `in_review` — and one
    // predicate for both controls hid the only lever an owner had over a container that is up.
    // The server never refused it: the route makes no status check at all, and had gone as far as
    // authoring the sentence about the queued version being untouched.
    h.state = 'in_review'
    h.hasServingRow = true
    mount()

    expect(screen.getByTestId('production-takedown')).toBeTruthy()
    // …and NOT Restart, which is refused here — the two are not accepted on the same grounds.
    expect(screen.queryByTestId('production-restart')).toBeNull()
    // The server's own reassurance, said where the decision is made rather than after it.
    expect(screen.getByTestId('production-tab').textContent).toContain(
      'The version waiting for review is untouched',
    )
  })

  it('★ …but not on a switched-off one, where an administrator already stopped it', () => {
    // A kill-switch severs the application's database too. Offering an owner a control over a
    // container an administrator has stopped is offering a refusal.
    h.state = 'switched_off'
    h.hasServingRow = true
    mount()

    expect(screen.getByTestId('status-panel-stub')).toBeTruthy()
    expect(screen.queryByTestId('production-takedown')).toBeNull()
    expect(screen.queryByTestId('production-restart')).toBeNull()
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

/**
 * ★ A RESTART THAT FAILED ON AN APPLICATION THAT IS STILL SERVING.
 *
 * `deployments` is append-only and a restart claims a row of its own, so a restart that fails
 * leaves a failed row newer than the attempt that published the container still running. The
 * server reports the state as LIVE — which is true, the previous revision never stopped — and
 * carries the attempt's ending alongside it. Saying nothing would leave an owner who pressed
 * Restart four minutes ago with no idea whether it ever finished.
 */
describe('a restart that did not finish', () => {
  it.each(['restart_failed', 'restart_not_ready'])('says so under %s, beside a live status', (code) => {
    h.failureCode = code
    mount()
    const notice = screen.getByTestId('production-restart-failed')
    expect(notice.textContent).toMatch(/last restart did not finish/i)
    // THE HALF THAT ANSWERS THE FEAR: nothing was lost, and the app never went down.
    expect(notice.textContent).toMatch(/still running/i)
  })

  it('★ leaves both controls offered — the application is serving, so both still apply', () => {
    // The defect this replaced withheld Take down as well as Restart, because it reported the
    // application as not live. An owner whose restart timed out could not take it down at all.
    h.failureCode = 'restart_not_ready'
    mount()
    expect(screen.getByTestId('production-restart')).toBeTruthy()
    expect(screen.getByTestId('production-takedown')).toBeTruthy()
  })

  it('★ says nothing about a restart when the failure was a PUBLISH', () => {
    // The paired negative. A build that never came up is a different event with a different
    // remedy, and the state word carries that one on its own.
    h.failureCode = 'build_failed'
    mount()
    expect(screen.getByTestId('production-restart')).toBeTruthy()
    expect(screen.queryByTestId('production-restart-failed')).toBeNull()
  })

  it('says nothing on an application that is not serving', () => {
    // Where nothing is running, "the version that was already running is still running" is
    // false — and the state word is the whole answer.
    h.failureCode = 'restart_failed'
    h.state = 'taken_offline'
    mount()
    expect(screen.getByTestId('status-panel-stub')).toBeTruthy()
    expect(screen.queryByTestId('production-restart-failed')).toBeNull()
  })

  it('says nothing when the last attempt ended cleanly', () => {
    mount()
    expect(screen.getByTestId('production-restart')).toBeTruthy()
    expect(screen.queryByTestId('production-restart-failed')).toBeNull()
  })
})
