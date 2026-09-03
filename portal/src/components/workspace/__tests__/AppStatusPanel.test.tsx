/**
 * THE APP STATUS PANEL (plan 002, U4) — every state, drawn open.
 *
 * The boards make this the fuller of the two publishing surfaces: a coloured pill, three
 * provenance rows with dates and short build ids, one sentence, one action. None of it existed —
 * the rail said nothing about publishing at all, and everything a citizen could learn lived
 * inside a popover, one row at a time.
 *
 * WHAT THIS FILE OWNS AND WHAT IT DOES NOT. The words, the colour and the action come from
 * `utils/publishPresentation.ts`, which the chip reads too — so a copy assertion here would be a
 * second place to edit the same sentence. What this pins is the PANEL: which rows each state
 * shows, how the saved row degrades when the platform cannot tell, that the drift is amber, and
 * that a state with nothing to do gets no button rather than a dead one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import type { ApprovalState, DeploymentView, PublishState } from '../../../utils/deployApi'
import type { UsePublishState } from '../../../hooks/usePublishState'

const h = vi.hoisted(() => ({ usePublishState: vi.fn() }))
vi.mock('../../../hooks/usePublishState', () => ({ usePublishState: h.usePublishState }))
vi.mock('../../DataClassificationModal', () => ({
  default: () => <div data-testid="data-classification-modal" />,
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
  savedHead: null,
  savedAt: null,
  ...over,
})

const hook = (over: Partial<UsePublishState> = {}): UsePublishState =>
  ({
    deployment: null,
    approval: null,
    loadError: null,
    refresh: vi.fn(async () => {}),
    unsaved: null,
    saving: false,
    onConfirm: vi.fn(async () => null),
    saveAndPublish: vi.fn(async () => null),
    dismissUnsaved: vi.fn(),
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
    // THE BOARD'S THREE ROWS. The third is the one that needed a server field: the saved head
    // and its timestamp did not reach this client at all before U4.
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

  it('takes a submission back directly, and opens the declaration for everything else', () => {
    const withdraw = vi.fn(async () => {})
    wire({ deployment: view('in_review'), withdraw })
    mount()
    fireEvent.click(screen.getByTestId('status-action'))
    expect(withdraw).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('data-classification-modal')).toBeNull()

    cleanup()
    wire({ deployment: view('draft') })
    mount()
    fireEvent.click(screen.getByTestId('status-action'))
    expect(screen.getByTestId('data-classification-modal')).toBeTruthy()
  })

  it('renders no control with a real disabled attribute', () => {
    wire({ deployment: view('draft'), saving: true })
    mount()
    for (const el of screen.getAllByRole('button')) expect(el.hasAttribute('disabled')).toBe(false)
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

describe('the unsaved-work question', () => {
  it('offers the second answer the server asks for', async () => {
    const saveAndPublish = vi.fn(async () => null)
    wire({
      deployment: view('draft'),
      unsaved: 'Your workspace has changes that are not saved yet.',
      saveAndPublish,
    })
    mount()

    expect(screen.getByTestId('status-unsaved').textContent).toMatch(/not saved yet/i)
    fireEvent.click(screen.getByTestId('status-save-and-publish'))
    await waitFor(() => expect(saveAndPublish).toHaveBeenCalledTimes(1))
  })
})
