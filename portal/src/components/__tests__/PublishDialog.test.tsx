/**
 * The owner's publish dialog: the reviewer's answers, the owner's corrections where the policy
 * allows them, a live score, and a note whenever the send goes to an administrator.
 *
 * The score shown here decides nothing — the server re-reads the stored review — so what is pinned
 * is that the dialog says what the server will do, and that it recovers when the server disagrees.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, within } from '@testing-library/react'

import PublishDialog from '../PublishDialog'
import { ApiError } from '../../utils/apiError'
import * as classificationApi from '../../utils/classificationApi'
import * as projectApi from '../../utils/projectApi'
import type { ClassificationReview, ReviewClass } from '../../utils/classificationApi'
import type { DeploymentView, PublishAnswers, PublishState } from '../../utils/deployApi'
import type { Project } from '../../utils/projectApi'

vi.mock('../../utils/classificationApi', async () => {
  const actual = await vi.importActual<typeof classificationApi>('../../utils/classificationApi')
  return { ...actual, ensureClassificationReview: vi.fn(), getClassificationReview: vi.fn() }
})
vi.mock('../../utils/projectApi', async () => {
  const actual = await vi.importActual<typeof projectApi>('../../utils/projectApi')
  return { ...actual, getProject: vi.fn() }
})

const ensureReview = vi.mocked(classificationApi.ensureClassificationReview)
const getReview = vi.mocked(classificationApi.getClassificationReview)
const getProject = vi.mocked(projectApi.getProject)

const SHA = '3f2a9c1d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d'
const LIVE_SHA = '2626e9b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7'

const CLASSES: ReviewClass[] = [
  { key: 'pii', title: 'PII', kind: 'hard_block', weight: null },
  { key: 'financial_data', title: 'Financial data', kind: 'hard_block', weight: null },
  { key: 'credentials_keys', title: 'Credentials & keys', kind: 'scored', weight: 20 },
  { key: 'confidential_business_data', title: 'Confidential business data', kind: 'scored', weight: 20 },
  { key: 'ai_usage', title: 'AI usage', kind: 'scored', weight: 20 },
  { key: 'integrations', title: 'Integrations', kind: 'scored', weight: 20 },
  { key: 'public_data', title: 'Public data', kind: 'scored', weight: 20 },
]
const SCORED_KEYS = CLASSES.filter((c) => c.kind === 'scored').map((c) => c.key)

const REASONS: Record<string, string> = {
  pii: "Stores a photo of each visitor's government ID (the upload on the visitor form).",
  confidential_business_data: 'shows vendor contract rates (rates.tsx).',
  ai_usage: 'summarises comments with an AI model (summarise.ts).',
  integrations: 'sends messages to an outside address (notify.ts).',
}

function localIso(day: number, hour: number, minute: number): string {
  return new Date(2026, 8, day, hour, minute).toISOString()
}

function readout({
  yes = [],
  threshold = 100,
  owners = true,
  classes = CLASSES,
  ...over
}: {
  yes?: string[]
  threshold?: number
  owners?: boolean
  classes?: ReviewClass[]
} & Partial<ClassificationReview> = {}): ClassificationReview {
  return {
    status: 'complete',
    policy: { threshold, ownersCanChangeAnswers: owners },
    classes,
    headSha: SHA,
    savedAt: localIso(26, 14, 5),
    reviewedSha: SHA,
    checkedAt: localIso(26, 14, 6),
    current: true,
    verdicts: Object.fromEntries(
      classes.map((c) => [
        c.key,
        yes.includes(c.key)
          ? { verdict: 'yes' as const, reason: REASONS[c.key] ?? `found ${c.key}.` }
          : { verdict: 'no' as const, reason: 'We found no sign of this.' },
      ]),
    ),
    failureCode: null,
    failureMessage: null,
    retryable: false,
    ...over,
  }
}

const RUNNING = readout({ status: 'running', checkedAt: null, verdicts: null })
const FAILED_RETRYABLE = readout({
  status: 'failed',
  verdicts: null,
  failureCode: 'review_failed',
  failureMessage: "The automatic check couldn't run.",
  retryable: true,
})
const OUT_OF_ATTEMPTS = { ...FAILED_RETRYABLE, retryable: false }

function deployment(publishState: PublishState, over: Partial<DeploymentView> = {}): DeploymentView {
  return {
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
    publishState,
    approvedRetryCommit: null,
    savedHead: null,
    savedAt: null,
    savedState: null,
    ...over,
  }
}

const LIVE = deployment('live_newer_work', {
  deploymentId: 'd1',
  status: 'succeeded',
  url: 'https://apps.example/a/pub-1',
  headSha: LIVE_SHA,
  finishedAt: localIso(22, 9, 30),
})

type OnConfirm = (commitSha: string, send: PublishAnswers) => Promise<void>

async function open({
  onConfirm = vi.fn<OnConfirm>(async () => {}),
  onCancel = vi.fn(),
  rejectionNote = null,
  live = LIVE,
}: {
  onConfirm?: OnConfirm
  onCancel?: () => void
  rejectionNote?: string | null
  live?: DeploymentView | null
} = {}) {
  render(
    <PublishDialog
      projectId="p1"
      deployment={live}
      rejectionNote={rejectionNote}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />,
  )
  await act(async () => {})
  return { onConfirm, onCancel }
}

const dialog = (): HTMLElement => screen.getByTestId('publish-dialog')
const confirm = (): HTMLButtonElement => {
  const button = screen.getByTestId('pd-confirm')
  if (!(button instanceof HTMLButtonElement)) throw new Error('pd-confirm is not a button')
  return button
}
const row = (key: string): HTMLElement => screen.getByTestId(`pd-class-${key}`)
const choice = (key: string, label: 'Yes' | 'No'): HTMLElement =>
  within(screen.getByTestId(`pd-toggle-${key}`)).getByRole('radio', { name: label })
const score = (): string => screen.getByTestId('pd-score').textContent ?? ''
const typeNote = (text: string): void => {
  fireEvent.change(screen.getByTestId('pd-note'), { target: { value: text } })
}

function noteRequired(reason: string): ApiError {
  return new ApiError(
    'This app needs an administrator. Add a note for them before sending it.',
    422,
    'note_required',
    { code: 'note_required', detail: { reason } },
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  getProject.mockResolvedValue({ id: 'p1', name: 'Visitor Feedback' } as Project)
  ensureReview.mockResolvedValue(readout())
  getReview.mockResolvedValue(readout())
})

afterEach(() => {
  vi.useRealTimers()
})

describe('an owner who may change the reviewer answers', () => {
  it('pre-fills each answer with the reviewer own, and moves the score as the owner corrects one', async () => {
    ensureReview.mockResolvedValue(
      readout({ threshold: 50, yes: ['confidential_business_data', 'ai_usage', 'integrations'] }),
    )
    await open()

    expect(screen.getByRole('heading', { name: 'Publish Visitor Feedback' })).toBeTruthy()
    expect(choice('integrations', 'Yes').getAttribute('aria-checked')).toBe('true')
    expect(choice('credentials_keys', 'No').getAttribute('aria-checked')).toBe('true')
    expect(score()).toContain('60/100')
    expect(score()).toContain('Over 50: this goes to an administrator')
    expect(confirm().textContent).toBe('Send for review')

    fireEvent.click(choice('integrations', 'No'))

    expect(choice('integrations', 'No').getAttribute('aria-checked')).toBe('true')
    expect(score()).toContain('40/100')
    expect(score()).toContain('Publishes by itself at 50 or below')
    expect(score()).toContain(
      "No administrator needed. The reviewer's answers and yours are both kept on record.",
    )
    expect(row('integrations').className).toContain('bg-amber-50')
    expect(row('integrations').textContent).toContain('changed by the owner')
    expect(row('ai_usage').textContent).not.toContain('changed by the owner')
    expect(confirm().textContent).toBe('Publish')
    expect(screen.queryByTestId('pd-note')).toBeNull()
  })

  it('shows the reviewer reason only on the classes it answered Yes', async () => {
    ensureReview.mockResolvedValue(readout({ yes: ['ai_usage'] }))
    await open()

    expect(row('ai_usage').textContent).toContain(
      'Reviewer: summarises comments with an AI model (summarise.ts).',
    )
    expect(row('credentials_keys').textContent).toBe('Credentials & keysNoYes')
    expect(dialog().textContent).not.toContain('We found no sign of this.')
  })

  it('locks the hard blocks as results the owner cannot change', async () => {
    await open()

    expect(dialog().textContent).toContain("Checked by the reviewer · you can't change these")
    expect(within(row('pii')).getByText('Not found')).toBeTruthy()
    expect(within(row('pii')).queryAllByRole('radio')).toHaveLength(0)
    expect(within(row('ai_usage')).getAllByRole('radio')).toHaveLength(2)
  })

  it('reads the score without a limit line at the seeded threshold of 100', async () => {
    ensureReview.mockResolvedValue(readout({ yes: ['ai_usage'] }))
    await open()

    expect(score()).toContain('Score 20/100')
    expect(score()).toContain('Publishes by itself. No administrator needed.')
    expect(score()).not.toMatch(/or below|Over /)
  })

  it('publishes with an answer for every scored class and no note', async () => {
    ensureReview.mockResolvedValue(readout({ yes: ['ai_usage', 'integrations'] }))
    const { onConfirm } = await open()

    fireEvent.click(choice('integrations', 'No'))
    await act(async () => {
      fireEvent.click(confirm())
    })

    expect(onConfirm).toHaveBeenCalledWith(SHA, {
      answers: {
        credentials_keys: false,
        confidential_business_data: false,
        ai_usage: true,
        integrations: false,
        public_data: false,
      },
      note: null,
    })
  })

  it('needs a note once the owner answers carry the score over the threshold', async () => {
    ensureReview.mockResolvedValue(readout({ threshold: 50, yes: ['ai_usage', 'integrations'] }))
    const { onConfirm } = await open()

    fireEvent.click(choice('public_data', 'Yes'))

    expect(screen.getByLabelText(/A note for the administrator/)).toBeTruthy()
    expect(confirm().disabled).toBe(true)
    typeNote('The Zoho connection only reads our own vendor list.')
    expect(confirm().disabled).toBe(false)
    await act(async () => {
      fireEvent.click(confirm())
    })
    expect(onConfirm).toHaveBeenCalledWith(SHA, {
      answers: expect.objectContaining({ public_data: true }) as Record<string, boolean>,
      note: 'The Zoho connection only reads our own vendor list.',
    })
  })
})

describe('a hard block the reviewer found', () => {
  it('leads with the block, hides the scored classes, and sends only once the note has text', async () => {
    ensureReview.mockResolvedValue(readout({ threshold: 50, yes: ['pii', 'ai_usage'] }))
    const { onConfirm } = await open()

    expect(screen.getByRole('heading', { name: 'Visitor Feedback needs an administrator' })).toBeTruthy()
    expect(dialog().textContent).toContain(
      'It handles data only an administrator can approve. Tell them why, then send it for review.',
    )
    expect(within(row('pii')).getByText('Found')).toBeTruthy()
    expect(row('pii').textContent).toContain(REASONS.pii)
    for (const key of SCORED_KEYS) expect(screen.queryByTestId(`pd-class-${key}`)).toBeNull()
    expect(screen.queryByTestId('pd-score')).toBeNull()
    expect(screen.getByLabelText(/Why does this app need this data\?/)).toBeTruthy()
    expect(dialog().textContent).toContain(
      'The administrator reads this first. Once they approve, the app publishes by itself.',
    )

    expect(confirm().textContent).toBe('Send for review')
    expect(confirm().disabled).toBe(true)
    typeNote('   ')
    expect(confirm().disabled).toBe(true)
    typeNote('Security keeps an ID copy for every airside visitor, for 30 days.')
    expect(confirm().disabled).toBe(false)

    await act(async () => {
      fireEvent.click(confirm())
    })
    expect(onConfirm).toHaveBeenCalledWith(SHA, {
      answers: {},
      note: 'Security keeps an ID copy for every airside visitor, for 30 days.',
    })
  })
})

describe('an owner who may not change the reviewer answers', () => {
  it('shows every answer as a locked result, and needs a note over the threshold', async () => {
    ensureReview.mockResolvedValue(
      readout({
        threshold: 50,
        owners: false,
        yes: ['confidential_business_data', 'ai_usage', 'integrations'],
      }),
    )
    const { onConfirm } = await open()

    expect(screen.queryAllByRole('radio')).toHaveLength(0)
    expect(dialog().textContent).toContain('Scored by the reviewer · locked by your administrator')
    expect(within(row('ai_usage')).getByText('Yes')).toBeTruthy()
    expect(within(row('credentials_keys')).getByText('No')).toBeTruthy()
    expect(dialog().textContent).toContain('The reviewer checked this version.')
    expect(dialog().textContent).not.toContain('Correct any answer it got wrong.')
    expect(score()).toContain('60/100')
    expect(score()).toContain('Over 50: this goes to an administrator')
    expect(score()).toContain("Answers are locked, so the reviewer's answers set the score.")

    expect(confirm().textContent).toBe('Send for review')
    expect(confirm().disabled).toBe(true)
    typeNote('The Zoho connection only reads our own vendor list.')
    await act(async () => {
      fireEvent.click(confirm())
    })
    expect(onConfirm).toHaveBeenCalledWith(SHA, {
      answers: {},
      note: 'The Zoho connection only reads our own vendor list.',
    })
  })
})

describe('while the reviewer runs, and when it fails', () => {
  it('shows the waiting line where the classes go, keeps Publish disabled, and Cancel still closes', async () => {
    ensureReview.mockResolvedValue(RUNNING)
    const { onCancel } = await open()

    expect(screen.getByTestId('pd-status').textContent).toContain(
      'We’re checking your saved app for the kinds of data it handles.',
    )
    expect(screen.queryByTestId('pd-class-ai_usage')).toBeNull()
    expect(confirm().textContent).toBe('Publish')
    expect(confirm().disabled).toBe(true)

    fireEvent.click(screen.getByTestId('pd-cancel'))
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('fills the answers in when the poll finds the review landed', async () => {
    vi.useFakeTimers()
    ensureReview.mockResolvedValue(RUNNING)
    getReview.mockResolvedValue(readout({ yes: ['ai_usage'] }))
    await open()
    expect(screen.queryByTestId('pd-class-ai_usage')).toBeNull()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })

    expect(choice('ai_usage', 'Yes').getAttribute('aria-checked')).toBe('true')
    expect(confirm().disabled).toBe(false)
  })

  it('offers Check again while attempts remain, then only Send for review with a note', async () => {
    ensureReview.mockResolvedValueOnce(FAILED_RETRYABLE).mockResolvedValueOnce(OUT_OF_ATTEMPTS)
    const { onConfirm } = await open()

    expect(screen.getByTestId('pd-status').textContent).toContain("The automatic check couldn't run.")
    expect(confirm().disabled).toBe(true)
    expect(screen.queryByTestId('pd-note')).toBeNull()

    await act(async () => {
      fireEvent.click(screen.getByTestId('pd-recheck'))
    })

    expect(ensureReview).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('pd-recheck')).toBeNull()
    expect(screen.getByTestId('pd-status').textContent).toContain("The automatic check couldn't run.")
    expect(confirm().textContent).toBe('Send for review')
    expect(confirm().disabled).toBe(true)
    typeNote('The check keeps failing; the app only lists flight gates.')
    await act(async () => {
      fireEvent.click(confirm())
    })
    expect(onConfirm).toHaveBeenCalledWith(SHA, {
      answers: {},
      note: 'The check keeps failing; the app only lists flight gates.',
    })
  })
})

describe('when the server answers differently', () => {
  it('starts a fresh review when a class changed after the reviewer ran, before anything publishes', async () => {
    const added: ReviewClass = { key: 'file_uploads', title: 'File uploads', kind: 'scored', weight: 20 }
    ensureReview
      .mockResolvedValueOnce(readout())
      .mockResolvedValueOnce(readout({ classes: [...CLASSES, added] }))
    getReview.mockResolvedValue(readout({ current: false, verdicts: null }))
    const onConfirm = vi.fn<OnConfirm>(async () => {
      throw noteRequired('review_unfinished')
    })
    await open({ onConfirm })
    expect(screen.queryByText('File uploads')).toBeNull()

    await act(async () => {
      fireEvent.click(confirm())
    })

    expect(getReview).toHaveBeenCalledTimes(1)
    expect(ensureReview).toHaveBeenCalledTimes(2)
    expect(row('file_uploads').textContent).toContain('File uploads')
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('asks for a note when the send needs one and the review still stands', async () => {
    const onConfirm = vi
      .fn<OnConfirm>()
      .mockRejectedValueOnce(noteRequired('rejection_standing'))
      .mockResolvedValueOnce(undefined)
    await open({ onConfirm })
    expect(confirm().textContent).toBe('Publish')

    await act(async () => {
      fireEvent.click(confirm())
    })

    expect(ensureReview).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('pd-error').textContent).toContain('Add a note for them')
    expect(confirm().textContent).toBe('Send for review')
    expect(score()).toContain('This goes to an administrator')
    typeNote('The rates are now read-only.')
    await act(async () => {
      fireEvent.click(confirm())
    })
    expect(onConfirm).toHaveBeenLastCalledWith(SHA, {
      answers: expect.any(Object) as Record<string, boolean>,
      note: 'The rates are now read-only.',
    })
  })

  it('starts again on the version saved now when the save moved, keeping the note', async () => {
    const NEXT = 'b'.repeat(40)
    ensureReview
      .mockResolvedValueOnce(readout({ threshold: 50, yes: ['ai_usage', 'integrations', 'public_data'] }))
      .mockResolvedValueOnce(
        readout({
          threshold: 50,
          yes: ['ai_usage', 'integrations', 'public_data'],
          headSha: NEXT,
          reviewedSha: NEXT,
        }),
      )
    const onConfirm = vi.fn<OnConfirm>(async () => {
      throw new ApiError('Your app was saved again while this request was being decided.', 409, 'snapshot_moved')
    })
    await open({ onConfirm })
    typeNote('Kept across the reload.')
    fireEvent.click(choice('public_data', 'No'))
    expect(confirm().textContent).toBe('Publish')

    await act(async () => {
      fireEvent.click(confirm())
    })

    expect(ensureReview).toHaveBeenCalledTimes(2)
    expect(screen.getByTestId('pd-error').textContent).toContain('saved again')
    expect(choice('public_data', 'Yes').getAttribute('aria-checked')).toBe('true')
    expect(screen.getByTestId('pd-version').textContent).toContain('bbbbbbb')
    expect((screen.getByTestId('pd-note') as HTMLTextAreaElement).value).toBe('Kept across the reload.')
  })

  it('leads with an administrator note when the app was sent back, and asks for a note in reply', async () => {
    await open({ rejectionNote: 'Say which vendor data this shows.' })

    const note = screen.getByTestId('pd-rejection-note')
    expect(note.textContent).toContain('An administrator sent this back')
    expect(note.textContent).toContain('Say which vendor data this shows.')
    expect(
      note.compareDocumentPosition(screen.getByTestId('pd-version')) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    expect(confirm().textContent).toBe('Send for review')
    expect(screen.getByTestId('pd-note')).toBeTruthy()
  })
})

describe('what an owner is shown about a class', () => {
  it('shows the title alone: no description, no help icon, no tooltip', async () => {
    const described = CLASSES.map((c) => ({
      ...c,
      description: 'Yes if the airside roster names a gate marshal.',
    }))
    ensureReview.mockResolvedValue(readout({ classes: described, yes: ['ai_usage'] }))
    await open()

    expect(row('credentials_keys').textContent).toContain('Credentials & keys')
    expect(row('pii').textContent).toContain('PII')
    expect(document.body.textContent).not.toContain('gate marshal')
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(screen.queryByRole('button', { name: /what counts/i })).toBeNull()
  })
})

describe('the version being published', () => {
  it('names the version, when it was saved and checked, and what is live now', async () => {
    await open()

    const strip = screen.getByTestId('pd-version').textContent ?? ''
    expect(strip).toContain('PublishingVersion 3f2a9c1')
    expect(strip).toContain('Saved 26 Sep, 14:05 · checked 14:06')
    expect(strip).toContain('Live nowVersion 2626e9b')
    expect(strip).toContain('Published 22 Sep')
  })

  it('says this will be the first published version when nothing was ever published', async () => {
    await open({ live: deployment('draft') })

    const strip = screen.getByTestId('pd-version').textContent ?? ''
    expect(strip).toContain('Version 3f2a9c1')
    expect(strip).toContain('Not live yet')
    expect(strip).toContain('This will be the first published version')
  })
})
