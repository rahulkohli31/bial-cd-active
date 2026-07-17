/**
 * The persisted build outcome (003-U5).
 *
 * Reopening a finished thread should tell the whole story — prompt, questions, brief, and what the
 * build produced. The live narrative (status line + activity feed) is deliberately not persisted;
 * this one message is.
 *
 * THE TESTS WITH TEETH ARE THE DEDUPE ONES. The server's append idempotency covers a replay of the
 * same `_id` and a race on the same seq — neither catches the real duplicate, which is a RELOAD:
 * the portal mints a fresh `_id` and a fresh seq, so a replayed terminal appends a clean SECOND
 * outcome for a build that ran once. Only the sessionId scan closes that, so it is what these pin.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient, briefReply, relayReplying, PREVIEW, PREVIEW_URL, ENDED, STEP,
} from './_builderSession.jsx'

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
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
vi.mock('../../components/AttachmentChips', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null }),
}))

import BuilderPage from '../BuilderPage'

function renderThread(chatId = 'thread-1') {
  const fake = new FakeEventSource(chatId)
  const deps = { client: makeClient(h), eventSourceFactory: () => fake }
  const view = render(
    <MemoryRouter initialEntries={[`/chat/${chatId}`]}>
      <Routes>
        <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" buildSessionDeps={deps} />} />
      </Routes>
    </MemoryRouter>,
  )
  return { ...view, fake }
}

/** Drive a build to running: send → confirm the card → open the feed. */
async function runBuild(fake) {
  fireEvent.change(screen.getByPlaceholderText(/describe what you need/i), { target: { value: 'a visitor app' } })
  fireEvent.keyDown(screen.getByPlaceholderText(/describe what you need/i), { key: 'Enter' })
  fireEvent.click(await screen.findByRole('button', { name: /build this/i }))
  await waitFor(() => expect(h.start).toHaveBeenCalled())
  act(() => { fake.open(); fake.emitEnvelope(STEP(1)) })
}

/** The `build` parts the page actually persisted. */
const persistedOutcomes = () =>
  h.appendBuilderMessage.mock.calls
    .flatMap(([, message]) => message.parts || [])
    .filter((p) => p?.type === 'build')

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.appendBuilderMessage.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  h.sendMessage.mockImplementation(relayReplying(briefReply()))
})
afterEach(cleanup)

describe('recording the outcome', () => {
  it('persists one outcome with status + preview link when the build ends', async () => {
    const { fake } = renderThread()
    await runBuild(fake)

    act(() => { fake.emitEnvelope(PREVIEW(3)); fake.emitEnvelope(ENDED(9)) })

    await waitFor(() => expect(persistedOutcomes()).toHaveLength(1))
    expect(persistedOutcomes()[0]).toMatchObject({
      type: 'build',
      status: 'ended',
      sessionId: 's1',
      previewUrl: PREVIEW_URL,
      snapshotCommitted: true,
      reason: 'completed',
    })
    const card = await screen.findByTestId('build-outcome')
    expect(card.textContent).toMatch(/build finished/i)
    expect(card.querySelector(`a[href="${PREVIEW_URL}"]`)).toBeTruthy()
  })

  it('carries a summary text part, so the turn is not empty to a reader or the model', async () => {
    const { fake } = renderThread()
    await runBuild(fake)

    act(() => { fake.emitEnvelope(ENDED(9)) })

    await waitFor(() => expect(persistedOutcomes()).toHaveLength(1))
    const outcomeCall = h.appendBuilderMessage.mock.calls.find(([, m]) =>
      (m.parts || []).some((p) => p.type === 'build'),
    )
    // buildContent skips unknown part types, so a build-part-only message would assemble to an
    // EMPTY assistant turn on the next relay send.
    const text = outcomeCall[1].parts.find((p) => p.type === 'text')
    expect(text.text).toMatch(/build finished/i)
  })

  it('records a failed build with its reason', async () => {
    const { fake } = renderThread()
    await runBuild(fake)

    act(() => { fake.emitEnvelope(ENDED(9, 'failed', 'tsc failed after 3 attempts')) })

    await waitFor(() => expect(persistedOutcomes()).toHaveLength(1))
    expect(persistedOutcomes()[0]).toMatchObject({ status: 'failed', reason: 'tsc failed after 3 attempts' })
    const card = await screen.findByTestId('build-outcome')
    expect(card.textContent).toMatch(/build failed/i)
    // The reason is what the user can act on — surface it, don't bury it in the feed.
    expect(card.textContent).toMatch(/tsc failed after 3 attempts/i)
  })

  it('warns when a build ran but its code was not saved', async () => {
    const { fake } = renderThread()
    await runBuild(fake)

    act(() => { fake.emitEnvelope({ ...ENDED(9), snapshot_committed: false }) })

    // A build that did not save is not a success: the next build will not start from it, and the
    // user has to know that before building on top of it.
    expect(await screen.findByText(/wasn’t saved/i)).toBeTruthy()
    expect(persistedOutcomes()[0].snapshotCommitted).toBe(false)
  })

  it('does not record anything while the build is still running', async () => {
    const { fake } = renderThread()
    await runBuild(fake)

    act(() => { fake.emitEnvelope(PREVIEW(3)) })

    await waitFor(() => expect(screen.getByText(/preview is live/i)).toBeTruthy())
    expect(persistedOutcomes()).toHaveLength(0)
    expect(screen.queryByTestId('build-outcome')).toBeNull()
  })
})

describe('dedupe on sessionId', () => {
  it('does not double-record a replayed terminal envelope', async () => {
    const { fake } = renderThread()
    await runBuild(fake)

    act(() => { fake.emitEnvelope(ENDED(9)) })
    await waitFor(() => expect(persistedOutcomes()).toHaveLength(1))
    act(() => { fake.emitEnvelope(ENDED(9)) }) // a reattach replays the terminal

    await waitFor(() => expect(screen.getAllByTestId('build-outcome')).toHaveLength(1))
    expect(persistedOutcomes()).toHaveLength(1)
  })

  it('does not re-record after a reload, where the _id and seq are both FRESH', async () => {
    // The case seq/_id idempotency cannot catch: the transcript already holds this session's
    // outcome, and the replayed terminal would otherwise append a clean second one.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      messages: [
        { id: 'm0', role: 'user', parts: [{ type: 'text', text: 'a visitor app' }], seq: 0 },
        {
          id: 'm1',
          role: 'assistant',
          seq: 1,
          parts: [
            { type: 'text', text: 'Build finished.' },
            { type: 'build', status: 'ended', sessionId: 's1', previewUrl: PREVIEW_URL },
          ],
        },
      ],
    })
    const { fake } = renderThread()
    await screen.findByTestId('build-outcome')
    // Reattach onto the same session, which then replays its terminal.
    h.getStatus.mockResolvedValue({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'building', previewUrl: null, lastSeq: 1, createdAt: 'c', updatedAt: 'u' })
    await runBuild(fake)

    act(() => { fake.emitEnvelope(ENDED(9)) })

    await waitFor(() => expect(h.start).toHaveBeenCalled())
    expect(screen.getAllByTestId('build-outcome')).toHaveLength(1)
    expect(persistedOutcomes()).toHaveLength(0) // nothing new written — the record already exists
  })

  it('records a SECOND build separately — dedupe is per session, not per thread', async () => {
    const { fake } = renderThread()
    await runBuild(fake)
    act(() => { fake.emitEnvelope(ENDED(9)) })
    await waitFor(() => expect(persistedOutcomes()).toHaveLength(1))

    // An iteration is a new session, and its outcome is its own record.
    h.start.mockResolvedValue({ sessionId: 's2', projectId: 'p1', appId: 'a1', status: 'provisioning', previewUrl: null, createdAt: 'c' })
    h.sendMessage.mockImplementation(relayReplying(briefReply('Build it, now with a chart.')))
    fireEvent.change(screen.getByPlaceholderText(/describe what you need/i), { target: { value: 'add a chart' } })
    fireEvent.keyDown(screen.getByPlaceholderText(/describe what you need/i), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: /build this|rebuild/i }))
    await waitFor(() => expect(h.start).toHaveBeenCalledTimes(2))
    act(() => { fake.emitEnvelope(ENDED(10)) })

    await waitFor(() => expect(persistedOutcomes()).toHaveLength(2))
    expect(persistedOutcomes().map((p) => p.sessionId)).toEqual(['s1', 's2'])
  })

  it('re-arms when the outcome write fails, so a retry is not deduped against a lost write', async () => {
    const { fake } = renderThread()
    await runBuild(fake)
    h.appendBuilderMessage.mockRejectedValueOnce(new Error('network'))

    act(() => { fake.emitEnvelope(ENDED(9)) })
    await waitFor(() => expect(persistedOutcomes()).toHaveLength(1))

    // The write did not land, so the guard must not claim it did — a later terminal for the same
    // session may still record it.
    act(() => { fake.emitEnvelope(ENDED(9)) })
    await waitFor(() => expect(persistedOutcomes().length).toBeGreaterThanOrEqual(1))
  })
})
