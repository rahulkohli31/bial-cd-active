/**
 * U11 generation-persistence guards. These exercise the resurrection bug fixed by
 * the deletedRef guard in BuilderPage.generate(): deleting a build while its
 * generation is still streaming must NOT persist the assistant turn / code (which
 * would re-upsert the just-deleted header and resurrect it), while a mid-stream
 * build SWITCH must still attribute the result to the build the run started on.
 *
 * The component is driven through its real UI (type → Enter → Recent dropdown →
 * delete/switch); the API + history store are mocked at the module boundary so we
 * can hold the stream open (deferred sendMessage) and assert exactly what is
 * persisted. The flat `/chat/:chatId` MemoryRouter mirrors App.jsx so navigate()
 * preserves the BuilderPage instance (its refs survive), matching production.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

const h = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  loadBuilds: vi.fn(),
  newBuild: vi.fn(),
  appendBuilderMessage: vi.fn(),
  getBuild: vi.fn(),
  deleteBuild: vi.fn(),
  patchBuildCode: vi.fn(),
  listProjectConversations: vi.fn(),
  provisionApp: vi.fn(),
  getAppStatus: vi.fn(),
}))

vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null }),
  buildSystemPrompt: () => 'sys',
  getContextLimits: () => ({ soft: 1e9, hard: 1e9 }),
  estimateConversationTokens: () => 0,
}))
vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds,
  newBuild: h.newBuild,
  appendBuilderMessage: h.appendBuilderMessage,
  getBuild: h.getBuild,
  deleteBuild: h.deleteBuild,
  patchBuildCode: h.patchBuildCode,
  deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
// Every code-bearing turn now provisions the project's app before patching the code, so
// the registry must be mocked or the page would reach the network.
vi.mock('../../utils/appRegistryApi', () => ({
  provisionApp: h.provisionApp,
  getAppStatus: h.getAppStatus,
  submitApp: vi.fn(),
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))

import BuilderPage from '../BuilderPage'

const CODE_RESULT = '```jsx:preview\nexport default function PreviewApp(){return null}\n```'

// Make sendMessage stay pending until the returned `release(result)` is called.
function deferredSend() {
  let resolveFn
  h.sendMessage.mockImplementation(() => new Promise((res) => { resolveFn = res }))
  return (result) => act(async () => { resolveFn(result); await Promise.resolve() })
}

// A build chat always has a client-minted id in the URL. A fresh one carries its
// project in a transient query until its first append; ChatRoute is bypassed here and
// the props it would inject are passed directly.
function renderBuilder(chatId = 'build-X') {
  return render(
    <MemoryRouter initialEntries={[`/chat/${chatId}?projectId=p1&kind=builder`]}>
      <Routes>
        <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="VIP Movement" />} />
        <Route path="/projects/:projectId" element={<div>project home</div>} />
        <Route path="/projects" element={<div>projects index</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

// Type a refinement and send it, returning the stream-release fn once the
// generation is in flight (sendMessage called, seed user turn already persisted).
async function startGeneration() {
  const release = deferredSend()
  const textarea = await screen.findByPlaceholderText(/Type instructions/i)
  fireEvent.change(textarea, { target: { value: 'make it blue' } })
  fireEvent.keyDown(textarea, { key: 'Enter' })
  await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))
  return release
}

const assistantWrites = () => h.appendBuilderMessage.mock.calls.filter((c) => c[1].role === 'assistant')

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn() // jsdom doesn't implement it
  h.newBuild.mockReturnValue('build-X')
  h.appendBuilderMessage.mockResolvedValue({ ok: true })
  h.patchBuildCode.mockResolvedValue({ ok: true })
  h.deleteBuild.mockResolvedValue(true)
  h.getBuild.mockResolvedValue(null) // a fresh chat: its row does not exist until the first append
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.provisionApp.mockResolvedValue({ appId: 'app-1', appKey: 'k', status: 'draft', loginRequired: false })
  h.getAppStatus.mockResolvedValue({ status: null })
})
afterEach(() => cleanup())

describe('BuilderPage — generation persistence guards (U11)', () => {
  it('gates the active build\'s delete while its turn streams, and re-enables it after (F-1)', async () => {
    h.listProjectConversations.mockResolvedValue([{ id: 'build-X', kind: 'builder', title: 'My build', updatedAt: new Date().toISOString() }])
    renderBuilder()
    const release = await startGeneration()

    // Only the seed user turn has been persisted so far.
    expect(h.appendBuilderMessage).toHaveBeenCalledTimes(1)
    expect(h.appendBuilderMessage.mock.calls[0][1].role).toBe('user')

    // The streaming build's delete control is disabled — the belt over the deletedRef guard, so
    // a real click can't issue a mid-stream delete (F-1). handleDeleteBuild also early-returns
    // for this exact case; the deletedRef backstop is exercised in a sibling test below.
    fireEvent.click(screen.getByTitle('Recent builds'))
    const delBtn = await screen.findByLabelText('Delete My build')
    expect(delBtn.disabled).toBe(true)
    expect(h.deleteBuild).not.toHaveBeenCalled() // nothing deleted while streaming

    // Once the stream resolves (the code turn persisted → generating is false), the control is
    // enabled again — wait for the turn to fully settle before asserting.
    await release(CODE_RESULT)
    await waitFor(() => expect(h.patchBuildCode).toHaveBeenCalled())
    expect(screen.getByLabelText('Delete My build').disabled).toBe(false)
  })

  it('still allows deleting a DIFFERENT, non-streaming build while one streams (no over-gating)', async () => {
    h.listProjectConversations.mockResolvedValue([
      { id: 'build-X', kind: 'builder', title: 'My build', updatedAt: new Date().toISOString() },
      { id: 'build-Y', kind: 'builder', title: 'Other build', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    renderBuilder()
    const release = await startGeneration() // the run streams on build-X (the current view)

    // build-Y is not the streaming build, so its delete stays enabled and works.
    fireEvent.click(screen.getByTitle('Recent builds'))
    const delOther = await screen.findByLabelText('Delete Other build')
    expect(delOther.disabled).toBe(false)
    fireEvent.click(delOther)
    await waitFor(() => expect(h.deleteBuild).toHaveBeenCalledWith('build-Y'))

    await release(CODE_RESULT) // let build-X's stream finish cleanly
  })

  it('a mid-stream delete of the run\'s origin build (after switching away) no-ops its persist (deletedRef)', async () => {
    // With the active build's delete gated, the deletedRef backstop is reached via the one path
    // the gate can't cover: switch away from build-X (so its delete re-enables while its turn
    // still streams in the background), then delete it. The in-flight persist must still no-op.
    h.listProjectConversations.mockResolvedValue([
      { id: 'build-X', kind: 'builder', title: 'My build', updatedAt: new Date().toISOString() },
      { id: 'build-Y', kind: 'builder', title: 'Other build', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    h.getBuild.mockImplementation(async (id) =>
      id === 'build-Y' ? { id: 'build-Y', title: 'Other build', messages: [], context: null, code: null } : null,
    )
    renderBuilder()
    const release = await startGeneration() // run starts on build-X

    fireEvent.click(screen.getByTitle('Recent builds'))
    fireEvent.click(await screen.findByText('Other build')) // switch to build-Y
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('build-Y'))

    // From build-Y's view, build-X is no longer the current build, so its delete is enabled.
    fireEvent.click(screen.getByTitle('Recent builds'))
    fireEvent.click(await screen.findByLabelText('Delete My build'))
    await waitFor(() => expect(h.deleteBuild).toHaveBeenCalledWith('build-X'))

    await release(CODE_RESULT)

    // The origin build was deleted mid-run — deletedRef no-ops its persist (no resurrection).
    expect(assistantWrites()).toHaveLength(0)
    expect(h.patchBuildCode).not.toHaveBeenCalled()
  })

  it('switching builds mid-generation still attributes the assistant turn + code to the ORIGINAL build', async () => {
    h.listProjectConversations.mockResolvedValue([
      { id: 'build-X', kind: 'builder', title: 'My build', updatedAt: new Date().toISOString() },
      { id: 'build-Y', kind: 'builder', title: 'Other build', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    h.getBuild.mockImplementation(async (id) =>
      id === 'build-Y' ? { id: 'build-Y', title: 'Other build', messages: [], context: null, code: null } : null,
    )
    renderBuilder()
    const release = await startGeneration()

    // Switch to build-Y mid-stream.
    fireEvent.click(screen.getByTitle('Recent builds'))
    fireEvent.click(await screen.findByText('Other build'))
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('build-Y'))

    await release(CODE_RESULT)

    // Result lands on build-X (the run's origin), not the now-active build-Y.
    await waitFor(() => expect(assistantWrites()).toHaveLength(1))
    expect(assistantWrites()[0][0]).toBe('build-X')
    expect(h.patchBuildCode).toHaveBeenCalledWith(
      'build-X',
      expect.objectContaining({ source: expect.any(String), entry: 'PreviewApp' }),
    )
  })
})
