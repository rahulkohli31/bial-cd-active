/**
 * `SharedProjectPage` — the restricted `/shared/:projectId` view. `projectApi` and
 * `buildSessionApi` are mocked at the module boundary; the point under test is the page's own
 * branching, not the network.
 *
 * The one behavior these tests exist to pin (#198 R10's second sentence): a project whose
 * owner has never saved must show a disabled, explained state WITHOUT ever calling Launch —
 * not a reactive failure after a wasted round trip.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import SharedProjectPage from '../SharedProjectPage'
import type { Project } from '../../utils/projectApi'
import type { authFetch } from '../../utils/api'
import { ApiError } from '../../utils/apiError'
import { startWaitsFrom } from './_observeBeacons'

const h = vi.hoisted(() => ({
  getProject: vi.fn(),
  launchSharedPreview: vi.fn(),
  refreshSharedPreview: vi.fn(),
  handOverWorkspace: vi.fn(),
  authFetch: vi.fn<typeof authFetch>(),
}))

// The real `observe` module runs here; only its transport is replaced.
vi.mock('../../utils/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/api')>()),
  authFetch: h.authFetch,
}))

vi.mock('../../utils/projectApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/projectApi')>()),
  getProject: h.getProject,
}))
vi.mock('../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/buildSessionApi')>()),
  launchSharedPreview: h.launchSharedPreview,
  refreshSharedPreview: h.refreshSharedPreview,
  handOverWorkspace: h.handOverWorkspace,
}))

/** The exact shape `asReclaimBlocked` reads off a thrown error — a real `sandbox_reclaim_
 *  blocked` 409, not a scripted mock of the narrower. */
function reclaimBlockedError(details: {
  projectId: string
  projectName: string
  building: boolean
  agentWorking: boolean
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
  isPublishing: false,
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
        <Route path="/shared-applications" element={<div data-testid="shared-list" />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  h.authFetch.mockResolvedValue({ ok: true } as Response)
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

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

it('bounces a recipient off a dead share onto Shared Applications, not the owner list', async () => {
  // A revoked share and a deleted project are the SAME non-leaking 404 here, and either way
  // this reader came from the shared list and belongs back on it.
  h.getProject.mockRejectedValue(new ApiError('gone', 404))

  renderAt()

  expect(await screen.findByTestId('shared-list')).toBeTruthy()
  expect(screen.queryByTestId('projects-page')).toBeNull()
})

// --- the hand-over prompt (requirement 24's frontend half) --------------------------------

it('shows the hand-over dialog, not a generic failure, when the caller`s own project holds the slot', async () => {
  h.getProject.mockResolvedValue(makeProject())
  h.launchSharedPreview.mockRejectedValue(
    reclaimBlockedError({
      projectId: 'my-other-project-id',
      projectName: 'My Other App',
      building: false,
      agentWorking: false,
    }),
  )

  renderAt()

  // The dialog, not the generic "Couldn't open this app" card.
  expect(await screen.findByRole('dialog')).toBeTruthy()
  expect(screen.queryByText('Couldn’t open this app')).toBeNull()
  expect(screen.getByRole('dialog').textContent).toMatch(/My Other App/)
})

it('hands over the named project on the dialog`s own button, then retries the launch', async () => {
  h.getProject.mockResolvedValue(makeProject())
  h.launchSharedPreview
    .mockRejectedValueOnce(
      reclaimBlockedError({
        projectId: 'my-other-project-id',
        projectName: 'My Other App',
        building: false,
        agentWorking: false,
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
      expect.objectContaining({ projectId: 'my-other-project-id' }),
      false,
      expect.anything(),
      expect.anything(),
    ),
  )
  await waitFor(() => expect(h.launchSharedPreview).toHaveBeenCalledTimes(2)) // retried
  expect(await screen.findByTitle('Visitor Log')).toBeTruthy() // the iframe, on retry
})

// --- the browser's clock for a shared view's start ----------------------------------------

describe('timing a shared view from the click to the app showing', () => {
  const APPS = 'https://apps.example'
  const VIEW = `${APPS}/a/shr-0123456789abcdef0123456789ab/`
  const VIEW_PATH = '/a/shr-0123456789abcdef0123456789ab'
  const T0 = new Date('2026-10-05T09:00:00Z')

  const served = (startId: string | null) => ({
    appId: 'app-1',
    previewUrl: VIEW,
    ready: true,
    snapshotTakenAt: null,
    startId,
  })

  /** A message arriving at the page, from `source`, as the browser delivers one. */
  function post(source: Window | null, data: unknown, origin: string = APPS) {
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data, origin, source }))
    })
  }

  async function theFrame(): Promise<HTMLIFrameElement> {
    const el = await screen.findByTitle('Visitor Log')
    if (!(el instanceof HTMLIFrameElement)) throw new Error('the shared view framed nothing')
    return el
  }

  const startWaits = () => startWaitsFrom(h.authFetch)

  it('★ reports the wait from the launch to the app`s own mounted signal, once, against its start', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(T0)
    h.getProject.mockResolvedValue(makeProject())
    h.launchSharedPreview.mockResolvedValue(served('s-launch'))

    renderAt()
    const frame = await theFrame()
    vi.setSystemTime(T0.getTime() + 41_250)
    post(frame.contentWindow, { type: 'bial:app-mounted', path: VIEW_PATH })
    post(frame.contentWindow, { type: 'bial:app-mounted', path: VIEW_PATH })

    expect(startWaits()).toEqual([{ startId: 's-launch', durationMs: 41_250 }])
  })

  it('★ is not stopped by the frame loading — a 502 loads too', async () => {
    // Mutation check: report on the iframe's `load` and this goes red.
    h.getProject.mockResolvedValue(makeProject())
    h.launchSharedPreview.mockResolvedValue(served('s-load'))

    renderAt()
    const frame = await theFrame()
    act(() => {
      fireEvent.load(frame)
    })

    expect(startWaits()).toEqual([])
    post(frame.contentWindow, { type: 'bial:app-mounted', path: VIEW_PATH })
    expect(startWaits()).toHaveLength(1)
  })

  it('★ trusts the mounted signal only from its own frame, at the app`s origin and path', async () => {
    // Every generated app shares one origin, so the window and the path are what say "this app".
    h.getProject.mockResolvedValue(makeProject())
    h.launchSharedPreview.mockResolvedValue(served('s-trust'))

    renderAt()
    const frame = await theFrame()
    post(window, { type: 'bial:app-mounted', path: VIEW_PATH })
    post(frame.contentWindow, { type: 'bial:app-mounted', path: VIEW_PATH }, 'https://elsewhere.example')
    post(frame.contentWindow, { type: 'bial:app-mounted', path: '/a/shr-ffffffffffffffffffffffffffff' })
    post(frame.contentWindow, { type: 'bial:app-painting', path: VIEW_PATH })

    expect(startWaits()).toEqual([])
    post(frame.contentWindow, { type: 'bial:app-mounted', path: VIEW_PATH })
    expect(startWaits()).toHaveLength(1)
  })

  it('reports nothing for a launch that attached to a view already running', async () => {
    h.getProject.mockResolvedValue(makeProject())
    h.launchSharedPreview.mockResolvedValue(served(null))

    renderAt()
    const frame = await theFrame()
    post(frame.contentWindow, { type: 'bial:app-mounted', path: VIEW_PATH })

    expect(startWaits()).toEqual([])
  })

  it('★ reports nothing for a start left before its app showed, even when the view is seen later', async () => {
    // The view the first visit started is attached to on the second; its app showing then is not
    // the end of the first visit's wait. Mutation check: drop the page's abandon and this goes red.
    h.getProject.mockResolvedValue(makeProject())
    h.launchSharedPreview.mockResolvedValueOnce(served('s-left')).mockResolvedValueOnce(served(null))
    h.refreshSharedPreview.mockResolvedValue(served('s-back'))

    renderAt()
    await theFrame()
    cleanup()
    renderAt()
    const frame = await theFrame()
    post(frame.contentWindow, { type: 'bial:app-mounted', path: VIEW_PATH })

    expect(startWaits()).toEqual([])
    fireEvent.click(screen.getByRole('button', { name: /refresh/i }))
    await waitFor(() => expect(screen.getByTitle('Visitor Log')).not.toBe(frame))
    post((await theFrame()).contentWindow, { type: 'bial:app-mounted', path: VIEW_PATH })
    expect(startWaits()).toEqual([{ startId: 's-back', durationMs: expect.any(Number) }])
  })

  it('times a Refresh from its own click, against the start it began', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(T0)
    h.getProject.mockResolvedValue(makeProject())
    h.launchSharedPreview.mockResolvedValue(served('s-first'))
    h.refreshSharedPreview.mockResolvedValue(served('s-refresh'))

    renderAt()
    const first = await theFrame()
    vi.setSystemTime(T0.getTime() + 30_000)
    post(first.contentWindow, { type: 'bial:app-mounted', path: VIEW_PATH })

    vi.setSystemTime(T0.getTime() + 120_000)
    fireEvent.click(screen.getByRole('button', { name: /refresh/i }))
    await waitFor(() => expect(h.refreshSharedPreview).toHaveBeenCalledWith('p1'))
    await waitFor(() => expect(screen.getByTitle('Visitor Log')).not.toBe(first))
    vi.setSystemTime(T0.getTime() + 129_000)
    post((await theFrame()).contentWindow, { type: 'bial:app-mounted', path: VIEW_PATH })

    expect(startWaits()).toEqual([
      { startId: 's-first', durationMs: 30_000 },
      { startId: 's-refresh', durationMs: 9_000 },
    ])
  })
})
