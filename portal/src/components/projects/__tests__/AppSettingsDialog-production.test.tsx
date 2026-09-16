/**
 * The Production tab, and the two things it lets an owner do.
 *
 * WHAT IT DOES NOT OWN: the status pill, the provenance rows, the published address and Send for
 * review. Those are `AppStatusPanel`'s, mounted here, with a suite of their own — and the naming
 * of a failed restart moved further still, into `publishPresentation.ts`, so the chip and the
 * panel cannot describe one failure in two ways. What is left in this file is the two controls
 * and the rules about when they may be pressed.
 *
 * THE COPY IS THE FEATURE HERE, more than in most panels. To a citizen who did not write the
 * code, "restart" is the appliance remedy for "my app is broken" — and it is not one: it recycles
 * the revision already serving, so a fault in the application's own logic survives it exactly.
 * Getting that wrong leaves an owner pressing a button that cannot help them, with nothing on
 * screen telling them so, which is why the sentences are asserted rather than the buttons alone.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'

const h = vi.hoisted(() => ({
  getDeployment: vi.fn(),
  restartApp: vi.fn(),
  takeAppDown: vi.fn(),
  usePublishState: vi.fn(),
}))

// The status panel mounted inside the tab has its own suite and its own read. Stubbed to a bare
// shell so this file's assertions stay about the two controls the tab itself owns.
vi.mock('../AppStatusPanel', () => ({
  default: ({ projectId }: { projectId: string }) => (
    <div data-testid="status-panel-stub" data-project={projectId} />
  ),
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
  it('mounts the status panel for this application, and offers both actions', async () => {
    mount()
    expect(await screen.findByTestId('production-restart')).toBeTruthy()
    expect(screen.getByTestId('production-takedown')).toBeTruthy()
    // ★ THE STATUS IS SAID ONCE. This tab does not restate the pill, the rows or the address —
    // it mounts the one component that owns them, on the same application.
    expect(screen.getByTestId('status-panel-stub').getAttribute('data-project')).toBe('p1')
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

describe('no control is offered where the endpoint would refuse it', () => {
  it.each(['draft', 'in_review', 'taken_offline', 'nothing_built', 'did_not_start'] as const)(
    'offers neither action on a %s application',
    async (state) => {
      h.getDeployment.mockResolvedValue(view(state))
      mount()
      // Absence PAIRED WITH LIVENESS: the tab really rendered and really mounted the panel that
      // says where the application stands, so this is the controls being withheld rather than a
      // blank tab.
      expect(await screen.findByTestId('status-panel-stub')).toBeTruthy()
      expect(screen.queryByTestId('production-restart')).toBeNull()
      expect(screen.queryByTestId('production-takedown')).toBeNull()
    },
  )
})

describe('the read itself can fail', () => {
  it('says so and offers a retry that actually re-fetches', async () => {
    h.getDeployment.mockRejectedValueOnce(new ApiError('Publishing is not configured.', 503, 'x'))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(h.getDeployment).toHaveBeenCalledTimes(2))
  })
})
