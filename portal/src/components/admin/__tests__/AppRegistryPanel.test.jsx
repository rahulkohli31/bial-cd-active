import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react'
import AppRegistryPanel from '../AppRegistryPanel.jsx'

const h = vi.hoisted(() => ({
  listApps: vi.fn(),
  approveApp: vi.fn(),
  rejectApp: vi.fn(),
  patchApp: vi.fn(),
  disableApp: vi.fn(),
  enableApp: vi.fn(),
  bundleDownloadUrl: vi.fn(),
  markDeployed: vi.fn(),
  deleteApp: vi.fn(),
  fetchAudit: vi.fn(),
}))
vi.mock('../../../utils/appRegistryApi', () => h)

const SHA = 'f0e1d2c3b4a5f0e1d2c3b4a5f0e1d2c3b4a5f0e1'

const PENDING = {
  appId: 'app-1',
  name: 'Gate Tool',
  ownerUsername: 'alice',
  status: 'pending',
  loginRequired: false,
  hasApprovedSnapshot: false,
  submissionId: 'sub-1',
  commitSha: SHA,
  submittedAt: '2026-07-16T09:00:00Z',
  redeployNeeded: false,
}

const APPROVED = {
  ...PENDING,
  appId: 'app-2',
  name: 'Live Tool',
  status: 'approved',
  hasApprovedSnapshot: true,
  redeployNeeded: true,
}

afterEach(cleanup)
beforeEach(() => {
  for (const fn of Object.values(h)) fn.mockReset()
  h.listApps.mockResolvedValue([PENDING])
})

describe('AppRegistryPanel — registry vocabulary + actions', () => {
  it('loads the pending list and renders the registry status sub-tabs (not the mock vocabulary)', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    expect(h.listApps).toHaveBeenCalledWith('pending')
    // registry sub-tabs exist; the mock "Security Flags"/"under_review" vocabulary does not
    expect(screen.getByTestId('apps-tab-approved')).toBeTruthy()
    expect(screen.getByTestId('apps-tab-disabled')).toBeTruthy()
    expect(screen.queryByText('Security Flags')).toBeNull()
    expect(screen.getAllByText('Pending Review').length).toBeGreaterThan(0) // tab + badge
  })

  it('the review modal shows submission METADATA (SHA, submitted-at, submission id) and offers a download', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTestId('review-app-1'))
    expect(screen.getByTestId('review-commit-sha').textContent).toContain(SHA.slice(0, 12))
    expect(screen.getByTestId('review-submission-id').textContent).toContain('sub-1')
    expect(screen.getByTestId('review-submitted-at').textContent).not.toContain('—')
    expect(screen.getByTestId('download-bundle')).toBeTruthy()
    // The false JSX-era claims are gone: no "pre-compiles" copy, no /apps/{id} link.
    expect(document.body.textContent).not.toMatch(/pre-compiles/i)
    expect(document.querySelector('a[href^="/apps/"]')).toBeNull()
  })

  it('Download bundle pre-opens a tab (riding the click gesture) then redirects it to the minted URL, never rendering it', async () => {
    h.bundleDownloadUrl.mockResolvedValue({ url: 'https://blob/sas-url', submissionId: 'sub-1' })
    const fakeWin = { location: '', close: vi.fn() }
    const opened = vi.spyOn(window, 'open').mockReturnValue(fakeWin)
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTestId('review-app-1'))
    fireEvent.click(screen.getByTestId('download-bundle'))
    // The tab is opened SYNCHRONOUSLY (blank), before the awaited mint — so the popup
    // survives Safari's user-activation rule instead of being blocked.
    expect(opened).toHaveBeenCalledWith('', '_blank', 'noopener')
    await waitFor(() => expect(h.bundleDownloadUrl).toHaveBeenCalledWith('app-1'))
    // The bearer URL only ever touches the tab's location — never the DOM.
    await waitFor(() => expect(fakeWin.location).toBe('https://blob/sas-url'))
    expect(document.body.textContent).not.toContain('sas-url')
    opened.mockRestore()
  })

  it('Download bundle surfaces a toast (and mints no URL) when the popup is blocked', async () => {
    const opened = vi.spyOn(window, 'open').mockReturnValue(null)
    const onToast = vi.fn()
    render(<AppRegistryPanel onToast={onToast} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTestId('review-app-1'))
    fireEvent.click(screen.getByTestId('download-bundle'))
    // A blocked popup is not a silent no-op; and no bearer credential is minted for a dead tab.
    await waitFor(() => expect(onToast).toHaveBeenCalledWith(expect.stringMatching(/blocked/i)))
    expect(h.bundleDownloadUrl).not.toHaveBeenCalled()
    opened.mockRestore()
  })

  it('Review → Approve sends the DISPLAYED submission id (the reviewed-id guard input) and reloads', async () => {
    h.approveApp.mockResolvedValue({ status: 'approved' })
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTestId('review-app-1'))
    fireEvent.click(screen.getByTestId('approve-btn'))
    await waitFor(() => expect(h.approveApp).toHaveBeenCalledWith('app-1', 'sub-1'))
    await waitFor(() => expect(h.listApps).toHaveBeenCalledTimes(2)) // initial + reload
  })

  it('an approve 409 surfaces the re-submitted-since-review copy, not a generic failure', async () => {
    const copy = 'This app was re-submitted since you reviewed it — please re-review.'
    h.approveApp.mockRejectedValue(new Error(copy))
    const onToast = vi.fn()
    render(<AppRegistryPanel onToast={onToast} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTestId('review-app-1'))
    fireEvent.click(screen.getByTestId('approve-btn'))
    await waitFor(() => expect(onToast).toHaveBeenCalledWith(copy))
    // The modal stays OPEN on the 409 — the admin still needs the submission metadata
    // to re-review; act() reports failure so onApprove does not setReview(null).
    expect(screen.getByTestId('approve-btn')).toBeTruthy()
  })

  it('an approved app shows the deploy-needed indicator and Mark deployed records the runbook run + URL', async () => {
    const live = 'https://apps.bial.example.com/gate-ops'
    const prompted = vi.spyOn(window, 'prompt').mockReturnValue(`  ${live}  `)
    h.listApps.mockResolvedValue([APPROVED])
    h.markDeployed.mockResolvedValue({ appId: 'app-2', deployedUrl: live })
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool')
    expect(screen.getByTestId('redeploy-needed-app-2')).toBeTruthy()
    fireEvent.click(screen.getByTestId('mark-deployed-app-2'))
    // Pasted URLs pick up stray whitespace — trimmed before it can 422 at the server.
    await waitFor(() => expect(h.markDeployed).toHaveBeenCalledWith('app-2', live))
    await waitFor(() => expect(h.listApps).toHaveBeenCalledTimes(2)) // reload reflects the marker
    prompted.mockRestore()
  })

  it('the URL prompt defaults to the recorded one and a blank answer keeps it (R5)', async () => {
    const live = 'https://apps.bial.example.com/gate-ops'
    const prompted = vi.spyOn(window, 'prompt').mockReturnValue('')
    h.listApps.mockResolvedValue([{ ...APPROVED, deployedUrl: live }])
    h.markDeployed.mockResolvedValue({ appId: 'app-2', deployedUrl: live })
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool')
    fireEvent.click(screen.getByTestId('mark-deployed-app-2'))

    // The already-recorded address is pre-filled, so a routine re-deploy is one Enter…
    expect(prompted.mock.calls[0][1]).toBe(live)
    // …and a blank answer still marks the deploy, sending no URL (server keeps it).
    await waitFor(() => expect(h.markDeployed).toHaveBeenCalledWith('app-2', ''))
    prompted.mockRestore()
  })

  it('cancelling the URL prompt marks nothing at all', async () => {
    const prompted = vi.spyOn(window, 'prompt').mockReturnValue(null)
    h.listApps.mockResolvedValue([APPROVED])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool')
    fireEvent.click(screen.getByTestId('mark-deployed-app-2'))
    expect(h.markDeployed).not.toHaveBeenCalled()
    prompted.mockRestore()
  })

  it('surfaces the server 422 for a bad URL as a toast (no client-side URL check)', async () => {
    const prompted = vi.spyOn(window, 'prompt').mockReturnValue('http://insecure.example.com')
    const onToast = vi.fn()
    h.listApps.mockResolvedValue([APPROVED])
    h.markDeployed.mockRejectedValue(new Error('URL scheme should be https'))
    render(<AppRegistryPanel onToast={onToast} />)
    await screen.findByText('Live Tool')
    fireEvent.click(screen.getByTestId('mark-deployed-app-2'))
    await waitFor(() => expect(onToast).toHaveBeenCalledWith('URL scheme should be https'))
    prompted.mockRestore()
  })

  it('a deployed-and-current app shows NO deploy-needed indicator', async () => {
    h.listApps.mockResolvedValue([{ ...APPROVED, redeployNeeded: false }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool')
    expect(screen.queryByTestId('redeploy-needed-app-2')).toBeNull()
  })

  it('renders the advisory database size column, human-formatted, and "—" when null', async () => {
    h.listApps.mockResolvedValue([
      { ...PENDING, appId: 'app-sized', databaseBytes: 2 * 1024 * 1024 },
      { ...PENDING, appId: 'app-null', name: 'No DB', databaseBytes: null },
    ])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    // The backend surfaces AdminAppOut.databaseBytes (R10) — the column must actually show it.
    expect(screen.getByTestId('db-bytes-app-sized').textContent).toBe('2.0 MB')
    // Null is "no number to show" (never provisioned / not ready / cluster unreachable), not 0 B.
    expect(screen.getByTestId('db-bytes-app-null').textContent).toBe('—')
  })

  it('toggling login PATCHes the inverse loginRequired', async () => {
    h.patchApp.mockResolvedValue({})
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByRole('button', { name: /Off/i }))
    await waitFor(() => expect(h.patchApp).toHaveBeenCalledWith('app-1', { loginRequired: true }))
  })
})
