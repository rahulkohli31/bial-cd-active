/**
 * DeployControl — the Publish card, the questionnaire, and what happens to each answer the
 * server can give.
 *
 * The load-bearing tests here are the ones about what a refusal ISN'T. A declaration that
 * flags sensitive data must reach the SERVER, and what comes back is not a failure: it is a
 * routed app, queued at the exact version examined. A client that blocked the call locally
 * would be enforcing a hand-synced copy of the weights; a client that painted the routed
 * outcome red would tell a citizen their app broke when the platform did precisely what the
 * button promised.
 *
 * The classification client is mocked to fail its ask, which is what the unmocked
 * environment already did (there is no fetch here) — pinned deliberately rather than left
 * to a network attempt, so the questions render on a decided state instead of a timing one.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import DeployControl from '../DeployControl'
import SubmitControl from '../SubmitControl'
import { ApiError } from '../../utils/apiError'
import * as deployApi from '../../utils/deployApi'
import * as approvalApi from '../../utils/approvalApi'
import type { ApprovalState, DeploymentView } from '../../utils/deployApi'

vi.mock('../../utils/deployApi', async () => {
  const actual = await vi.importActual<typeof deployApi>('../../utils/deployApi')
  return { ...actual, startDeploy: vi.fn(), getDeployment: vi.fn() }
})
vi.mock('../../utils/approvalApi', () => ({ withdrawSubmission: vi.fn() }))
vi.mock('../../utils/classificationApi', () => ({
  ensureClassificationReview: vi.fn(async () => {
    throw new ApiError('The automatic check is unavailable.', 503)
  }),
  getClassificationReview: vi.fn(async () => {
    throw new ApiError('The automatic check is unavailable.', 503)
  }),
  STORAGE_UNAVAILABLE: 'storage_unavailable',
}))

const startDeploy = vi.mocked(deployApi.startDeploy)
const getDeployment = vi.mocked(deployApi.getDeployment)
const withdrawSubmission = vi.mocked(approvalApi.withdrawSubmission)

const CATEGORY_KEYS = [
  'credentialsSecrets',
  'healthData',
  'personalInformation',
  'financialData',
  'confidentialBusinessData',
  'publicData',
] as const

const SHA = 'a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0'

const EMPTY: DeploymentView = {
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
  approval: null,
}

const approval = (over: Partial<ApprovalState> = {}): ApprovalState => ({
  status: 'draft',
  approvedCommitSha: null,
  approvalRoute: null,
  rejectionNote: null,
  submittedSha: null,
  submittedAt: null,
  ...over,
})

const STARTED = { outcome: 'started', deploymentId: 'd1', appId: 'a1', status: 'running' } as const

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
    startDeploy.mockResolvedValue(STARTED)
    render(<DeployControl projectId="p1" />)
    await openModal()
    declare() // all No — nothing weighted, so this one publishes unattended (R14)

    // The button is enabled: the client does not pre-judge.
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(screen.getByTestId('dc-confirm'))

    await waitFor(() => expect(startDeploy).toHaveBeenCalledTimes(1))
    expect(startDeploy).toHaveBeenCalledWith('p1', expect.objectContaining({ saveFirst: false }))
  })

  it("keeps the modal open on a refusal and shows the server's own words", async () => {
    // A LIVE refusal code, not the retired one: a disabled app is refused outright, and
    // the fix is not something the citizen can answer differently — so the copy has to be
    // readable where they are, with their answers still on screen.
    startDeploy.mockRejectedValue(
      new ApiError('An administrator has disabled this app.', 409, 'app_disabled'),
    )
    render(<DeployControl projectId="p1" />)
    await openModal()
    declare()
    fireEvent.click(screen.getByTestId('dc-confirm'))

    expect((await screen.findByRole('alert')).textContent).toContain('disabled this app')
    expect(screen.queryByTestId('data-classification-modal')).not.toBeNull()
  })

  it('closes the modal and starts polling once the server accepts', async () => {
    startDeploy.mockResolvedValue(STARTED)
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

    startDeploy.mockResolvedValueOnce({ ...STARTED, deploymentId: 'd2' })
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

// --- the retired terminal refusal ----------------------------------------------------

describe('the classification dead end is gone', () => {
  /**
   * A GUARD, not a deletion. Two tests used to live here pinning a 409
   * `classification_below_threshold` — a refusal that queued nothing and notified nobody,
   * and whose copy told the citizen to "ask an administrator" when no path performed that
   * review. U9 replaced it with the precedence ladder: the same declaration now ROUTES.
   * The code no longer exists on either side of the wire, and reintroducing it would
   * rebuild the dead end.
   */
  it('exports no CLASSIFICATION_REFUSED code to branch on', async () => {
    // The REAL module, not this file's partial mock: the guard is about what the client
    // actually ships, and a mock that happened to define the key would hide its return.
    const real = await vi.importActual<typeof deployApi>('../../utils/deployApi')
    expect('CLASSIFICATION_REFUSED' in real).toBe(false)
    expect(Object.keys(real).filter((k) => /classification_refused/i.test(k))).toEqual([])
  })

  it('does not special-case the retired code into a non-red presentation', async () => {
    // If the constant came back AND the special-casing with it, a row carrying the retired
    // code would be quietly softened. It is not recognised, so it renders as what it now
    // is: an unknown failure.
    getDeployment.mockResolvedValue({
      ...EMPTY,
      deploymentId: 'd1',
      status: 'failed',
      failureCode: 'classification_below_threshold',
      failureDetail: 'This app scored 55 and needs 0.',
      approval: approval(),
    })
    render(<DeployControl projectId="p1" />)

    expect((await screen.findByTestId('deploy-status')).textContent).toContain("Didn't publish")
    expect(screen.queryByTestId('deploy-routed')).toBeNull()
  })
})

// --- routing is an outcome, not a failure ---------------------------------------------

describe('a routed publish', () => {
  it('renders informationally after the POST resolves routed, and links to the status card', async () => {
    // The 200 shape (U9). The citizen pressed a button labelled "Send for review" and the
    // platform sent it for review — painting that red would tell them their app broke.
    startDeploy.mockResolvedValue({
      outcome: 'routed_for_review',
      appId: 'a1',
      submissionId: 'sub-1',
      commitSha: SHA,
      submittedAt: '2026-08-19T10:00:00Z',
      message: 'Sent to an administrator. You’ll be able to publish it once they approve.',
    })
    getDeployment
      .mockResolvedValueOnce({ ...EMPTY, appId: 'a1', approval: approval() })
      .mockResolvedValue({
        ...EMPTY,
        appId: 'a1',
        approval: approval({ status: 'pending', submittedSha: SHA, approvalRoute: 'self_publish' }),
      })

    render(<DeployControl projectId="p1" />)
    await openModal()
    declare(['credentialsSecrets'])
    fireEvent.change(screen.getByTestId('dc-notes'), { target: { value: 'Vendor API key.' } })
    fireEvent.click(screen.getByTestId('dc-confirm'))

    const routed = await screen.findByTestId('deploy-routed')
    expect(routed.textContent).toContain('once they approve')
    expect(routed.textContent).toContain(SHA.slice(0, 12))
    // Not a failure: no red badge, no alert, no failure detail.
    expect(screen.queryByTestId('deploy-failure')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByTestId('deploy-status')).toBeNull()
    expect(screen.getByTestId('deploy-routed-link').getAttribute('href')).toBe('#review-status')
  })

  it('renders a DRIFT-routed pipeline result the same way (ASM20)', async () => {
    // The pipeline stopped and queued a newer version. It arrives as the existing failed
    // terminal state carrying a distinct code, because adding a fourth status is a real
    // schema decision — so the presentation is chosen by a LOOKUP over routed codes, which
    // is the seam that stays extendable.
    //
    // Mutation receipt: remove `routed_for_review` from `ROUTED_FAILURE_CODES` in
    // deployApi.ts and this goes red twice over — a red "Didn't publish" badge appears and
    // the same sentence is repeated in an alert.
    getDeployment.mockResolvedValue({
      ...EMPTY,
      appId: 'a1',
      deploymentId: 'd9',
      status: 'failed',
      failureCode: 'routed_for_review',
      failureDetail: 'A newer version was saved, so that one went for review instead.',
      approval: approval({ status: 'pending', submittedSha: SHA }),
    })
    render(<DeployControl projectId="p1" />)

    const routed = await screen.findByTestId('deploy-routed')
    expect(routed.textContent).toContain('newer version was saved')
    expect(screen.queryByTestId('deploy-failure')).toBeNull()
    expect(screen.queryByTestId('deploy-status')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

// --- the two surfaces agree ------------------------------------------------------------

describe('the publish card and the review card read one state', () => {
  const pendingView: DeploymentView = {
    ...EMPTY,
    appId: 'app-1',
    approval: approval({
      status: 'pending',
      submittedSha: SHA,
      submittedAt: '2026-08-19T10:00:00Z',
      approvalRoute: 'self_publish',
    }),
  }

  it('both show the waiting state, and publishing cannot submit again (R15b)', async () => {
    getDeployment.mockResolvedValue(pendingView)
    render(
      <>
        <DeployControl projectId="p1" />
        <SubmitControl projectId="p1" />
      </>,
    )

    // The card says so, and closes the action.
    const button = (await screen.findByTestId('deploy-button')) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(button.textContent).toContain('Waiting for review')
    expect(screen.getByTestId('deploy-routed')).toBeTruthy()
    // And the status card beside it says the same thing, from the same read.
    expect(screen.getByTestId('submit-status').textContent).toContain('Waiting for review')
    expect(screen.getByTestId('withdraw-submission')).toBeTruthy()
  })

  it('announces the waiting state politely rather than only rendering it', async () => {
    getDeployment.mockResolvedValue(pendingView)
    render(<DeployControl projectId="p1" />)

    const region = await screen.findByTestId('deploy-announce')
    expect(region.getAttribute('role')).toBe('status')
    expect(region.getAttribute('aria-live')).toBe('polite')
    await waitFor(() => expect(region.textContent).toMatch(/with an administrator/i))
  })

  it('withdrawing returns BOTH surfaces to their pre-submission state', async () => {
    // The real cross-surface test: two mounts of the hook, one action, and no tab switch
    // in between. Before the same-tab nudge existed, the publish card would have gone on
    // saying "waiting for review" while the card two inches below it said "nothing
    // waiting" — the disagreement this unit exists to remove.
    //
    // Mutation receipt: delete the `announce()` call in the hook's `withdraw` and this goes
    // red on the deploy button still being disabled.
    let current: DeploymentView = pendingView
    getDeployment.mockImplementation(async () => current)
    withdrawSubmission.mockImplementation(async () => {
      current = { ...EMPTY, appId: 'app-1', approval: approval({ status: 'draft' }) }
      return { appId: 'app-1', status: 'draft' as const }
    })

    render(
      <>
        <DeployControl projectId="p1" />
        <SubmitControl projectId="p1" />
      </>,
    )
    await waitFor(() => expect(screen.getByTestId('withdraw-submission')).toBeTruthy())

    fireEvent.click(screen.getByTestId('withdraw-submission'))

    await waitFor(() => expect(withdrawSubmission).toHaveBeenCalledWith('app-1'))
    // The review card is back to nothing queued …
    await waitFor(() =>
      expect(screen.getByTestId('submit-status').textContent).toContain('Nothing waiting'),
    )
    expect(screen.queryByTestId('withdraw-submission')).toBeNull()
    // … and so is the publish card, without anyone having reloaded anything.
    await waitFor(() =>
      expect((screen.getByTestId('deploy-button') as HTMLButtonElement).disabled).toBe(false),
    )
    expect(screen.queryByTestId('deploy-routed')).toBeNull()
  })

  it('an approved app publishes — approval never publishes anything itself (R17)', async () => {
    getDeployment.mockResolvedValue({
      ...EMPTY,
      appId: 'app-1',
      approval: approval({
        status: 'approved',
        approvedCommitSha: SHA,
        approvalRoute: 'self_publish',
      }),
    })
    startDeploy.mockResolvedValue(STARTED)

    render(<DeployControl projectId="p1" />)

    const button = (await screen.findByTestId('deploy-button')) as HTMLButtonElement
    expect(button.disabled).toBe(false)
    expect(button.textContent).toContain('Publish')
    expect(button.textContent).not.toContain('Waiting')

    await openModal()
    declare(['credentialsSecrets'])
    fireEvent.change(screen.getByTestId('dc-notes'), { target: { value: 'Vendor API key.' } })
    fireEvent.click(screen.getByTestId('dc-confirm'))

    // It really goes: the approval is consumed by the citizen publishing it themselves.
    await waitFor(() => expect(startDeploy).toHaveBeenCalledTimes(1))
  })

  it('shows the rejection note inside the publish flow, above the questions', async () => {
    // A note that lives only on a card beside this dialog is a note a citizen can publish
    // straight past. Mutation receipt: stop passing `rejectionNote` from DeployControl and
    // this goes red.
    getDeployment.mockResolvedValue({
      ...EMPTY,
      appId: 'app-1',
      approval: approval({
        status: 'rejected',
        rejectionNote: 'The vendor key must not ship in the code.',
      }),
    })
    render(<DeployControl projectId="p1" />)
    await openModal()

    const note = screen.getByTestId('dc-rejection-note')
    expect(note.textContent).toContain('The vendor key must not ship in the code.')
    // BEFORE the questions in reading order, not merely present somewhere in the dialog.
    const firstQuestion = screen.getByTestId('dc-question-credentialsSecrets-yes')
    expect(note.compareDocumentPosition(firstQuestion) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    )
    // Labelled, so a screen reader reaches it as a titled region rather than loose prose.
    expect(note.getAttribute('aria-labelledby')).toBe('dc-rejection-heading')
    expect(screen.getByText('An administrator sent this back')).toBeTruthy()
  })

  it('shows no rejection note in the flow when nothing was rejected', async () => {
    getDeployment.mockResolvedValue({ ...EMPTY, appId: 'app-1', approval: approval() })
    render(<DeployControl projectId="p1" />)
    await openModal()

    expect(screen.queryByTestId('dc-rejection-note')).toBeNull()
    // Liveness beside the absence: the dialog really rendered its questions, so a modal
    // that threw could not false-green this.
    expect(screen.getByTestId('dc-question-credentialsSecrets-yes')).toBeTruthy()
  })

  it('claims nowhere that the platform team deploys an approved app', async () => {
    // R17a, across both cards and every lifecycle state: an approved app is published by
    // the citizen, so no rendered string may promise otherwise.
    for (const status of ['draft', 'pending', 'approved', 'rejected', 'disabled'] as const) {
      cleanup()
      getDeployment.mockResolvedValue({
        ...EMPTY,
        appId: 'app-1',
        approval: approval({ status, submittedSha: SHA, approvedCommitSha: SHA }),
      })
      render(
        <>
          <DeployControl projectId="p1" />
          <SubmitControl projectId="p1" />
        </>,
      )
      await waitFor(() => expect(screen.getByTestId('submit-status')).toBeTruthy())
      const text = document.body.textContent ?? ''
      expect(text).not.toMatch(/platform team/i)
      expect(text).not.toMatch(/deployed by/i)
      expect(text).not.toMatch(/ask an administrator/i)
    }
  })
})
