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
  dataSummary: vi.fn(),
  clearData: vi.fn(),
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
  dataCount: 0,
  dataBytes: 0,
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

  it('Download bundle mints the audited URL and opens it (never renders it)', async () => {
    h.bundleDownloadUrl.mockResolvedValue({ url: 'https://blob/sas-url', submissionId: 'sub-1' })
    const opened = vi.spyOn(window, 'open').mockReturnValue(null)
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTestId('review-app-1'))
    fireEvent.click(screen.getByTestId('download-bundle'))
    await waitFor(() => expect(h.bundleDownloadUrl).toHaveBeenCalledWith('app-1'))
    await waitFor(() => expect(opened).toHaveBeenCalledWith('https://blob/sas-url', '_blank', 'noopener'))
    // The bearer URL is never rendered into the DOM.
    expect(document.body.textContent).not.toContain('sas-url')
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
  })

  it('an approved app shows the deploy-needed indicator and Mark deployed records the runbook run', async () => {
    h.listApps.mockResolvedValue([APPROVED])
    h.markDeployed.mockResolvedValue({ appId: 'app-2' })
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool')
    expect(screen.getByTestId('redeploy-needed-app-2')).toBeTruthy()
    fireEvent.click(screen.getByTestId('mark-deployed-app-2'))
    await waitFor(() => expect(h.markDeployed).toHaveBeenCalledWith('app-2'))
    await waitFor(() => expect(h.listApps).toHaveBeenCalledTimes(2)) // reload reflects the marker
  })

  it('a deployed-and-current app shows NO deploy-needed indicator', async () => {
    h.listApps.mockResolvedValue([{ ...APPROVED, redeployNeeded: false }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool')
    expect(screen.queryByTestId('redeploy-needed-app-2')).toBeNull()
  })

  it('toggling login PATCHes the inverse loginRequired', async () => {
    h.patchApp.mockResolvedValue({})
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByRole('button', { name: /Off/i }))
    await waitFor(() => expect(h.patchApp).toHaveBeenCalledWith('app-1', { loginRequired: true }))
  })

  it('clear-data opens the two-step modal and runs only after the preflight token', async () => {
    h.dataSummary.mockResolvedValue({ dataCount: 3, dataBytes: 300, confirmToken: 'tok-1' })
    h.clearData.mockResolvedValue({ removed: 3 })
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTitle('Clear data'))
    await screen.findByTestId('clear-confirm')
    fireEvent.click(screen.getByTestId('clear-confirm'))
    await waitFor(() => expect(h.clearData).toHaveBeenCalledWith('app-1', 'tok-1', true))
  })
})
