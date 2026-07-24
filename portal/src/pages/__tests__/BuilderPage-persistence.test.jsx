/**
 * Build-chat persistence guards, re-expressed against the session model (U5) and the 003-U4 routing
 * rule (a send is a CHAT turn; the build fires only when the user confirms the returned brief card).
 *
 * The single-file era persisted an ASSISTANT turn + a code snapshot (patchBuildCode) at the end of
 * a stream; those are gone (the activity feed is the build narrative, not a persisted transcript).
 * U8/F10 removed the in-rail "Recent builds" dropdown (and with it the per-chat delete + its
 * live-session gate) — past conversations, and their deletion, live on the project page now — so the
 * delete-gating tests that drove that dropdown are gone too. What REMAINS and is pinned here:
 *   - the user turn — INCLUDING its attachment parts — is persisted via createBuild on the
 *     SEND, hence before the confirmed build starts, so BRAIN reads the image/PDF/office/deck
 *     context server-side (C3 §2.1).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { FakeEventSource, makeClient, primeClient, PLAN_CARD_ID, primeTurn } from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  sendMessage: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  switchMode: vi.fn(), resolvePlanOptions: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  acquireLock: vi.fn(), renewLock: vi.fn(), releaseLock: vi.fn(), heartbeat: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
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
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  switchMode: (...a) => h.switchMode(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
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
  fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
  await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.newBuild.mockReturnValue('build-X')
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  // A scripted relay that always answers with a ready-to-build brief, so these suites reach the
  // persistence + gating mechanics in one send. (Whether the model asks or briefs is its own
  // judgment, pinned server-side — `backend/tests/api/v1/claude/test_interview_protocol.py`.)
  primeTurn(h)
})
afterEach(() => cleanup())

describe('BuilderPage — the attachment user-turn is persisted before the build starts', () => {
  it('sends the attachment as an OWNED REF on the wire message, created-before-started (U7/R3)', async () => {
    // buildUserParts stands in for the upload: it yields a text part + a file part.
    h.buildUserParts.mockImplementation(async (text) => [
      { type: 'text', text },
      { type: 'file', attachmentId: 'a1', kind: 'image', name: 'gate.png', mediaType: 'image/png' },
    ])
    renderBuilder()
    await startBuild('use this layout')

    // U13: the turn reaches the SERVER with the attachment as an owned reference — the
    // server persists it into the thread BRAIN later reads.
    const [, wire] = h.startTurn.mock.calls[0]
    expect(wire.text).toBe('use this layout')
    expect(wire.attachmentIds).toEqual(['a1'])
    // …and the row exists BEFORE the build is started (create → stream → confirm → build).
    expect(h.createBuild.mock.invocationCallOrder[0]).toBeLessThan(
      h.buildFromPlan.mock.invocationCallOrder[0],
    )
  })

  it('passes the chat id as conversationId on start, so the persisted parts reach the build (R3)', async () => {
    // The other half of the seam: persisting the parts only matters if start TELLS the server
    // which thread to read them from. Without this the build is text-only and the file is
    // silently ignored — the exact bug R3 fixes.
    renderBuilder()
    await startBuild('use the attached sheet')

    // The transition names the THREAD (the server reads the plan + its attachments from it).
    expect(h.buildFromPlan).toHaveBeenCalledWith('build-X', PLAN_CARD_ID)
  })

  it('ABORTS the send when the upload fails — never starts a text-only build (R3)', async () => {
    h.buildUserParts.mockRejectedValue(new Error('Upload failed: storage is full.'))
    renderBuilder()
    const textarea = await screen.findByPlaceholderText(/describe what you need/i)
    fireEvent.change(textarea, { target: { value: 'use this sheet' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await screen.findByText(/Upload failed: storage is full./i)
    expect(h.createBuild).not.toHaveBeenCalled()
    // The turn never reaches the server, so no plan comes back and there is nothing to
    // confirm — the file-less build is unreachable rather than merely un-triggered.
    expect(h.startTurn).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /^Build it$/ })).toBeNull()
    expect(h.buildFromPlan).not.toHaveBeenCalled() // no build ignoring the file
  })
})
