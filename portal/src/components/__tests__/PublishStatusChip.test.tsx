/**
 * The publish chip: one label per state, one sentence, at most one action.
 *
 * TWO KINDS OF TEST LIVE HERE, and the second kind is the point. The first is this
 * component's own contract. The second is the PARITY CHECKLIST — the guarantees the three
 * retired controls (`DeployControl`, `SubmitControl`, `PublishButton`) pinned across 48
 * cases, walked and re-established here rather than deleted with their suites. A swap
 * drops guarantees in both directions (L5), and the old suites were the checklist someone
 * would otherwise have had to write from scratch.
 *
 * Two of those 48 are deliberately NOT carried, each with a verdict rather than a shrug:
 *   · "a 503 on the status read renders nothing at all" (`DeployControl.test.tsx:326`).
 *     A chip that renders nothing is indistinguishable from a broken page, and this is
 *     now the only publishing surface the citizen has. It becomes the ordinary
 *     read-failure chip with a re-read.
 *   · the canvas's "YOUR LATEST" / saved-version rows. The server spends its one
 *     object-store metadata HEAD on the drift comparison and serves the answer, not the
 *     head, so no saved commit reaches this client to render.
 *
 * The hook is mocked at the module boundary; its own behaviour is covered by
 * `usePublishState.reconciliation.test.tsx`. The questionnaire is stubbed for the same
 * reason — `DataClassificationModal.test.tsx` owns it, and these tests are about what the
 * chip hands it and what it does with the answer.
 */
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor, within } from '@testing-library/react'

import type { ApprovalState, DeploymentView, PublishState } from '../../utils/deployApi'
import type { UseDeployment } from '../../hooks/useDeployment'

const h = vi.hoisted(() => ({
  useDeployment: vi.fn(),
  // The stub records what the chip handed the questionnaire, so a test can drive either
  // success back through the real `onConfirm` the chip supplied.
  modal: { current: null as null | { rejectionNote?: string | null; onConfirm: (a: never) => Promise<void> } },
}))
vi.mock('../../hooks/useDeployment', () => ({ useDeployment: h.useDeployment }))
vi.mock('../DataClassificationModal', () => ({
  default: (props: { rejectionNote?: string | null; onConfirm: (a: never) => Promise<void> }) => {
    h.modal.current = props
    return <div data-testid="data-classification-modal" />
  },
}))

const PublishStatusChip = (await import('../PublishStatusChip')).default

beforeAll(() => {
  // Radix's Popper measures its anchor with APIs jsdom does not implement. Without these
  // the popover throws on open and every case below fails for a reason that has nothing
  // to do with this component.
  globalThis.ResizeObserver ??= class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => {}
  Element.prototype.releasePointerCapture ??= () => {}
  Element.prototype.scrollIntoView ??= () => {}
})

const SHA = 'a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0'
const APPROVED_SHA = 'f9e8d7c6b5a4f9e8d7c6b5a4f9e8d7c6b5a4f9e8'

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

const view = (publishState: PublishState, over: Partial<DeploymentView> = {}): DeploymentView => ({
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
  approval: approval(),
  publishState,
  ...over,
})

const wire = (deployment: DeploymentView | null, over: Partial<UseDeployment> = {}): void => {
  h.useDeployment.mockReturnValue({
    deployment,
    approval: deployment?.approval ?? null,
    running: false,
    waitingForReview: false,
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

const mount = (): void => {
  render(<PublishStatusChip projectId="p1" />)
}

const openChip = async (): Promise<HTMLElement> => {
  fireEvent.click(screen.getByTestId('publish-chip'))
  return screen.findByTestId('publish-popover')
}

/** Every value the server can send, and the words this chip answers with. Written out
 *  rather than derived, so a label that changes has to change HERE too — a table that
 *  computed itself from the component would pin nothing. */
const LABELS: ReadonlyArray<readonly [PublishState, string]> = [
  ['nothing_built', 'Nothing built yet'],
  ['draft', 'Draft'],
  ['in_review', 'In review'],
  ['changes_requested', 'Changes requested'],
  ['approved_ready_to_publish', 'Approved'],
  ['approved_needs_review_again', 'Approved'],
  ['starting_up', 'Starting up'],
  ['live_current', 'Live'],
  ['live_newer_work', 'Live · newer work saved'],
  ['live_drift_unknown', "Live · couldn't check"],
  ['taken_offline', 'Taken offline'],
  ['switched_off', 'Switched off'],
  ['did_not_start', "Didn't start"],
]

beforeEach(() => {
  vi.clearAllMocks()
  h.modal.current = null
})
afterEach(cleanup)

// ── U2 — the chip's own words ───────────────────────────────────────────────────────

describe('the chip names the state, and the closed chip is a complete answer', () => {
  it('gives every value its own words, and the two approved values share one on purpose', () => {
    for (const [state, label] of LABELS) {
      wire(view(state))
      mount()
      expect(screen.getByTestId('publish-chip').textContent).toContain(label)
      cleanup()
    }

    // R-1.8: the approved pair is the ONE deliberate sharing — both are "their app is
    // approved" to a citizen, and R38 puts the difference on the button, not the label.
    // Every other pair is distinct, which is what makes the closed chip complete.
    const spoken = LABELS.map(([, label]) => label)
    const shared = spoken.filter((l, i) => spoken.indexOf(l) !== i)
    expect(shared).toEqual(['Approved'])
  })

  it('covers AE24 — the drift is in the chip itself, with the popover closed', () => {
    // Mutation receipt: fold `live_newer_work`'s label back to plain "Live" and this goes
    // red twice — on the visible text and on the accessible name.
    wire(view('live_newer_work'))
    mount()

    const chip = screen.getByTestId('publish-chip')
    expect(chip.textContent).toContain('newer work saved')
    expect(chip.getAttribute('aria-label')).toBe('Publish status: Live · newer work saved')
    expect(screen.queryByTestId('publish-popover')).toBeNull()
  })

  it('says a live app is up to date only when the server said so', () => {
    // The ordinary state of a published app with nothing newer saved — reachable because
    // the server's read makes the comparison against the saved snapshot's head.
    wire(view('live_current'))
    mount()

    const chip = screen.getByTestId('publish-chip')
    expect(chip.textContent).toContain('Live')
    expect(chip.textContent).not.toContain('newer work')
    expect(chip.textContent).not.toContain("couldn't check")
  })

  it('never speaks a comparison it could not make as either of the other two', () => {
    wire(view('live_drift_unknown'))
    mount()

    const chip = screen.getByTestId('publish-chip')
    expect(chip.textContent).toContain("couldn't check")
    expect(chip.textContent).not.toContain('newer work saved')
    // "Live" is in the label as a prefix; what must not happen is it standing alone.
    expect(chip.getAttribute('aria-label')).not.toBe('Publish status: Live')
  })

  it('changes its text on a re-read without the popover being opened', () => {
    wire(view('live_current'))
    const { rerender } = render(<PublishStatusChip projectId="p1" />)
    expect(screen.getByTestId('publish-chip').textContent).not.toContain('newer work')

    wire(view('live_newer_work'))
    rerender(<PublishStatusChip projectId="p1" />)

    expect(screen.getByTestId('publish-chip').textContent).toContain('newer work saved')
    expect(screen.queryByTestId('publish-popover')).toBeNull()
  })

  it('holds its place while the first read is still in flight, claiming no state', () => {
    wire(null)
    mount()

    expect(screen.getByTestId('publish-chip-pending')).toBeTruthy()
    expect(screen.queryByTestId('publish-chip')).toBeNull()
  })
})

// ── U3 — one sentence, the version row, at most one action ──────────────────────────

describe('the popover explains the state and offers at most one thing to do', () => {
  it('covers AE23 — switched off says an administrator did it, and offers nothing', async () => {
    wire(view('switched_off'))
    mount()
    const pop = await openChip()

    expect(pop.textContent).toContain('An administrator switched this app off')
    // Asserted by querying for ANY button, not by one label's absence: a state with
    // nothing to do has no control at all, not a disabled one.
    expect(within(pop).queryAllByRole('button')).toHaveLength(0)
  })

  it('opens one sentence and exactly one button from draft', async () => {
    wire(view('draft'))
    mount()
    const pop = await openChip()

    expect(pop.textContent).toContain('checked by an administrator before it goes live')
    expect(within(pop).getAllByRole('button')).toHaveLength(1)
    expect(screen.getByTestId('publish-action').textContent).toBe('Send for review')
  })

  it('renders exactly zero or one action for every value the server can send', async () => {
    const WITH_ACTION = new Set<PublishState>([
      'draft',
      'in_review',
      'changes_requested',
      'approved_ready_to_publish',
      'approved_needs_review_again',
      'live_newer_work',
      'live_drift_unknown',
      'taken_offline',
      'did_not_start',
    ])

    for (const [state] of LABELS) {
      wire(view(state, { url: 'https://pub-abc.example/' }))
      mount()
      const pop = await openChip()
      const buttons = within(pop).queryAllByRole('button')

      expect(buttons.length).toBeLessThanOrEqual(1)
      expect(buttons.length).toBe(WITH_ACTION.has(state) ? 1 : 0)
      // One sentence, always — a state that explained nothing would be a chip you press
      // for no reason.
      expect((pop.textContent ?? '').trim().length).toBeGreaterThan(0)
      cleanup()
    }
  })

  it('shows the live address, the reassurance and no action when nothing is waiting', async () => {
    wire(
      view('live_current', {
        url: 'https://visitor-log.apps.example/',
        headSha: SHA,
        finishedAt: '2026-08-20T09:14:00Z',
      }),
    )
    mount()
    const pop = await openChip()

    expect(pop.textContent).toContain('nothing of yours is waiting')
    expect(screen.getByTestId('publish-url').getAttribute('href')).toBe(
      'https://visitor-log.apps.example/',
    )
    expect(screen.getByTestId('publish-version').textContent).toContain('Live now')
    expect(within(pop).queryAllByRole('button')).toHaveLength(0)
  })

  it('explains why a drifted app has two versions, and offers exactly one action', async () => {
    wire(view('live_newer_work', { url: 'https://x.example/', headSha: SHA, finishedAt: '2026-08-20T09:14:00Z' }))
    mount()
    const pop = await openChip()

    expect(pop.textContent).toContain('pinned to one exact build')
    // Routing pins a submission and publishes nothing, so this reassurance is true.
    expect(pop.textContent).toContain('keeps serving the approved version')
    expect(within(pop).getAllByRole('button')).toHaveLength(1)
    expect(screen.getByTestId('publish-action').textContent).toBe('Send update for review')
  })

  it('says a failed comparison happened JUST NOW, never as a standing state', async () => {
    wire(view('live_drift_unknown', { url: 'https://x.example/' }))
    mount()
    const pop = await openChip()
    const text = pop.textContent ?? ''

    // Mutation receipt: rewrite this sentence as a property of the app or the platform
    // ("we cannot check whether…") and the momentary phrasing assertion goes red. It is
    // the rare arm — a storage blip or a pre-stamp bundle — and copy that reads as
    // permanent would be an apology a citizen sees on every visit, which it is not.
    expect(text).toMatch(/just now/i)
    expect(text).toMatch(/try again in a minute/i)
    expect(text).not.toContain('nothing of yours is waiting')
    expect(screen.getByTestId('publish-action').textContent).toBe('Send update for review')
  })

  it('promises nothing about routing on an approved app', async () => {
    wire(
      view('approved_ready_to_publish', {
        approval: approval({
          status: 'approved',
          approvalRoute: 'self_publish',
          approvedCommitSha: APPROVED_SHA,
          approvedAt: '2026-08-19T10:00:00Z',
        }),
      }),
    )
    mount()
    const pop = await openChip()
    const text = pop.textContent ?? ''

    expect(screen.getByTestId('publish-action').textContent).toBe('Publish')
    // Both phrasings the retired control used, and which this plan exists to stop making.
    expect(text).not.toMatch(/publish it yourself/i)
    expect(text).not.toMatch(/sent for approval once more/i)
    expect(screen.getByTestId('publish-version').textContent).toContain('Approved version')
    expect(screen.getByTestId('publish-version-sha').textContent).toBe(APPROVED_SHA.slice(0, 7))
  })

  it('tells the two approved states apart on the button and the sentence, not the label', async () => {
    wire(
      view('approved_needs_review_again', {
        approval: approval({
          status: 'approved',
          approvalRoute: 'runbook',
          approvedCommitSha: APPROVED_SHA,
          approvedAt: '2026-08-19T10:00:00Z',
        }),
      }),
    )
    mount()

    expect(screen.getByTestId('publish-chip').textContent).toContain('Approved')
    const pop = await openChip()
    expect(screen.getByTestId('publish-action').textContent).toBe('Send for review')
    expect(pop.textContent).toContain('goes back to an administrator')
  })

  it('does not link a taken-down address, and does not borrow the switched-off sentence', async () => {
    wire(
      view('taken_offline', {
        url: 'https://gone.example/',
        headSha: SHA,
        finishedAt: '2026-08-20T09:14:00Z',
        unpublishedAt: '2026-08-21T09:14:00Z',
      }),
    )
    mount()
    const pop = await openChip()

    // Mutation receipt: give `last_published` the url and this goes red. A dead address a
    // citizen can click is indistinguishable to them from an app that has broken.
    expect(screen.queryByTestId('publish-url')).toBeNull()
    expect(pop.textContent).toContain('taken this app offline')
    expect(pop.textContent).toContain('back at the same address')
    expect(pop.textContent).not.toContain('switched this app off')
    expect(screen.getByTestId('publish-action').textContent).toBe('Publish again')
    expect(screen.getByTestId('publish-version').textContent).toContain('Last published')
  })

  it('renders no link rather than an empty one when a live app has no address', async () => {
    wire(view('live_current', { url: null, headSha: SHA }))
    mount()
    await openChip()

    expect(screen.queryByTestId('publish-url')).toBeNull()
  })

  it('makes no claim about whether trying again returns to an administrator', async () => {
    wire(view('did_not_start'))
    mount()
    const pop = await openChip()

    expect(screen.getByTestId('publish-action').textContent).toBe('Try again')
    expect(pop.textContent).not.toMatch(/administrator/i)
  })

  it('puts the administrator note where a citizen reads it before acting', async () => {
    wire(
      view('changes_requested', {
        approval: approval({
          status: 'rejected',
          rejectionNote: 'Explain where the vendor key is stored.',
          submittedSha: SHA,
          submittedAt: '2026-08-19T10:00:00Z',
        }),
      }),
    )
    mount()
    const pop = await openChip()

    expect(screen.getByTestId('publish-rejection-note').textContent).toContain(
      'Explain where the vendor key is stored.',
    )
    expect(within(pop).getAllByRole('button')).toHaveLength(1)
  })

  it('carries no count anywhere — one metadata head names a commit, never a number', async () => {
    for (const state of ['live_newer_work', 'live_drift_unknown'] as const) {
      wire(view(state, { url: 'https://x.example/', headSha: SHA, finishedAt: '2026-08-20T09:14:00Z' }))
      mount()
      const pop = await openChip()
      const text = pop.textContent ?? ''

      expect(text).not.toMatch(/\d+\s+newer/i)
      expect(text).not.toMatch(/\d+\s+saves?\b/i)
      cleanup()
    }
  })
})

// ── U5 — what a press attempts, and what happened ──────────────────────────────────

describe('one press, one request, and the server says which success it was', () => {
  it('opens the questionnaire and hands it the note when there is one', async () => {
    wire(
      view('changes_requested', {
        approval: approval({ status: 'rejected', rejectionNote: 'Say more about the data.' }),
      }),
    )
    mount()
    await openChip()
    fireEvent.click(screen.getByTestId('publish-action'))

    expect(await screen.findByTestId('data-classification-modal')).toBeTruthy()
    expect(h.modal.current?.rejectionNote).toBe('Say more about the data.')
  })

  it('announces the started sentence when the deploy actually began', async () => {
    const onConfirm = vi.fn(async () => ({
      outcome: 'started' as const,
      deploymentId: 'd1',
      appId: 'app-1',
      status: 'running',
    }))
    wire(view('draft'), { onConfirm })
    mount()
    await openChip()
    fireEvent.click(screen.getByTestId('publish-action'))
    await screen.findByTestId('data-classification-modal')

    await h.modal.current!.onConfirm({} as never)

    await waitFor(() => {
      expect(screen.getByTestId('publish-announce').textContent).toMatch(/publishing now/i)
    })
  })

  it('renders a routed answer in the server own words, as a success and never as an alert', async () => {
    // Mutation receipt: give `publish-answer` a `role="alert"` or the failure colour and
    // this goes red. Routing is the platform doing exactly what the button said it would.
    const onConfirm = vi.fn(async () => ({
      outcome: 'routed_for_review' as const,
      appId: 'app-1',
      submissionId: 's1',
      commitSha: SHA,
      submittedAt: '2026-08-19T10:00:00Z',
      message: "Your app was sent to an administrator for review. You'll be able to publish it once approved.",
    }))
    wire(view('live_newer_work', { url: 'https://x.example/' }), { onConfirm })
    mount()
    await openChip()
    fireEvent.click(screen.getByTestId('publish-action'))
    await screen.findByTestId('data-classification-modal')

    await h.modal.current!.onConfirm({} as never)

    const answer = await screen.findByTestId('publish-answer')
    expect(answer.textContent).toContain('sent to an administrator for review')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(answer.className).not.toContain('danger')
    expect(screen.getByTestId('publish-announce').textContent).toContain('sent to an administrator')
  })

  it('reads a direct publish as a success even where the button said review', async () => {
    // The one thing this surface is deliberately not trusted to predict: which of the two
    // successes a press produces. The decision is taken inside the request, against a tree
    // a `saveFirst` can move first.
    const onConfirm = vi.fn(async () => ({
      outcome: 'started' as const,
      deploymentId: 'd1',
      appId: 'app-1',
      status: 'running',
    }))
    wire(view('live_drift_unknown', { url: 'https://x.example/' }), { onConfirm })
    mount()
    await openChip()
    expect(screen.getByTestId('publish-action').textContent).toBe('Send update for review')
    fireEvent.click(screen.getByTestId('publish-action'))
    await screen.findByTestId('data-classification-modal')

    await h.modal.current!.onConfirm({} as never)

    await waitFor(() => {
      expect(screen.getByTestId('publish-announce').textContent).toMatch(/publishing now/i)
    })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('offers Save and publish, and re-sends without reopening the questionnaire', async () => {
    const saveAndPublish = vi.fn(async () => null)
    wire(view('draft'), {
      unsaved: 'You have changes that are not saved yet.',
      saveAndPublish,
    })
    mount()

    // The question opens the popover itself — an answer the citizen is owed must not land
    // behind a closed one.
    const pop = await screen.findByTestId('publish-popover')
    expect(within(pop).getByTestId('publish-unsaved').textContent).toContain('not saved yet')
    fireEvent.click(screen.getByTestId('publish-save-and-publish'))

    expect(saveAndPublish).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('data-classification-modal')).toBeNull()
    // While the question stands, the ordinary action is not also on offer — R37 allows one.
    expect(screen.queryByTestId('publish-action')).toBeNull()
  })

  it('marks an in-flight action unavailable with a reason, and never hard-disables it', async () => {
    // R64 / D1 / KTD-2: disabling a control that has focus blurs it to `document.body`,
    // which is how a keyboard user loses their place mid-flight.
    wire(view('draft'), { unsaved: 'You have changes that are not saved yet.', saving: true })
    mount()
    await screen.findByTestId('publish-popover')

    const button = screen.getByTestId('publish-save-and-publish')
    button.focus()

    expect(button.isConnected).toBe(true)
    expect(button.getAttribute('aria-disabled')).toBe('true')
    expect(button.hasAttribute('disabled')).toBe(false)
    expect(button.getAttribute('title')).toBeTruthy()
    expect(button.textContent).toBe('Save and publish')
    expect(document.activeElement).toBe(button)
  })
})

// ── The read itself failing, and the one failure that is NOT a read failure ─────────

describe('a failed read has one honest presentation, and a storage blip is not one', () => {
  it('says the status is unavailable and offers a re-read, never a blank space', async () => {
    const refresh = vi.fn(async () => {})
    wire(null, { loadError: 'The server sent a publish state we could not read.', refresh })
    mount()

    const chip = screen.getByTestId('publish-chip')
    expect(chip.getAttribute('aria-label')).toBe('Publish status: unavailable')
    const pop = await openChip()
    expect(pop.textContent).toContain('could not read')

    fireEvent.click(screen.getByTestId('publish-recheck'))
    expect(refresh).toHaveBeenCalledTimes(1)
    // The one action is a re-read, NOT a publish — and it is deliberately not called
    // "Try again", which is what the didn't-start state's re-publish says.
    expect(screen.queryByTestId('publish-action')).toBeNull()
  })

  it('renders a server-side storage failure as an ordinary state with its own action', async () => {
    // The server degrades a storage error on the drift read to `live_drift_unknown` and
    // answers 200 (a named departure from ASM21, made because this is the only publishing
    // surface there is). So it must NOT reach the unavailable presentation.
    wire(view('live_drift_unknown', { url: 'https://x.example/' }))
    mount()

    expect(screen.getByTestId('publish-chip').getAttribute('aria-label')).not.toContain(
      'unavailable',
    )
    await openChip()
    expect(screen.queryByTestId('publish-recheck')).toBeNull()
    expect(screen.getByTestId('publish-action')).toBeTruthy()
  })
})

// ── Taking a submission back (P6) — the four cases the retired control pinned ───────

describe('taking a version back out of the queue', () => {
  const IN_REVIEW = view('in_review', {
    approval: approval({ status: 'pending', submittedSha: SHA, submittedAt: '2026-08-19T10:00:00Z' }),
  })

  it('asks once before it acts, because leaving the queue is not undoable by pressing again', async () => {
    const withdraw = vi.fn(async () => {})
    wire(IN_REVIEW, { withdraw })
    mount()
    await openChip()

    expect(screen.getByTestId('publish-action').textContent).toBe('Take it back')
    fireEvent.click(screen.getByTestId('publish-action'))

    expect(withdraw).not.toHaveBeenCalled()
    expect(screen.getByTestId('publish-withdraw-confirm')).toBeTruthy()
  })

  it('calls the hook once the citizen confirms', async () => {
    const withdraw = vi.fn(async () => {})
    wire(IN_REVIEW, { withdraw })
    mount()
    await openChip()
    fireEvent.click(screen.getByTestId('publish-action'))
    fireEvent.click(screen.getByTestId('publish-withdraw-yes'))

    expect(withdraw).toHaveBeenCalledTimes(1)
  })

  it('backs out without acting', async () => {
    const withdraw = vi.fn(async () => {})
    wire(IN_REVIEW, { withdraw })
    mount()
    await openChip()
    fireEvent.click(screen.getByTestId('publish-action'))
    fireEvent.click(screen.getByTestId('publish-withdraw-no'))

    expect(withdraw).not.toHaveBeenCalled()
    expect(screen.getByTestId('publish-action')).toBeTruthy()
  })

  it('marks the confirm unavailable while the withdrawal is in flight', async () => {
    // R64 / D1 / KTD-2 on the one button the chip itself fires a request from. Driven in
    // the order it actually happens — confirm first, THEN in flight — because the request
    // cannot start before the confirm exists.
    wire(IN_REVIEW, { withdrawing: false })
    const { rerender } = render(<PublishStatusChip projectId="p1" />)
    await openChip()
    fireEvent.click(screen.getByTestId('publish-action'))

    wire(IN_REVIEW, { withdrawing: true })
    rerender(<PublishStatusChip projectId="p1" />)

    const yes = screen.getByTestId('publish-withdraw-yes')
    yes.focus()
    expect(yes.isConnected).toBe(true)
    expect(yes.getAttribute('aria-disabled')).toBe('true')
    expect(yes.hasAttribute('disabled')).toBe(false)
    expect(yes.getAttribute('title')).toBeTruthy()
    expect(yes.textContent).toBe('Take it back')
    expect(document.activeElement).toBe(yes)
  })

  it('will not stack a second request on top of one already in flight', async () => {
    // The affordance is `aria-disabled`; THIS is the enforcement. A control that only
    // looks unavailable is a control that fires twice on a double press.
    const withdraw = vi.fn(async () => {})
    wire(IN_REVIEW, { withdraw, withdrawing: true })
    mount()
    await openChip()
    fireEvent.click(screen.getByTestId('publish-action'))

    expect(screen.queryByTestId('publish-withdraw-confirm')).toBeNull()
    expect(withdraw).not.toHaveBeenCalled()
  })

  it('renders a refused withdrawal in the server own words', async () => {
    wire(IN_REVIEW, { withdrawError: 'An administrator has already decided this one.' })
    mount()
    const pop = await openChip()

    expect(within(pop).getByRole('alert').textContent).toContain('already decided')
  })

  it('shows which version is with an administrator, and when it went', async () => {
    wire(IN_REVIEW)
    mount()
    await openChip()

    const row = screen.getByTestId('publish-version')
    expect(row.textContent).toContain('Sent for review')
    expect(screen.getByTestId('publish-version-sha').textContent).toBe(SHA.slice(0, 7))
  })
})

// ── Parity guards carried from the three retired suites ────────────────────────────

describe('guarantees carried over from the controls this chip replaces', () => {
  it('claims nowhere, in any state, that the platform team deploys an approved app', async () => {
    for (const [state] of LABELS) {
      wire(view(state, { url: 'https://x.example/' }))
      mount()
      await openChip()
      const text = document.body.textContent ?? ''

      expect(text).not.toMatch(/platform team/i)
      expect(text).not.toMatch(/deployed by/i)
      expect(text).not.toMatch(/ask an administrator/i)
      cleanup()
    }
  })

  it('speaks the word "deploy" to nobody', async () => {
    for (const [state] of LABELS) {
      wire(view(state, { url: 'https://x.example/' }))
      mount()
      await openChip()

      expect(document.body.textContent ?? '').not.toMatch(/deploy/i)
      cleanup()
    }
  })

  it('renders ONE chip, never two badges that could disagree', () => {
    // The bug the old surface had structurally: a running deploy carrying a takedown
    // stamp rendered two contradictory pills. There is one node now, and `getByTestId`
    // throws on a duplicate, which IS the assertion.
    wire(view('taken_offline', { status: 'running', unpublishedAt: '2026-08-21T09:14:00Z' }))
    mount()

    expect(screen.getByTestId('publish-chip').textContent).toContain('Taken offline')
  })

  it('never renders the retired pipeline vocabulary while a publish runs', async () => {
    wire(view('starting_up', { status: 'running', step: 'packing' }))
    mount()
    const pop = await openChip()
    const text = (pop.textContent ?? '') + screen.getByTestId('publish-chip').textContent

    for (const phase of ['Getting ready', 'Packaging your app', 'Setting up the server', 'Starting it up', 'Working']) {
      expect(text).not.toContain(phase)
    }
    expect(screen.getByTestId('publish-chip').textContent).toContain('Starting up')
  })

  it('points at no review-status anchor — there is no second card to point at', async () => {
    wire(view('in_review', { approval: approval({ status: 'pending', submittedSha: SHA }) }))
    mount()
    const pop = await openChip()

    expect(pop.innerHTML).not.toContain('review-status')
  })
})
