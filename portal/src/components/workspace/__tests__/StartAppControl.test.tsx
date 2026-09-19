/**
 * THE ONE CONTROL THAT STARTS THE APP.
 *
 * A test here can honestly prove: one request, refusals discriminated correctly, no destructive
 * verb named. It CANNOT prove the endpoint is non-destructive — the component never could have
 * destroyed a container. That proof is server-side, against the confirmation triple, in
 * `backend/tests/api/v1/build_sessions/test_preview_state.py`, on the exact arm this button enters.
 *
 * So there is deliberately NO test here shaped "no stop, release or restore call was made". It
 * would pass in the very state that loses work, and its greenness would be mistaken for evidence.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import StartAppControl from '../StartAppControl'
import { createStarter } from '../startApp'
import type { StartSinks } from '../startApp'
import { LAUNCH_LABEL, type WorkspaceAction } from '../workspaceState'
import type { WorkspaceReport } from '../workspaceChannel'
import { ApiError } from '../../../utils/apiError'
import { BuildSessionAlreadyActiveError } from '../../../utils/buildSessionApi'

const api = vi.hoisted(() => ({ relaunchPreview: vi.fn() }))

vi.mock('../../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/buildSessionApi')>()),
  relaunchPreview: api.relaunchPreview,
}))

const START: WorkspaceAction = { kind: 'start', label: LAUNCH_LABEL }
const RETRY: WorkspaceAction = { kind: 'retry', label: 'Try again' }

function reportSpy(over: Partial<WorkspaceReport> = {}): WorkspaceReport {
  // THE REAL CLAIM, over this report's own sinks. The control has no in-flight guard of its own
  // any more — the one start at a time is the surface's, shared with the project opening and the
  // rail's send — so a stub here would prove nothing about a press and everything about the stub.
  const sinks = {
    projectId: 'p1',
    onStarted: vi.fn(),
    onStartPending: vi.fn(),
    onStartOutcome: vi.fn(),
    ...over,
  }
  return {
    settled: true,
    state: { name: 'not-running', headline: 'Your app is saved.', detail: null, action: START },
    ...sinks,
    onRefresh: vi.fn(),
    start: createStarter(() => sinks),
    ...over,
  }
}

function LocationProbe() {
  return <span data-testid="path">{useLocation().pathname}</span>
}

function renderControl(action: WorkspaceAction, report: WorkspaceReport) {
  return render(
    <MemoryRouter initialEntries={['/projects/p1']}>
      <Routes>
        {/* The probe rides ALONGSIDE the control, not on a catch-all: the go-to action navigates
            to another `/projects/:projectId`, which matches this same route — a catch-all would
            never render and the assertion would be about a element that does not exist. */}
        <Route
          path="/projects/:projectId"
          element={
            <>
              <StartAppControl action={action} report={report} />
              <LocationProbe />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  )
}

const button = () => screen.getAllByRole('button')[0]

beforeEach(() => {
  vi.clearAllMocks()
  api.relaunchPreview.mockResolvedValue({ appId: 'a1', previewUrl: 'https://app/', status: 'ready', restoredFromFailedBuild: false, ready: true })
})

afterEach(() => cleanup())

describe('one deliberate press, one request', () => {
  it('renders the client-approved label and fires nothing until it is pressed', () => {
    renderControl(START, reportSpy())

    expect(button().textContent).toContain('Launch Application')
    expect(api.relaunchPreview).not.toHaveBeenCalled()
  })

  it('fires exactly one start on a press', async () => {
    renderControl(START, reportSpy())
    fireEvent.click(button())

    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalledTimes(1))
    expect(api.relaunchPreview).toHaveBeenCalledWith({ projectId: 'p1' })
  })

  it('★ collapses two presses in the same tick into one request', async () => {
    // DISPATCHED WITHOUT A COMMIT BETWEEN THEM, which is what a double-click is. `fireEvent`
    // flushes React between calls, so two of those are two separate presses and the `pending` flag
    // alone blocks the second — a collapse this scenario would then be claiming without testing.
    // What actually collapses them is the surface's single-flight claim, shared with the project
    // opening and the rail's send, and it is the only guard that sees both presses at once.
    let release: (v: unknown) => void = () => {}
    api.relaunchPreview.mockImplementation(() => new Promise((r) => { release = r }))
    renderControl(START, reportSpy())
    const control = button()

    act(() => {
      control.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      control.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    release({ ready: true })

    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalledTimes(1))
  })

  it('clears the outcome when the start reached a serving app', async () => {
    const report = reportSpy()
    renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() => expect(report.onStartOutcome).toHaveBeenCalledWith(null))
  })
})

describe('a start that did not end in a running app says which way it ended', () => {
  it('reads `ready: false` as "started but not painted", never as dead', async () => {
    api.relaunchPreview.mockResolvedValue({ appId: 'a1', previewUrl: 'https://app/', status: 'provisioning', restoredFromFailedBuild: false, ready: false })
    const report = reportSpy()
    renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() => expect(report.onStartOutcome).toHaveBeenCalledWith({ kind: 'not-painted' }))
  })

  it("carries the server's named reason verbatim", async () => {
    api.relaunchPreview.mockRejectedValue(new ApiError('The sandbox is temporarily unavailable.', 503))
    const report = reportSpy()
    renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() =>
      expect(report.onStartOutcome).toHaveBeenCalledWith({
        kind: 'failed',
        reason: 'The sandbox is temporarily unavailable.',
      }),
    )
  })

  it('calls a failure with no server answer a timeout, not a named failure', async () => {
    // "We waited and nothing came back" is a different thing to have happened from "the server
    // said why" — and that difference must reach the citizen.
    api.relaunchPreview.mockRejectedValue(new TypeError('Failed to fetch'))
    const report = reportSpy()
    renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() => expect(report.onStartOutcome).toHaveBeenCalledWith({ kind: 'timed-out' }))
  })

  it('a start whose readiness cannot be read issues no second call of its own', async () => {
    // Asserting what the COMPONENT does. Deliberately not written as "no stop, release or restore
    // call was made" — see this file's docblock for why that assertion would prove nothing.
    api.relaunchPreview.mockRejectedValue(new ApiError('could not read', 503))
    renderControl(START, reportSpy())
    fireEvent.click(button())

    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalledTimes(1))
    expect(api.relaunchPreview).toHaveBeenCalledTimes(1)
  })
})

describe('★ no refusal opens a question — the 409s are sentences, not dialogs', () => {
  /** The server's refusal, with whichever `isSharedView` the body carried. */
  const refusal = (isSharedView: boolean) => {
    const err = new ApiError('“Car pool apps” is open for a colleague right now.', 409, 'sandbox_reclaim_blocked')
    Object.assign(err, {
      details: { projectId: 'p-other', projectName: 'Car pool apps', dirty: true, isSharedView },
    })
    return err
  }

  it('★ a colleague`s shared view is stated in the server`s own words, with nothing to press', async () => {
    // Pressing start cannot move a shared view — the server refuses it whatever the citizen
    // answers — so a dialog offering to hand it over would be a question with no true answer.
    api.relaunchPreview.mockRejectedValue(refusal(true))
    const report = reportSpy()
    const { container } = renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() => expect(report.onStartOutcome).toHaveBeenCalled())
    expect(report.onStartOutcome).toHaveBeenCalledWith({
      kind: 'failed',
      reason: '“Car pool apps” is open for a colleague right now.',
    })
    expect(container.querySelector('[role="dialog"]')).toBeNull()
  })

  it('★ and a refusal claiming it is NOT a shared view renders no dialog either', async () => {
    // The switch means this citizen's own project can no longer refuse the call, so this body is
    // the server contradicting itself. It is reported as an ordinary start failure; what must not
    // happen is a question being put to somebody about a conflict that should not exist.
    api.relaunchPreview.mockRejectedValue(refusal(false))
    const report = reportSpy()
    const { container } = renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() => expect(report.onStartOutcome).toHaveBeenCalled())
    expect((report.onStartOutcome as ReturnType<typeof vi.fn>).mock.calls[0][0]).toMatchObject({
      kind: 'failed',
    })
    expect(container.querySelector('[role="dialog"]')).toBeNull()
  })

  it('your own running build keeps its own sentence, not the server`s wire words', async () => {
    // Different cause, different remedy — finish or stop that build.
    api.relaunchPreview.mockRejectedValue(new BuildSessionAlreadyActiveError('already'))
    const report = reportSpy()
    renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() => expect(report.onStartOutcome).toHaveBeenCalled())
    expect(report.onStartOutcome).toHaveBeenCalledWith({
      kind: 'failed',
      reason: 'A build is already running in this application.',
    })
  })

  it('an uncoded 409 is an ordinary failure too', async () => {
    api.relaunchPreview.mockRejectedValue(new ApiError('conflict', 409))
    const report = reportSpy()
    renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() =>
      expect(report.onStartOutcome).toHaveBeenCalledWith({ kind: 'failed', reason: 'conflict' }),
    )
  })
})

describe('marked unavailable, never disabled', () => {
  it('★ stays focusable and named while a start is in flight', async () => {
    // Disabling a control that currently has focus blurs it to `document.body`, dropping a
    // keyboard user out of the interface at the moment something is happening.
    api.relaunchPreview.mockImplementation(() => new Promise(() => {}))
    renderControl(START, reportSpy())
    fireEvent.click(button())

    await waitFor(() => expect(button().getAttribute('aria-disabled')).toBe('true'))
    expect(button().hasAttribute('disabled')).toBe(false)
    // STILL NAMED, AND THE NAME IS NOW THE VISIBLE WORDS. The `aria-label` override that used to
    // carry it is gone, so the accessible name is what a sighted citizen reads.
    expect(button().getAttribute('aria-label')).toBeNull()
    expect(screen.getByRole('button', { name: 'Starting your app…' })).toBe(button())
    button().focus()
    expect(document.activeElement).toBe(button())
  })

  it('★ the WORDS change while a start is in flight, not just an attribute', async () => {
    // WHY THIS EXISTS. `index.css` suppresses `.animate-spin` under `prefers-reduced-motion`, and
    // the spinning glyph was the only part of this button that moved when it was pressed — the
    // label read "Launch Application" pressed and unpressed alike. With motion off, a citizen
    // pressed the button and nothing whatsoever changed on screen.
    api.relaunchPreview.mockImplementation(() => new Promise(() => {}))
    renderControl(START, reportSpy())
    expect(button().textContent).toContain(LAUNCH_LABEL)

    fireEvent.click(button())
    await waitFor(() => expect(button().textContent).toContain('Starting your app…'))

    // The old label is GONE, not merely joined — otherwise "Launch Application Starting your app…"
    // would pass this and read as two states at once.
    expect(button().textContent).not.toContain(LAUNCH_LABEL)
    expect(button().getAttribute('aria-busy')).toBe('true')
    // `aria-disabled` is RETAINED alongside it: busy is not the same claim as unavailable, and
    // dropping either one is a different regression.
    expect(button().getAttribute('aria-disabled')).toBe('true')
    expect(button().hasAttribute('disabled')).toBe(false)
  })

  it('★ adds NO live region of its own — the pane already announces this start', () => {
    // The other half of the one-live-region rule. `LivePreview` keeps one permanent polite
    // region that speaks for every state of the pane this button starts, including "Starting
    // your app…". A region here would be the same situation announced twice, which is the
    // duplicate `Announcer.tsx` records as having broken three tests.
    api.relaunchPreview.mockImplementation(() => new Promise(() => {}))
    const { container } = renderControl(START, reportSpy())
    fireEvent.click(button())

    expect(container.querySelectorAll('[aria-live], [role="status"], [role="alert"]')).toHaveLength(0)
    // Paired with a presence assertion, or a control that rendered nothing at all would pass.
    expect(container.querySelectorAll('button')).toHaveLength(1)
  })

  it('★ the retry verb gets its own in-flight words, not the start verb\'s', async () => {
    // One `pendingLabel` per action, so the button cannot say "Starting your app…" for a press
    // that was actually a retry.
    api.relaunchPreview.mockImplementation(() => new Promise(() => {}))
    renderControl(RETRY, reportSpy())
    fireEvent.click(button())

    await waitFor(() => expect(button().textContent).toContain('Trying again…'))
    expect(button().textContent).not.toContain('Starting your app')
  })
})

describe('the second verb, and the ones that do not exist', () => {
  it('a retry clears the last outcome before asking again', async () => {
    // Without the clear, a second failure of the same kind leaves the sentence unchanged and the
    // press looks like it did nothing.
    const report = reportSpy()
    renderControl(RETRY, report)
    fireEvent.click(button())

    expect(report.onStartOutcome).toHaveBeenCalledWith(null)
    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalled())
  })

  it('★ neither verb leaves this project — nothing here navigates anywhere', async () => {
    // Both members ask THIS project's own start. A control that could send somebody to another
    // project is how a workspace sentence turns back into an arbitration.
    for (const action of [START, RETRY]) {
      const report = reportSpy()
      renderControl(action, report)
      fireEvent.click(button())

      await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalled())
      expect(screen.getByTestId('path').textContent).toBe('/projects/p1')
      cleanup()
      api.relaunchPreview.mockClear()
    }
  })

  it('★ names no destructive verb, in any action, in any state', async () => {
    // The type is the enforcement — two members, neither destructive — but a LABEL can still say
    // a dangerous word, and this is what catches that.
    const destructive = /\b(restore|rebuild|reset|delete|destroy|tear down|discard|wipe|erase)\b/i
    const actions: WorkspaceAction[] = [START, RETRY]
    for (const action of actions) {
      const { container } = renderControl(action, reportSpy())
      expect(container.textContent ?? '').not.toMatch(destructive)
      // AND THERE IS NO SECOND NAME TO AUDIT. The `aria-label` override is gone, so the
      // visible words above ARE the accessible name — asserting its absence is what keeps this
      // check total rather than leaving a channel that could say a dangerous word unexamined.
      expect(container.querySelector('button')?.hasAttribute('aria-label')).toBe(false)
      cleanup()
    }
  })
})

describe('★ the URL a successful start produced reaches the surface that frames it', () => {
  it('hands the preview URL back before it reports the outcome', async () => {
    // WITHOUT THIS THE CONTROL DID NOTHING VISIBLE INSIDE A BUILD CHAT. That surface feeds the
    // address resolver's project-scoped arm with `null` — its own poll only runs over an ALREADY
    // framed URL, by design — and its `relaunchedUrl` arm was fed by a Relaunch button that has
    // since been retired. So a fresh start had no arm left to populate: the app came up in a
    // container nothing framed, and the citizen saw a sentence where their app should have been.
    const report = reportSpy()
    api.relaunchPreview.mockResolvedValue({
      appId: 'a1', previewUrl: 'https://app.example/', status: 'ready',
      restoredFromFailedBuild: false, ready: true,
    })
    renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() => expect(report.onStarted).toHaveBeenCalledWith('https://app.example/'))
    // ORDER MATTERS: the URL first, then the outcome. Reporting the outcome first leaves one
    // commit in which the state says "running" and the pane has no address to show for it.
    const startedAt = (report.onStarted as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0]
    const outcomeAt = (report.onStartOutcome as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0]
    expect(startedAt).toBeLessThan(outcomeAt)
  })

  it('hands it back even when the page has not painted yet', async () => {
    // The container is up and the DOCUMENT is what has not arrived, so the frame's own load-gated
    // reveal is the right thing to be waiting on — not a sentence drawn in front of it.
    const report = reportSpy()
    api.relaunchPreview.mockResolvedValue({
      appId: 'a1', previewUrl: 'https://app.example/', status: 'provisioning',
      restoredFromFailedBuild: false, ready: false,
    })
    renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() => expect(report.onStarted).toHaveBeenCalledWith('https://app.example/'))
    expect(report.onStartOutcome).toHaveBeenCalledWith({ kind: 'not-painted' })
  })

  it('reports no URL when the server sent none', async () => {
    const report = reportSpy()
    api.relaunchPreview.mockResolvedValue({
      appId: 'a1', previewUrl: '', status: 'ready', restoredFromFailedBuild: false, ready: true,
    })
    renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() => expect(report.onStartOutcome).toHaveBeenCalledWith(null))
    expect(report.onStarted).not.toHaveBeenCalled()
  })
})

describe('★ the report reaches the surface even after this control is gone', () => {
  it('records a successful start whose button unmounted mid-flight', async () => {
    // THE BUG THIS IS WRITTEN AGAINST, and it was introduced by the fix one layer up. The moment a
    // press reaches the map the state becomes `starting`, which offers no action — so the button
    // that fired the request is unmounted before the request comes back. A `mounted` guard in
    // front of the report then swallowed the control's own success: no URL for the pane to frame,
    // no outcome to clear the wait, and the screen sat on "Getting your app ready." forever while
    // a perfectly good container served underneath it.
    //
    // `mounted` protects THIS component's state. The report writes into the SURFACE, which outlives
    // it and needs the answer either way.
    let finish: (v: unknown) => void = () => {}
    api.relaunchPreview.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    const report = reportSpy()
    const { unmount } = renderControl(START, report)
    fireEvent.click(button())

    // The control goes away while the request is still in the air.
    unmount()
    await act(async () => {
      finish({ appId: 'a1', previewUrl: 'https://app.example/', status: 'ready', restoredFromFailedBuild: false, ready: true })
    })

    expect(report.onStarted).toHaveBeenCalledWith('https://app.example/')
    expect(report.onStartOutcome).toHaveBeenCalledWith(null)
    // …and the wait is cleared, or the pane holds `starting` for the life of the tab.
    expect(report.onStartPending).toHaveBeenLastCalledWith(false)
  })

  it('records a REFUSAL the same way', async () => {
    api.relaunchPreview.mockRejectedValue(new ApiError('the sandbox is unavailable', 503))
    const report = reportSpy()
    const { unmount } = renderControl(START, report)
    fireEvent.click(button())
    unmount()

    await waitFor(() =>
      expect(report.onStartOutcome).toHaveBeenCalledWith({ kind: 'failed', reason: 'the sandbox is unavailable' }),
    )
  })
})


/**
 * ★ THE CLAIM BELONGS TO ONE PROJECT, AND THE SURFACE HOLDING IT IS NOT REMOUNTED WHEN THE SCREEN
 * MOVES TO ANOTHER.
 *
 * So a start still in the air when the screen changes has two ways to be wrong: it can report a
 * preview URL, a busy flag or a failure sentence into the project that arrived, and it can be
 * JOINED by that project's own trigger — which would leave the new app never started and its
 * citizen waiting on an answer about somebody else's.
 */
describe('★ a start that is overtaken by a change of project', () => {
  const spySinks = (projectId: string): StartSinks => ({
    projectId,
    onStarted: vi.fn(),
    onStartPending: vi.fn(),
    onStartOutcome: vi.fn(),
  })

  it('★ reports into nothing once the screen has moved on', async () => {
    let finish: (() => void) | undefined
    api.relaunchPreview.mockImplementation(
      () => new Promise((resolve) => {
        finish = () => resolve({ appId: 'a1', previewUrl: 'https://app/', status: 'ready', ready: true })
      }),
    )
    let sinks = spySinks('p1')
    const start = createStarter(() => sinks)
    const flight = start()

    const arrived = spySinks('p2')
    sinks = arrived
    finish?.()
    await flight

    expect(arrived.onStarted).not.toHaveBeenCalled()
    expect(arrived.onStartOutcome).not.toHaveBeenCalled()
    // LIVENESS: the start really did answer — it answered into nobody, which is the point.
    expect(api.relaunchPreview).toHaveBeenCalledWith({ projectId: 'p1' })
  })

  it('★ and the project that arrived starts its own app instead of joining it', async () => {
    api.relaunchPreview.mockImplementation(() => new Promise(() => {}))
    let sinks = spySinks('p1')
    const start = createStarter(() => sinks)
    const first = start()

    sinks = spySinks('p2')
    const second = start()

    expect(second).not.toBe(first)
    expect(api.relaunchPreview).toHaveBeenCalledWith({ projectId: 'p2' })
    expect(api.relaunchPreview).toHaveBeenCalledTimes(2)
  })
})
