import { describe, it, expect, vi } from 'vitest'
import {
  listConversations,
  listProjectConversations,
  getConversation,
  appendMessage,
  patchConversation,
  deleteConversation,
  createConversationStore,
  deriveTitle,
} from '../conversationApi.js'

// authFetch deps injection — no real token/network.
const deps = (fetchImpl) => ({ fetchImpl, getToken: () => 'tok', refresh: vi.fn() })
const ok = (json) => ({ ok: true, status: 200, json: async () => json })

describe('listConversations', () => {
  it('GETs ?kind= and normalizes _id → id', async () => {
    const fetchImpl = vi.fn(async () => ok({ conversations: [{ _id: 'c1', kind: 'planning', title: 'T', updatedAt: '2026-06-20T00:00:00Z' }] }))
    const list = await listConversations('planning', deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/conversations?kind=planning')
    expect(list).toEqual([{ id: 'c1', kind: 'planning', title: 'T', createdAt: undefined, updatedAt: '2026-06-20T00:00:00Z' }])
  })
  it('throws the server message on failure', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 500, json: async () => ({ error: { message: 'boom' } }) }))
    await expect(listConversations('planning', deps(fetchImpl))).rejects.toThrow('boom')
  })
})

describe('listProjectConversations', () => {
  it('GETs ?projectId= and returns both kinds', async () => {
    const fetchImpl = vi.fn(async () =>
      ok({
        conversations: [
          { _id: 'c1', kind: 'planning', projectId: 'p1', title: 'Plan' },
          { _id: 'c2', kind: 'builder', projectId: 'p1', title: 'Build' },
        ],
      }),
    )
    const list = await listProjectConversations('p1', deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/conversations?projectId=p1')
    expect(list.map((c) => c.kind)).toEqual(['planning', 'builder'])
    expect(list.every((c) => c.projectId === 'p1')).toBe(true)
  })
  it('url-encodes the project id', async () => {
    const fetchImpl = vi.fn(async () => ok({ conversations: [] }))
    await listProjectConversations('a/b?c', deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/conversations?projectId=a%2Fb%3Fc')
  })
  it('throws the server message on failure', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 404, json: async () => ({ error: { message: 'Project not found.' } }) }))
    await expect(listProjectConversations('p1', deps(fetchImpl))).rejects.toThrow('Project not found.')
  })
})

describe('getConversation', () => {
  it('hydrates header + messages into the in-memory shape', async () => {
    const fetchImpl = vi.fn(async () =>
      ok({
        conversation: { _id: 'c1', kind: 'builder', title: 'App', code: { current: { source: 'X' } }, context: { theme: 'bial' } },
        messages: [{ _id: 'm0', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 0 }],
      }),
    )
    const conv = await getConversation('c1', deps(fetchImpl))
    expect(conv.id).toBe('c1')
    expect(conv.code.current.source).toBe('X')
    expect(conv.context).toEqual({ theme: 'bial' })
    expect(conv.messages).toEqual([{ id: 'm0', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 0, createdAt: undefined }])
  })
  it('returns null on 404', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) }))
    expect(await getConversation('missing', deps(fetchImpl))).toBeNull()
  })
  // Regression guard: normalizeHeader used to drop projectId on the floor, so every
  // caller read `undefined`. ChatRoute's kind dispatch and the chat breadcrumb both
  // depend on this surviving hydration.
  it('surfaces conversation.projectId', async () => {
    const fetchImpl = vi.fn(async () => ok({ conversation: { _id: 'c1', kind: 'builder', projectId: 'p1' }, messages: [] }))
    expect((await getConversation('c1', deps(fetchImpl))).projectId).toBe('p1')
  })
})

describe('appendMessage / patchConversation / deleteConversation', () => {
  it('POSTs {message, header} to the messages route', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 201, json: async () => ({ ok: true }) }))
    await appendMessage('c1', { _id: 'm1', role: 'user', parts: [{ type: 'text', text: 'x' }], seq: 0 }, { kind: 'planning', title: 'T' }, deps(fetchImpl))
    const [url, opts] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/conversations/c1/messages')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ message: { _id: 'm1', role: 'user', parts: [{ type: 'text', text: 'x' }], seq: 0 }, header: { kind: 'planning', title: 'T' } })
  })
  it('appendMessage rejects on a network failure (no silent drop)', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 500, json: async () => ({ error: { message: 'save failed' } }) }))
    await expect(appendMessage('c1', { _id: 'm', role: 'user', parts: [], seq: 0 }, { kind: 'planning' }, deps(fetchImpl))).rejects.toThrow('save failed')
  })
  it('patchConversation PATCHes the body; deleteConversation DELETEs (404 tolerated)', async () => {
    const patchFetch = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) }))
    await patchConversation('c1', { title: 'new' }, deps(patchFetch))
    expect(patchFetch.mock.calls[0][1].method).toBe('PATCH')

    const delFetch = vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) }))
    await expect(deleteConversation('c1', deps(delFetch))).resolves.toBe(true) // 404 is fine (already gone)
  })
})

describe('createConversationStore', () => {
  it('newConversation mints a client UUID synchronously (no network)', () => {
    const store = createConversationStore('planning')
    const a = store.newConversation()
    expect(a).toMatch(/^[0-9a-f-]{36}$/i)
    expect(store.newConversation()).not.toBe(a)
  })

  it('appendMessage mints _id/schemaVersion/createdAt and binds the kind into the header', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 201, json: async () => ({ ok: true }) }))
    const store = createConversationStore('assistant')
    await store.appendMessage('c1', { role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 2 }, { title: 'T' }, deps(fetchImpl))
    const body = JSON.parse(fetchImpl.mock.calls[0][1].body)
    expect(body.header).toEqual({ kind: 'assistant', title: 'T' })
    expect(body.message).toMatchObject({ role: 'user', seq: 2, schemaVersion: 1, parts: [{ type: 'text', text: 'hi' }] })
    expect(body.message._id).toMatch(/^[0-9a-f-]{36}$/i)
    expect(typeof body.message.createdAt).toBe('string')
  })
})

describe('deriveTitle', () => {
  it('truncates at 40 with ellipsis and trims', () => {
    expect(deriveTitle('  hello  ')).toBe('hello')
    expect(deriveTitle('y'.repeat(60))).toBe('y'.repeat(40) + '…')
  })
})
