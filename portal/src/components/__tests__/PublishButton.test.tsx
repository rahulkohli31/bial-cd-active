/**
 * PublishButton — the toolbar's "so where is it?" affordance, and now also its "so what
 * happened to it?" one.
 *
 * Two behaviours are pinned here. The one #113 added: the address is shown on `isLive`,
 * never on `status === 'succeeded'` — an admin-unpublished deployment KEEPS that status,
 * because the status describes how the attempt ended, not whether the app is up, so a
 * status-only test would leave a dead link in the toolbar with nothing explaining it.
 *
 * And the one U12 added: this button reports the review queue. It is mounted with a
 * project id and NO app id, so before the approval state started riding on the deploy
 * status response there was no path by which pending could reach it at all — a citizen in
 * the builder could press Publish over and over against a version already waiting for an
 * administrator, and only the project page would ever say so.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import PublishButton from '../PublishButton'
import type { ApprovalState, DeploymentView, RoutedForReview } from '../../utils/deployApi'
import type { UseDeployment } from '../../hooks/useDeployment'

const h = vi.hoisted(() => ({ useDeployment: vi.fn() }))
vi.mock('../../hooks/useDeployment', () => h)

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
  publishState: "draft",
}

const approval = (over: Partial<ApprovalState> = {}): ApprovalState => ({
  status: 'draft',
  approvedCommitSha: null,
  approvedAt: null,
  approvalRoute: null,
  rejectionNote: null,
  submittedSha: null,
  submittedAt: null,
  ...over,
})

function wire(deployment: DeploymentView, over: Partial<UseDeployment> = {}): void {
  h.useDeployment.mockReturnValue({
    deployment,
    approval: deployment.approval,
    running: deployment.status === 'running',
    waitingForReview: deployment.approval?.status === 'pending',
    loadError: null,
    refresh: vi.fn(),
    unsaved: null,
    saving: false,
    routed: null,
    onConfirm: vi.fn(),
    saveAndPublish: vi.fn(),
    dismissUnsaved: vi.fn(),
    withdraw: vi.fn(),
    withdrawing: false,
    withdrawError: null,
    ...over,
  } satisfies UseDeployment)
}

afterEach(cleanup)

describe('PublishButton shows the address only while the app is really up', () => {
  const LIVE = { ...EMPTY, status: 'succeeded' as const, url: 'https://pub-abc.example/' }

  it('links to the app for a live deployment', () => {
    wire(LIVE)
    render(<PublishButton projectId="p1" />)

    expect(screen.getByTestId('publish-url').getAttribute('href')).toBe(
      'https://pub-abc.example/',
    )
  })

  it('hides the address once an administrator has taken the app down', () => {
    // The status is STILL `succeeded` — that is the whole point. Only `unpublishedAt` says
    // the container is gone.
    //
    // Mutation receipt: change `isLive(deployment)` back to
    // `deployment?.status === 'succeeded'` in PublishButton and this goes red with the dead
    // link still rendered.
    wire({ ...LIVE, unpublishedAt: '2026-08-12T10:00:00Z' })
    render(<PublishButton projectId="p1" />)

    expect(screen.queryByTestId('publish-url')).toBeNull()
  })
})

describe('the toolbar reports the review queue, exactly as the project page does', () => {
  const PENDING = {
    ...EMPTY,
    appId: 'app-1',
    approval: approval({ status: 'pending', submittedSha: SHA, approvalRoute: 'self_publish' }),
  }

  it('says a version is waiting and refuses to submit another (R15b)', () => {
    // Mutation receipt: drop `waitingForReview` from the `disabled` expression and this
    // goes red on an enabled button that would post a second submission over the first.
    wire(PENDING)
    render(<PublishButton projectId="p1" />)

    const button = screen.getByTestId('publish-button') as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(button.textContent).toContain('In review')
    expect(screen.getByTestId('publish-review-pending').textContent).toContain(
      'Waiting for review',
    )
  })

  it('points at the one card that holds the rest of that state', () => {
    // The toolbar has no room for the version, the note, or the withdrawal — so it links
    // to the surface that does, rather than growing a second, drifting copy of them.
    wire(PENDING)
    render(<PublishButton projectId="p1" />)

    expect(screen.getByTestId('publish-review-link').getAttribute('href')).toBe(
      '/projects/p1#review-status',
    )
  })

  it('announces the waiting state through a polite live region', () => {
    // It arrives while the citizen is watching the preview, not this button.
    wire(PENDING)
    render(<PublishButton projectId="p1" />)

    const region = screen.getByTestId('publish-review-pending')
    expect(region.getAttribute('role')).toBe('status')
    expect(region.getAttribute('aria-live')).toBe('polite')
  })

  it('publishes normally again once nothing is queued', () => {
    wire({ ...EMPTY, appId: 'app-1', approval: approval({ status: 'draft' }) })
    render(<PublishButton projectId="p1" />)

    const button = screen.getByTestId('publish-button') as HTMLButtonElement
    expect(button.disabled).toBe(false)
    expect(button.textContent).toContain('Publish')
    expect(screen.queryByTestId('publish-review-pending')).toBeNull()
  })

  it('lets an approved app publish — approval never publishes anything itself (R17)', () => {
    wire({
      ...EMPTY,
      appId: 'app-1',
      approval: approval({
        status: 'approved',
        approvedCommitSha: SHA,
        approvalRoute: 'self_publish',
      }),
    })
    render(<PublishButton projectId="p1" />)

    const button = screen.getByTestId('publish-button') as HTMLButtonElement
    expect(button.disabled).toBe(false)
    expect(button.textContent).toContain('Publish')
    expect(screen.queryByTestId('publish-review-pending')).toBeNull()
  })
})

describe('a routed publish is an outcome, not a failure', () => {
  const routed: RoutedForReview = {
    outcome: 'routed_for_review',
    appId: 'app-1',
    submissionId: 'sub-1',
    commitSha: SHA,
    submittedAt: '2026-08-19T10:00:00Z',
    message: 'Sent to an administrator for review.',
  }

  it('renders the informational state after the POST resolves routed', () => {
    wire({ ...EMPTY, appId: 'app-1', approval: approval({ status: 'pending', submittedSha: SHA }) }, {
      routed,
    })
    render(<PublishButton projectId="p1" />)

    expect(screen.getByTestId('publish-review-pending')).toBeTruthy()
    // Nothing red, and nothing claiming a failure: `role="alert"` is the failure treatment.
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('renders a DRIFT-routed pipeline result as waiting, never as a failed publish', () => {
    // ASM20: the drift path arrives as the existing failed terminal state carrying a
    // distinct code, because adding a fourth status is a real schema decision. This
    // surface does NOT inspect that code — it has no failure presentation to suppress —
    // and it does not need to: the same status read that reports the failed row reports
    // the app as pending, which is what the strip and the closed button branch on.
    //
    // The code-level discrimination lives where failures are actually rendered, in
    // `DeployControl.test.tsx`; asserting it here as well would be a test that stayed
    // green with the lookup emptied, which is worse than no test.
    wire({
      ...EMPTY,
      appId: 'app-1',
      status: 'failed',
      failureCode: 'routed_for_review',
      failureDetail: 'A newer version was saved, so that one was sent for review instead.',
      approval: approval({ status: 'pending', submittedSha: SHA }),
    })
    render(<PublishButton projectId="p1" />)

    expect(screen.getByTestId('publish-review-pending')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect((screen.getByTestId('publish-button') as HTMLButtonElement).textContent).toContain(
      'In review',
    )
  })

  it('leaves a REAL pipeline failure alone — no waiting strip, and still publishable', () => {
    // A failed build with nothing queued must not inherit the informational presentation,
    // and the button must stay usable so a citizen can fix and retry from where they are.
    wire({
      ...EMPTY,
      appId: 'app-1',
      status: 'failed',
      failureCode: 'acr_build_failed',
      failureDetail: 'Type error.',
      approval: approval({ status: 'draft' }),
    })
    render(<PublishButton projectId="p1" />)

    expect(screen.queryByTestId('publish-review-pending')).toBeNull()
    expect((screen.getByTestId('publish-button') as HTMLButtonElement).disabled).toBe(false)
  })
})
