/**
 * WHAT THE PLATFORM OWES A CITIZEN WHOSE APP NOW LIVES AND DIES WITHOUT ASKING THEM.
 *
 * The exit prompts are gone and the start control is gone; opening a project starts its app and
 * leaving takes it away. Two facts a screen cannot infer from that, and both are stated here: the
 * container has a ceiling nothing postpones, and an automatic write-back can be refused — in
 * which case the app comes back from the citizen's own saved version and looks, from the screen,
 * exactly like an ordinary reopen.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import WorkspaceLifecycleNotes from '../WorkspaceLifecycleNotes'
import type { PreviewLifeState, PreviewState } from '../../../utils/buildSessionApi'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), resolvePlanOptions: vi.fn(),
  stop: vi.fn(), getStatus: vi.fn(),
  relaunchPreview: vi.fn(), fetchPreviewState: vi.fn(), fetchSaveState: vi.fn(),
  fetchCompileState: vi.fn(), checkWorkspace: vi.fn(), renewPresence: vi.fn(),
}))

vi.mock('../../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t: string) => (t || '').slice(0, 40),
}))
vi.mock('../../../utils/conversationApi', () => ({
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../../utils/attachmentStore', async (orig) => ({
  ...(await orig<typeof import('../../../utils/attachmentStore')>()),
  buildUserParts: h.buildUserParts,
}))
vi.mock('../../../utils/turnStreamApi', async (orig) => ({
  ...(await orig<typeof import('../../../utils/turnStreamApi')>()),
  startTurn: (...a: unknown[]) => h.startTurn(...a),
  readTurnStream: (...a: unknown[]) => h.readTurnStream(...a),
  buildFromPlan: (...a: unknown[]) => h.buildFromPlan(...a),
  resolvePlanOptions: (...a: unknown[]) => h.resolvePlanOptions(...a),
}))
vi.mock('../../../utils/buildSessionApi', async (orig) => ({
  ...(await orig<typeof import('../../../utils/buildSessionApi')>()),
  fetchPreviewState: (...a: unknown[]) => h.fetchPreviewState(...a),
  fetchSaveState: (...a: unknown[]) => h.fetchSaveState(...a),
  fetchCompileState: (...a: unknown[]) => h.fetchCompileState(...a),
  checkWorkspace: (...a: unknown[]) => h.checkWorkspace(...a),
  relaunchPreview: (...a: unknown[]) => h.relaunchPreview(...a),
  renewPresence: (...a: unknown[]) => h.renewPresence(...a),
}))

const { renderBuilder, makeClient, primeClient, FakeEventSource } =
  await import('../../../pages/__tests__/_builderSession.jsx')

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

const inMinutes = (n: number) => new Date(Date.now() + n * 60_000).toISOString()

describe('the closing-soon note', () => {
  it('names the time the app closes when the ceiling is near', () => {
    render(<WorkspaceLifecycleNotes drainingAt={inMinutes(10)} writeBackRefusedAt={null} />)

    expect(screen.getByTestId('closing-soon-note').textContent).toMatch(/closes at/i)
  })

  it('says NOTHING about a ceiling that is hours away', () => {
    // Every container has one. Announcing it on arrival turns a routine fact into a warning, and
    // a screen that always warns is a screen nobody reads.
    render(<WorkspaceLifecycleNotes drainingAt={inMinutes(115)} writeBackRefusedAt={null} />)

    expect(screen.queryByTestId('closing-soon-note')).toBeNull()
  })

  it('★ says nothing at all when no ceiling applies', () => {
    // `null` MEANS NO CEILING, NEVER "SOON". A screen that read it as imminent would announce a
    // collection that is not coming — and the flag ships off, so that is the ordinary case.
    render(<WorkspaceLifecycleNotes drainingAt={null} writeBackRefusedAt={null} />)

    expect(screen.queryByTestId('workspace-lifecycle-notes')).toBeNull()
  })

  it('says nothing when the instant is not readable as one', () => {
    render(<WorkspaceLifecycleNotes drainingAt="not-a-date" writeBackRefusedAt={null} />)

    expect(screen.queryByTestId('closing-soon-note')).toBeNull()
  })

  it('★ says nothing once the ceiling has passed', () => {
    // A window bounded only from ABOVE is satisfied by every instant already behind us, so the
    // note goes on naming a closing time that has been and gone — which is the one thing a
    // sentence written to make silence honest must never become.
    render(<WorkspaceLifecycleNotes drainingAt={inMinutes(-5)} writeBackRefusedAt={null} />)
    expect(screen.queryByTestId('closing-soon-note')).toBeNull()

    // LIVENESS: the same component, the same distance from now, ahead instead of behind. Without
    // it an absence proves only that nothing rendered.
    cleanup()
    render(<WorkspaceLifecycleNotes drainingAt={inMinutes(5)} writeBackRefusedAt={null} />)
    expect(screen.getByTestId('closing-soon-note')).toBeTruthy()
  })
})

describe('the refused write-back note', () => {
  it('★ states the refusal, where the work went, and what to do about it', () => {
    // THE SENTENCE THAT MAKES REMOVING THE EXIT PROMPTS HONEST. Without it, a citizen whose
    // write-back was refused reopens their app, finds older work, and has nothing on screen to
    // tell them why or that the newer tree still exists.
    render(<WorkspaceLifecycleNotes drainingAt={null} writeBackRefusedAt="2026-09-17T22:14:00Z" />)

    const note = screen.getByTestId('writeback-refused-note').textContent ?? ''
    expect(note).toMatch(/could not be saved back/i)
    expect(note).toMatch(/last saved version/i)
    expect(note).toMatch(/set aside/i)
  })

  it('is stated even when the ceiling is far away — they are independent facts', () => {
    render(<WorkspaceLifecycleNotes drainingAt={inMinutes(600)} writeBackRefusedAt="2026-09-17T22:14:00Z" />)

    expect(screen.getByTestId('writeback-refused-note')).toBeTruthy()
    expect(screen.queryByTestId('closing-soon-note')).toBeNull()
  })

  it('announces politely — neither of these interrupts anything', () => {
    render(<WorkspaceLifecycleNotes drainingAt={null} writeBackRefusedAt="2026-09-17T22:14:00Z" />)

    expect(screen.getByTestId('workspace-lifecycle-notes').getAttribute('aria-live')).toBe('polite')
  })
})

/**
 * ★ THE NOTE HAS TO REACH THE CITIZEN WHO IS MID-TASK, AND THAT CITIZEN IS IN A CHAT.
 *
 * Rendering this component directly proves only that it can draw a sentence. What has to be true
 * is that the sentence reaches an address that is a CONVERSATION — through the real shell, out of
 * the renewal answer that is the only place a ceiling comes from, into the column that frames the
 * app. A suite that stops at the component passes while nothing on screen ever mounts it.
 */
describe('★ reaching the citizen who is mid-conversation', () => {
  const LIVE: PreviewState = {
    state: 'alive' as PreviewLifeState,
    alive: true,
    previewUrl: 'https://app.example/',
    occupyingProjectName: null,
    occupyingProjectId: null,
    restorable: true,
  }

  function deps() {
    const fake = new FakeEventSource('x')
    return { client: makeClient(h), eventSourceFactory: () => fake }
  }

  beforeEach(() => {
    vi.clearAllMocks()
    Element.prototype.scrollIntoView = vi.fn()
    primeClient(h)
    h.newBuild.mockReturnValue('build-Y')
    h.createBuild.mockResolvedValue({ ok: true })
    h.getBuild.mockResolvedValue(null)
    h.loadBuilds.mockResolvedValue([])
    h.listProjectConversations.mockResolvedValue([
      { id: 'build-X', kind: 'build', title: 'My build', updatedAt: new Date().toISOString() },
    ])
    h.buildUserParts.mockImplementation(async (text: string) => [{ type: 'text', text }])
    h.fetchSaveState.mockResolvedValue({
      appId: 'a1', dirty: false, savedHead: null, containerHead: null,
      recoveryAt: null, writeBackRefusedAt: null,
    })
    h.fetchCompileState.mockResolvedValue({ state: 'unknown' })
    h.checkWorkspace.mockResolvedValue(false)
    h.fetchPreviewState.mockResolvedValue(LIVE)
    h.renewPresence.mockResolvedValue({ outcome: 'renewed', drainingAt: inMinutes(10) })
  })

  it('★ names the closing time on the chat surface, out of the renewal that answered it', async () => {
    renderBuilder({ deps: deps() })

    const note = await screen.findByTestId('closing-soon-note')
    expect(note.textContent).toMatch(/closes at/i)
  })

  it('★ and it is said in the column that frames the app, not in the one a route replaces', async () => {
    renderBuilder({ deps: deps() })
    const note = await screen.findByTestId('closing-soon-note')

    expect(note.closest('[data-testid="app-pane-region"]')).not.toBeNull()
    // LIVENESS for the absence below: the outlet column really rendered, so "the note is not in
    // it" is a fact about where the note lives rather than about a column that never existed.
    expect(screen.getByTestId('workspace-outlet')).toBeTruthy()
    expect(note.closest('[data-testid="workspace-outlet"]')).toBeNull()
  })

  it('★ says nothing when the renewal reports no ceiling', async () => {
    // `null` MEANS NO CEILING, and the flag ships off — so this is the ordinary case, and a note
    // that appeared here would be announcing a collection that is not coming.
    h.renewPresence.mockResolvedValue({ outcome: 'renewed', drainingAt: null })
    renderBuilder({ deps: deps() })

    // The surface really came up and really renewed, so the silence is the note's decision.
    await screen.findByTestId('app-pane-region')
    expect(screen.queryByTestId('workspace-lifecycle-notes')).toBeNull()
  })
})
