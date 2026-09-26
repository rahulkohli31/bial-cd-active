/**
 * THE STATUS PANEL — every state, drawn open. It is the body of Settings › Production now, and
 * the ONE owner of Send for review.
 *
 * The boards make this the fuller of the two publishing surfaces: a coloured pill, three
 * provenance rows with dates and short build ids, one sentence, one action.
 *
 * WHAT THIS FILE OWNS AND WHAT IT DOES NOT. The words, the colour and the action come from
 * `utils/publishPresentation.ts`, which the chip reads too — so a copy assertion here would be a
 * second place to edit the same sentence. What this pins is the PANEL: which rows each state
 * shows, how the saved row degrades when the platform cannot tell, that the drift is amber, and
 * that a state with nothing to do gets no button rather than a dead one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor, act } from '@testing-library/react'
import type {
  ApprovalState,
  DataClassificationAnswers,
  DeploymentView,
  PublishState,
} from '../../../utils/deployApi'
import type { UsePublishState } from '../../../hooks/usePublishState'
import { presentationFor } from '../../../utils/publishPresentation'

const h = vi.hoisted(() => ({
  usePublishState: vi.fn(),
  modal: {
    current: null as null | {
      onConfirm: (commitSha: string, answers: DataClassificationAnswers) => Promise<void>
    },
  },
}))
vi.mock('../../../hooks/usePublishState', () => ({ usePublishState: h.usePublishState }))
vi.mock('../../DataClassificationModal', () => ({
  default: (props: {
    onConfirm: (commitSha: string, answers: DataClassificationAnswers) => Promise<void>
  }) => {
    h.modal.current = props
    return <div data-testid="data-classification-modal" />
  },
}))

const AppStatusPanel = (await import('../AppStatusPanel')).default

const SHA = 'a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0'
const SAVED_SHA = 'f9e8d7c6b5a4f9e8d7c6b5a4f9e8d7c6b5a4f9e8'

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
  approval: null,
  publishState,
  approvedRetryCommit: null,
  savedHead: null,
  savedAt: null,
  // `null` is "the server did not say", which keeps the saved row — the neutral default
  // for suites that are not about the never-saved omission.
  savedState: null,
  ...over,
})

const hook = (over: Partial<UsePublishState> = {}): UsePublishState =>
  ({
    deployment: null,
    approval: null,
    loadError: null,
    refresh: vi.fn(async () => {}),
    publish: vi.fn(async () => 'review' as const),
    publishPhase: null,
    publishError: null,
    onConfirm: vi.fn(async () => {
      throw new Error('onConfirm was not wired for this test')
    }),
    withdraw: vi.fn(async () => {}),
    withdrawing: false,
    withdrawError: null,
    ...over,
  }) as UsePublishState

const wire = (over: Partial<UsePublishState>) => h.usePublishState.mockReturnValue(hook(over))
const mount = () => render(<AppStatusPanel projectId="p1" />)
const panel = () => screen.getByTestId('app-status-panel')

beforeEach(() => vi.clearAllMocks())
afterEach(cleanup)

describe('the state pill', () => {
  it('★ shares the section label\'s row, carried to its right', () => {
    // The pill was a `float-right` in the block BELOW the heading — a float cannot rise onto a
    // preceding block's line, so it dropped to a row of its own. The label is the panel's own
    // now: there is no rail left to hand one down.
    wire({ deployment: view('draft') })
    mount()
    const pill = screen.getByTestId('status-pill')
    const head = pill.parentElement as HTMLElement

    expect(head.textContent).toContain('STATUS')
    expect(head.className).toMatch(/(^|\s)flex(\s|$)/)
    expect(pill.className.split(/\s+/)).toContain('ms-auto')
    expect(pill.className.split(/\s+/)).not.toContain('float-right')
  })

  it('carries the state\'s own colour and a leading dot, exactly as the chip does', () => {
    wire({ deployment: view('in_review'), approval: approval({ status: 'pending' }) })
    mount()
    const pill = screen.getByTestId('status-pill')
    expect(pill.textContent).toContain('In review')
    expect(pill.className).toContain('text-status-amber-fg')
    expect(pill.querySelector('.bg-status-amber-dot')).not.toBeNull()
  })

  it('exposes the raw state, so a walk over every value has something to key on', () => {
    wire({ deployment: view('live_current') })
    mount()
    expect(panel().getAttribute('data-publish-state')).toBe('live_current')
  })
})

describe('the provenance rows', () => {
  it('★ shows published, approved and the citizen\'s own saved version together', () => {
    // THE BOARD'S THREE ROWS. The third needed a server field: the saved head and its
    // timestamp.
    wire({
      deployment: view('live_current', {
        finishedAt: '2026-08-20T09:14:00Z',
        headSha: SHA,
        url: 'https://visitor-log.apps.example/',
        savedAt: '2026-08-20T09:14:00Z',
        savedHead: SHA,
      }),
      approval: approval({ status: 'approved', approvedAt: '2026-08-19T00:00:00Z' }),
    })
    mount()

    expect(screen.getByTestId('status-row-published').textContent).toMatch(/PUBLISHED/)
    expect(screen.getByTestId('status-row-published').textContent).toContain('a1b2c3d')
    expect(screen.getByTestId('status-row-approved').textContent).toMatch(/APPROVED/)
    expect(screen.getByTestId('status-row-saved').textContent).toMatch(/YOUR LATEST/)
  })

  it('★ a project that has never been published shows the board\'s nothing-built state and no rows', () => {
    wire({ deployment: view('nothing_built') })
    mount()

    expect(screen.getByTestId('status-pill').textContent).toContain('Nothing built yet')
    expect(screen.queryByTestId('status-row-published')).toBeNull()
    expect(screen.queryByTestId('status-row-saved')).toBeNull()
    // Liveness: the panel rendered, it simply has no version of anything to date.
    expect(panel().textContent).toMatch(/describe what you need/i)
  })

  it('★ a stopped project still shows its saved row — no container is in the request path', () => {
    // THE WHOLE REASON THE FIELD RIDES ON THE DEPLOYMENT READ. `save-state` attaches to a
    // container before it can answer, so it is silent in exactly the reclaimed case this row is
    // for. This panel makes ONE call, `usePublishState`, and that read touches no sandbox.
    wire({
      deployment: view('draft', { savedAt: '2026-08-25T14:20:00Z', savedHead: SAVED_SHA }),
    })
    mount()

    const saved = screen.getByTestId('status-row-saved')
    expect(saved.textContent).toMatch(/LAST SAVED/)
    expect(saved.textContent).toContain('f9e8d7c')
  })

  it('★ a bundle with no stamped head prints its DATE and says the version is unknown', () => {
    // The mixed case, which is neither "both present" nor "both absent": a bundle written
    // before the metadata stamp existed still has a last-modified on the object, so the store
    // knows WHEN without knowing WHICH. Inventing an id, or blanking the row, would both be
    // worse than saying so.
    wire({ deployment: view('draft', { savedAt: '2026-08-25T14:20:00Z', savedHead: null }) })
    mount()

    const saved = screen.getByTestId('status-row-saved')
    expect(saved.textContent).toMatch(/version unknown/i)
    expect(saved.textContent).toMatch(/2026/)
    expect(screen.queryByTestId('status-row-saved-unknown')).toBeNull()
  })

  it('★ with neither half known it renders "cannot tell", never a blank row', () => {
    wire({ deployment: view('draft', { savedAt: null, savedHead: null }) })
    mount()
    expect(screen.getByTestId('status-row-saved-unknown').textContent).toMatch(/could not tell/i)
  })

  it('★ prints the drifted date in amber, and ONLY where the drift is known', () => {
    // #B45309 is the only amber TEXT the canvas uses anywhere, and the colour IS the signal.
    // `live_drift_unknown` must not borrow it: the server could not make the comparison, and
    // "yours is newer" is as much a claim as "nothing of yours is waiting".
    const drifted = { savedAt: '2026-08-25T14:20:00Z', savedHead: SAVED_SHA }
    for (const [state, amber] of [
      ['live_newer_work', true],
      ['live_current', false],
      ['live_drift_unknown', false],
    ] as const) {
      wire({ deployment: view(state, drifted) })
      mount()
      const saved = screen.getByTestId('status-row-saved')
      expect(saved.querySelector('.text-status-amber-fg') !== null, state).toBe(amber)
      cleanup()
    }
  })
})

/**
 * Sharing a published app without leaving the product.
 *
 * The panel already linked the address; copying it meant opening the tab and taking it out of
 * the browser's own address bar. What is asserted here is the VALUE copied, not the presence of
 * a control — a copy button wired to the wrong string passes every presence assertion.
 */
describe('the copy control on a published app', () => {
  const LIVE = 'https://visitor-log.apps.example/'

  const published = (over: Partial<DeploymentView> = {}) =>
    view('live_current', {
      finishedAt: '2026-08-20T09:14:00Z',
      headSha: SHA,
      url: LIVE,
      savedAt: '2026-08-20T09:14:00Z',
      savedHead: SHA,
      savedState: 'saved',
      ...over,
    })

  /** jsdom ships no `navigator.clipboard` at all, so every case here is a definition. */
  const stubClipboard = (writeText: (text: string) => Promise<void>) => {
    const spy = vi.fn(writeText)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: spy }, configurable: true })
    return spy
  }

  afterEach(() => {
    Reflect.deleteProperty(navigator, 'clipboard')
  })

  it('★ copies the LIVE APP\'S OWN ADDRESS, not merely something', async () => {
    const writeText = stubClipboard(async () => {})
    wire({ deployment: published() })
    mount()

    fireEvent.click(screen.getByTestId('status-copy-link'))

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    expect(writeText).toHaveBeenCalledWith(LIVE)
    // …and it says so, because a copy control that gives no answer is indistinguishable from
    // one that failed.
    await waitFor(() =>
      expect(screen.getByTestId('status-copy-link').getAttribute('aria-label')).toMatch(/copied/i),
    )
  })

  it('★ is absent on an app that is not published', () => {
    // Gated on the same `url` the row carries, so the two states below cover both reasons a
    // panel has no address to offer: never published, and published then taken down (whose
    // address would 404 — `provenanceRows` nulls it deliberately).
    for (const state of ['draft', 'in_review', 'nothing_built'] as const) {
      wire({ deployment: view(state, { savedState: 'saved', savedAt: '2026-08-20T09:14:00Z' }) })
      mount()
      expect(screen.queryByTestId('status-copy-link'), state).toBeNull()
      // Liveness: the panel rendered its state — an absence assertion on a crashed render
      // would pass on its own.
      expect(screen.getByTestId('status-pill'), state).toBeTruthy()
      cleanup()
    }

    wire({
      deployment: view('taken_offline', {
        url: LIVE,
        finishedAt: '2026-08-20T09:14:00Z',
        savedState: 'saved',
      }),
    })
    mount()
    expect(screen.queryByTestId('status-copy-link')).toBeNull()
    expect(screen.getByTestId('status-row-published').textContent).toMatch(/LAST PUBLISHED/)
  })

  it('★ says so when the clipboard refuses, and hands over the address to copy by hand', async () => {
    // The defect this forbids is SILENCE. `navigator.clipboard` rejects on a denied permission
    // and on an unfocused document, and the press otherwise does nothing at all — the citizen
    // pastes whatever was on their clipboard before and the platform never said a word.
    const writeText = stubClipboard(async () => {
      throw new Error('NotAllowedError')
    })
    wire({ deployment: published() })
    mount()

    fireEvent.click(screen.getByTestId('status-copy-link'))

    const alert = await screen.findByTestId('status-copy-refused')
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(alert.getAttribute('role')).toBe('alert')
    // Actionable, not merely apologetic: the address itself is in the message.
    expect(alert.textContent).toContain(LIVE)
    // Liveness: the panel is intact and the control is still there to try again.
    expect(screen.getByTestId('status-copy-link')).toBeTruthy()
  })

  it('★ says the same thing when the browser has no clipboard at all', async () => {
    // The OTHER refusal shape, and the one a `.catch()` alone would miss: on an insecure
    // origin `navigator.clipboard` is UNDEFINED, so an inline `await
    // navigator.clipboard.writeText(…)` throws rather than rejecting.
    Reflect.deleteProperty(navigator, 'clipboard')
    wire({ deployment: published() })
    mount()

    fireEvent.click(screen.getByTestId('status-copy-link'))

    expect((await screen.findByTestId('status-copy-refused')).textContent).toContain(LIVE)
  })
})

/**
 * The reviewer's reason, on the rail, without opening anything.
 *
 * WHAT IS NOT HERE, DELIBERATELY: the geometric half of that rule — that a 1,000-character
 * note leaves "Send for review" in view. jsdom has no layout engine, so a test here could
 * only assert that both elements EXIST, which is exactly the false pass this campaign has
 * already produced once. It lives in the browser suite; what this file pins is source order,
 * which is the mechanism the geometry depends on.
 */
describe('the reviewer\'s reason on the rail', () => {
  const NOTE = 'Move the hardcoded database URL and API key out of lib/db.ts, then send it again.'
  const rejected = (note: string | null) =>
    ({
      deployment: view('changes_requested', { savedState: 'saved', savedAt: '2026-08-20T09:14:00Z' }),
      approval: approval({ status: 'rejected', rejectionNote: note }),
    }) as const

  it('★ is on the panel with nothing opened, and in full', () => {
    wire(rejected(NOTE))
    mount()

    const note = screen.getByTestId('status-row-rejection-note')
    expect(note.textContent).toBe(NOTE)
    // The whole point of "without opening anything": no dialog, no popover, no press.
    expect(screen.queryByTestId('data-classification-modal')).toBeNull()
    // And it is not the dialog rendered early — the panel's own row carries it.
    expect(screen.getByTestId('status-row-rejection').textContent).toMatch(/WHY/)
  })

  it('★ sits ABOVE the state\'s action in source order', () => {
    // The mechanism behind the geometric requirement: a note that renders after the button
    // pushes it down the rail as it grows. Asserted on document position, which is the part
    // of that claim jsdom can actually see.
    wire(rejected(NOTE))
    mount()

    const note = screen.getByTestId('status-row-rejection')
    const action = screen.getByTestId('status-action')
    expect(note.compareDocumentPosition(action) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(action.textContent).toBe('Send for review')
  })

  it('★ bounds the BOX rather than the text', () => {
    // A 1,000-character cap in a 360px rail. The remedy that reproduces the defect is cutting
    // the string; the remedy is a bounded, scrollable box around all of it.
    const long = 'x'.repeat(1000)
    wire(rejected(long))
    mount()

    const note = screen.getByTestId('status-row-rejection-note')
    expect(note.textContent?.length).toBe(1000)
    expect(note.className).toMatch(/overflow-y-auto/)
    expect(note.className).toMatch(/max-h-/)
    // Reachable by keyboard, not by mouse wheel alone.
    expect(note.getAttribute('tabindex')).toBe('0')
  })

  it('★ shows no note row where an administrator wrote none', () => {
    wire(rejected(null))
    mount()

    expect(screen.queryByTestId('status-row-rejection')).toBeNull()
    // Liveness: the state itself still rendered, note or no note.
    expect(screen.getByTestId('status-pill').textContent).toBeTruthy()
    expect(screen.getByTestId('status-action').textContent).toBe('Send for review')
  })
})

/**
 * A project that has never been saved stops claiming the platform lost it.
 */
describe('the LAST SAVED row on a project with no save', () => {
  it('★ is not rendered at all, rather than rendered as "we could not tell"', () => {
    wire({ deployment: view('draft', { savedState: 'never_saved' }) })
    mount()

    expect(screen.queryByTestId('status-row-saved')).toBeNull()
    expect(screen.queryByTestId('status-row-saved-unknown')).toBeNull()
    // Liveness, twice over: the panel drew its state and its action, so the absence above is
    // an omitted row rather than a panel that failed to render.
    expect(screen.getByTestId('status-pill').textContent).toBeTruthy()
    expect(screen.getByTestId('status-action').textContent).toBe('Send for review')
  })

  it('★ still says "we could not tell" for a save the platform could not READ', () => {
    for (const state of ['storage_error', 'store_unconfigured'] as const) {
      wire({ deployment: view('draft', { savedState: state }) })
      mount()
      expect(screen.getByTestId('status-row-saved-unknown').textContent, state).toMatch(
        /could not tell/i,
      )
      cleanup()
    }
  })

  it('renders the real timestamp for a project that HAS saved', () => {
    wire({
      deployment: view('draft', {
        savedState: 'saved',
        savedAt: '2026-08-25T14:20:00Z',
        savedHead: SAVED_SHA,
      }),
    })
    mount()

    const saved = screen.getByTestId('status-row-saved')
    expect(saved.textContent).toMatch(/2026/)
    expect(saved.textContent).toContain('f9e8d7c')
  })
})

describe('the action', () => {
  it('carries the board\'s label for each state that has one', () => {
    for (const [state, label] of [
      ['draft', 'Send for review'],
      ['live_newer_work', 'Send update for review'],
      ['did_not_start', 'Try again'],
      ['in_review', 'Take it back'],
      ['taken_offline', 'Publish again'],
    ] as const) {
      wire({ deployment: view(state) })
      mount()
      expect(screen.getByTestId('status-action').textContent, state).toBe(label)
      cleanup()
    }
  })

  it('★ gives a state with nothing to do NO button, rather than one that fails when pressed', () => {
    // The board says so in as many words. Asserted by querying for ANY button, not by one
    // label's absence.
    for (const state of ['nothing_built', 'starting_up', 'live_current', 'switched_off'] as const) {
      wire({ deployment: view(state) })
      mount()
      expect(screen.queryByTestId('status-action'), state).toBeNull()
      // Liveness: the panel is rendering its pill for that state.
      expect(screen.getByTestId('status-pill'), state).toBeTruthy()
      cleanup()
    }
  })

  it('★ draws the ONE action the canvas does not paint teal as a secondary', () => {
    // Every other action moves the app forward — send for review, send the newer version, publish.
    // Taking a submission back moves it backwards, out of an administrator's queue, and painting it
    // in the encouraging colour asked a citizen to withdraw their own work in the same voice as it
    // asked them to submit it.
    wire({ deployment: view('in_review'), approval: approval({ status: 'pending' }) })
    mount()
    const back = screen.getByTestId('status-action')
    expect(back.textContent).toContain('Take it back')
    expect(back.className).toMatch(/bg-white/)
    expect(back.className).not.toMatch(/bg-primary/)

    // …and the forward action is still teal, or this would pass on a panel with no primary at all.
    cleanup()
    wire({ deployment: view('draft') })
    mount()
    const send = screen.getByTestId('status-action')
    expect(send.textContent).toContain('Send for review')
    expect(send.className).toMatch(/bg-primary/)
  })

  it('takes a submission back directly, and opens the declaration for everything else', async () => {
    const withdraw = vi.fn(async () => {})
    const publish = vi.fn(async () => 'review' as const)
    wire({ deployment: view('in_review'), withdraw, publish })
    mount()
    fireEvent.click(screen.getByTestId('status-action'))
    expect(withdraw).toHaveBeenCalledTimes(1)
    expect(publish).not.toHaveBeenCalled()
    expect(screen.queryByTestId('data-classification-modal')).toBeNull()

    cleanup()
    wire({ deployment: view('draft'), publish })
    mount()
    fireEvent.click(screen.getByTestId('status-action'))
    expect(await screen.findByTestId('data-classification-modal')).toBeTruthy()
    expect(publish).toHaveBeenCalledTimes(1)
  })

  it('★ says so when the withdrawal is refused, rather than looking like nothing happened', () => {
    // `withdraw` swallows its failure into `withdrawError` and does NOT re-read, so the pill, the
    // rows and the button are all identical after a refusal. Both origins land here as one
    // message: a 409 carries the server's own sentence (an administrator reached the submission
    // first), anything else carries the hook's. Neither may be silent.
    for (const message of [
      'Only a submission still waiting for review can be taken back.',
      'Could not withdraw this submission. Try again.',
    ]) {
      wire({ deployment: view('in_review'), approval: approval({ status: 'pending' }), withdrawError: message })
      mount()
      const alert = screen.getByRole('alert')
      expect(alert.textContent, message).toBe(message)
      // Liveness: the panel is still offering the action, so this is a refusal shown in place
      // rather than a panel that has replaced itself with an error.
      expect(screen.getByTestId('status-action').textContent, message).toContain('Take it back')
      cleanup()
    }
  })

  it('says nothing about a withdrawal that has not failed', () => {
    // Pairs with the case above: the alert must not be a permanent fixture of the state.
    wire({ deployment: view('in_review'), approval: approval({ status: 'pending' }) })
    mount()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByTestId('status-action').textContent).toContain('Take it back')
  })

  it('renders no control with a real disabled attribute', () => {
    wire({ deployment: view('draft'), publishPhase: 'saving' })
    mount()
    for (const el of screen.getAllByRole('button')) expect(el.hasAttribute('disabled')).toBe(false)
    expect(screen.getByTestId('status-action').textContent).toBe('Saving…')
  })
})

describe('when the read itself fails', () => {
  it('says so and offers a re-read, rather than leaving a blank section', () => {
    // A panel that renders nothing is indistinguishable from a broken page, and this is the
    // surface a citizen goes to in order to find out whether anything is wrong.
    const refresh = vi.fn(async () => {})
    wire({ loadError: 'We could not check on your app just now.', refresh })
    mount()

    expect(panel().textContent).toMatch(/could not check/i)
    fireEvent.click(screen.getByTestId('status-recheck'))
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('holds the section\'s shape while the first read is out, and claims no state', () => {
    wire({ deployment: null })
    mount()
    expect(panel().getAttribute('data-publish-state')).toBe('pending')
    expect(screen.queryByTestId('status-pill')).toBeNull()
  })
})

/**
 * ★ ONE STATUS, ONE BUTTON. Every state the server can send, walked: the panel draws exactly the
 * one action its state names, or none — never a second control beside it, and no banner.
 */
describe('one button per state', () => {
  const EVERY_STATE: Record<PublishState, null> = {
    nothing_built: null,
    draft: null,
    in_review: null,
    changes_requested: null,
    starting_up: null,
    live_current: null,
    live_newer_work: null,
    live_drift_unknown: null,
    taken_offline: null,
    switched_off: null,
    did_not_start: null,
  }
  const RETRY = 'f9e8d7c6b5a4f9e8d7c6b5a4f9e8d7c6b5a4f9e8'
  const cases = (Object.keys(EVERY_STATE) as PublishState[]).flatMap((state) => [
    [state, null] as const,
    [state, RETRY] as const,
  ])

  it.each(cases)('%s (approved retry %s) draws the one action it names', (state, retry) => {
    wire({ deployment: view(state, { approvedRetryCommit: retry }) })
    mount()

    const expected = presentationFor(state, retry).action
    const buttons = screen.queryAllByRole('button')
    expect(buttons.length).toBe(expected === null ? 0 : 1)
    if (expected !== null) expect(buttons[0].getAttribute('data-testid')).toBe('status-action')
    // Liveness: the panel drew its state, so zero buttons is an omission, not a crash.
    expect(screen.getByTestId('status-pill').textContent).toBe(presentationFor(state, retry).label)
  })

  it('★ saves first and then opens the dialog, with no banner and no second button', async () => {
    const publish = vi.fn(async () => 'review' as const)
    wire({ deployment: view('draft', { savedState: 'saved' }), publish })
    mount()

    fireEvent.click(screen.getByTestId('status-action'))

    expect(await screen.findByTestId('data-classification-modal')).toBeTruthy()
    expect(publish).toHaveBeenCalledTimes(1)
    expect(screen.getAllByTestId('status-action')).toHaveLength(1)
    expect(panel().textContent).not.toMatch(/not saved yet/i)
  })

  it('★ opens no dialog when the save fails, and shows why', async () => {
    const publish = vi.fn(async () => null)
    wire({
      deployment: view('draft'),
      publish,
      publishError: 'Your workspace is not running, so there was nothing to save.',
    })
    mount()

    fireEvent.click(screen.getByTestId('status-action'))
    await waitFor(() => expect(publish).toHaveBeenCalledTimes(1))

    expect(screen.getByTestId('status-publish-error').textContent).toMatch(/nothing to save/)
    expect(screen.queryByTestId('data-classification-modal')).toBeNull()
  })

  it('★ Try again on an approved copy sends it straight away — no dialog', async () => {
    const publish = vi.fn(async () => ({
      outcome: 'started' as const,
      deploymentId: 'd1',
      appId: 'app-1',
      status: 'running',
    }))
    wire({ deployment: view('did_not_start', { approvedRetryCommit: 'abc' }), publish })
    mount()

    const action = screen.getByTestId('status-action')
    expect(action.textContent).toBe('Try again')
    fireEvent.click(action)
    await waitFor(() => expect(publish).toHaveBeenCalledTimes(1))

    expect(screen.queryByTestId('data-classification-modal')).toBeNull()
    expect(panel().textContent).toMatch(/version an administrator approved/i)
  })

  it('hands the dialog\'s reviewed commit through to the send', async () => {
    const onConfirm = vi.fn(async () => ({
      outcome: 'started' as const,
      deploymentId: 'd1',
      appId: 'app-1',
      status: 'running',
    }))
    wire({ deployment: view('draft'), onConfirm })
    mount()
    fireEvent.click(screen.getByTestId('status-action'))
    await screen.findByTestId('data-classification-modal')

    const answers = {
      credentialsSecrets: false,
      healthData: false,
      personalInformation: false,
      financialData: false,
      confidentialBusinessData: false,
      publicData: false,
      notes: null,
    }
    await act(async () => {
      await h.modal.current?.onConfirm('abc123', answers)
    })

    expect(onConfirm).toHaveBeenCalledWith('abc123', answers)
    expect(screen.queryByTestId('data-classification-modal')).toBeNull()
  })
})

/**
 * ★ THE FOOT SLOT. Whatever else an owner may do to this application is rendered here, and it is
 * handed the two facts it needs rather than fetching them: the state, and a way to ask again.
 * That is what lets the Production tab offer Restart and Take down off ONE deployment read
 * instead of opening a second one beside this panel's.
 */
describe('the actions slot', () => {
  it('★ renders what it is given, with the state the panel read', () => {
    wire({ deployment: view('live_current') })
    render(
      <AppStatusPanel
        projectId="p1"
        actions={({ state }) => <span data-testid="slot">{state}</span>}
      />,
    )
    expect(screen.getByTestId('slot').textContent).toBe('live_current')
  })

  it('★ hands down the attempt\'s failure code, which is not the same fact as the state', () => {
    // A restart that failed leaves its ending on a row newer than the container still serving,
    // so the state reads live and the code reads `restart_failed` at the same moment. A slot
    // given only the state cannot tell an owner their restart never finished.
    wire({ deployment: view('live_current', { failureCode: 'restart_not_ready' }) })
    render(
      <AppStatusPanel
        projectId="p1"
        actions={({ failureCode }) => <span data-testid="slot">{failureCode}</span>}
      />,
    )
    expect(screen.getByTestId('slot').textContent).toBe('restart_not_ready')
  })

  it('hands down a refresh that really re-reads', () => {
    const refresh = vi.fn()
    wire({ deployment: view('draft'), refresh })
    render(
      <AppStatusPanel
        projectId="p1"
        actions={({ refresh: ask }) => <button onClick={() => void ask()}>ask</button>}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'ask' }))
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('renders nothing extra when nobody passes one', () => {
    // Liveness beside the absence: the panel is fully drawn, it simply has no foot.
    wire({ deployment: view('draft') })
    mount()
    expect(screen.getByTestId('status-pill').textContent).toContain('Draft')
    expect(screen.queryByTestId('slot')).toBeNull()
  })
})
