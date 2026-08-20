/**
 * SubmitControl — the review STATUS card. It reports; it does not submit.
 *
 * Two things are pinned here that a future edit would most plausibly break. First, the
 * card has no way into the queue: R15a allows exactly one, and it is the publish request.
 * Second, it reads its lifecycle off the shared deploy status (`useDeployment`), not off
 * its own `/apps/:id/status` fetch — the old card read that once on mount and never again,
 * so a citizen who watched their app route into the queue was left being told it was still
 * a draft.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react'
import SubmitControl from '../SubmitControl'
import type { ApprovalState, DeploymentView } from '../../utils/deployApi'
import type { UseDeployment } from '../../hooks/useDeployment'

const h = vi.hoisted(() => ({ useDeployment: vi.fn() }))
vi.mock('../../hooks/useDeployment', () => h)

const SHA = 'a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0'
// Deliberately distinct from SHA in its FIRST twelve characters — the card renders a short
// sha, so two values differing only past the truncation would let the wrong one pass.
const APPROVED_SHA = '9f8e7d6c5b4a39281706f5e4d3c2b1a098765432'

const EMPTY: DeploymentView = {
  deploymentId: null,
  appId: 'app-1',
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

const withdraw = vi.fn(async () => {})

function wire(over: Partial<UseDeployment> = {}): void {
  const state = over.approval ?? null
  h.useDeployment.mockReturnValue({
    deployment: { ...EMPTY, approval: state },
    approval: state,
    running: false,
    waitingForReview: state?.status === 'pending',
    loadError: null,
    unsaved: null,
    saving: false,
    routed: null,
    onConfirm: vi.fn(),
    saveAndPublish: vi.fn(),
    dismissUnsaved: vi.fn(),
    withdraw,
    withdrawing: false,
    withdrawError: null,
    ...over,
  } satisfies UseDeployment)
}

afterEach(cleanup)
beforeEach(() => {
  vi.clearAllMocks()
})

describe('SubmitControl is a status card, not a way into the queue', () => {
  it('renders no submit control of any kind', () => {
    // The RETIRED VERB, guarded rather than merely un-tested. There is one route into the
    // review queue (R15a) and it is the publish request; a button here would be the second,
    // differently-worded one, and it would post a submission with no declaration attached.
    wire({ approval: approval() })
    render(<SubmitControl projectId="p1" />)

    expect(screen.queryByTestId('submit-for-review')).toBeNull()
    const card = screen.getByTestId('submit-control')
    expect(card.querySelectorAll('button')).toHaveLength(0)
    expect(card.textContent).not.toMatch(/submit/i)
  })

  it('claims nowhere that the platform team deploys an approved app', () => {
    // The behaviour is retired (R17: the citizen publishes the approved version
    // themselves), so the copy that promised it goes with it. Mutation receipt: put the
    // sentence back in `statusMeta` and this goes red.
    for (const status of ['draft', 'pending', 'approved', 'rejected', 'disabled'] as const) {
      cleanup()
      wire({ approval: approval({ status }) })
      render(<SubmitControl projectId="p1" />)
      const text = screen.getByTestId('submit-control').textContent ?? ''
      expect(text).not.toMatch(/platform team/i)
      expect(text).not.toMatch(/deployed by/i)
    }
  })
})

describe('SubmitControl reports the approval lifecycle', () => {
  it('shows the pending badge, the submitted version and a withdraw action', async () => {
    wire({
      approval: approval({
        status: 'pending',
        submittedSha: SHA,
        submittedAt: '2026-08-19T10:00:00Z',
        approvalRoute: 'self_publish',
      }),
    })
    render(<SubmitControl projectId="p1" />)

    expect(screen.getByTestId('submit-status').textContent).toContain('Waiting for review')
    expect(screen.getByTestId('commit-sha').textContent).toContain(SHA.slice(0, 12))
    expect(screen.getByTestId('submitted-at')).toBeTruthy()
    expect(screen.getByTestId('withdraw-submission')).toBeTruthy()
  })

  it('announces each transition through a polite live region', () => {
    wire({ approval: approval({ status: 'pending', submittedSha: SHA }) })
    render(<SubmitControl projectId="p1" />)

    const region = screen.getByTestId('submit-announce')
    expect(region.getAttribute('role')).toBe('status')
    expect(region.getAttribute('aria-live')).toBe('polite')
    expect(region.textContent).toMatch(/with an administrator/i)

    cleanup()
    wire({ approval: approval({ status: 'approved', approvedCommitSha: APPROVED_SHA }) })
    render(<SubmitControl projectId="p1" />)
    expect(screen.getByTestId('submit-announce').textContent).toMatch(/approved this version/i)
  })

  it('names the APPROVED commit once approved, not the submitted one', () => {
    // Showing the submitted sha after approval would name a version the approval may not
    // cover — a later Save produces exactly that situation (R18).
    wire({
      approval: approval({
        status: 'approved',
        submittedSha: SHA,
        approvedCommitSha: APPROVED_SHA,
        approvalRoute: 'self_publish',
      }),
    })
    render(<SubmitControl projectId="p1" />)

    expect(screen.getByTestId('commit-sha').textContent).toContain(APPROVED_SHA.slice(0, 12))
    expect(screen.getByTestId('commit-sha').textContent).not.toContain(SHA.slice(0, 12))
    expect(screen.getByTestId('submit-control').textContent).toContain('Version approved')
  })

  it('shows the rejection note, and offers no withdrawal for a rejected app', () => {
    wire({ approval: approval({ status: 'rejected', rejectionNote: 'Remove the sample data.' }) })
    render(<SubmitControl projectId="p1" />)

    expect(screen.getByTestId('rejection-note').textContent).toContain('Remove the sample data.')
    expect(screen.getByTestId('submit-status').textContent).toContain('Changes requested')
    // Nothing is in the queue to withdraw — P6 is about a PENDING submission only.
    expect(screen.queryByTestId('withdraw-submission')).toBeNull()
  })

  it('says nothing is waiting when the app has never routed', () => {
    wire({ approval: approval() })
    render(<SubmitControl projectId="p1" />)

    expect(screen.getByTestId('submit-status').textContent).toContain('Nothing waiting')
    expect(screen.queryByTestId('commit-sha')).toBeNull()
    expect(screen.queryByTestId('withdraw-submission')).toBeNull()
  })

  it('renders a load failure as an alert, not a broken card', () => {
    wire({ approval: null, loadError: 'Could not read the publish status.' })
    render(<SubmitControl projectId="p1" />)

    expect(screen.getByRole('alert').textContent).toContain('Could not read the publish status.')
    // A liveness assertion beside the absence one: the card itself still rendered, so a
    // component that threw could not false-green this.
    expect(screen.getByTestId('submit-control')).toBeTruthy()
    expect(screen.queryByTestId('submit-announce')).toBeNull()
  })
})

describe('withdrawing a pending submission (P6)', () => {
  const pending = approval({ status: 'pending', submittedSha: SHA })

  it('calls the hook’s withdraw and is labelled for a screen reader', async () => {
    wire({ approval: pending })
    render(<SubmitControl projectId="p1" />)

    const button = screen.getByTestId('withdraw-submission')
    expect(button.getAttribute('aria-label')).toBe('Withdraw this submission from review')
    expect(button.tagName).toBe('BUTTON') // reachable by keyboard alone, not a div
    fireEvent.click(button)

    await waitFor(() => expect(withdraw).toHaveBeenCalledTimes(1))
  })

  it('returns to the pre-submission state once the hook reports draft', () => {
    // The withdrawal's effect arrives the same way every other lifecycle change does —
    // through the shared status read — which is what keeps this card and the Publish card
    // beside it from disagreeing about whether anything is queued.
    wire({ approval: approval({ status: 'draft' }) })
    render(<SubmitControl projectId="p1" />)

    expect(screen.getByTestId('submit-status').textContent).toContain('Nothing waiting')
    expect(screen.queryByTestId('withdraw-submission')).toBeNull()
    expect(screen.queryByTestId('submitted-at')).toBeNull()
  })

  it('disables the action while the withdrawal is in flight', () => {
    wire({ approval: pending, withdrawing: true })
    render(<SubmitControl projectId="p1" />)

    expect((screen.getByTestId('withdraw-submission') as HTMLButtonElement).disabled).toBe(true)
  })

  it('renders a refused withdrawal as the server’s own words', () => {
    wire({
      approval: pending,
      withdrawError: 'Only a submission that is waiting for review can be withdrawn.',
    })
    render(<SubmitControl projectId="p1" />)

    expect(screen.getByRole('alert').textContent).toContain('waiting for review can be withdrawn')
  })
})

/*
 * P5 — an approval only authorises self-publishing when it came through THIS flow.
 * The cutover backfilled every pre-existing approval to the `runbook` lineage, and the
 * publish gate's override requires `self_publish`; telling a runbook-lineage owner they
 * can publish it themselves is copy asserting behaviour the platform does not have.
 */
describe('what an approval is claimed to authorise (P5)', () => {
  it('offers self-publishing only on a self-publish-lineage approval', () => {
    wire({
      approval: approval({
        status: 'approved',
        approvedCommitSha: APPROVED_SHA,
        approvalRoute: 'self_publish',
      }),
    })
    render(<SubmitControl projectId="p1" />)

    expect(screen.getByTestId('submit-announce').textContent).toMatch(/publish it yourself/i)
  })

  it.each([['runbook' as const], [null]])(
    'does not promise self-publishing on a %s lineage — it says what actually happens',
    (route) => {
      wire({
        approval: approval({
          status: 'approved',
          approvedCommitSha: APPROVED_SHA,
          approvalRoute: route,
        }),
      })
      render(<SubmitControl projectId="p1" />)

      const said = screen.getByTestId('submit-announce').textContent ?? ''
      expect(said).not.toMatch(/publish it yourself/i)
      // and it must still tell them what to do, not merely withhold the claim
      expect(said).toMatch(/sent for approval|press publish/i)
    },
  )
})
