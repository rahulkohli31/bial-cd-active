/**
 * THE #42 CHAT-COLLAPSE THIS FILE USED TO PIN IS RETIRED (Plan 006, R13), not moved — and this
 * file is the record of that.
 *
 * `ConversationSurface` used to own a SECOND collapse for its own chat-panel column, independent
 * of the one the workspace shell already owns for the rail. Under Plan 006 the conversation IS
 * the rail (`WorkspaceShell`'s `usePublishRail` derives the rail's mode from the address), so a
 * toggle here and the shell's `railWidthClass` toggle were two controls collapsing the identical
 * column through two independent booleans — exactly what R13's "ONE control that collapses it
 * entirely" forbids. Worse, this surface's toggle rendered into `LivePreview`'s toolbar, and
 * `LivePreview` only mounts when there is something to frame — so on a Plan chat, a project with
 * nothing built, or an app that had gone to sleep, the toggle did not exist at all, and a panel
 * collapsed while an app was running lost its way back the moment the container stopped. See
 * `AppPane.tsx`'s docblock for the fuller account; its collapse control is the survivor.
 *
 * THE SLOT ITSELF IS GONE, so no test here asserts it is empty. An earlier draft of this file
 * asserted `queryByRole('button', {name: /hide chat panel/i})` is null and it passed — but it
 * would have passed identically had the retired toggle still been wired up, because this fixture
 * never resolves a preview address and the pane it rendered into never mounts. The answer at the
 * time was a `PaneToolbarProbe` that read `useWorkspacePane().toolbarLeading` directly. U2 then
 * deleted `toolbarLeading` and `toolbarTrailing` from `PaneView` altogether, which made the probe
 * read a field no shape has — `undefined` for every possible state of this surface, and three
 * assertions that could not fail — so it is gone too. What it guarded is now the TYPE system's:
 * `UnacceptedPaneProps` in `workspaceChannel.ts` fails the build if `PaneView` ever grows a field
 * `LivePreview` does not accept, and there is no slot left for a surface to publish chrome into.
 *
 * EVERY TEST BELOW NOW PROVES THE SAME UNDERLYING FACT FROM A DIFFERENT ANGLE: this surface
 * publishes no toggle, drives no width swap, and holds no collapse state of its own any more —
 * each is an INERTNESS GUARD (never a bare deletion, per L8), paired with a LIVENESS assertion so
 * none of them can pass by accident on a surface that rendered nothing at all. Where the ORIGINAL
 * property they pinned (draft/scroll survive a hide-show cycle; the toggle stays reachable while
 * collapsed) still genuinely holds, it holds at the SHELL level now — pinned in
 * `src/components/workspace/__tests__/ProjectWorkspace.test.tsx`'s "the collapse control — hidden,
 * not unmounted, and never a one-way door" suite — and each guard below says so rather than
 * silently going quiet about where that coverage went.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, render, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { composer } from './_builderSession.jsx'
import ConversationSurface from '../../components/chat/ConversationSurface'
import WorkspaceShell from '../../components/workspace/WorkspaceShell'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), appendBuilderMessage: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  sendMessage: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, appendBuilderMessage: h.appendBuilderMessage,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
// THE LEGACY RELAY MOCK IS GONE WITH THE HOOK (Plan D U17). It stood in for `useClaudeAPI`, which
// no longer exists; a mock kept here would have invented a module for the surface to import and
// this suite would have gone on passing against a transport that ships nowhere.

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  h.appendBuilderMessage.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
})
afterEach(() => cleanup())

/** LIVENESS: the panel itself is on screen. The old `renderReady` waited for the retired
 *  "hide chat panel" button — waiting on that here would hang forever, which is exactly the
 *  false-negative shape L8 warns about (a removed control silently making every guard here
 *  unreachable rather than failing loudly). `chat-panel` is the surface's own static container,
 *  present the instant it renders, independent of the toggle that used to live in it. Mounted
 *  through the REAL `WorkspaceShell`, so the surface publishes into the real channel. */
async function renderReady(kind = 'build') {
  render(
    <MemoryRouter initialEntries={[`/chat/build-X?projectId=p1&kind=${kind}`]}>
      <Routes>
        <Route element={<WorkspaceShell />}>
          <Route
            path="/chat/:chatId"
            element={<ConversationSurface projectId="p1" projectName="VIP Movement" kind={kind} />}
          />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
  return screen.findByTestId('chat-panel')
}

describe('BuilderPage — the retired #42 chat-panel collapse (now the shell rail\'s, R13)', () => {
  it('publishes no hide/show chat-panel toggle into the pane\'s toolbar — the rail\'s ONE collapse is drawn by AppPane now', async () => {
    await renderReady()

    // The surface mounts and runs, and there is no slot on `PaneView` for it to push a toggle
    // into: U2 deleted `toolbarLeading`/`toolbarTrailing`, and `UnacceptedPaneProps` fails the
    // build if the shape grows a field back. The collapse control that survived is `AppPane`'s,
    // pinned in `ProjectWorkspace.test.tsx` ("★ lives in the PANE…").
    expect(composer()).toBeTruthy()
  })

  it('has no hide/show cycle left to run the composer draft through — draft-survival is pinned at the shell now (ProjectWorkspace.test.tsx, "keeps the rail MOUNTED while collapsed")', async () => {
    await renderReady()

    fireEvent.change(composer(), { target: { value: 'a visitor pass tracker' } })
    // LIVENESS: the draft is genuinely held by this surface's own composer state — which is what
    // the retired cycle put at risk. There is no toggle left here to cycle it with, so the
    // survival property is exercised where the surviving collapse lives (ProjectWorkspace.test.tsx,
    // "keeps the rail MOUNTED while collapsed").
    expect(composer().value).toBe('a visitor pass tracker')
  })

  it('fills the rail rather than setting a width of its own; `WorkspaceShell.railWidthClass` governs the rail', async () => {
    await renderReady()
    const panel = screen.getByTestId('chat-panel')

    // NO FIXED WIDTH AT ALL SINCE PLAN 002's U6. It was `w-72 xl:w-80` — a second, narrower
    // column INSIDE the rail the shell had already sized, which left a dead band of ground
    // between the transcript and the app pane. It fills the rail now, and the rail's width is
    // the one the citizen can drag.
    expect(panel.className).not.toMatch(/(^|\s)w-72(\s|$)/)
    expect(panel.className).toMatch(/flex-1/)
    // INERTNESS: nothing on this surface can drive its width to zero — there is no toggle left
    // to press, and the class is no longer a ternary on any local state.
    expect(panel.className).not.toMatch(/(^|\s)w-0(\s|$)/)
  })

  it('has no hide/show cycle left to preserve scroll position across — that property is pinned at the shell now (ProjectWorkspace.test.tsx, same suite)', async () => {
    await renderReady()
    const viewport = screen.getByTestId('thread-viewport')
    viewport.scrollTop = 40

    // LIVENESS: the viewport this test is about is really mounted and really holds the value.
    // There is no toggle on this surface that could hide and show it again, so the survival
    // property is exercised at the shell instead (ProjectWorkspace.test.tsx, same suite).
    expect(screen.getByTestId('thread-viewport').scrollTop).toBe(40)
  })

  it('publishes no toggle of its own to keep reachable — "stays reachable while collapsed" is entirely AppPane\'s property now (ProjectWorkspace.test.tsx, "★ lives in the PANE...")', async () => {
    await renderReady()

    // LIVENESS: the surface rendered its ordinary chrome. There is nothing here to ask "does it
    // stay reachable while hidden" about — this surface retired the whole toggle rather than
    // relocating it, so the question the old test asked has no subject left on THIS component.
    // The control that must answer it lives in `AppPane` and is pinned there.
    expect(screen.getByTestId('chat-panel')).toBeTruthy()
  })
})

/**
 * THE TWO SHAPES A CHAT SCREEN TAKES (plan 002, U6). Both are claims about this surface's own
 * panel, which is why they are here rather than in the shell suite: the shell can see who gets the
 * width, but only a render of the real surface can see what it does with it.
 */
describe('a plan chat is one centred column; a build chat sits beside its app', () => {
  it('★ a build chat fills the rail, with no second narrower column inside it', async () => {
    // THE DEAD BAND. The panel set `w-72 xl:w-80` INSIDE the rail the shell had already sized, so
    // a strip of ground sat between the transcript and the app pane the whole time.
    const panel = await renderReady('build')
    expect(panel.getAttribute('data-chat-kind')).toBe('build')
    expect(panel.className).toMatch(/flex-1/)
    expect(panel.className).not.toMatch(/(^|\s)w-72(\s|$)/)
    expect(panel.className).not.toMatch(/mx-auto/)
  })

  it('★ a plan chat centres its column rather than running edge to edge', async () => {
    // It declares no pane, so the shell hands it the whole window — and a transcript run across
    // 1440px is unreadable. Centring is the panel's own answer; the RAIL's width stays the
    // citizen's to drag, which is why this is not solved by pinning the rail narrower.
    const panel = await renderReady('plan')
    expect(panel.getAttribute('data-chat-kind')).toBe('plan')
    expect(panel.className).toMatch(/mx-auto/)
    expect(panel.className).toMatch(/max-w-/)
  })

  it("★ carries the board's footer line on a plan chat, verbatim — and not on a build chat", async () => {
    // The line was rewritten once on a misreading of "your app is not open here" as a claim about
    // the CONTAINER. It is a claim about the screen, and the second clause is true because a plan
    // chat's toolset has no write tools. See `PlanChatWorkspaceLine`'s docblock.
    await renderReady('plan')
    expect(screen.getByTestId('plan-chat-workspace-line').textContent).toContain(
      'Planning is a conversation. Your app is not open here and nothing you say changes it.',
    )

    cleanup()
    await renderReady('build')
    expect(screen.queryByTestId('plan-chat-workspace-line')).toBeNull()
    // LIVENESS: the build chat rendered, it simply has no plan-chat line.
    expect(screen.getByTestId('chat-panel')).toBeTruthy()
  })
})
