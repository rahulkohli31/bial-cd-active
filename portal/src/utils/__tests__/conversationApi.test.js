import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  uuidv7,
  listConversations,
  listProjectConversations,
  getConversation,
  createConversation,
  messagesFromProjection,
  patchConversation,
  createConversationStore,
  deriveTitle,
} from '../conversationApi'
import { toStepItem } from '../turnStreamApi'

// authFetch deps injection — no real token/network.
const deps = (fetchImpl) => ({ fetchImpl, getToken: () => 'tok', refresh: vi.fn() })
const ok = (json) => ({ ok: true, status: 200, json: async () => json })

describe('listConversations', () => {
  it('GETs ?kind= and normalizes _id → id', async () => {
    const fetchImpl = vi.fn(async () => ok({ conversations: [{ _id: 'c1', kind: 'plan', title: 'T', updatedAt: '2026-06-20T00:00:00Z' }] }))
    const list = await listConversations('plan', deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/conversations?kind=plan')
    expect(list).toEqual([{ id: 'c1', kind: 'plan', title: 'T', createdAt: undefined, updatedAt: '2026-06-20T00:00:00Z' }])
  })
  it('throws the server message on failure', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 500, json: async () => ({ error: { message: 'boom' } }) }))
    await expect(listConversations('plan', deps(fetchImpl))).rejects.toThrow('boom')
  })
})

describe('listProjectConversations', () => {
  it('GETs ?projectId= and returns both kinds', async () => {
    const fetchImpl = vi.fn(async () =>
      ok({
        conversations: [
          { _id: 'c1', kind: 'plan', projectId: 'p1', title: 'Plan' },
          { _id: 'c2', kind: 'build', projectId: 'p1', title: 'Build' },
        ],
      }),
    )
    const list = await listProjectConversations('p1', deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/conversations?projectId=p1')
    expect(list.map((c) => c.kind)).toEqual(['plan', 'build'])
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
    // `mode` is gone from the wire doc entirely — ConversationHeader lost the field, and the
    // reload projection no longer carries a per-item mode either. `kind` is the whole of what
    // a chat is now, fixed at creation. No assertion below reads `.mode`; that IS the proof.
    const fetchImpl = vi.fn(async () =>
      ok({
        conversation: { _id: 'c1', kind: 'build', title: 'App', context: { theme: 'bial' } },
        projection: [
          { type: 'user_text', seq: 0, text: 'hi', attachmentIds: [] },
          { type: 'assistant_text', seq: 1, text: 'hello!' },
        ],
        activeTurn: null,
      }),
    )
    const conv = await getConversation('c1', deps(fetchImpl))
    expect(conv.id).toBe('c1')
    expect(conv.kind).toBe('build')
    expect(conv.context).toEqual({ theme: 'bial' })
    expect(conv.activeTurn).toBeNull()
    expect(conv.messages).toEqual([
      { id: 'srv_0_u_0', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 0 },
      { id: 'srv_1_a_1', role: 'assistant', parts: [{ type: 'text', text: 'hello!' }], seq: 1 },
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
    const fetchImpl = vi.fn(async () => ok({ conversation: { _id: 'c1', kind: 'build', projectId: 'p1' }, projection: [] }))
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
        id: 'srv_3_b_0',
        role: 'assistant',
        parts: [
          { type: 'text', text: 'Build finished.' },
          { type: 'build', sessionId: 's1', status: 'ended', reason: 'completed', previewUrl: 'https://x' },
        ],
        seq: 3,
      },
    ])
  })
  it('maps a plan_options item to a card part carrying the NARROWED item (U13) — mode/reason do not ride along', () => {
    // The stored item is fed through toPlanOptionsItem, same as the live path (turnStreamApi.ts)
    // — not forwarded verbatim. `mode` and `reason` are given here as an old stored row could
    // still carry them, and neither reaches the rendered part: PlanOptionsItem dropped `reason`
    // along with the `build_failed` state that used to need it.
    const stored = { type: 'plan_options', seq: 3, mode: 'plan', toolCallId: 't', state: 'pending', reason: null }
    expect(messagesFromProjection([stored])).toEqual([
      {
        id: 'srv_3_p_0',
        role: 'assistant',
        parts: [{ type: 'plan_options', item: { type: 'plan_options', seq: 3, toolCallId: 't', state: 'pending' } }],
        seq: 3,
      },
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
      // The step part is now toStepItem(visible), not the raw stored item — the same
      // narrowing function turnStreamApi.ts's live path uses (PR #93 review finding 9).
      // It used to default-fill two fields the stored row never had, `mode` and
      // `detail: {args: null, result: null}`; StepItem carries neither any more, so a
      // reloaded step is exactly the six fields below and nothing is synthesized.
      {
        id: 'srv_1_s_0',
        role: 'assistant',
        parts: [{ type: 'step', step: { ...visible } }],
        seq: 1,
      },
      // …index 2, not 1: the ordinal counts SOURCE position, so skipping the hidden step at
      // index 1 does not renumber everything after it.
      { id: 'srv_3_g_2', role: 'assistant', parts: [{ type: 'build_in_progress', sessionId: 's' }], seq: 3 },
    ])
  })

  it('drops a malformed plan_options item (no toolCallId) instead of rendering a dead card (PR #93 review finding 9)', () => {
    // The concrete "drop" case toPlanOptionsItem defines: a card without a toolCallId is
    // an unclickable ghost, so it's dropped rather than rendered — same as the live path
    // (turnStreamApi.ts's 'plan_options' case returns null for the same input, and its
    // caller pushes nothing for a null item).
    const malformed = { type: 'plan_options', seq: 5, mode: 'plan', state: 'pending', reason: null }
    expect(messagesFromProjection([malformed])).toEqual([])
  })

  it('drops a malformed step item the same way (parity with the live path, PR #93 review finding 9)', () => {
    // toStepItem only returns null for a non-record value, which a RawProjectionItem
    // can't be — so this can't fire through messagesFromProjection today. Pinned anyway
    // for parity with the plan_options case above and with the live path's own guard.
    expect(toStepItem('not a record')).toBeNull()
  })
})

describe('messagesFromProjection — the loud fallback arm (Plan D U4, L4)', () => {
  // Until this arm existed the if/else-if chain simply ENDED, so a projection item type this
  // client did not recognise vanished with no error, no warning and no trace — on the one path a
  // reloaded transcript is rebuilt from, for both kinds of chat. That is the four-edit change no
  // compiler enforces, and this is the edit that makes the fourth one impossible to forget.

  it('surfaces an unknown item type instead of swallowing it', () => {
    const onUnknown = vi.fn()
    const messages = messagesFromProjection(
      [
        { type: 'user_text', seq: 1, text: 'hello' },
        { type: 'something_the_server_added_last_week', seq: 2, payload: { a: 1 } },
      ],
      onUnknown,
    )

    expect(onUnknown).toHaveBeenCalledTimes(1)
    // The SHAPE is asserted, not just the count: whoever reads this report needs the type name
    // and the seq to find the item, and a bare "something was dropped" is not actionable.
    expect(onUnknown.mock.calls[0][0]).toMatchObject({
      type: 'something_the_server_added_last_week',
      seq: 2,
    })
    // Liveness, and the deliberate non-behaviour: the rest of the transcript still renders. A
    // throw here would take a whole conversation down because the server shipped one new item
    // kind ahead of the browser, which is a routine deployment order.
    expect(messages).toHaveLength(1)
    expect(messages[0].parts[0]).toEqual({ type: 'text', text: 'hello' })
  })

  it('stays silent for turn_terminal, which is KNOWN and deliberately draws nothing', () => {
    // The distinction the arm exists to draw. "We decided this renders nothing" and "we have
    // never heard of this" are different facts, and only the second is a bug — collapsing them
    // would train everyone to ignore the report.
    const onUnknown = vi.fn()
    const messages = messagesFromProjection(
      [{ type: 'turn_terminal', seq: 3, status: 'completed' }],
      onUnknown,
    )

    expect(onUnknown).not.toHaveBeenCalled()
    expect(messages).toEqual([])
  })

  it('never reports an item it rendered', () => {
    const onUnknown = vi.fn()
    messagesFromProjection(
      [
        { type: 'user_text', seq: 1, text: 'hi' },
        { type: 'assistant_text', seq: 2, text: 'hello' },
        { type: 'step', seq: 3, tool: 'bash', label: 'Read the file', state: 'ok', hidden: false },
        { type: 'build_in_progress', seq: 4, sessionId: 's1' },
      ],
      onUnknown,
    )

    expect(onUnknown).not.toHaveBeenCalled()
  })

  it('defaults to a console report when no handler is injected', () => {
    // The default matters: production has no handler, and the whole point is that the drop is
    // visible to a developer rather than silent.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      messagesFromProjection([{ type: 'brand_new_kind', seq: 9 }])
      expect(spy).toHaveBeenCalledTimes(1)
      expect(String(spy.mock.calls[0][0])).toMatch(/brand_new_kind/)
    } finally {
      spy.mockRestore()
    }
  })
})

describe('createConversation / patchConversation / deleteConversation', () => {
  it('POSTs {id, projectId, kind} (+title/context when given) to the conversations route', async () => {
    // `mode` is gone from CreateConversationArgs and from ConversationHeader — proven from BOTH
    // ends here, not merely by asserting the new shape: a stray `mode` on the CALL never reaches
    // the wire body (createConversation destructures only the named fields it still has), and a
    // `mode` on the SERVER doc never survives normalizeHeader into the returned header.
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 201, json: async () => ({ conversation: { _id: 'c1', kind: 'plan', projectId: 'p1', mode: 'plan' } }) }))
    const header = await createConversation('c1', { projectId: 'p1', kind: 'plan', title: 'T', mode: 'plan' }, deps(fetchImpl))
    const [url, opts] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/conversations')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ id: 'c1', projectId: 'p1', kind: 'plan', title: 'T' })
    expect(header).toMatchObject({ id: 'c1', kind: 'plan', projectId: 'p1' })
    expect(header).not.toHaveProperty('mode')
  })
  it('createConversation rejects on failure (no silent drop)', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ error: { message: 'id already in use' } }) }))
    await expect(createConversation('c1', { projectId: 'p1', kind: 'plan' }, deps(fetchImpl))).rejects.toThrow('id already in use')
  })
  it('patchConversation PATCHes the body', async () => {
    const patchFetch = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) }))
    await patchConversation('c1', { title: 'new' }, deps(patchFetch))
    expect(patchFetch.mock.calls[0][1].method).toBe('PATCH')
  })

  it('★ offers no delete at all — the module and the store both', async () => {
    // `deleteConversation` had exactly one caller, the project rail's past-conversations list, and
    // the ruling of 2026-09-02 deleted the list: nothing points back to a chat, so nothing offers
    // to delete one. The SERVER route is untouched. Asserted rather than left silent so that
    // re-adding a client has to be a decision someone makes on purpose.
    const mod = await import('../conversationApi')
    expect('deleteConversation' in mod).toBe(false)
    expect('deleteConversation' in mod.createConversationStore('plan')).toBe(false)
  })
})

// ADR-0006: the client-minted conversation id IS the row's primary key (the create route builds
// `Conversation(id=body.id, …)`, overriding the server's UUIDv7 default), so minting a v4 here
// scatters inserts across the btree. `crypto.randomUUID()` mints v4 and is not a substitute.
describe('uuidv7', () => {
  afterEach(() => vi.restoreAllMocks())

  it('mints a canonical lowercase v7 with RFC-4122 variant bits', () => {
    const id = uuidv7()
    expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)
    expect(id[14]).toBe('7') // version nibble
    expect('89ab').toContain(id[19]) // variant nibble → 10xx
  })

  it('mints a distinct id within the same millisecond', () => {
    vi.spyOn(Date, 'now').mockReturnValue(1_785_000_000_000)
    expect(uuidv7()).not.toBe(uuidv7()) // the 74 non-timestamp bits are random
  })

  it('THE POINT OF v7: ids minted later sort lexicographically after earlier ones', () => {
    // A version-nibble check alone passes on a LITTLE-endian timestamp too, and that layout
    // destroys the only property v7 exists for. So pick a base whose low byte is 0xff: one
    // millisecond later it wraps, and under little-endian the wrapped byte LEADS the string,
    // sorting `t+1` before `t`. Big-endian keeps them in mint order.
    const base = 1_785_000_000_000 - (1_785_000_000_000 % 256) + 255
    const now = vi.spyOn(Date, 'now')
    const mintedInOrder = [base, base + 1, base + 1_000, base + 1_000_000].map((ms) => {
      now.mockReturnValue(ms)
      return uuidv7()
    })
    expect([...mintedInOrder].sort()).toEqual(mintedInOrder)
  })
})

describe('createConversationStore', () => {
  it('newConversation mints a client UUIDv7 synchronously (no network)', () => {
    const store = createConversationStore('plan')
    const a = store.newConversation()
    expect(a).toMatch(/^[0-9a-f-]{36}$/i)
    // The wiring, asserted where the decision is made: the store's mint IS `uuidv7`, so a
    // `crypto.randomUUID()` regression here shows up as a `4` in the version nibble.
    expect(a[14]).toBe('7')
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

// N3 — one `messages` row can project SEVERAL items, and every one of them inherits that row's
// seq. Keyed `srv_{seq}_{kind}`, those collided. React states plainly that duplicate keys "may
// cause children to be duplicated and/or omitted", so this was latent message-list corruption
// rather than a console warning: a re-render could drop a bubble or paint one twice.
describe('messagesFromProjection — keys are unique per ITEM, not per row (N3)', () => {
  const keysOf = (projection) => messagesFromProjection(projection).map((m) => m.id)
  const unique = (keys) => new Set(keys).size === keys.length

  it('THE BUG: one row projecting two assistant_text items yields two DISTINCT keys', () => {
    const keys = keysOf([
      { type: 'assistant_text', seq: 4, mode: 'write', text: 'first part' },
      { type: 'assistant_text', seq: 4, mode: 'write', text: 'second part' },
    ])
    expect(keys).toHaveLength(2)
    expect(unique(keys)).toBe(true)
  })

  it('holds for every kind that can repeat within one row', () => {
    // The same collision shape for _u / _s / _b / _p / _g, since any of them can be emitted
    // more than once for a single stored row.
    const keys = keysOf([
      { type: 'user_text', seq: 1, mode: 'ask', text: 'a', attachmentIds: [] },
      { type: 'user_text', seq: 1, mode: 'ask', text: 'b', attachmentIds: [] },
      { type: 'step', seq: 2, tool: 'write_file', label: 'x', state: 'ok', hidden: false },
      { type: 'step', seq: 2, tool: 'write_file', label: 'y', state: 'ok', hidden: false },
      { type: 'build_in_progress', seq: 3, sessionId: 's' },
      { type: 'build_in_progress', seq: 3, sessionId: 's' },
    ])
    expect(keys).toHaveLength(6)
    expect(unique(keys)).toBe(true)
  })

  it('every key across a mixed transcript is unique', () => {
    const keys = keysOf([
      { type: 'user_text', seq: 0, mode: 'plan', text: 'build me a thing', attachmentIds: [] },
      { type: 'assistant_text', seq: 1, mode: 'plan', text: 'here is the plan' },
      { type: 'plan_options', seq: 1, mode: 'plan', toolCallId: 't1', state: 'build', reason: null },
      { type: 'step', seq: 2, tool: 'write_file', label: 'Updated the page', state: 'ok', hidden: false },
      { type: 'banner', seq: 2, mode: 'write', banner: 'completed', text: 'Done.', previewUrl: null, sessionId: 's1' },
    ])
    expect(unique(keys)).toBe(true)
  })

  it('keys are STABLE across repeated projections of the same transcript', () => {
    // A key that moved between renders would remount the bubble and lose its DOM state — the
    // cure being worse than the collision.
    const projection = [
      { type: 'user_text', seq: 0, mode: 'ask', text: 'hi', attachmentIds: [] },
      { type: 'assistant_text', seq: 1, mode: 'ask', text: 'hello' },
    ]
    expect(keysOf(projection)).toEqual(keysOf(projection))
  })

  it('appending a turn does not renumber the keys already on screen', () => {
    const base = [
      { type: 'user_text', seq: 0, mode: 'ask', text: 'hi', attachmentIds: [] },
      { type: 'assistant_text', seq: 1, mode: 'ask', text: 'hello' },
    ]
    const grown = [...base, { type: 'user_text', seq: 2, mode: 'ask', text: 'more', attachmentIds: [] }]
    expect(keysOf(grown).slice(0, 2)).toEqual(keysOf(base))
  })

  it('a hidden step does not renumber the items after it', () => {
    // The ordinal counts SOURCE position precisely so that flipping a step's `hidden` cannot
    // shift every later key — which an output-array index would have done.
    const withHidden = [
      { type: 'step', seq: 1, tool: 'write_file', label: 'x', state: 'ok', hidden: false },
      { type: 'step', seq: 2, tool: 'read_file', label: 'y', state: 'ok', hidden: true },
      { type: 'assistant_text', seq: 3, mode: 'write', text: 'done' },
    ]
    expect(keysOf(withHidden)).toEqual(['srv_1_s_0', 'srv_3_a_2'])
  })
})
