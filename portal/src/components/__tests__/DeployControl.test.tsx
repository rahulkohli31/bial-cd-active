/**
 * DeployControl — the Publish button, the questionnaire, and what happens to each answer
 * the server can give.
 *
 * The load-bearing test here is the refusal: a low-scoring declaration must reach the
 * SERVER and come back refused, with the modal still open and the answers still on screen.
 * A client that quietly blocked the call instead would be enforcing a hand-synced copy of
 * the weights, and would silently diverge from the real gate the moment either side moved.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import DeployControl from '../DeployControl'
import { ApiError } from '../../utils/apiError'
import * as deployApi from '../../utils/deployApi'
import type { DeploymentView } from '../../utils/deployApi'

vi.mock('../../utils/deployApi', async () => {
  const actual = await vi.importActual<typeof deployApi>('../../utils/deployApi')
  return { ...actual, startDeploy: vi.fn(), getDeployment: vi.fn() }
})

const startDeploy = vi.mocked(deployApi.startDeploy)
const getDeployment = vi.mocked(deployApi.getDeployment)

const CATEGORY_KEYS = [
  'credentialsSecrets',
  'healthData',
  'personalInformation',
  'financialData',
  'confidentialBusinessData',
  'publicData',
] as const

const EMPTY: deployApi.DeploymentView = {
  deploymentId: null,
  appId: null,
  status: null,
  step: null,
  url: null,
  headSha: null,
  failureCode: null,
  failureDetail: null,
  startedAt: null,
  finishedAt: null,
  unpublishedAt: null,
}

/** Answer every question, then opt a few into Yes. */
function declare(yes: readonly (typeof CATEGORY_KEYS)[number][] = []): void {
  for (const key of CATEGORY_KEYS) {
    fireEvent.click(screen.getByTestId(`dc-question-${key}-${yes.includes(key) ? 'yes' : 'no'}`))
  }
}

async function openModal(): Promise<void> {
  fireEvent.click(await screen.findByTestId('deploy-button'))
  await screen.findByTestId('data-classification-modal')
}

beforeEach(() => {
  vi.clearAllMocks()
  getDeployment.mockResolvedValue(EMPTY)
})
afterEach(cleanup)

describe('DeployControl', () => {
  it('reads as "never published" without erroring — that is a normal state, not a failure', async () => {
    render(<DeployControl projectId="p1" />)
    expect((await screen.findByTestId('deploy-button')).textContent).toContain('Publish')
    expect(screen.queryByTestId('deploy-status')).toBeNull()
  })

  it('sends a low-scoring declaration to the server rather than blocking it locally', async () => {
    startDeploy.mockRejectedValue(
      new ApiError('This app scored 0 and needs 50.', 409, deployApi.CLASSIFICATION_REFUSED),
    )
    render(<DeployControl projectId="p1" />)
    await openModal()
    declare() // all No — a score of 0, far below the threshold

    // The button is enabled: the client does not pre-judge.
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(screen.getByTestId('dc-confirm'))

    await waitFor(() => expect(startDeploy).toHaveBeenCalledTimes(1))
    expect(startDeploy).toHaveBeenCalledWith(
      'p1',
      expect.objectContaining({ saveFirst: false }),
    )
  })

  it("keeps the modal open on a refusal and shows the server's own words", async () => {
    startDeploy.mockRejectedValue(
      new ApiError(
        'This app scored 0 on the data-classification questions and needs 50.',
        409,
        deployApi.CLASSIFICATION_REFUSED,
      ),
    )
    render(<DeployControl projectId="p1" />)
    await openModal()
    declare()
    fireEvent.click(screen.getByTestId('dc-confirm'))

    // Still open, with the answers intact — the fix is to change an answer, so the refusal
    // is only actionable while they are on screen.
    expect((await screen.findByRole('alert')).textContent).toContain('needs 50')
    expect(screen.queryByTestId('data-classification-modal')).not.toBeNull()
  })

  it('closes the modal and starts polling once the server accepts', async () => {
    startDeploy.mockResolvedValue({ deploymentId: 'd1', appId: 'a1', status: 'running' })
    getDeployment
      .mockResolvedValueOnce(EMPTY)
      .mockResolvedValue({ ...EMPTY, deploymentId: 'd1', status: 'running', step: 'building' })

    render(<DeployControl projectId="p1" />)
    await openModal()
    declare(['credentialsSecrets', 'confidentialBusinessData'])
    fireEvent.change(screen.getByTestId('dc-notes'), { target: { value: 'Vendor API key.' } })
    fireEvent.click(screen.getByTestId('dc-confirm'))

    await waitFor(() => expect(screen.queryByTestId('data-classification-modal')).toBeNull())
    expect((await screen.findByTestId('deploy-status')).textContent).toContain('Building')
    // A deploy in flight must not be startable a second time.
    expect((screen.getByTestId('deploy-button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('offers "Save and publish" when the workspace is ahead of the last save', async () => {
    startDeploy.mockRejectedValueOnce(
      new ApiError('You have changes that are not saved yet.', 409, deployApi.UNSAVED_CHANGES),
    )
    render(<DeployControl projectId="p1" />)
    await openModal()
    declare()
    fireEvent.click(screen.getByTestId('dc-confirm'))

    // This one is a question, not a failure, so it leaves the modal and offers the choice.
    expect((await screen.findByTestId('deploy-unsaved')).textContent).toContain('not saved yet')
    expect(screen.queryByTestId('data-classification-modal')).toBeNull()

    startDeploy.mockResolvedValueOnce({ deploymentId: 'd2', appId: 'a1', status: 'running' })
    fireEvent.click(screen.getByTestId('deploy-save-first'))

    // The retry resends the SAME answers rather than re-asking the questionnaire.
    await waitFor(() => expect(startDeploy).toHaveBeenCalledTimes(2))
    expect(startDeploy).toHaveBeenLastCalledWith('p1', expect.objectContaining({ saveFirst: true }))
    expect(startDeploy.mock.calls[1][1].answers).toEqual(startDeploy.mock.calls[0][1].answers)
  })

  it('shows the address once it is live, and offers to publish again', async () => {
    getDeployment.mockResolvedValue({
      ...EMPTY,
      deploymentId: 'd1',
      status: 'succeeded',
      step: 'live',
      url: 'https://pub-abc.example.azurecontainerapps.io/',
    })
    render(<DeployControl projectId="p1" />)

    const link = await screen.findByTestId('deploy-url')
    expect(link.getAttribute('href')).toBe('https://pub-abc.example.azurecontainerapps.io/')
    expect(screen.getByTestId('deploy-button').textContent).toContain('Publish again')
  })

  it('stops linking the address once an administrator takes the app down', async () => {
    // The row still reads `succeeded` — that is how the deploy ENDED, and unpublishing does
    // not rewrite it (#113). Branching on the status alone therefore renders a "Live" badge
    // over a URL that 404s, which is the bug this pins: only `unpublishedAt` separates the
    // two states. Mutation receipt: revert either guard in DeployControl to
    // `deployment?.status === 'succeeded'` and this goes red on the link or the badge.
    getDeployment.mockResolvedValue({
      ...EMPTY,
      deploymentId: 'd1',
      status: 'succeeded',
      step: 'live',
      url: 'https://pub-abc.example.azurecontainerapps.io/',
      unpublishedAt: '2026-08-12T10:00:00Z',
    })
    render(<DeployControl projectId="p1" />)

    expect(await screen.findByTestId('deploy-taken-down')).toBeTruthy()
    expect(screen.queryByTestId('deploy-url')).toBeNull()
    expect(screen.getByTestId('deploy-status').textContent).toContain('Taken down')
    // Republishing is still offered — the takedown is an operator convenience, not a lock.
    expect(screen.getByTestId('deploy-button').textContent).toContain('Publish again')
  })

  it('says an administrator acted even when the taken-down run had FAILED', async () => {
    // The route stamps whichever deployment is NEWEST, whatever its status
    // (`store.latest_for_app`) — the pipeline creates the container at step 5 and only then
    // awaits the revision, so a run that settles FAILED at step 6 leaves `pub-<app_id>`
    // serving, and that is the case the kill-switch most obviously exists for. Gating
    // `takenDown` on `succeeded` stamped the row server-side and then told the citizen only
    // that their deploy failed, with nothing on screen saying an administrator acted.
    // Mutation receipt: restore `deployment?.status === 'succeeded' &&` on `takenDown` and
    // this goes red on the explanation; drop the `!takenDown` guard on the "Didn't publish"
    // badge and it goes red instead on a duplicate `deploy-status` node.
    getDeployment.mockResolvedValue({
      ...EMPTY,
      deploymentId: 'd2',
      status: 'failed',
      failureDetail: 'The app did not become ready in time.',
      unpublishedAt: '2026-08-12T10:00:00Z',
    })
    render(<DeployControl projectId="p1" />)

    expect(await screen.findByTestId('deploy-taken-down')).toBeTruthy()
    // Exactly ONE status badge, and it is the later admin-initiated fact, not "Didn't publish".
    expect(screen.getByTestId('deploy-status').textContent).toContain('Taken down')
    // The failure reason still stands — it is why the run ended that way, and stays actionable.
    expect(screen.getByText(/did not become ready/i)).toBeTruthy()
  })

  it('shows ONE badge when a still-running deploy carries a takedown stamp', async () => {
    // `takenDown` is status-agnostic by design, so `running` can wear a stamp too — the same
    // collision the "Didn't publish" badge already guards against, and the same consequence:
    // two `deploy-status` nodes is a duplicate-testid bug and two contradictory answers.
    //
    // Mutation receipt: drop `!takenDown` from the `running` badge and this goes red with
    // "Found multiple elements by: [data-testid='deploy-status']".
    getDeployment.mockResolvedValue({
      ...EMPTY,
      deploymentId: 'd3',
      status: 'running',
      step: 'provision',
      unpublishedAt: '2026-08-12T10:00:00Z',
    })
    render(<DeployControl projectId="p1" />)

    expect(await screen.findByTestId('deploy-taken-down')).toBeTruthy()
    // `getByTestId` throws on more than one match, which IS the assertion.
    expect(screen.getByTestId('deploy-status').textContent).toContain('Taken down')
  })

  it('reports a failed deploy with the detail the citizen can act on', async () => {
    getDeployment.mockResolvedValue({
      ...EMPTY,
      deploymentId: 'd1',
      status: 'failed',
      failureCode: 'acr_build_failed',
      failureDetail: 'Type error: Property "naem" does not exist.',
    })
    render(<DeployControl projectId="p1" />)

    expect((await screen.findByTestId('deploy-failure')).textContent).toContain('Property "naem"')
    expect(screen.getByTestId('deploy-status').textContent).toContain("Didn't publish")
  })

  /**
   * Drive the clock from BEFORE the render, or the interval is created on the real timer and
   * advancing a fake one moves nothing — which reads as "it stopped polling" no matter what
   * the code does. The first version of this test passed for exactly that reason.
   */
  async function callsOverTime(view: DeploymentView, ms: number): Promise<[number, number]> {
    getDeployment.mockResolvedValue(view)
    vi.useFakeTimers()
    try {
      render(<DeployControl projectId="p1" />)
      await vi.advanceTimersByTimeAsync(0) // let the mount's fetch resolve
      const settled = getDeployment.mock.calls.length
      await vi.advanceTimersByTimeAsync(ms)
      return [settled, getDeployment.mock.calls.length]
    } finally {
      vi.useRealTimers()
    }
  }

  it('stops polling once the deploy is finished', async () => {
    // Regression: the poll used to run for the life of the mount, so a FINISHED deploy kept
    // hitting the API every five seconds for as long as the page stayed open — 132 requests
    // on one idle project page. A deploy is the only thing that changes on its own, so a
    // timer that outlives it is pure traffic.
    const [settled, after] = await callsOverTime(
      { ...EMPTY, deploymentId: 'd1', status: 'succeeded', url: 'https://pub-abc.example/' },
      60_000,
    )
    expect(settled).toBeGreaterThan(0) // it DID load once — otherwise this proves nothing
    expect(after).toBe(settled)
  })

  it('does poll while one is actually in flight', async () => {
    const [settled, after] = await callsOverTime(
      { ...EMPTY, deploymentId: 'd1', status: 'running', step: 'building' },
      16_000,
    )
    expect(after).toBeGreaterThan(settled)
  })

  it('stays quiet when publishing is not configured, instead of showing an error nobody can act on', async () => {
    getDeployment.mockRejectedValue(new ApiError('Deploying is not switched on.', 503))
    render(<DeployControl projectId="p1" />)

    await waitFor(() => expect(getDeployment).toHaveBeenCalled())
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
