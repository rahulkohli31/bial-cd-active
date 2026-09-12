/**
 * The passive "seed the preview from the project's stored app code" fallback is GONE. A stored
 * app is not a running sandbox: the live preview now comes ONLY from a per-session build. This
 * pins the removed path as INERT — landing a saved chat fires no `getAppSource`, and no stored code
 * is flashed into the preview (which stays its empty, session-driven state until a build starts).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, cleanup } from '@testing-library/react'
import { FakeEventSource, makeClient, primeClient, renderBuilder } from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), appendBuilderMessage: vi.fn(), getBuild: vi.fn(),
  listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  stop: vi.fn(), getStatus: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, appendBuilderMessage: h.appendBuilderMessage,
  getBuild: h.getBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({
  // The send path creates the chat before its first upload; stubbed so no network is reached.
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))

function deps() {
  const fake = new FakeEventSource('x')
  return { client: makeClient(h), eventSourceFactory: () => fake }
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([{ id: 'build-X', kind: 'build', title: 'My build', updatedAt: new Date().toISOString() }])
})
afterEach(() => cleanup())

describe('BuilderPage — the passive stored-app preview is inert', () => {
  it('landing a saved chat whose project has an app fires NO getAppSource and frames no stored code', async () => {
    h.getBuild.mockResolvedValue({
      messages: [{ id: 'm1', role: 'user', parts: [{ type: 'text', text: 'build the gate board' }], seq: 0 }],
      context: { theme: 'bial' },
      code: { current: { source: 'DURABLE-APP-CODE', entry: 'PreviewApp' } },
    })
    renderBuilder({ deps: deps() })

    expect(await screen.findByText(/build the gate board/i)).toBeTruthy()
    // The durable app code is deliberately never read into the preview; no frame is mounted.
    await waitFor(() => expect(h.getBuild).toHaveBeenCalled())
    // getAppSource itself is retired from appRegistryApi (owner surface gone;
    // pinned by appRegistryApi.test.js) — the stale-code framing path cannot exist.
    expect(document.querySelector('iframe')).toBeNull()
    expect(screen.queryByText(/DURABLE-APP-CODE/)).toBeNull()
  })
})
