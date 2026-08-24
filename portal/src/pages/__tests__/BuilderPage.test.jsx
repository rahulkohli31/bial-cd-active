/**
 * U8 (F7 + F10) — the trimmed chat header.
 *
 * The header is now back-navigation (ProjectBreadcrumb) + the project name and NOTHING else: the
 * redundant in-rail usage meter (F7), the AI branding block, its avatar, and the "Recent" builds
 * dropdown (F10) are gone. Past conversations are reached through the project page the breadcrumb
 * links to — ProjectPage already lists every conversation, so nothing is orphaned.
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
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  switchMode: vi.fn(), resolvePlanOptions: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(), relaunchPreview: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
vi.mock('../../components/AttachmentChips', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  switchMode: (...a) => h.switchMode(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

import BuilderPage from '../BuilderPage'

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
          element={<BuilderPage projectId={projectId} projectName={projectName} buildSessionDeps={deps} />}
        />
        <Route path="/projects/:pid" element={<ProjectPageStub />} />
      </Routes>
    </MemoryRouter>,
  )
  return { ...view, fake }
}

const composerIn = (c) => within(c).getByPlaceholderText(/describe what you need/i)
async function sendFrom(c, text = 'a visitor app') {
  await waitForGateOpen()
  fireEvent.change(composerIn(c), { target: { value: text } })
  fireEvent.keyDown(composerIn(c), { key: 'Enter' })
}
/** The whole user path to a build: send a turn, get the brief card, click Build it. */
async function buildFrom(c, text = 'a visitor app') {
  sendFrom(c, text)
  fireEvent.click(await within(c).findByRole('button', { name: /^Build it$/ }))
}
async function lastCard(c) {
  const cards = await within(c).findAllByTestId('plan-options-card')
  return cards[cards.length - 1]
}
// BroadcastChannel delivery is queued on a task: a new manager posts `poll`, the holder answers
// `announce`, and only then does the newcomer's `blockedBy` see the claim. Drain a few ticks.
const flushChannel = () => act(async () => { for (let i = 0; i < 6; i += 1) await new Promise((r) => setTimeout(r, 0)) })

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockImplementation(async (id) => ({ id, kind: 'builder', messages: [] }))
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  primeTurn(h)
  // A build is a Write TURN (U5): the confirmed brief opens a second socket, and it stays open —
  // a HELD cross-tab claim is exactly a build that has not finished.
  h.readTurnStream.mockImplementation(scriptBuildTurn().impl)
})
afterEach(() => cleanup())

describe('U8 — the chat header is back-navigation + project name only', () => {
  it('renders the back link + project name and none of the removed chrome', async () => {
    const { container } = renderBuilder()
    // Settle the async adopt (getBuild → welcome message) before asserting on the header.
    await screen.findByPlaceholderText(/describe what you need/i)

    // KEPT: the breadcrumb back link, labelled with the project name.
    const back = screen.getByRole('link', { name: /VIP Movement/i })
    expect(back.getAttribute('href')).toBe('/projects/p1')

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

  it('the back link routes to /projects/{projectId} — the past-conversation surface', async () => {
    renderBuilder()
    await screen.findByPlaceholderText(/describe what you need/i)

    fireEvent.click(screen.getByRole('link', { name: /VIP Movement/i }))
    expect((await screen.findByTestId('project-page')).textContent).toContain('p1')
  })
})

describe('U8 regression guard — builds/refreshBuilds survive the dropdown removal', () => {
  it('the Build-it blocked advisory still names the blocking chat by title', async () => {
    // Another builder chat in the same project is mid-build; refreshBuilds learns its title.
    h.listProjectConversations.mockResolvedValue([
      { id: 'build-A', kind: 'builder', title: 'First build', updatedAt: new Date().toISOString() },
    ])

    // Tab A starts a build → holds the advisory cross-tab claim.
    const a = renderBuilder({ chatId: 'build-A' })
    await buildFrom(a.container)
    await within(a.container).findByTestId('build-progress')

    // Tab B (same project) learns of A's claim over the channel, then tries to build.
    const b = renderBuilder({ chatId: 'build-B' })
    await within(b.container).findByPlaceholderText(/describe what you need/i)
    await flushChannel()
    await buildFrom(b.container, 'add a table')

    // buildBlockedMessage READ `builds` to name the holder — proving builds/refreshBuilds were kept.
    const card = await lastCard(b.container)
    const alert = await within(card).findByRole('alert')
    expect(alert.textContent).toMatch(/already building this project/i)
    expect(alert.textContent).toMatch(/First build/) // named the holder, not "another build chat"
    // B never started its own build — only A's transition fired.
    expect(h.buildFromPlan).toHaveBeenCalledTimes(1)
  })
})
