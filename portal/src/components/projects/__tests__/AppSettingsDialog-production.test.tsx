/**
 * The Production tab, and the two things it lets an owner do.
 *
 * THE COPY IS THE FEATURE HERE, more than in most panels. To a citizen who did not write the
 * code, "restart" is the appliance remedy for "my app is broken" — and it is not one: it recycles
 * the revision already serving, so a fault in the application's own logic survives it exactly.
 * Getting that wrong leaves an owner pressing a button that cannot help them, with nothing on
 * screen telling them so, which is why the sentences are asserted rather than the buttons alone.
 *
 * A FAILED RESTART IS NOT "DIDN'T START". The deployment read reports `did_not_start` for it —
 * the right word for a first deploy and the wrong one on an application whose previous version is
 * still serving — so the panel reads `failureCode` instead. A test that accepted the state word
 * would pin the confusion.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'

const h = vi.hoisted(() => ({
  getDeployment: vi.fn(),
  restartApp: vi.fn(),
  takeAppDown: vi.fn(),
}))

vi.mock('../../../utils/deployApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getDeployment: h.getDeployment,
  restartApp: h.restartApp,
  takeAppDown: h.takeAppDown,
}))

import ProductionTab from '../ProductionTab'
import type { DeploymentView, PublishState } from '../../../utils/deployApi'
import { ApiError } from '../../../utils/apiError'

const view = (publishState: PublishState, over: Partial<DeploymentView> = {}): DeploymentView => ({
  deploymentId: 'd1',
  appId: 'app-1',
  status: 'succeeded',
  step: null,
  url: null,
  headSha: null,
  failureCode: null,
  failureDetail: null,
  startedAt: null,
  finishedAt: null,
  unpublishedAt: null,
  approval: {
    status: 'draft',
    approvedCommitSha: null,
    approvedAt: null,
    approvalRoute: null,
    rejectionNote: null,
    submittedSha: null,
    submittedAt: null,
  },
  publishState,
  savedHead: null,
  savedAt: null,
  savedState: null,
  ...over,
})

const mount = (over: Partial<React.ComponentProps<typeof ProductionTab>> = {}) =>
  render(<ProductionTab projectId="p1" {...over} />)

beforeEach(() => {
  vi.clearAllMocks()
  h.getDeployment.mockResolvedValue(view('live_current', { url: 'https://app.example/' }))
  h.restartApp.mockResolvedValue({ deploymentId: 'd2' })
  h.takeAppDown.mockResolvedValue({ message: 'done' })
})
afterEach(() => cleanup())

describe('a live application', () => {
  it('states where it stands, its address, and offers both actions', async () => {
    mount()
    expect((await screen.findByTestId('production-state')).textContent).toBe('Live')
    expect(screen.getByTestId('production-url').getAttribute('href')).toBe('https://app.example/')
    expect(screen.getByTestId('production-restart')).toBeTruthy()
    expect(screen.getByTestId('production-takedown')).toBeTruthy()
  })

  it('★ says restart runs the SAME version, so nobody presses it expecting a repair', async () => {
    mount()
    const restart = await screen.findByTestId('production-restart')
    const line = restart.parentElement?.textContent ?? ''
    expect(line).toMatch(/same version again/i)
    // And it must not promise to pick up work saved since — that is the publish gate's job.
    expect(line).toMatch(/does not pick up anything you have saved since/i)
  })

  it('★ take down says what is KEPT, because the fear it answers is losing work', async () => {
    mount()
    const takedown = await screen.findByTestId('production-takedown')
    const line = takedown.parentElement?.textContent ?? ''
    expect(line).toMatch(/chats, its data and its files are all kept/i)
    expect(line).toMatch(/publish again/i)
  })

  it('restarts through the endpoint and re-reads the state', async () => {
    mount()
    fireEvent.click(await screen.findByTestId('production-restart'))
    await waitFor(() => expect(h.restartApp).toHaveBeenCalledWith('p1'))
    await waitFor(() => expect(h.getDeployment).toHaveBeenCalledTimes(2))
  })

  it('takes it down through the endpoint and tells the list to refresh', async () => {
    const onSettled = vi.fn()
    mount({ onSettled })
    fireEvent.click(await screen.findByTestId('production-takedown'))
    await waitFor(() => expect(h.takeAppDown).toHaveBeenCalledWith('p1'))
    await waitFor(() => expect(onSettled).toHaveBeenCalled())
  })

  it('★ a second press while one is in flight starts no second operation', async () => {
    // The server treats a second restart as a no-op on the same operation, which is exactly why
    // the interface must not accept the press: a click that silently does nothing is the failure
    // mode this whole plan avoids elsewhere.
    h.restartApp.mockReturnValue(new Promise(() => {}))
    mount()
    const restart = await screen.findByTestId('production-restart')
    fireEvent.click(restart)
    await waitFor(() => expect(restart.getAttribute('aria-disabled')).toBe('true'))
    fireEvent.click(restart)
    fireEvent.click(restart)
    expect(h.restartApp).toHaveBeenCalledTimes(1)
  })

  it('shows the operation is running rather than leaving the control looking idle', async () => {
    h.restartApp.mockReturnValue(new Promise(() => {}))
    mount()
    fireEvent.click(await screen.findByTestId('production-restart'))
    await waitFor(() =>
      expect(screen.getByTestId('production-restart').textContent).toMatch(/Restarting/),
    )
  })
})

describe('refusals are the server\'s own words', () => {
  it.each([
    ['taken_offline', 'This app has been taken offline. Publish again to put it back.'],
    ['app_disabled', 'This app has been disabled by an administrator and cannot be restarted.'],
    ['deploy_in_flight', 'Something is already running for this app.'],
  ])('surfaces the stated reason for %s rather than a generic failure', async (code, message) => {
    h.restartApp.mockRejectedValue(new ApiError(message, 409, code))
    mount()
    fireEvent.click(await screen.findByTestId('production-restart'))
    expect((await screen.findByTestId('production-refusal')).textContent).toContain(message)
  })

  it('lets the owner try again after a refusal — it is not a dead end', async () => {
    h.restartApp.mockRejectedValueOnce(new ApiError('busy', 409, 'deploy_in_flight'))
    mount()
    const restart = await screen.findByTestId('production-restart')
    fireEvent.click(restart)
    await screen.findByTestId('production-refusal')
    expect(restart.getAttribute('aria-disabled')).toBe('false')
    fireEvent.click(restart)
    await waitFor(() => expect(h.restartApp).toHaveBeenCalledTimes(2))
  })
})

describe('a failed restart is not a failed first deploy', () => {
  it.each(['restart_failed', 'restart_not_ready'])(
    '★ reads %s as "could not restart" rather than "Didn\'t start"',
    async (code) => {
      // Mutation receipt: read `publishState` instead of `failureCode` and this goes red — the
      // state word is `did_not_start`, which is true of a first deploy and false here.
      h.getDeployment.mockResolvedValue(view('did_not_start', { failureCode: code, status: 'failed' }))
      mount()
      expect((await screen.findByTestId('production-state')).textContent).toBe('Could not restart')
    },
  )

  it('points at the remedy that actually works', async () => {
    h.getDeployment.mockResolvedValue(
      view('did_not_start', { failureCode: 'restart_failed', status: 'failed' }),
    )
    mount()
    await screen.findByTestId('production-state')
    // A restart runs the same version, so if it keeps failing the fault is in the application.
    // Telling them to press it again would be telling them to wait for nothing.
    expect(screen.getByTestId('production-tab').textContent).toMatch(/send it for review/i)
  })

  it.each([
    ['in_review', 'In review'],
    ['switched_off', 'Switched off'],
  ] as const)(
    '★ does not shout over %s, which is the more current fact',
    async (state, label) => {
      // `failureCode` outlives the attempt that wrote it, and the server ranks a pending
      // submission and an administrator's lockout ABOVE the deployment row. A panel that read the
      // code alone would answer "Could not restart" to an owner whose app is now with a reviewer.
      h.getDeployment.mockResolvedValue(
        view(state, { failureCode: 'restart_failed', status: 'failed' }),
      )
      mount()
      expect((await screen.findByTestId('production-state')).textContent).toBe(label)
    },
  )

  it('leaves an ordinary failed first deploy saying what it always said', async () => {
    // The paired negative: the rename above must not swallow the state it is distinguishing from.
    h.getDeployment.mockResolvedValue(view('did_not_start', { failureCode: 'build_failed', status: 'failed' }))
    mount()
    expect((await screen.findByTestId('production-state')).textContent).toBe("Didn't start")
  })
})

describe('no control is offered where the endpoint would refuse it', () => {
  it.each(['draft', 'in_review', 'taken_offline', 'nothing_built'] as const)(
    'offers neither action on a %s application',
    async (state) => {
      h.getDeployment.mockResolvedValue(view(state))
      mount()
      // Absence PAIRED WITH LIVENESS: the panel really rendered and really said where the
      // application stands, so this is the controls being withheld rather than a blank tab.
      await screen.findByTestId('production-state')
      expect(screen.queryByTestId('production-restart')).toBeNull()
      expect(screen.queryByTestId('production-takedown')).toBeNull()
    },
  )

  it('says plainly where a never-deployed application stands', async () => {
    h.getDeployment.mockResolvedValue(view('nothing_built'))
    mount()
    expect((await screen.findByTestId('production-state')).textContent).toBe('Nothing built yet')
  })

  it('links no address for an application that is not serving one', async () => {
    h.getDeployment.mockResolvedValue(view('taken_offline', { url: 'https://gone.example/' }))
    mount()
    await screen.findByTestId('production-state')
    // A dead address a citizen can click is indistinguishable to them from an app that broke.
    expect(screen.queryByTestId('production-url')).toBeNull()
  })
})

describe('the read itself can fail', () => {
  it('says so and offers a retry that actually re-fetches', async () => {
    h.getDeployment.mockRejectedValueOnce(new ApiError('Publishing is not configured.', 503, 'x'))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(h.getDeployment).toHaveBeenCalledTimes(2))
  })
})
