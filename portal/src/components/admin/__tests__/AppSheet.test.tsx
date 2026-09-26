/**
 * One app's side panel: Review while a decision is pending, History always. The history client
 * is mocked with what the server sends, so these pin how the panel draws each version, each event
 * between versions and each version's own answers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, within } from '@testing-library/react'
import type { AppHistory, HistoryVersion, RegistryApp } from '../../../utils/appRegistryApi'

const h = vi.hoisted(() => ({ fetchHistory: vi.fn() }))
vi.mock('../../../utils/appRegistryApi', () => h)

import AppSheet from '../AppSheet'

/** An ISO time built from local parts, so what the panel prints is the same in every time zone. */
const local = (day: number, hour: number, minute = 0): string => new Date(2026, 8, day, hour, minute).toISOString()

const C1 = '2e77b10' + '1'.repeat(33)
const C2 = 'c09a4e1' + '2'.repeat(33)
const C3 = '51bd2f8' + '3'.repeat(33)
const C4 = '7a3c9e0' + '4'.repeat(33)

const APP: RegistryApp = {
  appId: 'lost-and-found',
  name: 'Lost & Found Register',
  ownerId: 'owner-1',
  ownerUsername: 'kavya.n@bialairport.com',
  status: 'approved',
  registryStatus: 'live',
  liveVersion: { number: 4, commitSha: C4, since: local(25, 16, 40) },
  loginRequired: false,
  hasApprovedSnapshot: true,
  submissionId: 'sub-3',
  commitSha: C3,
  submittedAt: local(17, 11, 20),
  approvedSubmissionId: 'sub-3',
  approvedCommitSha: C3,
  approvedBy: 'admin-1',
  approvedAt: local(18, 9, 5),
  declaration: null,
  databaseBytes: null,
  rejectionNote: null,
  createdAt: local(4, 9),
  updatedAt: local(25, 16, 40),
}

const answered = (commit: string, answers: Record<string, boolean>) => ({
  commits: { shipping: commit, reviewed: commit },
  citizen: { answers },
})

const version = (overrides: Partial<HistoryVersion> & Pick<HistoryVersion, 'number'>): HistoryVersion => ({
  kind: 'version',
  commitSha: null,
  submissionId: null,
  sentAt: local(1, 9),
  sentBy: 'kavya.n@bialairport.com',
  declaration: null,
  decision: { kind: 'published', by: null, at: null, note: null },
  attempts: [],
  state: 'live',
  publishedAt: null,
  replacedBy: null,
  replacedAt: null,
  ...overrides,
})

const LOST_AND_FOUND: AppHistory = {
  live: { number: 4, commitSha: C4, since: local(25, 16, 40) },
  liveUrl: 'https://pub.example/lost-and-found',
  truncated: false,
  entries: [
    version({
      number: 4,
      commitSha: C4,
      sentAt: local(25, 16, 2),
      attempts: [{ status: 'succeeded', startedAt: local(25, 16, 2), finishedAt: local(25, 16, 40), failureCode: null }],
      publishedAt: local(25, 16, 40),
      declaration: answered(C4, { personal_information: false, financial_data: false }),
    }),
    { kind: 'event', action: 'restart', at: local(23, 10, 12), by: 'kavya.n@bialairport.com', reenabledAt: null },
    version({
      number: 3,
      commitSha: C3,
      submissionId: 'sub-3',
      sentAt: local(17, 11, 20),
      decision: { kind: 'approved', by: 'adiseshu@bialairport.com', at: local(18, 9, 5), note: null },
      attempts: [{ status: 'succeeded', startedAt: local(18, 9, 5), finishedAt: local(18, 9, 12), failureCode: null }],
      state: 'replaced',
      publishedAt: local(18, 9, 12),
      replacedBy: 4,
      replacedAt: local(25, 16, 40),
      declaration: answered(C3, { personal_information: true, financial_data: false, public_data: false }),
    }),
    version({
      number: 2,
      commitSha: C2,
      submissionId: 'sub-2',
      sentAt: local(15, 15, 40),
      decision: {
        kind: 'rejected',
        by: 'adiseshu@bialairport.com',
        at: local(16, 10),
        note: 'Remove the passport number field before this goes live.',
      },
      state: 'rejected',
      declaration: answered(C2, { financial_data: true, personal_information: false }),
    }),
    { kind: 'event', action: 'disable', at: local(13, 18, 40), by: 'adiseshu@bialairport.com', reenabledAt: local(14, 9, 2) },
    version({
      number: 1,
      commitSha: C1,
      submissionId: 'sub-1',
      sentAt: local(4, 17, 15),
      decision: { kind: 'approved', by: 'adiseshu@bialairport.com', at: local(5, 14), note: null },
      attempts: [
        { status: 'failed', startedAt: local(5, 14), finishedAt: local(5, 14, 20), failureCode: 'build_failed' },
        { status: 'succeeded', startedAt: local(5, 14, 50), finishedAt: local(5, 15, 2), failureCode: null },
      ],
      state: 'replaced',
      publishedAt: local(5, 15, 2),
      replacedBy: 3,
      replacedAt: local(18, 9, 12),
    }),
  ],
}

const PENDING: RegistryApp = {
  ...APP,
  appId: 'visitor-pass',
  name: 'Visitor ID Pass',
  status: 'pending',
  registryStatus: 'waiting_for_review',
  liveVersion: null,
  submissionId: 'sub-9',
  commitSha: '3f2a9c1' + '9'.repeat(33),
}

const WAITING: AppHistory = {
  live: null,
  liveUrl: null,
  truncated: false,
  entries: [version({ number: 1, submissionId: 'sub-9', state: 'waiting', decision: { kind: 'waiting', by: null, at: null, note: null } })],
}

const noop = async () => {}

function open(app: RegistryApp, onClose = vi.fn()) {
  render(
    <AppSheet
      app={app}
      title={app.name}
      status={<span>status</span>}
      withdrawn={null}
      onClose={onClose}
      onApprove={noop}
      onReject={noop}
    />,
  )
  return onClose
}

const card = (number: number) => screen.getByTestId(`version-${number}`)

afterEach(cleanup)
beforeEach(() => {
  h.fetchHistory.mockReset()
  h.fetchHistory.mockResolvedValue(LOST_AND_FOUND)
})

describe('an app with nothing to decide', () => {
  it('opens on History alone, with no Review and no decision to make', async () => {
    open(APP)

    const panel = screen.getByRole('dialog', { name: 'Lost & Found Register' })
    expect(within(panel).getByRole('heading', { name: 'History' })).toBeTruthy()
    expect(await screen.findByTestId('version-4')).toBeTruthy()
    expect(within(panel).queryByRole('tab')).toBeNull()
    expect(within(panel).queryByTestId('approve-btn')).toBeNull()
    expect(h.fetchHistory).toHaveBeenCalledWith('lost-and-found')
  })

  it('names the owner under the app', () => {
    open(APP)

    expect(screen.getByText('kavya.n@bialairport.com')).toBeTruthy()
  })
})

describe('History reads newest first, with the events between the versions', () => {
  it('lists v4, the restart, v3, v2, the disable and v1 in that order', async () => {
    open(APP)
    await screen.findByTestId('version-4')

    const order = [...document.querySelectorAll('[data-testid^="version-"], [data-testid^="event-"]')].map((node) =>
      node.getAttribute('data-testid'),
    )
    expect(order).toEqual(['version-4', 'event-restart', 'version-3', 'version-2', 'event-disable', 'version-1'])
  })

  it('says which version is live now and since when, with a way to open it', async () => {
    open(APP)

    const live = await screen.findByTestId('history-live-now')
    expect(live.textContent).toContain('Live now: v4 · 7a3c9e0')
    expect(live.textContent).toContain('Published 25 Sep, 16:40 · 4 versions sent for publishing since 4 Sep')
    expect(within(live).getByRole('link', { name: /Open live app/ }).getAttribute('href')).toBe(
      'https://pub.example/lost-and-found',
    )
    expect(within(card(4)).getByText('Live')).toBeTruthy()
  })

  it('gives each version its sender, its decision and its publishing, in the board’s words', async () => {
    open(APP)
    await screen.findByTestId('version-4')

    expect(card(4).textContent).toContain('25 Sep, 16:02 by kavya.n')
    expect(card(4).textContent).toContain('Published by itself (under the threshold)')
    expect(card(3).textContent).toContain('Approved by adiseshu, 18 Sep, 09:05')
    expect(card(3).textContent).toContain('18 Sep, 09:12 · replaced by v4 on 25 Sep')
    expect(within(card(3)).getByText('Replaced')).toBeTruthy()
    expect(card(2).textContent).toContain('Rejected by adiseshu, 16 Sep: "Remove the passport number field before this goes live."')
    expect(within(card(2)).getByText('Rejected')).toBeTruthy()
    expect(card(1).textContent).toContain(
      '5 Sep, 15:02, on the second attempt (the first failed to build) · replaced by v3 on 18 Sep',
    )
  })

  it('draws a version rejected before it could be published with no publishing line', async () => {
    open(APP)
    await screen.findByTestId('version-2')

    expect(within(card(3)).getByText('Published')).toBeTruthy()
    expect(within(card(2)).queryByText('Published')).toBeNull()
  })

  it('folds a disable and the re-enable that ended it into one line, and names who restarted', async () => {
    open(APP)

    expect((await screen.findByTestId('event-disable')).textContent).toBe(
      'Disabled by adiseshu · re-enabled 14 Sep, 09:0213 Sep, 18:40',
    )
    expect(screen.getByTestId('event-restart').textContent).toBe('Restarted by kavya.n23 Sep, 10:12')
  })

  it('says a rejection whose note was never recorded, rather than leaving it blank', async () => {
    h.fetchHistory.mockResolvedValue({
      ...WAITING,
      entries: [
        version({
          number: 1,
          state: 'rejected',
          decision: { kind: 'rejected', by: 'adiseshu@bialairport.com', at: local(16, 10), note: null },
        }),
      ],
    })
    open(APP)

    expect((await screen.findByTestId('version-1')).textContent).toContain('Rejected by adiseshu, 16 Sep · note not recorded')
  })

  it('says when History stops short of the oldest records', async () => {
    h.fetchHistory.mockResolvedValue({ ...LOST_AND_FOUND, truncated: true })
    open(APP)

    expect(await screen.findByText(/Only the newest records are read/)).toBeTruthy()
  })

  it('never claims History was cut short when it was not', async () => {
    open(APP)

    expect(await screen.findByTestId('version-1')).toBeTruthy()
    expect(screen.queryByText(/Only the newest records are read/)).toBeNull()
  })

  it('shows why History could not load', async () => {
    h.fetchHistory.mockRejectedValue(new Error('Failed to load the history'))
    open(APP)

    expect(await screen.findByText('Failed to load the history')).toBeTruthy()
  })
})

describe('each version’s Answers', () => {
  it('opens that version’s own answers inline and closes them on a second click', async () => {
    open(APP)
    await screen.findByTestId('version-3')
    const answers3 = within(card(3)).getByRole('button', { name: 'Answers' })
    expect(answers3.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(answers3)

    expect(answers3.getAttribute('aria-expanded')).toBe('true')
    expect(card(3).textContent).toContain('Personal Information (PII) · Yes')
    expect(card(3).textContent).toContain('2 other classes No')
    expect(card(2).textContent).not.toContain('Personal Information (PII) · Yes')

    fireEvent.click(answers3)

    expect(answers3.getAttribute('aria-expanded')).toBe('false')
    expect(card(3).textContent).not.toContain('Personal Information (PII) · Yes')
    expect(within(card(3)).getByText('Approved by adiseshu, 18 Sep, 09:05')).toBeTruthy()
  })

  it('shows two versions their own, different answers', async () => {
    open(APP)
    await screen.findByTestId('version-2')

    fireEvent.click(within(card(3)).getByRole('button', { name: 'Answers' }))
    fireEvent.click(within(card(2)).getByRole('button', { name: 'Answers' }))

    expect(card(3).textContent).toContain('Personal Information (PII) · Yes')
    expect(card(3).textContent).not.toContain('Financial Data · Yes')
    expect(card(2).textContent).toContain('Financial Data · Yes')
    expect(card(2).textContent).not.toContain('Personal Information (PII) · Yes')
  })

  it('says a version with no stored declaration has no answers on record', async () => {
    open(APP)
    await screen.findByTestId('version-1')

    fireEvent.click(within(card(1)).getByRole('button', { name: 'Answers' }))

    expect(card(1).textContent).toContain('Answers not recorded')
  })
})

describe('an app waiting for a decision', () => {
  beforeEach(() => {
    h.fetchHistory.mockResolvedValue(WAITING)
  })

  it('opens on Review, with History one tab away', async () => {
    open(PENDING)

    expect(screen.getByRole('tab', { name: 'Review' }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByRole('tab', { name: 'History' }).getAttribute('aria-selected')).toBe('false')
    expect(screen.getByTestId('approve-btn').textContent).toContain('Approve and publish')
    expect(await screen.findByText(/Approving publishes exactly v1 · 3f2a9c1\./)).toBeTruthy()
  })

  it('names the version it approves by its commit alone until History has numbered it', () => {
    h.fetchHistory.mockReturnValue(new Promise(() => {}))
    open(PENDING)

    expect(screen.getByTestId('review-publish-note').textContent).toBe('Approving publishes exactly 3f2a9c1.')
  })

  it('warns that rejecting a live app leaves it running at its URL', () => {
    open({ ...PENDING, liveVersion: { number: 1, commitSha: C1, since: local(5, 15, 2) } })

    fireEvent.click(screen.getByTestId('reject-btn'))

    expect(screen.getByTestId('reject-delists-warning').textContent).toBe(
      'This app is live. Rejecting removes it from the Marketplace but leaves it running at its URL, and only its owner can undo that by submitting again. To take it down, use Unpublish instead.',
    )
  })

  it('gives no such warning for an app that was never published', () => {
    open(PENDING)

    fireEvent.click(screen.getByTestId('reject-btn'))

    // Liveness: the rejection form really opened.
    expect(screen.getByTestId('reject-note')).toBeTruthy()
    expect(screen.queryByTestId('reject-delists-warning')).toBeNull()
  })
})

describe('opening and closing the panel', () => {
  it('puts focus on the panel itself, so a keyboard starts at its top', () => {
    open(APP)

    expect(document.activeElement).toBe(screen.getByRole('dialog'))
  })

  it('closes on Escape', () => {
    const onClose = open(APP)

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })

    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('closes from its own Close button', () => {
    const onClose = open(APP)

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
