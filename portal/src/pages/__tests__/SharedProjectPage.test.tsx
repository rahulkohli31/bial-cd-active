/**
 * `SharedProjectPage` — the restricted `/shared/:projectId` view. `projectApi` and
 * `buildSessionApi` are mocked at the module boundary; the point under test is the page's own
 * branching, not the network.
 *
 * The one behavior these tests exist to pin (#198 R10's second sentence): a project whose
 * owner has never saved must show a disabled, explained state WITHOUT ever calling Launch —
 * not a reactive failure after a wasted round trip.
 */
import { it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import SharedProjectPage from '../SharedProjectPage'
import type { Project } from '../../utils/projectApi'
import { ApiError } from '../../utils/apiError'

const h = vi.hoisted(() => ({
  getProject: vi.fn(),
  launchSharedPreview: vi.fn(),
  refreshSharedPreview: vi.fn(),
  giveUpSharedView: vi.fn(),
  handOverWorkspace: vi.fn(),
}))

vi.mock('../../utils/projectApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/projectApi')>()),
  getProject: h.getProject,
}))
vi.mock('../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/buildSessionApi')>()),
  launchSharedPreview: h.launchSharedPreview,
  refreshSharedPreview: h.refreshSharedPreview,
  giveUpSharedView: h.giveUpSharedView,
  handOverWorkspace: h.handOverWorkspace,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))

/** The exact shape `asReclaimBlocked` reads off a thrown error — a real `sandbox_reclaim_
 *  blocked` 409, not a scripted mock of the narrower. */
function reclaimBlockedError(details: {
  projectId: string
  projectName: string
  dirty: boolean | null
  building: boolean
  agentWorking: boolean
  isSharedView: boolean
}): ApiError {
  return new ApiError('blocked', 409, 'sandbox_reclaim_blocked', details)
}

const makeProject = (over: Partial<Project> = {}): Project => ({
  id: 'p1',
  name: 'Visitor Log',
  description: null,
  appId: 'app-1',
  appStatus: null,
  hasRelaunchableSnapshot: null,
  hasSavedSnapshot: true,
  isServing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'shared',
  ...over,
})

function renderAt(projectId = 'p1') {
  return render(
    <MemoryRouter initialEntries={[`/shared/${projectId}`]}>
      <Routes>
        <Route path="/shared/:projectId" element={<SharedProjectPage />} />
        <Route path="/projects" element={<div data-testid="projects-page" />} />
        <Route path="/projects/:projectId" element={<div data-testid="owner-workspace" />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})
afterEach(() => cleanup())

it('does not call Launch when the owner has never saved, and explains why', async () => {
  h.getProject.mockResolvedValue(makeProject({ hasSavedSnapshot: false }))

  renderAt()

  await waitFor(() => expect(screen.getByText('Nothing to launch yet')).toBeTruthy())
  expect(screen.getByText('The owner hasn’t saved a version of this app yet.')).toBeTruthy()
  expect(h.launchSharedPreview).not.toHaveBeenCalled()
})

it('launches normally when a saved snapshot exists', async () => {
  h.getProject.mockResolvedValue(makeProject({ hasSavedSnapshot: true }))
  h.launchSharedPreview.mockResolvedValue({
    appId: 'app-1',
    previewUrl: 'https://app-1.example/',
    ready: true,
    snapshotTakenAt: null,
  })

  renderAt()

  await waitFor(() => expect(h.launchSharedPreview).toHaveBeenCalledWith('p1'))
  expect(await screen.findByTitle('Visitor Log')).toBeTruthy() // the iframe
})

it('still attempts Launch when the snapshot presence is unknown (null)', async () => {
  h.getProject.mockResolvedValue(makeProject({ hasSavedSnapshot: null }))
  h.launchSharedPreview.mockResolvedValue({
    appId: 'app-1',
    previewUrl: 'https://app-1.example/',
    ready: true,
    snapshotTakenAt: null,
  })

  renderAt()

  await waitFor(() => expect(h.launchSharedPreview).toHaveBeenCalledWith('p1'))
})

it('redirects an owner to their own workspace instead of rendering this view', async () => {
  h.getProject.mockResolvedValue(makeProject({ access: 'owner' }))

  renderAt()

  expect(await screen.findByTestId('owner-workspace')).toBeTruthy()
  expect(h.launchSharedPreview).not.toHaveBeenCalled()
})

// --- the hand-over prompt (requirement 24's frontend half) --------------------------------

it('shows the hand-over dialog, not a generic failure, when the slot holds another shared view', async () => {
  h.getProject.mockResolvedValue(makeProject())
  h.launchSharedPreview.mockRejectedValue(
    reclaimBlockedError({
      projectId: 'owner-project-id',
      projectName: 'Someone Else’s App',
      dirty: false,
      building: false,
      agentWorking: false,
      isSharedView: true,
    }),
  )

  renderAt()

  // The dialog, not the generic "Couldn't open this app" card.
  expect(await screen.findByRole('dialog')).toBeTruthy()
  expect(screen.queryByText('Couldn’t open this app')).toBeNull()
  expect(screen.getByRole('dialog').textContent).toMatch(/Someone Else’s App/)
})

it('routes a shared occupant through handOverWorkspace with isSharedView, never giveUpSharedView directly', async () => {
  // `handOverWorkspace` itself owns the `isSharedView` branch (see buildSessionApi.test.ts for
  // that branching behavior) — it is mocked at the module boundary here, so this test only
  // pins that the PAGE hands it the whole blocked object rather than calling `giveUpSharedView`
  // itself, which would silently reintroduce the bug of every call site needing its own check.
  h.getProject.mockResolvedValue(makeProject())
  h.launchSharedPreview
    .mockRejectedValueOnce(
      reclaimBlockedError({
        projectId: 'owner-project-id',
        projectName: 'Someone Else’s App',
        dirty: false,
        building: false,
        agentWorking: false,
        isSharedView: true,
      }),
    )
    .mockResolvedValueOnce({
      appId: 'app-1',
      previewUrl: 'https://app-1.example/',
      ready: true,
      snapshotTakenAt: null,
    })
  h.handOverWorkspace.mockResolvedValue(undefined)

  renderAt()
  await screen.findByRole('dialog')
  // `dirty: false` renders the clean-stop copy, which offers ONLY the discard button.
  fireEvent.click(screen.getByRole('button', { name: /Stop/i }))

  await waitFor(() =>
    expect(h.handOverWorkspace).toHaveBeenCalledWith(
      expect.objectContaining({ projectId: 'owner-project-id', isSharedView: true }),
      false,
      expect.anything(),
      expect.anything(),
    ),
  )
  expect(h.giveUpSharedView).not.toHaveBeenCalled()
  await waitFor(() => expect(h.launchSharedPreview).toHaveBeenCalledTimes(2)) // retried
  expect(await screen.findByTitle('Visitor Log')).toBeTruthy() // the iframe, on retry
})

it('hands over the recipients own build (never giveUpSharedView) when the occupant is not a shared view', async () => {
  h.getProject.mockResolvedValue(makeProject())
  h.launchSharedPreview
    .mockRejectedValueOnce(
      reclaimBlockedError({
        projectId: 'recipients-own-project-id',
        projectName: 'My Other App',
        dirty: true,
        building: false,
        agentWorking: false,
        isSharedView: false,
      }),
    )
    .mockResolvedValueOnce({
      appId: 'app-1',
      previewUrl: 'https://app-1.example/',
      ready: true,
      snapshotTakenAt: null,
    })
  h.handOverWorkspace.mockResolvedValue(undefined)

  renderAt()
  await screen.findByRole('dialog')
  fireEvent.click(screen.getByRole('button', { name: /without saving/i }))

  await waitFor(() =>
    expect(h.handOverWorkspace).toHaveBeenCalledWith(
      expect.objectContaining({ projectId: 'recipients-own-project-id', isSharedView: false }),
      false,
      expect.anything(),
      expect.anything(),
    ),
  )
  expect(h.giveUpSharedView).not.toHaveBeenCalled()
  await waitFor(() => expect(h.launchSharedPreview).toHaveBeenCalledTimes(2)) // retried
})
