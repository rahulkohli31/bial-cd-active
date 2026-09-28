/**
 * One app's side panel: Review while a decision is pending, History always. The history client
 * is mocked with what the server sends, so these pin how the panel draws each version, each event
 * between versions and each version's own answers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor, within } from '@testing-library/react'
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

const CLASSES = [
  { key: 'pii', title: 'PII', kind: 'hard_block', weight: null },
  { key: 'financial_data', title: 'Financial data', kind: 'hard_block', weight: null },
  { key: 'credentials_keys', title: 'Credentials & keys', kind: 'scored', weight: 20 },
  { key: 'confidential_business_data', title: 'Confidential business data', kind: 'scored', weight: 20 },
  { key: 'ai_usage', title: 'AI usage', kind: 'scored', weight: 20 },
  { key: 'integrations', title: 'Integrations', kind: 'scored', weight: 20 },
  { key: 'public_data', title: 'Public data', kind: 'scored', weight: 20 },
]

const ALL_NO = Object.fromEntries(CLASSES.map((entry) => [entry.key, false]))

const SCORED_NO = Object.fromEntries(CLASSES.filter((entry) => entry.kind === 'scored').map((entry) => [entry.key, false]))

/** A declaration in the shape the publish gate stores with every decision. */
const judged = (overrides: Record<string, unknown> = {}): Record<string, unknown> => ({
  version: 2,
  commit: C4,
  savedAt: local(17, 11, 5),
  decidedAt: local(26, 9, 30),
  policy: { threshold: 50, ownersCanChangeAnswers: true },
  classes: CLASSES,
  review: { current: true, status: 'complete', failureCode: null, checkedAt: local(17, 11, 6) },
  reviewerAnswers: ALL_NO,
  reviewerReasons: {},
  ownerAnswers: SCORED_NO,
  reviewerScore: 0,
  score: 0,
  outcome: 'published',
  reason: null,
  note: null,
  ...overrides,
})

const PII_FOUND = judged({
  reviewerAnswers: { ...ALL_NO, pii: true },
  reviewerReasons: { pii: 'stores a photo of each visitor’s government ID (the upload on the visitor form).' },
  ownerAnswers: {},
  outcome: 'routed',
  reason: 'hard_block',
  note: 'Security asks us to keep an ID copy for every visitor entering airside, for 30 days.',
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
      declaration: judged({
        reviewerAnswers: { ...ALL_NO, integrations: true },
        ownerAnswers: SCORED_NO,
        reviewerScore: 20,
        score: 0,
      }),
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
      declaration: { ...PII_FOUND, commit: C3 },
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
      overtaken={null}
      problem={null}
      busy={false}
      onClose={onClose}
      onApprove={noop}
      onReject={noop}
    />,
  )
  return onClose
}

const card = (number: number) => screen.getByTestId(`version-${number}`)

const cellsOf = (row: HTMLElement) => within(row).getAllByRole('cell').map((cell) => cell.textContent)

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

  it('says what the agent found on each version decided by the classes, in the board’s words', async () => {
    open(APP)
    await screen.findByTestId('version-4')

    expect(card(4).textContent).toContain('AgentNo hard block · score 20/100')
    expect(card(3).textContent).toContain('AgentPII found (hard block)')
  })

  it('gives a version decided by the six questions no agent line', async () => {
    open(APP)
    await screen.findByTestId('version-2')

    expect(within(card(2)).getByText('Sent')).toBeTruthy()
    expect(within(card(2)).queryByText('Agent')).toBeNull()
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

  it('does not say an app failed to build when the platform could not build it', async () => {
    h.fetchHistory.mockResolvedValue({
      ...WAITING,
      entries: [
        version({
          number: 1,
          state: 'publish_failed',
          attempts: [
            { status: 'failed', startedAt: local(28, 8, 26), finishedAt: local(28, 8, 27), failureCode: 'build_unavailable' },
          ],
        }),
      ],
    })
    open(APP)

    const text = (await screen.findByTestId('version-1')).textContent
    expect(text).toContain("Not yet: the attempt on 28 Sep, 08:26 failed on the platform's side")
    expect(text).not.toContain('failed to build')
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
    expect(cellsOf(within(card(3)).getByTestId('answer-pii'))).toEqual(['PII', 'Hard block', 'Yes', '—'])
    expect(within(card(2)).queryByTestId('answer-pii')).toBeNull()

    fireEvent.click(answers3)

    expect(answers3.getAttribute('aria-expanded')).toBe('false')
    expect(within(card(3)).queryByTestId('answer-pii')).toBeNull()
    expect(within(card(3)).getByText('Approved by adiseshu, 18 Sep, 09:05')).toBeTruthy()
  })

  it('shows two versions their own, different answers', async () => {
    open(APP)
    await screen.findByTestId('version-3')

    fireEvent.click(within(card(4)).getByRole('button', { name: 'Answers' }))
    fireEvent.click(within(card(3)).getByRole('button', { name: 'Answers' }))

    expect(cellsOf(within(card(4)).getByTestId('answer-integrations'))).toEqual([
      'Integrations',
      'Scored · 20',
      'Yes',
      'Nochanged by the owner',
    ])
    expect(cellsOf(within(card(4)).getByTestId('answer-pii'))).toEqual(['PII', 'Hard block', 'No', '—'])
    expect(cellsOf(within(card(3)).getByTestId('answer-pii'))).toEqual(['PII', 'Hard block', 'Yes', '—'])
    expect(cellsOf(within(card(3)).getByTestId('answer-integrations'))).toEqual(['Integrations', 'Scored · 20', 'No', 'No'])
  })

  it('reads a version decided by the six questions through its own questions', async () => {
    open(APP)
    await screen.findByTestId('version-2')

    fireEvent.click(within(card(2)).getByRole('button', { name: 'Answers' }))

    expect(card(2).textContent).toContain('Financial Data · Yes')
    expect(card(2).textContent).toContain('1 other class No')
    expect(within(card(2)).queryByRole('table')).toBeNull()
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
      'This app is live. Rejecting removes it from the Marketplace but leaves it running at its URL, and only its owner can undo that by submitting again. Disabling it from its row’s menu cuts it off from its data, but its address keeps answering until its owner takes it down.',
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

describe('the review of a version decided by the classes', () => {
  beforeEach(() => {
    h.fetchHistory.mockResolvedValue(WAITING)
  })

  const review = (declaration: Record<string, unknown>) => open({ ...PENDING, declaration })

  it('leads with the hard block and the agent’s reason, then the owner’s note, then the answers', () => {
    review(PII_FOUND)

    const why = screen.getByTestId('review-why')
    expect(why.textContent).toContain('Why it is here: PII found (hard block)')
    expect(why.textContent).toContain(
      'Agent: stores a photo of each visitor’s government ID (the upload on the visitor form).',
    )
    expect(screen.getByTestId('review-note').textContent).toBe(
      'Security asks us to keep an ID copy for every visitor entering airside, for 30 days.',
    )
    expect(cellsOf(screen.getByTestId('answer-pii'))).toEqual(['PII', 'Hard block', 'Yes', '—'])
    expect(cellsOf(screen.getByTestId('answer-credentials_keys'))).toEqual(['Credentials & keys', 'Scored · 20', 'No', 'No'])

    const body = screen.getByTestId('review-scroll').textContent ?? ''
    const whyAt = body.indexOf('Why it is here')
    const noteAt = body.indexOf("Owner's note")
    const answersAt = body.indexOf('Answers')
    expect(whyAt).toBeGreaterThan(-1)
    expect(whyAt).toBeLessThan(noteAt)
    expect(noteAt).toBeLessThan(answersAt)
  })

  it('shows when the version was sent and by whom, which version it is, and what is live', async () => {
    review(PII_FOUND)

    const grid = screen.getByTestId('review-version')
    expect(grid.textContent).toContain('Sent17 Sep, 11:20by kavya.n')
    expect(grid.textContent).toContain('Checked17 Sep, 11:06by the agent')
    expect(grid.textContent).toContain('Live nowNot live yet')
    await waitFor(() => expect(grid.textContent).toContain('Versionv1 · 3f2a9c1Saved 17 Sep, 11:05'))
    expect(grid.textContent).toContain('First version')
  })

  it('shows no saved or checked time the declaration did not record', () => {
    review(
      judged({
        savedAt: null,
        review: { current: false, status: 'failed', failureCode: 'timeout' },
        reviewerAnswers: null,
        ownerAnswers: null,
        reviewerScore: null,
        score: null,
        outcome: 'routed',
        reason: 'review_unfinished',
        note: 'The check kept timing out.',
      }),
    )

    const grid = screen.getByTestId('review-version')
    expect(grid.textContent).toContain('Sent17 Sep, 11:20by kavya.n')
    expect(grid.textContent).not.toContain('Saved')
    expect(grid.textContent).not.toContain('Checked')
  })

  it('names the version live now beside the one under review', () => {
    open({ ...PENDING, declaration: PII_FOUND, liveVersion: { number: 3, commitSha: C3, since: local(18, 9, 12) } })

    expect(screen.getByTestId('review-version').textContent).toContain('Live nowv3 · 51bd2f8since 18 Sep')
  })

  it('gives an over-threshold send its score line, and marks the answers the owner changed', () => {
    review(
      judged({
        policy: { threshold: 30, ownersCanChangeAnswers: true },
        reviewerAnswers: { ...ALL_NO, credentials_keys: true, ai_usage: true, integrations: true },
        ownerAnswers: { ...SCORED_NO, credentials_keys: true, ai_usage: true },
        reviewerScore: 60,
        score: 40,
        outcome: 'routed',
        reason: 'over_threshold',
        note: 'The rates are already public on the vendor portal.',
      }),
    )

    expect(screen.getByTestId('review-why').textContent).toContain('Why it is here: Score 40/100 is over 30')
    const line = screen.getByTestId('review-score').textContent
    expect(line).toContain('Score agent 60 · owner 40 (of 100)')
    expect(line).toContain('Publish without review up to 30')
    expect(line).toContain('Owners can change answers Yes')
    expect(line).toContain('Policy as of 26 Sep, 09:30')

    const changed = screen.getByTestId('answer-integrations')
    expect(cellsOf(changed)).toEqual(['Integrations', 'Scored · 20', 'Yes', 'Nochanged by the owner'])
    expect(changed.className).toContain('bg-amber-50')
    expect(within(changed).getByText('changed by the owner').className).toContain('sr-only')
    const kept = screen.getByTestId('answer-ai_usage')
    expect(cellsOf(kept)).toEqual(['AI usage', 'Scored · 20', 'Yes', 'Yes'])
    expect(kept.className).not.toContain('bg-amber-50')
  })

  it('shows a dash in every owner cell when owners could not change answers', () => {
    review(
      judged({
        policy: { threshold: 50, ownersCanChangeAnswers: false },
        reviewerAnswers: { ...ALL_NO, integrations: true, ai_usage: true, public_data: true },
        ownerAnswers: null,
        reviewerScore: 60,
        score: 60,
        outcome: 'routed',
        reason: 'over_threshold',
        note: 'Needed for the vendor desk.',
      }),
    )

    const owners = screen.getAllByTestId(/^answer-/).map((row) => cellsOf(row)[3])
    expect(owners).toHaveLength(CLASSES.length)
    expect(new Set(owners)).toEqual(new Set(['—']))
    expect(screen.getByTestId('review-score').textContent).toContain('Score agent 60 (of 100)')
    expect(screen.getByTestId('review-score').textContent).toContain('Owners can change answers No')
  })

  it('shows a class under the title it was judged by, whatever it is called now', () => {
    review(
      judged({
        ...PII_FOUND,
        classes: [{ key: 'pii', title: 'Personal data (as judged)', kind: 'hard_block', weight: null }, ...CLASSES.slice(1)],
      }),
    )

    expect(screen.getByTestId('review-why').textContent).toContain('Personal data (as judged) found (hard block)')
    expect(cellsOf(screen.getByTestId('answer-pii'))[0]).toBe('Personal data (as judged)')
  })

  it('says an unfinished review left no answers and no score', () => {
    review(
      judged({
        review: { current: false, status: 'failed', failureCode: 'timeout' },
        reviewerAnswers: null,
        ownerAnswers: null,
        reviewerScore: null,
        score: null,
        outcome: 'routed',
        reason: 'review_unfinished',
        note: 'The check kept timing out.',
      }),
    )

    expect(screen.getByTestId('review-why').textContent).toContain("Why it is here: The agent's review did not finish")
    expect(cellsOf(screen.getByTestId('answer-integrations'))).toEqual(['Integrations', 'Scored · 20', '—', '—'])
    expect(screen.getByTestId('review-score').textContent).toContain('Score not recorded')
  })

  it('says a standing rejection is why it is here', () => {
    review(judged({ outcome: 'routed', reason: 'rejection_standing', note: 'The passport field is gone now.' }))

    expect(screen.getByTestId('review-why').textContent).toContain('Why it is here: An earlier version was rejected')
  })

  it('renders a six-question declaration in the queue as it was sent', () => {
    review({
      commits: { shipping: PENDING.commitSha, reviewed: PENDING.commitSha },
      citizen: { answers: { personal_information: false, public_data: true }, explanation: 'Staff names only.' },
      review: { answers: { personal_information: 'yes' }, reasons: { personal_information: 'Stores staff names.' } },
      merged: { answers: { personal_information: true, public_data: true } },
      differences: { personal_information: ['review_yes_over_citizen_no'] },
    })

    expect(screen.getByTestId('dispute-personal_information').textContent).toContain('Developer said No')
    expect(screen.getByTestId('review-explanation').textContent).toBe('Staff names only.')
    expect(screen.queryByTestId('review-why')).toBeNull()
    expect(screen.queryByTestId('review-score')).toBeNull()
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
