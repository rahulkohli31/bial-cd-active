/**
 * PublishButton — the toolbar's "so where is it?" affordance.
 *
 * One behaviour is pinned here, and it is the one #113 added: the address is shown on
 * `isLive`, never on `status === 'succeeded'`. An admin-unpublished deployment KEEPS that
 * status — the status describes how the attempt ended, not whether the app is up — so a
 * status-only test would leave a dead link in the toolbar with nothing explaining it.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import PublishButton from '../PublishButton'
import type { DeploymentView } from '../../utils/deployApi'

const h = vi.hoisted(() => ({ useDeployment: vi.fn() }))
vi.mock('../../hooks/useDeployment', () => h)

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
}

function wire(deployment: DeploymentView): void {
  h.useDeployment.mockReturnValue({
    deployment,
    running: false,
    unsaved: null,
    saving: false,
    onConfirm: vi.fn(),
    saveAndPublish: vi.fn(),
    dismissUnsaved: vi.fn(),
  })
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
