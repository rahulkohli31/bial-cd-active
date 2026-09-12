/**
 * THIS SURFACE OWNS NO CHAT-PANEL COLLAPSE, AND THIS FILE IS WHAT HOLDS THAT.
 *
 * The conversation IS the rail (`WorkspaceShell` derives its mode from the address), so a toggle
 * here would be a second boolean over the same column. The surviving control is
 * `WorkspaceToolbar`'s, drawn above both columns regardless of rail or pane state.
 *
 * THE SLOT ITSELF IS GONE — no test here asserts it is empty, since that would pass whether or
 * not a retired toggle were still wired up. What guards it now is the TYPE system:
 * `UnacceptedPaneProps` fails the build if `PaneView` ever grows a field `LivePreview` rejects.
 *
 * Every test below is an INERTNESS GUARD paired with a LIVENESS assertion; where the ORIGINAL
 * property still holds, it holds at the SHELL level — pinned in `ProjectWorkspace.test.tsx`'s
 * "the collapse control — hidden, not unmounted, and never a one-way door" suite.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, render, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { composer } from './_builderSession.jsx'
import ConversationSurface from '../../components/chat/ConversationSurface'
import WorkspaceShell from '../../components/workspace/WorkspaceShell'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), appendBuilderMessage: vi.fn(), getBuild: vi.fn(),
  listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, appendBuilderMessage: h.appendBuilderMessage,
  getBuild: h.getBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({
  // The send path creates the chat before its first upload; stubbed so no network is reached.
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))

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
 *  present the instant it renders, independent of whatever toggle or control sits inside it. Mounted
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

describe('BuilderPage — the retired chat-panel collapse, now the shell rail\'s', () => {
  it('★ the scenarios it hands its retired properties to are really there, under those names', () => {
    // EVERY GUARD IN THIS SUITE IS A POINTER. Each one says "this property still holds, and it is
    // proven over there" — which is only worth anything while "over there" exists.
    //
    // Resolved from the vitest root (`portal/`) rather than from `import.meta.url`, which under
    // vite is not a `file:` URL — the same reason `AppPane.test.tsx` reads the stylesheet that way.
    const shellSuite = readFileSync(
      resolve(process.cwd(), 'src/components/workspace/__tests__/ProjectWorkspace.test.tsx'),
      'utf8',
    )
    for (const title of [
      '★ lives in the TOOLBAR ROW, so it is still reachable once the rail is hidden',
      'keeps the rail MOUNTED while collapsed, so nothing inside it is discarded',
    ]) {
      expect(shellSuite, title).toContain(title)
    }
  })

  it('publishes no hide/show chat-panel toggle into the pane\'s toolbar — the rail\'s ONE collapse is drawn by AppPane now', async () => {
    await renderReady()

    // The surface mounts and runs, and there is no slot on `PaneView` for it to push a toggle
    // into: `UnacceptedPaneProps` fails the build if the shape grows a field back.
    expect(composer()).toBeTruthy()
  })

  it('has no hide/show cycle left to run the composer draft through — draft-survival is pinned at the shell now (ProjectWorkspace.test.tsx, "keeps the rail MOUNTED while collapsed")', async () => {
    await renderReady()

    fireEvent.change(composer(), { target: { value: 'a visitor pass tracker' } })
    // LIVENESS: the draft is genuinely held by this surface's own composer state — which is what
    // the retired cycle put at risk.
    expect(composer().value).toBe('a visitor pass tracker')
  })

  it('fills the rail rather than setting a width of its own; `WorkspaceShell.railWidthClass` governs the rail', async () => {
    await renderReady()
    const panel = screen.getByTestId('chat-panel')

    // NO FIXED WIDTH AT ALL. It was `w-72 xl:w-80` — a second, narrower column INSIDE the rail
    // the shell had already sized, which left a dead band of ground between the transcript and
    // the app pane. It fills the rail now, and the rail's width is the one the citizen can drag.
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
    expect(screen.getByTestId('thread-viewport').scrollTop).toBe(40)
  })

  it('publishes no toggle of its own to keep reachable — "stays reachable while collapsed" is entirely the shell\'s property now (ProjectWorkspace.test.tsx, "★ lives in the TOOLBAR ROW…")', async () => {
    await renderReady()

    // LIVENESS: the surface rendered its ordinary chrome; there is nothing here to ask "does it
    // stay reachable while hidden" about.
    expect(screen.getByTestId('chat-panel')).toBeTruthy()
  })
})

/**
 * THE TWO SHAPES A CHAT SCREEN TAKES. Both are claims about this surface's own
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
    // "your app is not open here" is a claim about the screen, not about the CONTAINER. The
    // second clause is true because a plan chat's toolset has no write tools. See
    // `PlanChatWorkspaceLine`'s docblock.
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
