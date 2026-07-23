import { describe, it, expect, vi } from 'vitest'
import {
  listConversations,
  listProjectConversations,
  getConversation,
  createConversation,
  messagesFromProjection,
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
  it('hydrates header + PROJECTION into the in-memory message shape (U7)', async () => {
    const fetchImpl = vi.fn(async () =>
      ok({
        conversation: { _id: 'c1', kind: 'builder', mode: 'plan', title: 'App', context: { theme: 'bial' } },
        projection: [
          { type: 'user_text', seq: 0, mode: 'plan', text: 'hi', attachmentIds: [] },
          { type: 'assistant_text', seq: 1, mode: 'plan', text: 'hello!' },
        ],
        activeTurn: null,
      }),
    )
    const conv = await getConversation('c1', deps(fetchImpl))
    expect(conv.id).toBe('c1')
    expect(conv.mode).toBe('plan')
    expect(conv.context).toEqual({ theme: 'bial' })
    expect(conv.activeTurn).toBeNull()
    expect(conv.messages).toEqual([
      { id: 'srv_0_u', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 0 },
      { id: 'srv_1_a', role: 'assistant', parts: [{ type: 'text', text: 'hello!' }], seq: 1 },
    ])
  })
  it('returns null on 404', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) }))
    expect(await getConversation('missing', deps(fetchImpl))).toBeNull()
  })
  // Regression guard: normalizeHeader used to drop projectId on the floor, so every
  // caller read `undefined`. ChatRoute's kind dispatch and the chat breadcrumb both
  // depend on this surviving hydration.
  it('surfaces conversation.projectId', async () => {
    const fetchImpl = vi.fn(async () => ok({ conversation: { _id: 'c1', kind: 'builder', projectId: 'p1' }, projection: [] }))
    expect((await getConversation('c1', deps(fetchImpl))).projectId).toBe('p1')
  })
})

describe('messagesFromProjection', () => {
  it('maps a banner item to the outcome bubble (text + the build part the page renders)', () => {
    const messages = messagesFromProjection([
      { type: 'banner', seq: 3, mode: 'write', banner: 'completed', text: 'Build finished.', previewUrl: 'https://x', sessionId: 's1' },
    ])
    expect(messages).toEqual([
      {
        id: 'srv_3_b',
        role: 'assistant',
        parts: [
          { type: 'text', text: 'Build finished.' },
          { type: 'build', sessionId: 's1', status: 'ended', reason: 'completed', previewUrl: 'https://x' },
        ],
        seq: 3,
      },
    ])
  })
  it('maps a plan_options item to a card part carrying the STORED item (U13)', () => {
    const item = { type: 'plan_options', seq: 3, mode: 'plan', toolCallId: 't', state: 'pending', reason: null }
    expect(messagesFromProjection([item])).toEqual([
      { id: 'srv_3_p', role: 'assistant', parts: [{ type: 'plan_options', item }], seq: 3 },
    ])
  })
  it('maps visible steps and the in-progress anchor; hidden (read) steps stay out (U15)', () => {
    const visible = { type: 'step', seq: 1, tool: 'write_file', label: 'Updated x', state: 'ok', hidden: false }
    expect(
      messagesFromProjection([
        visible,
        { type: 'step', seq: 2, tool: 'read_file', label: 'Read y', state: 'ok', hidden: true },
        { type: 'build_in_progress', seq: 3, sessionId: 's' },
      ]),
    ).toEqual([
      { id: 'srv_1_s', role: 'assistant', parts: [{ type: 'step', step: visible }], seq: 1 },
      { id: 'srv_3_g', role: 'assistant', parts: [{ type: 'build_in_progress', sessionId: 's' }], seq: 3 },
    ])
  })
})

describe('createConversation / patchConversation / deleteConversation', () => {
  it('POSTs {id, projectId, kind} (+title/context when given) to the conversations route', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 201, json: async () => ({ conversation: { _id: 'c1', kind: 'planning', projectId: 'p1', mode: 'plan' } }) }))
    const header = await createConversation('c1', { projectId: 'p1', kind: 'planning', title: 'T' }, deps(fetchImpl))
    const [url, opts] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/conversations')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ id: 'c1', projectId: 'p1', kind: 'planning', title: 'T' })
    expect(header).toMatchObject({ id: 'c1', kind: 'planning', projectId: 'p1', mode: 'plan' })
  })
  it('createConversation rejects on failure (no silent drop)', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ error: { message: 'id already in use' } }) }))
    await expect(createConversation('c1', { projectId: 'p1', kind: 'planning' }, deps(fetchImpl))).rejects.toThrow('id already in use')
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

  it('createConversation binds the kind into the create body', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 201, json: async () => ({ conversation: { _id: 'c1', kind: 'assistant' } }) }))
    const store = createConversationStore('assistant')
    await store.createConversation('c1', { projectId: 'p1', title: 'T' }, deps(fetchImpl))
    const body = JSON.parse(fetchImpl.mock.calls[0][1].body)
    expect(body).toEqual({ id: 'c1', projectId: 'p1', kind: 'assistant', title: 'T' })
  })
})

describe('deriveTitle', () => {
  it('truncates at 40 with ellipsis and trims', () => {
    expect(deriveTitle('  hello  ')).toBe('hello')
    expect(deriveTitle('y'.repeat(60))).toBe('y'.repeat(40) + '…')
  })
})
