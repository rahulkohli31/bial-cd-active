/**
 * The publish chip: one label per state, one sentence, at most one action. This file
 * also carries the PARITY CHECKLIST inherited from the three retired publish controls
 * it replaced — walked here rather than deleted with their suites.
 *
 * Two of those guarantees are deliberately NOT carried: a 503 on the status read now
 * becomes the ordinary read-failure chip with a re-read (not a blank state), and the
 * saved-version rows moved to the rail's `AppStatusPanel.test.tsx`, which owns them
 * now.
 *
 * The hook is mocked at the module boundary (see
 * `usePublishState.reconciliation.test.tsx`); the questionnaire is stubbed too
 * (`DataClassificationModal.test.tsx` owns it).
 */
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor, within } from '@testing-library/react'

import type { ApprovalState, DeploymentView, PublishState } from '../../utils/deployApi'
import type { UsePublishState } from '../../hooks/usePublishState'
import { lookFor, presentationFor } from '../../utils/publishPresentation'

const h = vi.hoisted(() => ({
  usePublishState: vi.fn(),
  // The stub records what the chip handed the questionnaire, so a test can drive either
  // success back through the real `onConfirm` the chip supplied.
  modal: {
    current: null as null | {
      rejectionNote?: string | null
      alreadyApproved?: boolean
      onConfirm: (a: never) => Promise<void>
    },
  },
}))
vi.mock('../../hooks/usePublishState', () => ({ usePublishState: h.usePublishState }))
vi.mock('../DataClassificationModal', () => ({
  default: (props: {
    rejectionNote?: string | null
    alreadyApproved?: boolean
    onConfirm: (a: never) => Promise<void>
  }) => {
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
  savedHead: null,
  savedAt: null,
  // `null` is "the server did not say", which keeps the saved row — the neutral default
  // for suites that are not about the never-saved-state omission.
  savedState: null,
  ...over,
})

const wire = (deployment: DeploymentView | null, over: Partial<UsePublishState> = {}): void => {
  h.usePublishState.mockReturnValue({
    deployment,
    approval: deployment?.approval ?? null,
    loadError: null,
    refresh: vi.fn(),
    unsaved: null,
    saving: false,
    onConfirm: vi.fn(),
    saveAndPublish: vi.fn(),
    dismissUnsaved: vi.fn(),
    withdraw: vi.fn(),
    withdrawing: false,
    withdrawError: null,
    ...over,
  } satisfies UsePublishState)
}

const mount = (): void => {
  render(<PublishStatusChip projectId="p1" />)
}

const openChip = async (): Promise<HTMLElement> => {
  fireEvent.click(screen.getByTestId('publish-chip'))
  return screen.findByTestId('publish-popover')
}

/**
 * Keyed by the union and turned into rows here, so a value added to `PublishState` and not
 * to this table is a type error — an array annotated `readonly PublishState[]` would be
 * satisfied by any subset, letting a new state drop silently out of every walk below.
 */
const rowsFor = <T,>(table: Record<PublishState, T>): ReadonlyArray<readonly [PublishState, T]> =>
  Object.entries(table) as ReadonlyArray<readonly [PublishState, T]>

/** Every value the server can send, and the words this chip answers with. Written out
 *  rather than derived, so a label that changes has to change HERE too. */
const LABELS = rowsFor({
  nothing_built: 'Nothing built yet',
  draft: 'Draft',
  in_review: 'In review',
  changes_requested: 'Changes requested',
  approved_ready_to_publish: 'Approved',
  approved_needs_review_again: 'Approved',
  starting_up: 'Starting up',
  live_current: 'Live',
  live_newer_work: 'Live · newer work saved',
  live_drift_unknown: "Live · couldn't check",
  taken_offline: 'Taken offline',
  switched_off: 'Switched off',
  did_not_start: "Didn't start",
})

beforeEach(() => {
  vi.clearAllMocks()
  h.modal.current = null
})
afterEach(cleanup)

/**
 * Colour carries the state signal, so it is asserted on class names — jsdom computes no
 * Tailwind styles, so a `getComputedStyle` check could not tell amber from grey here.
 */
const EXPECTED_LOOK = rowsFor({
  nothing_built: 'faint',
  draft: 'grey',
  in_review: 'amber',
  changes_requested: 'red',
  did_not_start: 'red',
  approved_ready_to_publish: 'green',
  approved_needs_review_again: 'green',
  starting_up: 'green',
  live_current: 'green',
  live_newer_work: 'green',
  live_drift_unknown: 'green',
  taken_offline: 'off',
  switched_off: 'off',
})

describe('the chip is coloured by its state, with a leading dot', () => {
  it('gives each state the board\'s own colour pair', () => {
    for (const [state, family] of EXPECTED_LOOK) {
      wire(view(state))
      mount()
      const chip = screen.getByTestId('publish-chip')
      expect(chip.className, state).toContain(`text-status-${family}-fg`)
      expect(chip.className, state).toContain(`bg-status-${family}-bg`)
      expect(chip.querySelector(`.bg-status-${family}-dot`), state).not.toBeNull()
      cleanup()
    }
  })

  it('★ no two states that mean different things share a colour AND a word', () => {
    // Reads the state off `lookFor`, the module under test — not off `EXPECTED_LOOK`/`LABELS`
    // above, which would compare the fixtures with themselves and stay green through a real
    // regression.
    //
    // Mutation receipt: return one shared look from `lookFor` and the family count below
    // goes red.
    const seen = new Map<string, string>()
    const families = new Set<string>()
    for (const [state] of LABELS) {
      const { pill } = lookFor(state)
      const family = /bg-status-([a-z]+)-bg/.exec(pill)?.[1] ?? pill
      families.add(family)
      const key = `${family}|${presentationFor(state).label}`
      const clash = seen.get(key)
      // The two `Approved` states DO share both, deliberately — they are the same state to a
      // citizen and the difference is on the button. Nothing else may.
      if (clash) expect([clash, state].sort()).toEqual(['approved_needs_review_again', 'approved_ready_to_publish'])
      seen.set(key, state)
    }
    expect(seen.size).toBe(LABELS.length - 1)
    expect(families.size).toBeGreaterThan(4)
  })

  it('is a 999px pill, not the rounded-md box it used to be', () => {
    wire(view('draft'))
    mount()
    expect(screen.getByTestId('publish-chip').className).toContain('rounded-full')
    expect(screen.getByTestId('publish-chip').className).not.toContain('rounded-md')
  })
})

describe('the chip names the state, and the closed chip is a complete answer', () => {
  it('gives every value its own words, and the two approved values share one on purpose', () => {
    for (const [state, label] of LABELS) {
      wire(view(state))
      mount()
      expect(screen.getByTestId('publish-chip').textContent).toContain(label)
      cleanup()
    }

    // The approved pair is the ONE deliberate sharing — both are "their app is
    // approved" to a citizen, and the difference is put on the button, not the label.
    // Every other pair is distinct, which is what makes the closed chip complete.
    const spoken = LABELS.map(([, label]) => label)
    const shared = spoken.filter((l, i) => spoken.indexOf(l) !== i)
    expect(shared).toEqual(['Approved'])
  })

  it('the drift is in the chip itself, with the popover closed', () => {
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

  it('announces a state that arrives on its own, not only one the citizen pressed for', () => {
    // Both retired controls announced state changes that arrived without a press (an
    // approval overnight, a publish from another tab) — a region filled only on press is
    // silent for those.
    //
    // Mutation receipt: make the region's text `answer ?? ''` again and this goes red on
    // the very first assertion.
    wire(view('in_review'))
    const { rerender } = render(<PublishStatusChip projectId="p1" />)

    const region = screen.getByTestId('publish-announce')
    expect(region.getAttribute('role')).toBe('status')
    expect(region.getAttribute('aria-live')).toBe('polite')
    expect(region.textContent).toContain('In review')

    wire(view('switched_off'))
    rerender(<PublishStatusChip projectId="p1" />)

    expect(screen.getByTestId('publish-announce').textContent).toContain('Switched off')
  })

  it('keeps the region mounted and empty before there is anything to say', () => {
    // A region injected together with its text is frequently not announced at all, so it
    // must exist before the first read answers.
    wire(null)
    mount()

    expect(screen.getByTestId('publish-announce').textContent).toBe('')
  })

  it('says the status is unavailable through the region too, not only on the chip', () => {
    wire(null, { loadError: 'Could not read the publish status.' })
    mount()

    expect(screen.getByTestId('publish-announce').textContent).toContain('unavailable')
  })
})

describe('the popover explains the state and offers at most one thing to do', () => {
  it('switched off says an administrator did it, and offers nothing', async () => {
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

    // Conditional on purpose — a zero-score declaration publishes unattended under ladder
    // rule 7, so promising a review outright would be untrue for the common case.
    expect(pop.textContent).toContain('If it handles anything sensitive')
    // Must not claim "nobody else can see this yet" — that describes who can REACH the
    // app, not whether it will be reviewed, and is not a claim this chip makes.
    expect(pop.textContent).not.toMatch(/nobody else can see/i)
    expect(pop.textContent).not.toMatch(/only you can see/i)
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

    expect(pop.textContent).toContain('one exact build')
    // Says "that build", not "the approved version" — an app published unattended under
    // ladder rule 7 may have no approval to serve, so routing is the only true claim.
    expect(pop.textContent).toContain('keeps serving that build')
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
    // Both phrasings the retired control used, and neither may come back.
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
    // NAMES NO ACTOR. This state is reachable by the owner's own take-down as well as by an
    // administrator's, so the sentence says what is true of both and keeps the remedy.
    expect(pop.textContent).toContain('not running in production')
    expect(pop.textContent).not.toMatch(/administrator/i)
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

  it('claims no administrator in any state an app can reach without one', async () => {
    // Ladder rule 7 can publish unattended, and `AppStatus.APPROVED` is set in exactly one
    // place (the admin approve route) — so every state below is reachable with no
    // administrator involved and `approved_commit_sha` NULL. A sentence claiming approval
    // or review here would be untrue.
    //
    // Mutation receipt: restore the canvas's "It was approved but would not start", or
    // "Every app is checked by an administrator before it goes live", or "keeps serving
    // the approved version" — each goes red here by name.
    const REACHABLE_WITHOUT_AN_ADMIN: readonly PublishState[] = [
      'nothing_built',
      'draft',
      'starting_up',
      'live_current',
      'live_newer_work',
      'live_drift_unknown',
      'did_not_start',
    ]

    for (const state of REACHABLE_WITHOUT_AN_ADMIN) {
      wire(view(state, { url: 'https://x.example/' }))
      mount()
      const text = (await openChip()).textContent ?? ''

      expect(text).not.toMatch(/\bapproved\b/i)
      expect(text).not.toMatch(/an administrator (approved|checked|signed)/i)
      expect(text).not.toMatch(/every app is checked/i)
      cleanup()
    }
  })

  it('still names the administrator in the states that genuinely have one', async () => {
    // The paired positive, so the rule above cannot be satisfied by scrubbing the word
    // everywhere. These three are only reachable THROUGH an administrator. `taken_offline` is
    // deliberately not among them: an owner can now take their own application down, so naming
    // an administrator there would tell them somebody else did what they just did.
    const ADMIN_STATES: ReadonlyArray<readonly [PublishState, RegExp]> = [
      ['in_review', /with an administrator/i],
      ['changes_requested', /an administrator asked/i],
      ['approved_ready_to_publish', /an administrator approved/i],
    ]

    for (const [state, phrase] of ADMIN_STATES) {
      wire(view(state))
      mount()
      expect((await openChip()).textContent ?? '').toMatch(phrase)
      cleanup()
    }
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

  it('gives every state the version row it is ABOUT, and no other', async () => {
    // Covers the whole table, not just the states used above — a wrong mapping would show
    // a citizen the wrong version's date/commit, or a "Live now" heading on a dead app.
    //
    // Mutation receipt: change any `version:` in `presentationFor` and this goes red on
    // the state whose row moved.
    const ROWS: ReadonlyArray<readonly [PublishState, string | null]> = [
      ['nothing_built', null],
      ['draft', null],
      ['in_review', 'Sent for review'],
      ['changes_requested', 'Sent for review'],
      ['approved_ready_to_publish', 'Approved version'],
      ['approved_needs_review_again', 'Approved version'],
      ['starting_up', null],
      ['live_current', 'Live now'],
      ['live_newer_work', 'Live now'],
      ['live_drift_unknown', 'Live now'],
      ['taken_offline', 'Last published'],
      ['switched_off', null],
      ['did_not_start', null],
    ]

    for (const [state, heading] of ROWS) {
      wire(
        view(state, {
          url: 'https://x.example/',
          headSha: SHA,
          finishedAt: '2026-08-20T09:14:00Z',
          approval: approval({
            submittedSha: SHA,
            submittedAt: '2026-08-19T10:00:00Z',
            approvedCommitSha: APPROVED_SHA,
            approvedAt: '2026-08-19T10:00:00Z',
          }),
        }),
      )
      mount()
      await openChip()

      if (heading === null) {
        expect(screen.queryByTestId('publish-version')).toBeNull()
        // Liveness — an absent row must mean "this state has none", never "the component
        // threw and rendered nothing".
        expect(screen.getByTestId('publish-chip')).toBeTruthy()
      } else {
        expect(screen.getByTestId('publish-version').textContent).toContain(heading)
      }
      cleanup()
    }
  })

  it('links an address only from the states that are actually serving one', async () => {
    for (const state of ['live_current', 'live_newer_work', 'live_drift_unknown'] as const) {
      wire(view(state, { url: 'https://x.example/', headSha: SHA }))
      mount()
      await openChip()
      expect(screen.getByTestId('publish-url')).toBeTruthy()
      cleanup()
    }
    // Every other state either has no row or has one that must not be linked.
    for (const state of ['taken_offline', 'draft', 'in_review', 'did_not_start'] as const) {
      wire(view(state, { url: 'https://x.example/', headSha: SHA }))
      mount()
      await openChip()
      expect(screen.queryByTestId('publish-url')).toBeNull()
      expect(screen.getByTestId('publish-chip')).toBeTruthy()
      cleanup()
    }
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

  it('tells the questionnaire when an approval already pins what is saved', async () => {
    // Only this state means the press publishes; the sibling approved state sends the
    // current work back. Mutation check: hardcode either value and one half goes red.
    wire(view('approved_ready_to_publish', { approval: approval({ status: 'approved', approvedCommitSha: SHA }) }))
    mount()
    await openChip()
    fireEvent.click(screen.getByTestId('publish-action'))
    await screen.findByTestId('data-classification-modal')
    expect(h.modal.current?.alreadyApproved).toBe(true)

    cleanup()
    wire(
      view('approved_needs_review_again', {
        approval: approval({ status: 'approved', approvedCommitSha: APPROVED_SHA }),
      }),
    )
    mount()
    await openChip()
    fireEvent.click(screen.getByTestId('publish-action'))
    await screen.findByTestId('data-classification-modal')
    expect(h.modal.current?.alreadyApproved).toBe(false)
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
    expect(screen.queryByTestId('publish-action')).toBeNull()
  })

  it('marks an in-flight action unavailable with a reason, and never hard-disables it', async () => {
    // Disabling a control that has focus blurs it to `document.body`, which is how a
    // keyboard user loses their place mid-flight.
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
    // answers 200, deliberately, since this is the only publishing surface there is — so
    // it must NOT reach the unavailable presentation.
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
    // Driven in the order it actually happens — confirm first, THEN in flight — the
    // request cannot start before the confirm exists.
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

  it('announces the new state once the version is back with the citizen', async () => {
    // The withdrawal has no success sentence of its own — the region announces the
    // resulting state instead, same as any other state change.
    wire(IN_REVIEW)
    const { rerender } = render(<PublishStatusChip projectId="p1" />)
    expect(screen.getByTestId('publish-announce').textContent).toContain('In review')

    wire(view('draft'))
    rerender(<PublishStatusChip projectId="p1" />)

    expect(screen.getByTestId('publish-announce').textContent).toContain('Draft')
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
    // `getByTestId` throwing on a duplicate node IS the assertion here — there is no
    // separate "exactly one" check.
    wire(view('taken_offline', { status: 'running', unpublishedAt: '2026-08-21T09:14:00Z' }))
    mount()

    expect(screen.getByTestId('publish-chip').textContent).toContain('Taken offline')
  })

  it('says only that it is starting up, whatever phase the pipeline reports', async () => {
    // The retired phase vocabulary itself is guarded across the whole tree by
    // `jsx-deploy-retirement.test.ts`, which is strictly stronger than checking rendered
    // text here — and which is why the phrases are not written out again in this file.
    // What this pins is the other half: `step` is on the response and is IGNORED.
    wire(view('starting_up', { status: 'running', step: 'packing' }))
    mount()
    const pop = await openChip()

    expect(screen.getByTestId('publish-chip').textContent).toContain('Starting up')
    expect(pop.textContent).not.toContain('packing')
    expect(within(pop).queryAllByRole('button')).toHaveLength(0)
  })

  it('points at no review-status anchor — there is no second card to point at', async () => {
    wire(view('in_review', { approval: approval({ status: 'pending', submittedSha: SHA }) }))
    mount()
    const pop = await openChip()

    expect(pop.innerHTML).not.toContain('review-status')
  })

  /**
   * THE CHIP IS A PRESS, SO IT CARRIES THE TOOLBAR'S TOUCH FLOOR.
   *
   * It is the third of the workspace row's nine occupants and the only one that lives in another
   * file, which is exactly how a sweep over `WorkspaceToolbar.tsx` would have left ~26px of pill
   * as the one target below the floor.
   *
   * STRUCTURAL, LIKE EVERY OTHER SIZE ASSERTION IN THIS FILE: jsdom computes no Tailwind, so what
   * is checked is which class each state resolves to. The rectangle belongs to the browser suite.
   */
  it('★ every state\'s chip declares the 44px touch floor below the stacking threshold', () => {
    for (const [state] of LABELS) {
      wire(view(state, { status: 'running' }))
      mount()
      const chip = screen.getByTestId('publish-chip')
      // A HEIGHT ONLY: every one of the thirteen words is already wider than 44px inside the
      // pill's padding, and `min-h` leaves the 999px radius, the dot and the chevron exactly as
      // the board draws them at every width above the threshold.
      expect(chip.className).toContain('narrow:min-h-[44px]')
      expect(chip.className).not.toContain('narrow:min-w-')
      // Above the threshold nothing changed: strip the variant and the pill's own geometry is
      // untouched, so the desktop chip is the one that shipped.
      const desktop = chip.className.split(/\s+/).filter((token) => !token.startsWith('narrow:'))
      expect(desktop.join(' ')).not.toMatch(/min-[hw]-/)
      expect(desktop.join(' ')).toContain('py-[5px]')
      cleanup()
    }
  })

  it('★ …and so does the chip the read-failure branch draws, which is the only way to retry', () => {
    // The branch a walk over the thirteen states cannot reach: `loadError` replaces the pill
    // entirely, and the button it replaces it with is the only route to "Check again".
    wire(null, { loadError: 'The publish status could not be read.' })
    mount()

    const chip = screen.getByTestId('publish-chip')
    expect(chip.textContent).toContain('Status unavailable')
    expect(chip.className).toContain('narrow:min-h-[44px]')
  })
})

describe('★ the publish wait says what it is doing', () => {
  // `busyReason` existed and was rendered ONLY as a `title` attribute — neither visible text
  // nor an exposed busy state, and unreachable to a keyboard or a touch screen. So the one
  // thing this component announced was the publish OUTCOME: press Save and publish, and hear
  // nothing at all until it is over, on an operation that uploads a bundle, claims a
  // deployment row and starts a container.

  it('names the wait in the button, in the region, and as a busy state', async () => {
    wire(view('draft'), { saving: true })
    mount()
    await openChip()

    const action = screen.getByTestId('publish-action')
    // VISIBLE TEXT, not a tooltip. Under the defect the label still read "Save and publish"
    // while it was already saving — a control that looks pressable and is doing the thing.
    expect(action.textContent).toContain('Saving and publishing')
    expect(action.getAttribute('aria-busy')).toBe('true')
    // ANNOUNCED, through the region that previously only ever spoke the outcome.
    expect(screen.getByTestId('publish-announce').textContent).toContain('Saving and publishing')
  })

  it('names a take-back the same way, and gives the label back when the wait ends', async () => {
    // PAIRED WITH THE LEAVING, because an announcement on entering a wait and silence on
    // leaving it is a screen that never says the thing finished.
    wire(view('draft'), { withdrawing: true })
    mount()
    await openChip()
    expect(screen.getByTestId('publish-action').textContent).toContain('Taking it back')
    expect(screen.getByTestId('publish-announce').textContent).toContain('Taking it back')

    cleanup()
    wire(view('draft'))
    mount()
    await openChip()

    const settled = screen.getByTestId('publish-action')
    expect(settled.textContent).not.toContain('Taking it back')
    expect(settled.getAttribute('aria-busy')).toBe('false')
    // LIVENESS: the control really is the same one, back to offering its action.
    expect(settled.textContent?.trim().length).toBeGreaterThan(0)
  })
})
