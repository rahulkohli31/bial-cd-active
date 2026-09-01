/**
 * THE ONE CONTROL THAT STARTS THE APP (Plan F, U3).
 *
 * ═══ WHAT A TEST IN THIS FILE CAN HONESTLY PROVE ═══
 *
 * That this component makes one request, discriminates the refusals correctly, and never names a
 * destructive verb. It CANNOT prove the endpoint is non-destructive — the component was never the
 * thing that could have destroyed a container. That proof is server-side, against L3's confirmation
 * triple, in `backend/tests/api/v1/build_sessions/test_preview_state.py`, and this plan added the
 * guard it was missing on the exact arm this button enters.
 *
 * So there is deliberately NO test here shaped "no stop, release or restore call was made". It
 * would pass in the very state that loses work, and its greenness would be mistaken for evidence.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import StartAppControl from '../StartAppControl'
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
  return {
    state: { name: 'not-running', headline: 'Your app is saved.', detail: null, action: START },
    projectId: 'p1',
    onStarted: vi.fn(),
    onStartOutcome: vi.fn(),
    onRefresh: vi.fn(),
    onReclaimRefusal: vi.fn(),
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

describe('AE1 — one deliberate press, one request', () => {
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
    // A synchronous ref, not state: state would not have committed between the two clicks, so a
    // `pending` flag alone lets both through and provisions a second container.
    let release: (v: unknown) => void = () => {}
    api.relaunchPreview.mockImplementation(() => new Promise((r) => { release = r }))
    renderControl(START, reportSpy())

    fireEvent.click(button())
    fireEvent.click(button())
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

describe('R4b — a start that did not end in a running app says which way it ended', () => {
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
    // said why", and R4b asks for the difference to reach the citizen.
    api.relaunchPreview.mockRejectedValue(new TypeError('Failed to fetch'))
    const report = reportSpy()
    renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() => expect(report.onStartOutcome).toHaveBeenCalledWith({ kind: 'timed-out' }))
  })

  it('AE3: a start whose readiness cannot be read issues no second call of its own', async () => {
    // Asserting what the COMPONENT does. Deliberately not written as "no stop, release or restore
    // call was made" — see this file's docblock for why that assertion would prove nothing.
    api.relaunchPreview.mockRejectedValue(new ApiError('could not read', 503))
    renderControl(START, reportSpy())
    fireEvent.click(button())

    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalledTimes(1))
    expect(api.relaunchPreview).toHaveBeenCalledTimes(1)
  })
})

describe('the two 409s stay apart — one status, two causes, two remedies', () => {
  it('★ routes a reclaim refusal to the ONE dialog, not to a retry', async () => {
    const err = new ApiError('another project', 409, 'sandbox_reclaim_blocked')
    Object.assign(err, { details: { projectId: 'p-other', projectName: 'Car pool apps', dirty: true } })
    api.relaunchPreview.mockRejectedValue(err)
    const report = reportSpy()
    renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() => expect(report.onReclaimRefusal).toHaveBeenCalled())
    expect(report.onStartOutcome).not.toHaveBeenCalled()
    const [blocked] = (report.onReclaimRefusal as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(blocked).toMatchObject({ projectId: 'p-other', projectName: 'Car pool apps' })
  })

  it('does NOT route your own running build to that dialog', async () => {
    // Different cause, different remedy — finish or stop it, versus save or switch that project.
    // Merging them would put a Save button in front of somebody it cannot help.
    api.relaunchPreview.mockRejectedValue(new BuildSessionAlreadyActiveError('already', 's-1'))
    const report = reportSpy()
    renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() => expect(report.onStartOutcome).toHaveBeenCalled())
    expect(report.onReclaimRefusal).not.toHaveBeenCalled()
  })

  it('does not treat an uncoded 409 as the reclaim refusal', async () => {
    api.relaunchPreview.mockRejectedValue(new ApiError('conflict', 409))
    const report = reportSpy()
    renderControl(START, report)
    fireEvent.click(button())

    await waitFor(() => expect(report.onStartOutcome).toHaveBeenCalled())
    expect(report.onReclaimRefusal).not.toHaveBeenCalled()
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
    // Still named, and now carrying the reason.
    expect(button().getAttribute('aria-label')).toMatch(/Launch Application — Starting your app/)
    button().focus()
    expect(document.activeElement).toBe(button())
  })
})

describe('the other two verbs, and the one that does not exist', () => {
  it('a retry clears the last outcome before asking again', async () => {
    // Without the clear, a second failure of the same kind leaves the sentence unchanged and the
    // press looks like it did nothing.
    const report = reportSpy()
    renderControl(RETRY, report)
    fireEvent.click(button())

    expect(report.onStartOutcome).toHaveBeenCalledWith(null)
    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalled())
  })

  it('the go-to action navigates and starts NOTHING', async () => {
    const report = reportSpy()
    renderControl({ kind: 'go-to-project', label: 'Open “Roster”', projectId: 'p-other' }, report)
    fireEvent.click(button())

    await waitFor(() => expect(screen.getByTestId('path').textContent).toBe('/projects/p-other'))
    expect(api.relaunchPreview).not.toHaveBeenCalled()
  })

  it('★ names no destructive verb, in any action, in any state', async () => {
    // The type is the enforcement — three members, none destructive — but a LABEL can still say a
    // dangerous word, and this is what catches that.
    const destructive = /\b(restore|rebuild|reset|delete|destroy|tear down|discard|wipe|erase)\b/i
    const actions: WorkspaceAction[] = [
      START,
      RETRY,
      { kind: 'go-to-project', label: 'Open “Roster”', projectId: 'p-other' },
    ]
    for (const action of actions) {
      const { container } = renderControl(action, reportSpy())
      expect(container.textContent ?? '').not.toMatch(destructive)
      expect(container.querySelector('button')?.getAttribute('aria-label') ?? '').not.toMatch(destructive)
      cleanup()
    }
  })
})

describe('★ the URL a successful start produced reaches the surface that frames it', () => {
  it('hands the preview URL back before it reports the outcome', async () => {
    // WITHOUT THIS THE CONTROL DID NOTHING VISIBLE INSIDE A BUILD CHAT. That surface feeds the
    // address resolver's project-scoped arm with `null` — its own poll only runs over an ALREADY
    // framed URL, by design — and its `relaunchedUrl` arm was fed by a Relaunch button this plan
    // retired. So a fresh start had no arm left to populate: the app came up in a container
    // nothing framed, and the citizen saw a sentence where their app should have been.
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
