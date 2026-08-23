/**
 * The compile signal's last mile, portal side (U11/U12).
 *
 * The supervisor derives the state and the turn engine emits it; this file covers the only part
 * neither of those can: that a `compile` frame arriving on the turn stream actually reaches the
 * preview pane as a prop, that a catch-up `snapshot` carries it for a tab that reloaded
 * mid-build, and that a new turn does not start by claiming the app is fine.
 *
 * LivePreview is stubbed to RECORD ITS PROPS rather than render. The pane's own behaviour on each
 * value is pinned in `components/__tests__/LivePreview.test.jsx`; what is unproven without this
 * file is the wiring between the two, which is exactly where a frame gets dropped silently.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient, primeTurn,
  waitForGateOpen, scriptBuildTurn, BUILD_TURN_ID, T_STEP, T_PREVIEW, T_BUILD_END, PREVIEW_URL,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  sendMessage: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), stopTurn: vi.fn(),
  switchMode: vi.fn(), resolvePlanOptions: vi.fn(),
  start: vi.fn(), relaunchPreview: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  acquireLock: vi.fn(), releaseLock: vi.fn(),
  fetchPreviewState: vi.fn(), fetchCompileState: vi.fn(), fetchSaveState: vi.fn(),
}))

/** Every `compileState` the pane has been handed, in order. */
const seen = []

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({
  default: (props) => {
    seen.push(props.compileState)
    return null
  },
}))
vi.mock('../../components/AttachmentChips', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({
  ...(await orig()), buildUserParts: h.buildUserParts,
}))
vi.mock('../../utils/chatErrors', async (orig) => await orig())
vi.mock('../../utils/buildSessionApi', async (orig) => ({
  ...(await orig()),
  fetchPreviewState: (...a) => h.fetchPreviewState(...a),
  fetchCompileState: (...a) => h.fetchCompileState(...a),
  fetchSaveState: (...a) => h.fetchSaveState(...a),
}))
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  stopTurn: (...a) => h.stopTurn(...a),
  switchMode: (...a) => h.switchMode(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

import BuilderPage from '../BuilderPage'

function renderThread(chatId = 'thread-1') {
  const fake = new FakeEventSource(chatId)
  const deps = { client: makeClient(h), eventSourceFactory: () => fake }
  return render(
    <MemoryRouter initialEntries={[`/chat/${chatId}`]}>
      <Routes>
        <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" buildSessionDeps={deps} />} />
      </Routes>
    </MemoryRouter>,
  )
}

const composer = () => screen.getByPlaceholderText(/describe what you need/i)

async function send(text = 'a visitor app') {
  await waitForGateOpen()
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

async function runBuild(turn) {
  await send()
  fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
  await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
  await waitFor(() =>
    expect(h.readTurnStream).toHaveBeenCalledWith(expect.objectContaining({ turnId: BUILD_TURN_ID })),
  )
  await turn.frame(T_STEP('Scaffolding your app…'))
}

const COMPILE = (state, seq = 5) => ({ type: 'compile', seq, state })

beforeEach(() => {
  seen.length = 0
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  h.fetchCompileState.mockResolvedValue('unknown')
  h.fetchSaveState.mockResolvedValue({ appId: null, dirty: null, containerHead: null, savedHead: null, recoveryAt: null })
  h.fetchPreviewState.mockResolvedValue({
    state: 'unknown', alive: false, previewUrl: null, occupyingProjectName: null, restorable: null,
  })
  primeTurn(h)
})

afterEach(cleanup)

describe('BuilderPage — the compile signal reaches the preview pane', () => {
  it('asks the idle route when a preview is framed and no turn is running', async () => {
    // ★ THE RELOAD HOLE. The turn stream's producer stops at the terminal, so a tab that
    // reloads after a red turn has no signal at all — it comes up uncovered, showing the
    // framework's error screen under a live-preview label. This route is the producer that
    // outlives the turn, and it is asked on the preview probe's own tick.
    h.fetchCompileState.mockResolvedValue('failed')
    h.fetchPreviewState.mockResolvedValue({
      state: 'alive', alive: true, previewUrl: PREVIEW_URL,
      occupyingProjectName: null, restorable: true,
    })
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)
    // A framed preview, and then a turn that ENDS — which is precisely the moment the stream's
    // producer goes away and the reload hole opens.
    await turn.frame(T_PREVIEW())
    await turn.frame(T_BUILD_END())
    await turn.end('completed')
    h.fetchCompileState.mockClear()

    // Tabbing back — a deliberate human act the probe listens for, and the realistic moment a
    // citizen returns to a preview whose turn ended while they were elsewhere. On a genuine
    // reload the same probe runs at mount instead; both reach this branch the same way.
    await act(async () => {
      window.dispatchEvent(new Event('focus'))
      await Promise.resolve()
    })

    await waitFor(() => expect(h.fetchCompileState).toHaveBeenCalledWith('p1'))
    await waitFor(() => expect(seen.at(-1)).toBe('failed'))
  })

  it('does not ask while a turn is running — the stream is the better authority', async () => {
    h.fetchPreviewState.mockResolvedValue({
      state: 'alive', alive: true, previewUrl: PREVIEW_URL,
      occupyingProjectName: null, restorable: true,
    })
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)
    // Frame a preview so the probe CAN run — otherwise the absence below proves nothing.
    await turn.frame(T_PREVIEW())
    await turn.frame(COMPILE('building', 5))
    h.fetchCompileState.mockClear()

    // Force a probe while the turn is still open. Without this the assertion would pass on a
    // probe that simply never fired, which is the classic way an absence test proves nothing.
    await act(async () => {
      window.dispatchEvent(new Event('focus'))
      await Promise.resolve()
    })
    await waitFor(() => expect(h.fetchPreviewState).toHaveBeenCalled())

    // ABSENCE: the probe ran and deliberately did NOT ask about compilation…
    expect(h.fetchCompileState).not.toHaveBeenCalled()
    // …and LIVENESS: the live frame is still what the pane is holding, unmoved by the probe.
    expect(seen.at(-1)).toBe('building')
  })

  it('starts with no claim at all, rather than claiming the app compiles', async () => {
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    // `null`, never `'clean'`. A default of clean would uncover a pane on the strength of a
    // signal nothing has sent — which is the one behaviour this whole mechanism forbids.
    expect(seen.every((v) => v === null)).toBe(true)
  })

  it('hands each live compile frame straight through', async () => {
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(COMPILE('building', 5))
    expect(seen.at(-1)).toBe('building')

    await turn.frame(COMPILE('failed', 6))
    expect(seen.at(-1)).toBe('failed')

    await turn.frame(COMPILE('clean', 7))
    expect(seen.at(-1)).toBe('clean')
  })

  it('passes `unknown` through as itself rather than dropping it', async () => {
    // The pane HOLDS its cover on `unknown`, so the value has to arrive to mean anything. A
    // dropped frame would look to the pane exactly like "nothing changed since the last clean".
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(COMPILE('clean', 5))
    await turn.frame(COMPILE('unknown', 6))

    expect(seen.at(-1)).toBe('unknown')
  })

  it('recovers the state from a catch-up snapshot, so a reload mid-build lands covered', async () => {
    // Compile frames are emitted ON CHANGE, so a tab that reloads while the app is sitting broken
    // learns nothing until the next change. The snapshot is what closes that window.
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame({
      type: 'snapshot', seq: 4, turnId: BUILD_TURN_ID, turnStatus: 'running',
      items: [], textSoFar: '', steps: [], compileState: 'failed',
    })

    expect(seen.at(-1)).toBe('failed')
  })

  it('does not let a snapshot without a compile fact overwrite what the tail established', async () => {
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(COMPILE('failed', 5))
    await turn.frame({
      type: 'snapshot', seq: 6, turnId: BUILD_TURN_ID, turnStatus: 'running',
      items: [], textSoFar: '', steps: [],
    })

    expect(seen.at(-1)).toBe('failed')
  })
})
