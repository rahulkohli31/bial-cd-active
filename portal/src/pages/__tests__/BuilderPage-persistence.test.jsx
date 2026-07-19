/**
 * Build-chat persistence guards, re-expressed against the session model (U5) and the 003-U4 routing
 * rule (a send is a CHAT turn; the build fires only when the user confirms the returned brief card).
 *
 * The single-file era persisted an ASSISTANT turn + a code snapshot (patchBuildCode) at the end of
 * a stream; those are gone (the activity feed is the build narrative, not a persisted transcript).
 * What REMAINS and is pinned here:
 *   - a build chat's delete is GATED while ITS session is live (deleting the chat that owns a running
 *     build would strand it), and re-enabled once the session ends; a DIFFERENT chat deletes freely;
 *   - the user turn — INCLUDING its attachment parts — is persisted via appendBuilderMessage on the
 *     SEND, hence before the confirmed build starts, so BRAIN reads the image/PDF/office/deck
 *     context server-side (C3 §2.1).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { FakeEventSource, makeClient, primeClient, ENDED, BRIEF, briefReply, relayReplying } from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), appendBuilderMessage: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  sendMessage: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  acquireLock: vi.fn(), renewLock: vi.fn(), releaseLock: vi.fn(), heartbeat: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, appendBuilderMessage: h.appendBuilderMessage,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
// A rendered file-part message would mount AttachmentChips, which fetches the object URL over the
// real (relative-URL) network — stub it so the transcript render triggers no unhandled fetch.
vi.mock('../../components/AttachmentChips', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null, clearError: vi.fn() }),
}))

import BuilderPage from '../BuilderPage'

function renderBuilder(chatId = 'build-X') {
  const fake = new FakeEventSource(chatId)
  const deps = { client: makeClient(h), eventSourceFactory: () => fake }
  const view = render(
    <MemoryRouter initialEntries={[`/chat/${chatId}?projectId=p1&kind=builder`]}>
      <Routes>
        <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="VIP Movement" buildSessionDeps={deps} />} />
        <Route path="/projects/:projectId" element={<div>project home</div>} />
        <Route path="/projects" element={<div>projects index</div>} />
      </Routes>
    </MemoryRouter>,
  )
  return { ...view, fake }
}

/**
 * Drive a build the way a user does (003-U4): send a turn, then confirm the brief card the model
 * replies with. The send alone only persists the turn and asks the assistant — the click is the
 * page's single build trigger, so `start` sees the refined BRIEF, not `text`.
 */
async function startBuild(text = 'make it blue') {
  const textarea = await screen.findByPlaceholderText(/describe what you need/i)
  fireEvent.change(textarea, { target: { value: text } })
  fireEvent.keyDown(textarea, { key: 'Enter' })
  fireEvent.click(await screen.findByRole('button', { name: /build this|rebuild with these changes/i }))
  await waitFor(() => expect(h.start).toHaveBeenCalled())
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.newBuild.mockReturnValue('build-X')
  h.appendBuilderMessage.mockResolvedValue({ ok: true })
  h.deleteBuild.mockResolvedValue(true)
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  // A scripted relay that always answers with a ready-to-build brief, so these suites reach the
  // persistence + gating mechanics in one send. (Whether the model asks or briefs is its own
  // judgment, pinned server-side — `backend/tests/api/v1/claude/test_interview_protocol.py`.)
  h.sendMessage.mockImplementation(relayReplying(briefReply()))
})
afterEach(() => cleanup())

describe('BuilderPage — delete gating around a live session (F-1)', () => {
  it("gates the active build's delete while its session is live, and re-enables it once it ends", async () => {
    h.listProjectConversations.mockResolvedValue([{ id: 'build-X', kind: 'builder', title: 'My build', updatedAt: new Date().toISOString() }])
    const { fake } = renderBuilder()
    await startBuild()

    fireEvent.click(screen.getByTitle('Recent builds'))
    const delBtn = await screen.findByLabelText('Delete My build')
    expect(delBtn.disabled).toBe(true) // can't strand a running build
    expect(h.deleteBuild).not.toHaveBeenCalled()

    // The session ends → the gate lifts.
    act(() => { fake.open(); fake.emitEnvelope(ENDED(9)) })
    await waitFor(() => expect(screen.getByLabelText('Delete My build').disabled).toBe(false))
  })

  it('still allows deleting a DIFFERENT, non-live build while one is building (no over-gating)', async () => {
    h.listProjectConversations.mockResolvedValue([
      { id: 'build-X', kind: 'builder', title: 'My build', updatedAt: new Date().toISOString() },
      { id: 'build-Y', kind: 'builder', title: 'Other build', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    renderBuilder()
    await startBuild()

    fireEvent.click(screen.getByTitle('Recent builds'))
    const delOther = await screen.findByLabelText('Delete Other build')
    expect(delOther.disabled).toBe(false)
    fireEvent.click(delOther)
    await waitFor(() => expect(h.deleteBuild).toHaveBeenCalledWith('build-Y'))
  })
})

describe('BuilderPage — the attachment user-turn is persisted before the build starts', () => {
  it('persists the parts (incl. attachment parts) via appendBuilderMessage BEFORE start (C3 §2.1)', async () => {
    // buildUserParts stands in for the upload: it yields a text part + a file part.
    h.buildUserParts.mockImplementation(async (text) => [
      { type: 'text', text },
      { type: 'file', attachmentId: 'a1', kind: 'image', name: 'gate.png', mediaType: 'image/png' },
    ])
    renderBuilder()
    await startBuild('use this layout')

    // The persisted turn carries the attachment part…
    const [, message] = h.appendBuilderMessage.mock.calls[0]
    expect(message.role).toBe('user')
    expect(message.parts.some((p) => p.type === 'file' && p.attachmentId === 'a1')).toBe(true)
    // …and it lands BEFORE the build is started (BRAIN reads it server-side). The brief-card
    // confirmation sits between the two, so this ordering is what makes the file reach the build.
    expect(h.appendBuilderMessage.mock.invocationCallOrder[0]).toBeLessThan(h.start.mock.invocationCallOrder[0])
  })

  it('passes the chat id as conversationId on start, so the persisted parts reach the build (R3)', async () => {
    // The other half of the seam: persisting the parts only matters if start TELLS the server
    // which thread to read them from. Without this the build is text-only and the file is
    // silently ignored — the exact bug R3 fixes.
    renderBuilder()
    await startBuild('use the attached sheet')

    expect(h.start).toHaveBeenCalledWith({ projectId: 'p1', prompt: BRIEF, conversationId: 'build-X' })
  })

  it('ABORTS the send when the upload fails — never starts a text-only build (R3)', async () => {
    h.buildUserParts.mockRejectedValue(new Error('Upload failed: storage is full.'))
    renderBuilder()
    const textarea = await screen.findByPlaceholderText(/describe what you need/i)
    fireEvent.change(textarea, { target: { value: 'use this sheet' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await screen.findByText(/Upload failed: storage is full./i)
    expect(h.appendBuilderMessage).not.toHaveBeenCalled()
    // The turn never reaches the relay, so no brief comes back and there is nothing to confirm —
    // the file-less build is unreachable rather than merely un-triggered.
    expect(h.sendMessage).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /build this|rebuild with these changes/i })).toBeNull()
    expect(h.start).not.toHaveBeenCalled() // no build ignoring the file
  })
})
