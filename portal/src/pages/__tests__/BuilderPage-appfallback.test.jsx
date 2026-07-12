/**
 * The builder preview falls back to the PROJECT'S durable app code.
 *
 * Code is stored per-conversation (`patchBuildCode` writes the chat's own `code.current`), so
 * a chat that did not build the app — a second chat, or a plain "hi" turn — opens with an empty
 * snapshot. The bug this pins: that chat rendered LivePreview's blank empty state over a
 * fully-built app. The fix reads the project's ONE durable source (`app_registry.current_code`)
 * by appId and seeds the preview, but strictly as a FALLBACK:
 *
 *   1. no own code + project has an app  → preview shows the durable app source;
 *   2. the chat HAS its own code          → own code wins, the fallback never fetches;
 *   3. the project has no app             → no fetch, the empty state stays.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
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
  buildUserParts: vi.fn(),
  getAppStatus: vi.fn(),
  getAppSource: vi.fn(),
  provisionApp: vi.fn(),
  previewCodes: [],
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
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../utils/appRegistryApi', () => ({
  getAppStatus: h.getAppStatus,
  getAppSource: h.getAppSource,
  provisionApp: h.provisionApp,
  submitApp: vi.fn(),
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
// Expose previewCode as text AND record every value it was handed, so we can assert both the
// final render and that a live turn was never clobbered.
vi.mock('../../components/LivePreview', () => ({
  default: ({ previewCode }) => {
    h.previewCodes.push(previewCode)
    return <div data-testid="preview">{previewCode ?? 'EMPTY'}</div>
  },
}))
vi.mock('../../utils/attachmentStore', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, buildUserParts: h.buildUserParts }
})

import BuilderPage from '../BuilderPage'

/** A saved builder row with an optional own-code snapshot. */
function savedBuild({ code = null } = {}) {
  return {
    id: 'chat-1',
    kind: 'builder',
    messages: [{ id: 'u0', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 0 }],
    code,
  }
}

function renderBuilder({ projectAppId = null } = {}) {
  return render(
    <MemoryRouter initialEntries={['/chat/chat-1']}>
      <Routes>
        <Route
          path="/chat/:chatId"
          element={<BuilderPage projectId="p1" projectName="VIP" projectAppId={projectAppId} />}
        />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn() // jsdom doesn't implement it
  h.previewCodes.length = 0
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.getAppStatus.mockResolvedValue({ status: null })
  h.getAppSource.mockResolvedValue({ source: 'DURABLE-APP-CODE', entry: 'PreviewApp' })
})
afterEach(() => cleanup())

describe('BuilderPage — preview fallback to the project app', () => {
  it('a chat with no code of its own renders the project’s durable app (read by appId)', async () => {
    h.getBuild.mockResolvedValue(savedBuild({ code: null }))
    renderBuilder({ projectAppId: 'app-existing' })

    await waitFor(() => expect(screen.getByTestId('preview').textContent).toBe('DURABLE-APP-CODE'))
    expect(h.getAppSource).toHaveBeenCalledWith('app-existing')
  })

  it('a chat WITH its own code wins — the fallback never fetches the app source', async () => {
    h.getBuild.mockResolvedValue(savedBuild({ code: { current: { source: 'OWN-CODE' } } }))
    renderBuilder({ projectAppId: 'app-existing' })

    await waitFor(() => expect(screen.getByTestId('preview').textContent).toBe('OWN-CODE'))
    // The own snapshot owns the canvas; the durable source is never even requested…
    expect(h.getAppSource).not.toHaveBeenCalled()
    // …and DURABLE-APP-CODE never flashed onto the preview at any render.
    expect(h.previewCodes).not.toContain('DURABLE-APP-CODE')
  })

  it('a project with no app leaves the empty state in place (no fetch)', async () => {
    h.getBuild.mockResolvedValue(savedBuild({ code: null }))
    renderBuilder({ projectAppId: null })

    await waitFor(() => expect(h.getBuild).toHaveBeenCalled())
    expect(h.getAppSource).not.toHaveBeenCalled()
    expect(screen.getByTestId('preview').textContent).toBe('EMPTY')
  })
})
