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

  it('stays quiet when publishing is not configured, instead of showing an error nobody can act on', async () => {
    getDeployment.mockRejectedValue(new ApiError('Deploying is not switched on.', 503))
    render(<DeployControl projectId="p1" />)

    await waitFor(() => expect(getDeployment).toHaveBeenCalled())
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
