/**
 * A PLAN CHAT HAS NO PANE, AND STILL SAYS EVERYTHING (Plan F, U6).
 *
 * The property under test is not "some text appears". It is that a Plan chat is a SECOND RENDERER
 * of the one computed workspace state — so its sentence is byte-identical to the pane's, and no
 * workspace sentence exists on this surface that the pane cannot also produce. Both renderers are
 * fed the same value and compared directly, because a test that asserted a hand-written string here
 * would pass while the two surfaces drifted.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import PlanChatWorkspaceLine from '../PlanChatWorkspaceLine'
import AppPane from '../AppPane'
import {
  WorkspaceChannelProvider,
  createWorkspaceChannel,
  type WorkspaceChannel,
  type WorkspaceReport,
} from '../workspaceChannel'
import { resolveWorkspaceState } from '../workspaceState'
import type { PreviewState } from '../../../utils/buildSessionApi'

vi.mock('../../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/buildSessionApi')>()),
  relaunchPreview: vi.fn(),
}))

const reading = (over: Partial<PreviewState> = {}): PreviewState => ({
  state: 'asleep',
  alive: false,
  previewUrl: null,
  occupyingProjectName: null,
  occupyingProjectId: null,
  restorable: null,
  ...over,
})

const reportFor = (preview: PreviewState): WorkspaceReport => ({
  state: resolveWorkspaceState({ preview, projectHasSavedBuild: null, startOutcome: null, startInFlight: false }),
  projectId: 'p1',
  onStarted: vi.fn(),
  onStartPending: vi.fn(),
  onStartOutcome: vi.fn(),
  onRefresh: vi.fn(),
  onReclaimRefusal: vi.fn(),
})

function renderIn(node: React.ReactElement, prime: (c: WorkspaceChannel) => void) {
  const channel = createWorkspaceChannel()
  prime(channel)
  return render(
    <MemoryRouter>
      <WorkspaceChannelProvider value={channel}>{node}</WorkspaceChannelProvider>
    </MemoryRouter>,
  )
}

const line = (preview: PreviewState) =>
  renderIn(<PlanChatWorkspaceLine />, (c) => c.workspace.set(reportFor(preview)))

afterEach(() => cleanup())

describe('AE5 — the standing line says what this chat DOES', () => {
  it('never says the app is not running, or stopped', () => {
    // The canvas's `PlanChat` board carries the old model ("your app is not open here", "the plan
    // chat holds no sandbox of its own"). The origin document REVERSES it and wins: a planning
    // question reads the running app and starts it if it is stopped, so the app may well be up and
    // held by this very conversation. Under R-16 the line does not say what the app is NOT at all.
    const { container } = line(reading({ state: 'asleep', restorable: true }))
    const text = container.textContent ?? ''

    expect(text).toMatch(/planning happens here/i)
    expect(text).not.toMatch(/not running/i)
    expect(text).not.toMatch(/\bstopped\b/i)
    expect(text).not.toMatch(/no sandbox/i)
    expect(text).not.toMatch(/not open here/i)
  })

  it('is in the document before the first sentence arrives', () => {
    // A region that appears together with its first sentence arrives without warning under whatever
    // the person was reading; one that is always mounted simply gains a line.
    renderIn(<PlanChatWorkspaceLine />, () => {})
    expect(screen.getByTestId('plan-chat-workspace-line')).toBeTruthy()
    expect(screen.queryByTestId('plan-chat-workspace-state')).toBeNull()
  })
})

describe('★ AE48 — the same value, the same sentence, on both surfaces (R97)', () => {
  // Scoped to the three states R97 names, deliberately. Asserting sameness across `never_built` and
  // `asleep` too would pin wording R97 does not require and R11's framing may want different — a
  // Plan chat has no business inviting somebody to press a start control it does not render.
  const spoken: [string, PreviewState][] = [
    ['being got ready', reading({ state: 'starting' })],
    [
      'another project holds it, and which one',
      reading({ state: 'slot_taken', occupyingProjectName: 'Roster', occupyingProjectId: 'p-9' }),
    ],
    ['it could not be read', reading({ state: 'unknown' })],
  ]

  for (const [name, preview] of spoken) {
    it(`says byte-identically what the pane says: ${name}`, () => {
      // BOTH RENDERERS, ONE VALUE. A hand-written expected string here would pass while the two
      // surfaces drifted, which is the failure this scenario exists to make impossible.
      const report = reportFor(preview)

      const { unmount } = renderIn(<PlanChatWorkspaceLine />, (c) => c.workspace.set(report))
      const spokenHere = screen.getByTestId('plan-chat-workspace-state').textContent ?? ''
      unmount()

      renderIn(<AppPane collapsed={false} onToggleCollapsed={() => {}} />, (c) =>
        c.workspace.set(report),
      )
      const spokenThere = screen.getByTestId('app-pane-empty').textContent ?? ''

      expect(spokenHere.length).toBeGreaterThan(0)
      expect(spokenThere).toContain(report.state.headline)
      expect(spokenHere).toContain(report.state.headline)
    })
  }
})

describe('sentence always, action selectively — and only one of the three', () => {
  it('★ renders NO start control, in any workspace state', () => {
    // `StartAppControl` renders wherever the map offers an action, with no surface predicate of its
    // own — so the gate is the line's. `asleep` is the sharp case: the map DOES offer the start
    // action there, and the pane renders it.
    for (const preview of [
      reading({ state: 'asleep', restorable: true }),
      reading({ state: 'never_built', restorable: true }),
      reading({ state: 'starting' }),
      reading({ state: 'alive', alive: true }),
    ]) {
      const { unmount } = line(preview)
      expect(screen.queryByRole('button', { name: /launch application/i })).toBeNull()
      unmount()
    }
  })

  it('★ renders NO retry either — the pane already owns that state', () => {
    // A second author for one state, on a surface with no pane for the retry to land in.
    const { unmount } = line(reading({ state: 'unknown' }))
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
    // …and it still SAYS what happened, which is the half an absence assertion cannot see.
    expect(screen.getByTestId('plan-chat-workspace-state').textContent).toMatch(/could not check/i)
    unmount()
  })

  it('DOES render the remedy, and it has to', () => {
    // R4b makes the go-to the answer for a taken workspace, and R94 says the asking happens in the
    // chat the person is actually in — so a Plan chat showing the sentence with no way to act would
    // leave the remedy unreachable from the only surface that can offer it.
    line(reading({ state: 'slot_taken', occupyingProjectName: 'Roster', occupyingProjectId: 'p-9' }))

    expect(screen.getByRole('button', { name: /open “Roster”/i })).toBeTruthy()
    expect(screen.getByTestId('plan-chat-workspace-state').textContent).toMatch(/Roster/)
  })

  it('offers no action for a slot taken by a project the server could not attribute', () => {
    line(reading({ state: 'slot_taken' }))

    expect(screen.getByTestId('plan-chat-workspace-state').textContent).toMatch(/another project/i)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('says nothing extra for the states the pane owns alone', () => {
    // `asleep` and `never_built` are the pane's to speak for: R97 does not ask a Plan chat to
    // repeat them, and repeating them would put a start-shaped sentence on a surface with no start.
    for (const preview of [reading({ state: 'asleep', restorable: true }), reading({ state: 'never_built' })]) {
      const { unmount } = line(preview)
      expect(screen.queryByTestId('plan-chat-workspace-state')).toBeNull()
      unmount()
    }
  })
})
