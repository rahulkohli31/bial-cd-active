/**
 * Build-chat persistence guards, re-expressed against the session model (U5) and the 003-U4 routing
 * rule (a send is a CHAT turn; the build fires only when the user confirms the returned brief card).
 *
 * The single-file era persisted an ASSISTANT turn + a code snapshot (patchBuildCode) at the end of
 * a stream; those are gone (the activity feed is the build narrative, not a persisted transcript).
 * U8/F10 removed the in-rail "Recent builds" dropdown (and with it the per-chat delete + its
 * live-session gate) — past conversations, and their deletion, live on the project page now — so the
 * delete-gating tests that drove that dropdown are gone too. What REMAINS and is pinned here:
 *   - the user turn — INCLUDING its attachment parts — is persisted on the SEND, hence before
 *     the confirmed build starts, so BRAIN reads the image/PDF/office/deck context server-side
 *     (C3 §2.1). R-18/U13 (plan 006) retired the separate `createBuild` round trip this used to
 *     go through: the row's parentage now rides the turn's own `POST .../turns` request as a
 *     `create` block, so the server can check the workspace BEFORE creating anything — see
 *     `fireRelayTurn`'s R-18 comment in `ConversationSurface.tsx`. `h.createBuild` stays mocked
 *     below for the harness's sake but is never called; nothing in this file asserts on it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { FakeEventSource, makeClient, primeClient, PLAN_CARD_ID, primeTurn, waitForGateOpen } from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  resolvePlanOptions: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
// SPREAD THE ORIGINAL — `handleBuildIt` mints the new build chat's id through the shared
// `uuidv7` (ADR-0006), and a factory naming only `listProjectConversations` leaves every other
// export (including that one) undefined; Vitest now warns the moment a real caller reaches for
// it, which every Build-it press in this suite does.
vi.mock('../../utils/conversationApi', async (importOriginal) => ({
  ...(await importOriginal()),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
// A rendered file-part message would mount AttachmentChips, which fetches the object URL over the
// real (relative-URL) network — stub it so the transcript render triggers no unhandled fetch.
vi.mock('../../components/AttachmentChips', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
// `switchMode` is GONE from this list (U1/U19): the route it posted to no longer exists.
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

import ConversationSurface from '../../components/chat/ConversationSurface'

function renderBuilder(chatId = 'build-X') {
  const fake = new FakeEventSource(chatId)
  const deps = { client: makeClient(h), eventSourceFactory: () => fake }
  const view = render(
    <MemoryRouter initialEntries={[`/chat/${chatId}?projectId=p1&kind=build`]}>
      <Routes>
        <Route path="/chat/:chatId" element={<ConversationSurface projectId="p1" projectName="VIP Movement" buildSessionDeps={deps} />} />
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
  await waitForGateOpen()
  const textarea = await screen.findByPlaceholderText(/ask for another change/i)
  fireEvent.change(textarea, { target: { value: text } })
  fireEvent.keyDown(textarea, { key: 'Enter' })
  fireEvent.click(await screen.findByRole('button', { name: /^Build this plan$/ }))
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
  // A scripted turn that always answers with a ready-to-build brief, so these suites reach the
  // persistence + gating mechanics in one send. (Whether the model asks or briefs is its own
  // judgment, and it is a property of the server-composed prompt —
  // `backend/tests/services/agent/test_mode_prompts.py`. The relay suite this used to point at
  // was retired with the relay.)
  primeTurn(h)
})
afterEach(() => cleanup())

describe('BuilderPage — the attachment user-turn is persisted before the build starts', () => {
  it('sends the attachment as an OWNED REF on the wire message, WITH the row\'s own create block, before the build starts (R-18/U13, was U7/R3)', async () => {
    // buildUserParts stands in for the upload: it yields a text part + a file part.
    h.buildUserParts.mockImplementation(async (text) => [
      { type: 'text', text },
      { type: 'file', attachmentId: 'a1', kind: 'image', name: 'gate.png', mediaType: 'image/png' },
    ])
    renderBuilder()
    await startBuild('use this layout')

    // U13: the turn reaches the SERVER with the attachment as an owned reference — the
    // server persists it into the thread BRAIN later reads.
    const [, wire, , create] = h.startTurn.mock.calls[0]
    expect(wire.text).toBe('use this layout')
    expect(wire.attachmentIds).toEqual(['a1'])
    // R-18/U13 — THE ROW'S PARENTAGE RIDES THIS SAME CALL NOW, so there is no separate
    // `createBuild` round trip left to order against `buildFromPlan`: `create` is this call's
    // 4th argument, present because this is the FIRST message on an empty thread, and it is
    // what lets the server check the workspace before creating the row at all (rather than
    // committing it a round trip earlier, as the retired two-call protocol did — see
    // `fireRelayTurn`'s R-18 comment in `ConversationSurface.tsx`).
    expect(create).toMatchObject({ projectId: 'p1', kind: 'build' })
    // …and this ONE call — carrying both the message and the row's parentage — still happens
    // BEFORE the build is started (create+turn → stream → confirm → build). The two-call
    // ordering this test used to prove via `createBuild` vs `buildFromPlan` collapses to a
    // single call ordered against `buildFromPlan`, because there is only one call left to order.
    expect(h.startTurn.mock.invocationCallOrder[0]).toBeLessThan(
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
    // Third arg is the client-minted id of the NEW build chat the handoff creates (U5/U12) —
    // a real `uuidv7()`, so only its shape is pinned here, not its value.
    expect(h.buildFromPlan).toHaveBeenCalledWith('build-X', PLAN_CARD_ID, expect.any(String))
  })

  it('ABORTS the send when the upload fails — never starts a text-only build (R3)', async () => {
    h.buildUserParts.mockRejectedValue(new Error('Upload failed: storage is full.'))
    renderBuilder()
    const textarea = await screen.findByPlaceholderText(/ask for another change/i)
    fireEvent.change(textarea, { target: { value: 'use this sheet' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await screen.findByText(/Upload failed: storage is full./i)
    // The turn never reaches the server AT ALL — no `create` block, no message, nothing — so no
    // plan comes back and there is nothing to confirm — the file-less build is unreachable rather
    // than merely un-triggered. (There is no separate `createBuild` call left to assert absent:
    // R-18 folded the row's creation into this same, never-attempted `startTurn`.)
    expect(h.startTurn).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /^Build this plan$/ })).toBeNull()
    expect(h.buildFromPlan).not.toHaveBeenCalled() // no build ignoring the file
  })
})
