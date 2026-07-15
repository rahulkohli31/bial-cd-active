/**
 * U5 core: Send drives a C3 BUILD SESSION — the activity feed streams the C7 envelopes, the live
 * preview frames the sandbox `preview_url` on `preview_ready`, and Stop / Force-end control it. The
 * 409 identity model (same-project reattach vs cross-project block) is pinned here too.
 *
 * The REAL useBuildSession hook + LivePreview + ActivityFeed + SessionControls run; only the C3
 * transport (client + EventSource) is a mock injected via `buildSessionDeps`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import BuilderPage from '../BuilderPage'
import { ApiError } from '../../utils/apiError'
import { BuildSessionAlreadyActiveError } from '../../utils/buildSessionApi'
import {
  FakeEventSource, PREVIEW_URL, makeClient, primeClient, renderBuilder,
  startResp, statusResp, STEP, LOG, PREVIEW, ENDED, QUOTA,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(),
  newBuild: vi.fn(),
  appendBuilderMessage: vi.fn(),
  getBuild: vi.fn(),
  deleteBuild: vi.fn(),
  listProjectConversations: vi.fn(),
  buildUserParts: vi.fn(),
  start: vi.fn(),
  stop: vi.fn(),
  getStatus: vi.fn(),
  forceEnd: vi.fn(),
  acquireLock: vi.fn(),
  renewLock: vi.fn(),
  releaseLock: vi.fn(),
  heartbeat: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds,
  newBuild: h.newBuild,
  appendBuilderMessage: h.appendBuilderMessage,
  getBuild: h.getBuild,
  deleteBuild: h.deleteBuild,
  deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))

function deps() {
  const fake = new FakeEventSource('x')
  return { fake, deps: { client: makeClient(h), eventSourceFactory: () => fake } }
}

async function sendPrompt(text = 'build me a tool') {
  const textarea = await screen.findByPlaceholderText(/Type instructions/i)
  fireEvent.change(textarea, { target: { value: text } })
  fireEvent.keyDown(textarea, { key: 'Enter' })
  await waitFor(() => expect(h.start).toHaveBeenCalled())
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.newBuild.mockReturnValue('build-Y')
  h.appendBuilderMessage.mockResolvedValue({ ok: true })
  h.deleteBuild.mockResolvedValue(true)
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([{ id: 'build-X', kind: 'builder', title: 'My build', updatedAt: new Date().toISOString() }])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
})
afterEach(() => cleanup())

describe('BuilderPage — the build-session flow (ORIG-§3-d/f)', () => {
  it('Send starts a session; the activity feed renders C7 envelopes; preview_ready frames the sandbox URL', async () => {
    const { fake, deps: sessionDeps } = deps()
    renderBuilder({ deps: sessionDeps })
    await sendPrompt()

    expect(h.start).toHaveBeenCalledWith({ projectId: 'p1', prompt: 'build me a tool' })

    act(() => { fake.open(); fake.emitEnvelope(STEP(1)); fake.emitEnvelope(LOG(2, 'added 312 packages')) })
    // The feed rows are actually in the DOM (not just props) — no remount needed.
    expect(await screen.findByText(/Scaffolding your app/i)).toBeTruthy()
    expect(screen.getByText(/added 312 packages/i)).toBeTruthy()

    act(() => { fake.emitEnvelope(PREVIEW(3)) })
    await waitFor(() => expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL))
  })

  it('Stop → graceful end reflected in the UI (preview reaches the terminal placeholder)', async () => {
    const d = deps()
    renderBuilder({ deps: d.deps })
    await sendPrompt()
    act(() => { d.fake.open(); d.fake.emitEnvelope(PREVIEW(3)) })
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: /^stop$/i }))
    await waitFor(() => expect(h.stop).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByText(/no longer running/i)).toBeTruthy())
    expect(document.querySelector('iframe')).toBeNull() // terminal collapses the dead frame
  })

  it('Force-end → the kill switch confirms, then ends the session', async () => {
    const d = deps()
    renderBuilder({ deps: d.deps })
    await sendPrompt()
    act(() => { d.fake.open(); d.fake.emitEnvelope(STEP(1)) })

    fireEvent.click(screen.getByRole('button', { name: /force-end/i }))
    fireEvent.click(screen.getByRole('button', { name: /^force-end$/i })) // confirm
    await waitFor(() => expect(h.forceEnd).toHaveBeenCalledWith('s1'))
    await waitFor(() => expect(screen.getByText(/no longer running/i)).toBeTruthy())
  })

  it('a quota breach ends gracefully and shows the daily-limit banner (C7 §8)', async () => {
    const d = deps()
    renderBuilder({ deps: d.deps })
    await sendPrompt()
    act(() => { d.fake.open(); d.fake.emitEnvelope(QUOTA(3)); d.fake.emitEnvelope(ENDED(4, 'ended', 'quota_exceeded')) })
    // "resets at midnight IST" shows in BOTH the feed row and the SessionControls banner.
    await waitFor(() => expect(screen.getAllByText(/resets at midnight IST/i).length).toBeGreaterThan(0))
    expect(screen.getByText(/no longer running/i)).toBeTruthy() // graceful terminal, not a spinner
  })
})

describe('BuilderPage — the 409 identity model (KTD-8a)', () => {
  it('a cross-project 409 renders the block UI + force-end affordance (no stream started)', async () => {
    h.start.mockRejectedValue(new BuildSessionAlreadyActiveError('busy', 'other-9'))
    h.getStatus.mockResolvedValue(statusResp({ sessionId: 'other-9', projectId: 'a-different-project' }))
    const d = deps()
    renderBuilder({ deps: d.deps })
    await sendPrompt()

    // getStatus disambiguates the 409: different project → block, not reattach.
    await waitFor(() => expect(h.getStatus).toHaveBeenCalledWith('other-9'))
    expect(await screen.findByText(/already have a build running/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /force-end it/i })).toBeTruthy()
    expect(document.querySelector('iframe')).toBeNull() // no framed session for this chat
  })

  it('a same-project 409 RE-ATTACHES the live session (frames via the getStatus seed, KTD-1)', async () => {
    h.start.mockRejectedValue(new BuildSessionAlreadyActiveError('busy', 'other-9'))
    // Same project + already ready: the reattach seed frames the app even with no live preview_ready.
    h.getStatus.mockResolvedValue(statusResp({ sessionId: 'other-9', projectId: 'p1', status: 'ready', previewUrl: PREVIEW_URL }))
    const d = deps()
    renderBuilder({ deps: d.deps })
    await sendPrompt()

    await waitFor(() => expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL))
    expect(screen.queryByText(/already have a build running/i)).toBeNull() // reattached, not blocked
  })
})

describe('BuilderPage — refine turn (default (a): stop + start, no C3 refine verb)', () => {
  it('a second Send on a live session stops it, then starts a fresh one (never a self-inflicted 409)', async () => {
    const d = deps()
    renderBuilder({ deps: d.deps })
    await sendPrompt('first build')
    act(() => { d.fake.open(); d.fake.emitEnvelope(PREVIEW(3)) })
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())

    h.start.mockClear()
    h.start.mockResolvedValue(startResp())
    fireEvent.change(screen.getByPlaceholderText(/Type instructions/i), { target: { value: 'make it dark mode' } })
    fireEvent.keyDown(screen.getByPlaceholderText(/Type instructions/i), { key: 'Enter' })

    await waitFor(() => expect(h.stop).toHaveBeenCalled()) // the live session is ended first
    await waitFor(() => expect(h.start).toHaveBeenCalledWith({ projectId: 'p1', prompt: 'make it dark mode' }))
  })

  it('a rejecting stop() ABORTS the refine — no start() over a still-live session, the stop error stays surfaced (finding #19)', async () => {
    const d = deps()
    renderBuilder({ deps: d.deps })
    await sendPrompt('first build')
    act(() => { d.fake.open(); d.fake.emitEnvelope(PREVIEW(3)) })
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())

    h.start.mockClear()
    h.stop.mockRejectedValue(new ApiError('Could not stop the build session.', 503))
    fireEvent.change(screen.getByPlaceholderText(/Type instructions/i), { target: { value: 'make it dark mode' } })
    fireEvent.keyDown(screen.getByPlaceholderText(/Type instructions/i), { key: 'Enter' })

    await waitFor(() => expect(h.stop).toHaveBeenCalled())
    // The refine ABORTED: no start() fired (it would 409 our own live session and its reset()
    // would wipe the error), and the stop failure stays on screen.
    expect(await screen.findByText(/could not stop the build session/i)).toBeTruthy()
    expect(h.start).not.toHaveBeenCalled()
    expect(document.querySelector('iframe')).toBeTruthy() // the old session is still live + framed
  })

  it('a Send in a DIFFERENT project does NOT tear down another project\'s live build (review F1)', async () => {
    // One BuilderPage instance persists across project switches (flat routing). A live build in
    // project A must survive a Send made from project B's chat — the bug was a tautological refine
    // guard that stopped A's build instead of blocking B.
    const fake = new FakeEventSource('x')
    const sessionDeps = { client: makeClient(h), eventSourceFactory: () => fake }
    const { rerender } = render(
      <MemoryRouter initialEntries={['/x']}>
        <BuilderPage chatId="chat-A" projectId="pA" projectName="Project A" buildSessionDeps={sessionDeps} />
      </MemoryRouter>,
    )
    // Build + ready a session in project A.
    const ta = await screen.findByPlaceholderText(/Type instructions/i)
    fireEvent.change(ta, { target: { value: 'build A' } })
    fireEvent.keyDown(ta, { key: 'Enter' })
    await waitFor(() => expect(h.start).toHaveBeenCalledWith({ projectId: 'pA', prompt: 'build A' }))
    act(() => { fake.open(); fake.emitEnvelope(PREVIEW(3)) })
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())

    // Navigate the SAME instance to project B's builder chat.
    h.start.mockClear()
    h.stop.mockClear()
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <BuilderPage chatId="chat-B" projectId="pB" projectName="Project B" buildSessionDeps={sessionDeps} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('chat-B'))
    expect(document.querySelector('iframe')).toBeNull() // A's build is NOT shown under project B

    // Send in project B → must block WITHOUT stopping or restarting A's build.
    const tb = await screen.findByPlaceholderText(/Type instructions/i)
    fireEvent.change(tb, { target: { value: 'build B' } })
    fireEvent.keyDown(tb, { key: 'Enter' })
    expect(await screen.findByText(/running in another project/i)).toBeTruthy()
    expect(h.stop).not.toHaveBeenCalled() // A's live build is untouched
    expect(h.start).not.toHaveBeenCalled() // B never started (blocked, not a self-inflicted stop+start)
  })
})
