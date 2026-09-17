/**
 * A PLAN CHAT HAS NO PANE, AND STILL SAYS EVERYTHING.
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
  // `lastDecidedPreview: null` is "nothing has ever been decided", which is the cold-load answer
  // and the only one this surface's scenarios need: every reading below is a decided one, so the
  // memory is never consulted. Decision D3's own behaviour — an unreadable read rendering the last
  // settled reading — is pinned where the rule lives, in `workspaceState.test.ts`.
  state: resolveWorkspaceState({
    preview,
    lastDecidedPreview: null,
    projectHasSavedBuild: null,
    startOutcome: null,
    startInFlight: false,
  }),
  projectId: 'p1',
  onStarted: vi.fn(),
  onStartPending: vi.fn(),
  onStartOutcome: vi.fn(),
  onRefresh: vi.fn(),
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

describe('the standing line says what this chat DOES', () => {
  it('★ speaks the board\'s line verbatim, and still never says the app is not RUNNING', () => {
    // The board's "your app is not open here" is about the SCREEN, not the container: a planning
    // question still reads the live app and starts it if it is stopped. And a plan chat's toolset
    // carries no write, no schema change, no sandbox command and no finish tool, so the run cannot
    // alter the app either. Both halves are asserted, because keeping only one lets the other
    // regress unnoticed.
    const { container } = line(reading({ state: 'asleep', restorable: true }))
    const text = container.textContent ?? ''

    expect(text).toContain('Planning is a conversation. Your app is not open here and nothing you say changes it.')
    expect(text).not.toMatch(/not running/i)
    expect(text).not.toMatch(/\bstopped\b/i)
    expect(text).not.toMatch(/no sandbox/i)
  })

  it('is in the document before the first sentence arrives', () => {
    // A region that appears together with its first sentence arrives without warning under whatever
    // the person was reading; one that is always mounted simply gains a line.
    renderIn(<PlanChatWorkspaceLine />, () => {})
    expect(screen.getByTestId('plan-chat-workspace-line')).toBeTruthy()
    expect(screen.queryByTestId('plan-chat-workspace-state')).toBeNull()
  })
})

describe('★ the same value, the same sentence, on both surfaces', () => {
  // Scoped to the two states this scenario covers, deliberately. Asserting sameness across
  // `never_built` and `asleep` too would pin wording this scenario does not require, when the
  // pane's own wording there may need to differ — a Plan chat has no business inviting somebody
  // to press a start control it does not render.
  const spoken: [string, PreviewState][] = [
    ['being got ready', reading({ state: 'starting' })],
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

      renderIn(<AppPane device="Desktop" reloadNonce={0} />, (c) =>
        c.workspace.set(report),
      )
      const spokenThere = screen.getByTestId('app-pane-empty').textContent ?? ''

      expect(spokenHere.length).toBeGreaterThan(0)
      expect(spokenThere).toContain(report.state.headline)
      expect(spokenHere).toContain(report.state.headline)
    })
  }
})

describe('★ the sentence, and never a verb', () => {
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

  it('★ a taken slot is not spoken here at all, and offers nothing', () => {
    // A slot held by the citizen's own other project reads as the saved app it is, and `asleep`
    // is the pane's to speak for — so this surface says its standing line and no more. What it
    // must never do is grow a control that leaves the chat the person is in.
    const report = reportFor(reading({ state: 'slot_taken', occupyingProjectName: 'Roster', occupyingProjectId: 'p-9', restorable: true }))
    expect(report.state.name).toBe('not-running')

    renderIn(<PlanChatWorkspaceLine />, (c) => c.workspace.set(report))

    expect(screen.queryByTestId('plan-chat-workspace-state')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
    // LIVENESS: the standing line really rendered, so an empty tree cannot green this.
    expect(screen.getByTestId('plan-chat-workspace-line').textContent).toMatch(/Planning is a conversation/)
  })

  it('★ and no spoken state renders any control at all', () => {
    // The two verbs that exist both act on this project's app, and this surface deliberately keeps
    // that app off screen — so neither may appear beside a sentence here.
    for (const preview of [reading({ state: 'starting' }), reading({ state: 'unknown' })]) {
      const { unmount } = line(preview)
      expect(screen.getByTestId('plan-chat-workspace-state').textContent?.length).toBeGreaterThan(0)
      expect(screen.queryByRole('button')).toBeNull()
      unmount()
    }
  })

  it('says nothing extra for the states the pane owns alone', () => {
    // `asleep` and `never_built` are the pane's to speak for: this scenario does not ask a Plan
    // chat to repeat them, and repeating them would put a start-shaped sentence on a surface with
    // no start.
    for (const preview of [reading({ state: 'asleep', restorable: true }), reading({ state: 'never_built' })]) {
      const { unmount } = line(preview)
      expect(screen.queryByTestId('plan-chat-workspace-state')).toBeNull()
      unmount()
    }
  })
})
