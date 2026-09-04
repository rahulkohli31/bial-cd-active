/**
 * U8 (F7 + F10) — the trimmed chat header.
 *
 * The header is gone from this surface entirely (plan 002, U2): the redundant in-rail usage meter
 * (F7), the AI branding block, its avatar and the "Recent" builds dropdown (F10) went first, and
 * the breadcrumb that was left went to the shell's toolbar row, which draws the project, the chat's
 * kind and the chat's title above both columns. What this file still pins is the ABSENCE of the
 * four removed things, which is unaffected by where the surviving header lives.
 *
 * THE LOAD-BEARING CORRECTION (the review's catch): only the strictly dropdown-scoped state was
 * removed. `builds`/`refreshBuilds` were KEPT — `buildBlockedMessage` reads `builds` to name the
 * chat holding a cross-tab build lock. The last test pins that: with another tab building, the
 * Build-it blocked advisory still names the blocking chat by title. Had `builds`/`refreshBuilds`
 * gone with the dropdown, that message would degrade to "another build chat" (or throw).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, within, act, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useParams } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient, primeTurn, waitForGateOpen, scriptBuildTurn,
  BUILD_TURN_ID,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  resolvePlanOptions: vi.fn(),
  stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(), relaunchPreview: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
// SPREAD THE ORIGINAL — `uuidv7` is the shared mint `handleBuildIt` uses for the new build chat's
// id (ADR-0006), and a factory that lists only `listProjectConversations` leaves every OTHER
// export undefined. That used to be silent breakage; Vitest now warns loudly ("No 'uuidv7' export
// is defined on the mock") the moment a real caller reaches for it — which `handleBuildIt` does on
// every Build-it press, so the case had to be fixed here rather than merely noted.
vi.mock('../../utils/conversationApi', async (importOriginal) => ({
  ...(await importOriginal()),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
vi.mock('../../components/AttachmentChips', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
// `switchMode` is GONE from this list (U1/U19): the route it posted to no longer exists, and a
// chat's kind can't change after creation, so there is nothing left for a mock to intercept.
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

import ConversationSurface from '../../components/chat/ConversationSurface'

/** The project page the breadcrumb links back to — echoes its :pid so the route is assertable. */
function ProjectPageStub() {
  const { pid } = useParams()
  return <div data-testid="project-page">project page: {pid}</div>
}

function renderBuilder({ chatId = 'thread-1', projectId = 'p1', projectName = 'VIP Movement' } = {}) {
  const fake = new FakeEventSource(chatId)
  const deps = { client: makeClient(h), eventSourceFactory: () => fake }
  const view = render(
    <MemoryRouter initialEntries={[`/chat/${chatId}`]}>
      <Routes>
        <Route
          path="/chat/:chatId"
          element={<ConversationSurface projectId={projectId} projectName={projectName} buildSessionDeps={deps} />}
        />
        <Route path="/projects/:pid" element={<ProjectPageStub />} />
      </Routes>
    </MemoryRouter>,
  )
  return { ...view, fake }
}

const composerIn = (c) => within(c).getByPlaceholderText(/ask for another change/i)
async function sendFrom(c, text = 'a visitor app') {
  await waitForGateOpen()
  fireEvent.change(composerIn(c), { target: { value: text } })
  fireEvent.keyDown(composerIn(c), { key: 'Enter' })
}
/** The whole user path to a build: send a turn, get the brief card, click Build it. */
async function buildFrom(c, text = 'a visitor app') {
  sendFrom(c, text)
  fireEvent.click(await within(c).findByRole('button', { name: /^Build this plan$/ }))
}
// BroadcastChannel delivery is queued on a task: a new manager posts `poll`, the holder answers
// `announce`, and only then does the newcomer's `blockedBy` see the claim. Drain a few ticks.
const flushChannel = () => act(async () => { for (let i = 0; i < 6; i += 1) await new Promise((r) => setTimeout(r, 0)) })

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockImplementation(async (id) => ({ id, kind: 'build', messages: [] }))
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  primeTurn(h)
  // A build is a Write TURN (U5): the confirmed brief opens a second socket, and it stays open —
  // a HELD cross-tab claim is exactly a build that has not finished.
  h.readTurnStream.mockImplementation(scriptBuildTurn().impl)
})
afterEach(() => cleanup())

describe('U8 — the chat surface draws no header of its own', () => {
  it('renders none of the removed chrome, and no header either', async () => {
    const { container } = renderBuilder()
    // Settle the async adopt (getBuild → welcome message) before asserting on the header.
    await screen.findByPlaceholderText(/ask for another change/i)

    // GONE (plan 002, U2): the breadcrumb that was the last thing left in this header. The
    // project, the chat's kind and the chat's title are drawn by the shell's toolbar row above
    // both columns — see `WorkspaceToolbar.test.tsx`.
    expect(screen.queryByRole('link', { name: /VIP Movement/i })).toBeNull()

    // GONE (F10): the AI branding block + its avatar. "powered by Anthropic" was branding-only
    // (the welcome bubble never says it); the online-status dot was the avatar's distinguishing mark.
    expect(screen.queryByText(/powered by Anthropic/i)).toBeNull()
    expect(container.querySelector('.bg-green-400')).toBeNull()

    // GONE (F10): the "Recent" builds button + its dropdown.
    expect(screen.queryByRole('button', { name: /Recent/i })).toBeNull()
    expect(screen.queryByText(/Recent builds/i)).toBeNull()

    // GONE (F7): the redundant in-rail usage meter (real usage lives in the global nav).
    expect(screen.queryByRole('progressbar', { name: /daily assistant usage/i })).toBeNull()
    expect(container.querySelector('.usage-meter')).toBeNull()
  })

  /* WHERE THE BACK CONTROL GOES is the toolbar row's, and it is asserted there — including the
     half this test could never see, that it routes through the workspace's unsaved-work guard
     rather than navigating straight out. */
})

describe('U8 regression guard — builds/refreshBuilds survive the dropdown removal', () => {
  it('the Build-it blocked advisory still names the blocking chat by title', async () => {
    // Build-it is a HANDOFF now (U5/U12): pressing it in `build-A` creates a SECOND, brand-new
    // build chat and the claim + the live turn both belong to THAT chat, not to `build-A` — so
    // the entry `refreshBuilds` has to find by id is the handed-off chat's, not the plan chat's.
    const LIVE_BUILD_CHAT = 'build-A-live'
    h.listProjectConversations.mockResolvedValue([
      { id: LIVE_BUILD_CHAT, kind: 'build', title: 'First build', updatedAt: new Date().toISOString() },
    ])
    h.buildFromPlan.mockResolvedValue({ outcome: 'started', chatId: LIVE_BUILD_CHAT, turnId: BUILD_TURN_ID })
    // The handed-off chat's own hydration is what re-subscribes to the running turn (the
    // `activeTurn` the read projection carries) — this page never watches a build it did not
    // navigate into.
    h.getBuild.mockImplementation(async (id) =>
      id === LIVE_BUILD_CHAT
        ? { id, kind: 'build', messages: [], activeTurn: { turnId: BUILD_TURN_ID, lastSeq: 0 } }
        : { id, kind: 'build', messages: [] },
    )

    // Tab A starts a build → Build-it navigates it to the new chat, which holds the advisory
    // cross-tab claim and shows the live narrative.
    const a = renderBuilder({ chatId: 'build-A' })
    await buildFrom(a.container)
    await within(a.container).findByTestId('stop-turn')

    // Tab B (same project) learns of the claim over the channel, then tries to build.
    const b = renderBuilder({ chatId: 'build-B' })
    await within(b.container).findByPlaceholderText(/ask for another change/i)
    await flushChannel()
    await buildFrom(b.container, 'add a table')

    // buildBlockedMessage READ `builds` to name the holder — proving builds/refreshBuilds were kept.
    // RE-POINTED (Plan D U17): the refusal used to render inside the plan card, one per card id.
    // There is one offer on the composer now, so the sentence goes to the surface's assertive
    // slot instead — which is also where every other interrupting refusal lands, so a citizen has
    // one place to look rather than one per control.
    const alert = await within(b.container).findByTestId('urgent-banner')
    expect(alert.textContent).toMatch(/already building this project/i)
    expect(alert.textContent).toMatch(/First build/) // named the holder, not "another build chat"
    // B never started its own build — only A's transition fired.
    expect(h.buildFromPlan).toHaveBeenCalledTimes(1)
  })
})
