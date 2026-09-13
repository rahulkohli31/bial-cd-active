import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  uuidv7,
  listConversations,
  listProjectConversations,
  getConversation,
  messagesFromProjection,
  createConversation as mod_createConversation,
  createConversationStore,
  deriveTitle,
} from '../conversationApi'
import { toStepItem } from '../turnStreamApi'
import { OUTCOME_COPY, outcomeSummary } from '../messageTypes'

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
  it('hydrates header + PROJECTION into the in-memory message shape', async () => {
    // `mode` is gone from the wire doc entirely — ConversationHeader lost the field, and the
    // reload projection no longer carries a per-item mode either. `kind` is the whole of what
    // a chat is now, fixed at creation. No assertion below reads `.mode`; that IS the proof.
    const fetchImpl = vi.fn(async () =>
      ok({
        conversation: { _id: 'c1', kind: 'build', title: 'App', context: { theme: 'bial' } },
        projection: [
          { type: 'user_text', seq: 0, text: 'hi', attachments: [] },
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

  it('★ carries the chat’s occupancy, and reads its absence as unmeasured (#194)', async () => {
    // The read is where the "this chat is getting long" line gets its number on a cold load, and
    // that number is the RAW prompt count the provider reported — the same one the send route
    // refuses on. Passed through untouched; the moment this client adjusts it, it is estimating.
    const measured = vi.fn(async () =>
      ok({ conversation: { _id: 'c1', kind: 'build' }, projection: [], contextTokens: 412_345 }),
    )
    expect((await getConversation('c1', deps(measured))).contextTokens).toBe(412_345)

    // `null` and a missing field mean the same thing — nobody has counted — and NEITHER may
    // become `0`, which would be the browser claiming the chat is empty.
    const explicitNull = vi.fn(async () =>
      ok({ conversation: { _id: 'c1', kind: 'build' }, projection: [], contextTokens: null }),
    )
    const absent = vi.fn(async () =>
      ok({ conversation: { _id: 'c1', kind: 'build' }, projection: [] }),
    )
    expect((await getConversation('c1', deps(explicitNull))).contextTokens).toBeNull()
    expect((await getConversation('c1', deps(absent))).contextTokens).toBeNull()
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
  it('maps a plan_options item to a card part carrying the NARROWED item — mode/reason do not ride along', () => {
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
  it('maps visible steps and the in-progress anchor; hidden (read) steps stay out', () => {
    const visible = { type: 'step', seq: 1, tool: 'write_file', label: 'Updated x', state: 'ok', hidden: false }
    expect(
      messagesFromProjection([
        visible,
        { type: 'step', seq: 2, tool: 'read_file', label: 'Read y', state: 'ok', hidden: true },
        { type: 'build_in_progress', seq: 3, sessionId: 's' },
      ]),
    ).toEqual([
      // The step part is now toStepItem(visible), not the raw stored item — the same
      // narrowing function turnStreamApi.ts's live path uses.
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

  it('drops a malformed plan_options item (no toolCallId) instead of rendering a dead card', () => {
    // The concrete "drop" case toPlanOptionsItem defines: a card without a toolCallId is
    // an unclickable ghost, so it's dropped rather than rendered — same as the live path
    // (turnStreamApi.ts's 'plan_options' case returns null for the same input, and its
    // caller pushes nothing for a null item).
    const malformed = { type: 'plan_options', seq: 5, mode: 'plan', state: 'pending', reason: null }
    expect(messagesFromProjection([malformed])).toEqual([])
  })

  it('drops a malformed step item the same way (parity with the live path)', () => {
    // toStepItem only returns null for a non-record value, which a RawProjectionItem
    // can't be — so this can't fire through messagesFromProjection today. Pinned anyway
    // for parity with the plan_options case above and with the live path's own guard.
    expect(toStepItem('not a record')).toBeNull()
  })
})

describe('messagesFromProjection — the loud fallback arm', () => {
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

  it('stays silent for a COMPLETED turn_terminal, which is KNOWN and deliberately draws nothing', () => {
    // ★ THE MUTANT'S TEST. Narrowing this silence to completed terminals did not remove it, and
    // the difference is the whole design. `_write_turn_terminal` writes one of these
    // rows for EVERY turn of BOTH kinds, unconditionally — so a `turn_terminal` arm that drew
    // whatever it was handed would stamp "Build finished." after every single exchange in every
    // chat, including a Plan conversation that never built anything. Make the arm render
    // unconditionally and this goes red; that is the guard.
    //
    // The distinction the fallback arm exists to draw rides along: "we decided this renders
    // nothing" and "we have never heard of this" are different facts, and only the second is a
    // bug — collapsing them would train everyone to ignore the report.
    const onUnknown = vi.fn()
    const messages = messagesFromProjection(
      [{ type: 'turn_terminal', seq: 3, turnId: 't1', terminal: 'completed', reason: null }],
      onUnknown,
    )

    expect(onUnknown).not.toHaveBeenCalled()
    expect(messages).toEqual([])
  })

  it('stays silent for a terminal word it does not recognise, without reporting it', () => {
    // The same degradation rule the banner mapping follows: a client behind its server says
    // LESS, never something invented. And it is still a known arm, so it is not a bug report —
    // the item reached a branch that decided about it.
    const onUnknown = vi.fn()
    const messages = messagesFromProjection(
      [{ type: 'turn_terminal', seq: 3, turnId: 't1', terminal: 'evaporated', reason: null }],
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

describe('the patch / delete round trips are gone, and create came back on purpose', () => {
  /**
   * A GUARD, NARROWED — not deleted, and not widened by accident.
   *
   * It used to cover three absences. `createConversation` IS BACK, and that was a decision
   * someone made on purpose, which is exactly what this block existed to force: an upload now
   * names the conversation it belongs to, so the row has to exist before the first file is sent,
   * a round trip earlier than the turn that used to create it. The guarantee that went with the
   * old ordering — a refused first message leaving no chat behind — is knowingly traded, and the
   * empty row it leaves is a tracked follow-up rather than a surprise.
   *
   * THE OTHER TWO STAY ABSENT, and for reasons nothing in this change touches. `patchConversation`
   * had no caller once a chat's title came from its first message. `deleteConversation` had
   * exactly one, the project rail's past-conversations list, and a later product decision deleted
   * the list: nothing points back to a chat, so nothing offers to delete one. The SERVER routes
   * are all untouched.
   */
  it('★ the module offers create and a read half — and still no patch or delete', async () => {
    const mod = await import('../conversationApi')
    expect(typeof mod.createConversation).toBe('function')
    expect('patchConversation' in mod).toBe(false)
    expect('deleteConversation' in mod).toBe(false)
    // THE STORE IS A READ STORE STILL. `createConversation` is called by the send path directly,
    // where the conversation id and its project are already in hand; putting it back on the store
    // would offer it to every holder of one, which is a wider surface than the change needs.
    const store = mod.createConversationStore('plan')
    expect('createConversation' in store).toBe(false)
    expect('deleteConversation' in store).toBe(false)
    // Paired with a liveness assertion: the READ half is still there, so the absences above are
    // real absences and not an empty module or an empty store object.
    expect(typeof mod.listProjectConversations).toBe('function')
    expect(typeof store.getConversation).toBe('function')
  })

  it('★ creates with NO title — the draft is not known a round trip early', async () => {
    // Stamping the refused text into a row nobody can delete would be worse than leaving it
    // unnamed, so the first message that actually lands titles the chat.
    const fetchImpl = vi.fn(async () => ok({ conversation: { _id: 'c1', kind: 'build', projectId: 'p1' } }))

    const header = await mod_createConversation(
      { id: 'c1', projectId: 'p1', kind: 'build' },
      deps(fetchImpl),
    )

    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/conversations')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ id: 'c1', projectId: 'p1', kind: 'build' })
    expect(header.id).toBe('c1')
  })

  it('throws the server sentence so the composer can show it and keep the message', async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: false,
      status: 404,
      json: async () => ({ error: { message: 'Project not found.' } }),
    }))
    await expect(
      mod_createConversation({ id: 'c1', projectId: 'p1', kind: 'build' }, deps(fetchImpl)),
    ).rejects.toThrow('Project not found.')
  })
})

// The client-minted conversation id IS the row's primary key (the create route builds
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
})

describe('deriveTitle', () => {
  it('truncates at 40 with ellipsis and trims', () => {
    expect(deriveTitle('  hello  ')).toBe('hello')
    expect(deriveTitle('y'.repeat(60))).toBe('y'.repeat(40) + '…')
  })
})

// One `messages` row can project SEVERAL items, and every one of them inherits that row's
// seq. Keyed `srv_{seq}_{kind}`, those collided. React states plainly that duplicate keys "may
// cause children to be duplicated and/or omitted", so this was latent message-list corruption
// rather than a console warning: a re-render could drop a bubble or paint one twice.
describe('messagesFromProjection — keys are unique per ITEM, not per row', () => {
  const keysOf = (projection) => messagesFromProjection(projection).map((m) => m.id)
  const unique = (keys) => new Set(keys).size === keys.length

  it('two assistant_text items in one row become ONE reply, under one key', () => {
    // The collision is answered by there being nothing to collide: consecutive assistant
    // content is now PARTS of one reply rather than separate messages. The source ordinal is
    // still in the key, which is what keeps it unique against everything around it.
    const messages = messagesFromProjection([
      { type: 'assistant_text', seq: 4, mode: 'write', text: 'first part' },
      { type: 'assistant_text', seq: 4, mode: 'write', text: 'second part' },
    ])
    expect(messages).toHaveLength(1)
    expect(messages[0].parts.map((p) => p.text)).toEqual(['first part', 'second part'])
    expect(unique(messages.map((m) => m.id))).toBe(true)
  })

  it('keys stay unique across kinds that can repeat within one row', () => {
    const keys = keysOf([
      { type: 'user_text', seq: 1, mode: 'ask', text: 'a', attachments: [] },
      { type: 'user_text', seq: 1, mode: 'ask', text: 'b', attachmentIds: [] },
      { type: 'step', seq: 2, tool: 'write_file', label: 'x', state: 'ok', hidden: false },
      { type: 'step', seq: 2, tool: 'write_file', label: 'y', state: 'ok', hidden: false },
      { type: 'build_in_progress', seq: 3, sessionId: 's' },
      { type: 'build_in_progress', seq: 3, sessionId: 's' },
    ])
    expect(keys).toHaveLength(5)
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
    // The ordinal counts SOURCE position, so a hidden step does not shift the keys after it —
    // which an output-array index would have done. The interposed user turn gives the trailing
    // text its own message, so `srv_3_u_2` (not `_2`) is the ordinal actually under test.
    const withHidden = [
      { type: 'step', seq: 1, tool: 'write_file', label: 'x', state: 'ok', hidden: false },
      { type: 'step', seq: 2, tool: 'read_file', label: 'y', state: 'ok', hidden: true },
      { type: 'user_text', seq: 3, mode: 'ask', text: 'and now?', attachmentIds: [] },
      { type: 'assistant_text', seq: 4, mode: 'write', text: 'done' },
    ]
    expect(keysOf(withHidden)).toEqual(['srv_1_s_0', 'srv_3_u_2', 'srv_4_a_3'])
  })
})

/**
 * ONE REPLY IS ONE MESSAGE — the regression guard for the copy control.
 *
 * A citizen asks once and is answered once. This path used to push a separate message per
 * projection item, so a reply of prose-and-steps came back from a reload as seven or fourteen
 * messages. Anything mounted per message multiplied with them: a real eight-turn transcript
 * offered 41 copy buttons, and none of them copied the reply that had been read — only the
 * fragment beside it. The live path never had this shape (`streamingParts` builds one ordered
 * part list per turn), so this was also a live-vs-reload divergence.
 */
describe('one reply is one message (the copy-control guard)', () => {
  it('prose and steps interleaved come back as ONE assistant message, in order', () => {
    const messages = messagesFromProjection([
      { type: 'user_text', seq: 0, mode: 'ask', text: 'build me a visitor log', attachmentIds: [] },
      { type: 'assistant_text', seq: 1, mode: 'write', text: "I'll start by looking around." },
      { type: 'step', seq: 2, tool: 'read_file', label: "Looked through the app's files", state: 'ok', hidden: false },
      { type: 'step', seq: 3, tool: 'read_file', label: 'Looked at the main page', state: 'ok', hidden: false },
      { type: 'assistant_text', seq: 4, mode: 'write', text: 'Now let me build the schema.' },
      { type: 'step', seq: 5, tool: 'write_file', label: 'Updated the page', state: 'ok', hidden: false },
    ])

    expect(messages.map((m) => m.role)).toEqual(['user', 'assistant'])
    // ORDER IS THE RENDER: the library groups ADJACENT tool parts, so this part list draws one
    // paragraph, a group of two steps, a second paragraph, then a group of one.
    expect(messages[1].parts.map((p) => p.type)).toEqual(['text', 'step', 'step', 'text', 'step'])
  })

  it('a new question starts a new reply', () => {
    const messages = messagesFromProjection([
      { type: 'user_text', seq: 0, mode: 'ask', text: 'first', attachmentIds: [] },
      { type: 'assistant_text', seq: 1, mode: 'write', text: 'answer one' },
      { type: 'user_text', seq: 2, mode: 'ask', text: 'second', attachmentIds: [] },
      { type: 'assistant_text', seq: 3, mode: 'write', text: 'answer two' },
    ])
    expect(messages.map((m) => m.role)).toEqual(['user', 'assistant', 'user', 'assistant'])
    expect(messages.filter((m) => m.role === 'assistant')).toHaveLength(2)
  })

  it('a banner ends the reply rather than joining it', () => {
    // The outcome bubble is its own message on the live path too, so it stays one here.
    const messages = messagesFromProjection([
      { type: 'assistant_text', seq: 1, mode: 'write', text: 'working on it' },
      { type: 'banner', seq: 2, mode: 'write', banner: 'completed', text: 'Done.', previewUrl: null, sessionId: 's1' },
      { type: 'assistant_text', seq: 3, mode: 'write', text: 'anything else?' },
    ])
    expect(messages).toHaveLength(3)
    expect(messages[1].parts.map((p) => p.type)).toEqual(['text', 'build'])
  })

  it('a hidden step does not split the reply around it', () => {
    // Dropping a step must not seal the group and open a second one — the adjacency the
    // library's grouping reads is the whole reason hidden steps are dropped, not positioned.
    const messages = messagesFromProjection([
      { type: 'step', seq: 1, tool: 'write_file', label: 'x', state: 'ok', hidden: false },
      { type: 'step', seq: 2, tool: 'read_file', label: 'plumbing', state: 'ok', hidden: true },
      { type: 'step', seq: 3, tool: 'write_file', label: 'y', state: 'ok', hidden: false },
    ])
    expect(messages).toHaveLength(1)
    expect(messages[0].parts).toHaveLength(2)
  })
})

describe('messagesFromProjection — a stopped turn still looks stopped after a reload', () => {
  // A stopped build used to say NOTHING once the page was refreshed. Live, the surface writes a
  // sentence the moment the turn ends; the durable `turn_terminal` row is the only record of that
  // ending (a turn writes no build-outcome part on purpose — that would render the same ending
  // twice) and it drew nothing at all. So a citizen coming back to a build they had stopped found
  // a transcript that simply trailed off.

  /** What a turn terminal looks like on the wire, in the shape the projection sends it. */
  const terminalItem = (terminal, reason) => ({
    type: 'turn_terminal',
    seq: 7,
    turnId: '01a05879-5345-73b6-b795-47767884ea4c',
    terminal,
    reason,
  })

  /** The sentence a RELOAD produces — read off the real projection mapping. */
  const reloaded = (terminal, reason) => {
    const messages = messagesFromProjection([terminalItem(terminal, reason)])
    if (messages.length === 0) return null
    expect(messages).toHaveLength(1)
    expect(messages[0].role).toBe('assistant')
    expect(messages[0].parts).toHaveLength(1)
    expect(messages[0].parts[0].type).toBe('text')
    return messages[0].parts[0].text
  }

  /**
   * The sentence the LIVE path produces, derived the way the live path derives it.
   *
   * This is `ConversationSurface`'s `announceTerminal` verbatim — `status: sink.terminal ===
   * 'completed' ? 'ended' : sink.terminal`, then the shared table. It is here so the assertions
   * below can compare the two DERIVATIONS rather than compare each of them to a string literal:
   * two tests that each pin their own copy of the expected sentence both stay green while the
   * paths drift apart — exactly the failure that let two independent emitters disagree: fixing
   * one of them only changed WHEN the wrong text appeared.
   */
  const live = (terminal, reason) =>
    outcomeSummary({ status: terminal === 'completed' ? 'ended' : terminal, reason })

  it('renders the stored stop, where it used to render nothing', () => {
    const messages = messagesFromProjection([terminalItem('stopped', 'stopped_by_user')])

    expect(messages).toHaveLength(1)
    expect(messages[0].parts[0].text).toBe('You stopped this build before it finished.')
    // …and the sentence is the citizen's, not the machine's: the token that used to be printed
    // at people is nowhere in it.
    expect(messages[0].parts[0].text).not.toMatch(/stopped_by_user/)
  })

  it('says the same thing after a reload as it said live, for every reason in the table', () => {
    // ★ THE EQUALITY, and it is asserted as an equality on purpose. The two paths reach the
    // sentence by different routes — live maps `sink.terminal` with a ternary, reload maps
    // `item.terminal` through `bannerStatus` — and this is the only assertion that goes red when
    // those two mappings stop agreeing. Driven off `OUTCOME_COPY` itself, so a reason added to
    // the table tomorrow is covered by this test the day it lands.
    const reasons = [...Object.keys(OUTCOME_COPY), null, 'a_reason_no_client_has_heard_of']
    expect(reasons.length).toBeGreaterThan(5)

    for (const reason of reasons) {
      for (const terminal of ['failed', 'stopped']) {
        expect([terminal, reason, reloaded(terminal, reason)]).toEqual([
          terminal,
          reason,
          live(terminal, reason),
        ])
      }
    }
  })

  it('reads a quota terminal as the stop it is, exactly as the live path would', () => {
    // `quota` exists in the stored vocabulary but never on the live sink, so it is pinned here
    // rather than in the loop above: both mappings land it on `stopped`, which is what makes the
    // sentence match a stop rather than a failure.
    expect(reloaded('quota', 'quota_exceeded')).toBe(live('stopped', 'quota_exceeded'))
    expect(reloaded('quota', 'quota_exceeded')).toBe('The build stopped: you reached your daily limit.')
  })

  it('discriminates: the table is doing work, not returning one sentence for everything', () => {
    // The anti-false-pass guard for the equality above. If `outcomeSummary` collapsed to a single
    // string, every assertion in this file would pass and say nothing — so the distinctness of
    // what the reload renders is asserted directly.
    const rendered = Object.keys(OUTCOME_COPY).map((reason) => reloaded('stopped', reason))
    // ONE pair shares a sentence on purpose: `request_limit` and `wall_clock_deadline_exceeded`
    // are two internal bounds the citizen cannot act on differently (2026-09-11). So the
    // distinct count is the key count less exactly that pair — anything less is a collapse.
    expect(reloaded('stopped', 'request_limit')).toBe(
      reloaded('stopped', 'wall_clock_deadline_exceeded'),
    )
    expect(new Set(rendered).size).toBe(rendered.length - 1)
    expect(rendered.every((text) => typeof text === 'string' && text.length > 0)).toBe(true)
  })

  it('falls back to the neutral sentence rather than printing the machine token', () => {
    // Every `reason` on this wire is a machine token — `sandbox_unavailable`,
    // `self_heal_budget_exhausted` — and none of them is prose. An unlisted one gets the
    // neutral line for its terminal; interpolating it is the defect this replaced.
    // (`wall_clock_deadline_exceeded` used to be the example here; it has had its own sentence
    // since 2026-09-11, so the example is now a token the table still does not name.)
    const text = reloaded('failed', 'sandbox_unavailable')
    expect(text).toBe('The build failed.')
    expect(text).not.toMatch(/sandbox_unavailable/)
  })

  it('preserves the absence signal: a turn with no terminal row says nothing about how it ended', () => {
    // ENDED-UNKNOWN IS THE ABSENCE OF THE ITEM, by the server's design — a turn killed by a
    // restart never reaches the write, so there is no row and no `unknown` member to fake one.
    // The reload path must not invent an ending for a turn that never recorded one.
    const messages = messagesFromProjection([
      { type: 'user_text', seq: 1, text: 'add a chart' },
      { type: 'assistant_text', seq: 2, text: 'Working on it…' },
    ])

    // LIVENESS FIRST — the transcript still renders, so the absence below is an absence of the
    // outcome sentence and not of everything.
    expect(messages).toHaveLength(2)
    expect(messages[1].parts[0].text).toBe('Working on it…')
    const everything = messages.flatMap((m) => m.parts.map((p) => p.text ?? ''))
    for (const sentence of Object.values(OUTCOME_COPY)) {
      expect(everything).not.toContain(sentence)
    }
    expect(everything).not.toContain('The build failed.')
    expect(everything).not.toContain('This build was stopped before it finished.')
  })

  it('is its own message, so a reply is not swallowed into the outcome or vice versa', () => {
    // It seals the open reply the same way a banner does. Appended to the reply instead, the
    // outcome sentence would land inside the assistant bubble's activity group and the copy
    // control would hand back the platform's words as part of the model's answer.
    const messages = messagesFromProjection([
      { type: 'assistant_text', seq: 1, text: 'Starting.' },
      terminalItem('stopped', 'stopped_by_user'),
      { type: 'assistant_text', seq: 8, text: 'Anything else?' },
    ])

    expect(messages.map((m) => m.parts.map((p) => p.type))).toEqual([['text'], ['text'], ['text']])
    expect(messages.map((m) => m.parts[0].text)).toEqual([
      'Starting.',
      'You stopped this build before it finished.',
      'Anything else?',
    ])
    // Distinct keys, since one row can project several items and React silently corrupts a list
    // with duplicates.
    expect(new Set(messages.map((m) => m.id)).size).toBe(3)
  })
})


describe('attachment chips survive a reload', () => {
  const withAttachments = (attachments) =>
    messagesFromProjection([{ type: 'user_text', seq: 1, text: 'what is in this?', attachments }])

  it('rebuilds a file part per attachment, so the chip can name its file', () => {
    // THE DEFECT THIS CLOSES. The server has always sent the attachment identities on every
    // user_text item and this path used to build `parts: [{type:'text'}]` and nothing else, so
    // every chip vanished on refresh — for every format, not only images. The citizen lost
    // their only sight of the files still riding on every turn, and the per-conversation tally
    // taken over these messages silently reset to zero.
    //
    // Mutation receipt: drop `...fileParts(item)` from the parts array and this goes red.
    const [msg] = withAttachments([
      { attachmentId: 'att-1', kind: 'document', name: 'roster.pdf', mediaType: 'application/pdf' },
      { attachmentId: 'att-2', kind: 'image', name: 'gate.png', mediaType: 'image/png' },
    ])

    expect(msg.parts).toEqual([
      { type: 'file', kind: 'document', attachmentId: 'att-1', name: 'roster.pdf', mediaType: 'application/pdf' },
      { type: 'file', kind: 'image', attachmentId: 'att-2', name: 'gate.png', mediaType: 'image/png' },
      { type: 'text', text: 'what is in this?' },
    ])
  })

  it('puts the files BEFORE the prose, matching how the message was composed', () => {
    // `buildUserParts` pushes files then text, so a reloaded turn must too or the same message
    // renders in two different orders depending on whether the page has been refreshed.
    const [msg] = withAttachments([
      { attachmentId: 'att-1', kind: 'image', name: 'a.png', mediaType: 'image/png' },
    ])

    expect(msg.parts[0].type).toBe('file')
    expect(msg.parts[msg.parts.length - 1].type).toBe('text')
  })

  it('keeps a reclaimed attachment as a part so the chip can say it is unavailable', () => {
    // The row is deleted when its conversation is reclaimed, but the id lives in the message
    // payload forever, so the server sends the id with an empty name. Dropping it here would
    // hide from the citizen that a file was ever attached; emitting it lets the chip's own
    // fetch fail and render "attachment unavailable", which is the honest answer.
    const [msg] = withAttachments([{ attachmentId: 'att-gone', kind: '', name: '', mediaType: '' }])

    expect(msg.parts[0]).toEqual({
      type: 'file',
      kind: 'image',
      attachmentId: 'att-gone',
      name: '',
      mediaType: '',
    })
  })

  it('draws nothing for an entry with no id, rather than a chip that cannot be fetched', () => {
    const [msg] = withAttachments([{ kind: 'image', name: 'orphan.png', mediaType: 'image/png' }])

    expect(msg.parts).toEqual([{ type: 'text', text: 'what is in this?' }])
  })

  it('a turn with no attachments is unchanged', () => {
    const [msg] = withAttachments(undefined)

    expect(msg.parts).toEqual([{ type: 'text', text: 'what is in this?' }])
  })
})
