/**
 * The U10 stream reader's transport disciplines: carry-buffered reassembly of torn
 * frames, distinct outcomes (completed / truncated / stalled / aborted), and the
 * cross-repo keepalive⁄stall inequality pin.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  TURN_STREAM_STALL_TIMEOUT_MS,
  TurnStartError,
  buildFromPlan,
  isKnownFrame,
  parseSseText,
  readTurnStream,
  resolvePlanOptions,
  startTurn,
  stopTurn,
  switchMode,
  type TurnFrame,
} from '../turnStreamApi'

const SNAPSHOT =
  '{"type":"snapshot","seq":3,"turnId":"t1","turnStatus":"running","items":[],"textSoFar":"hi ","steps":[]}'
const DELTA = '{"type":"text_delta","seq":4,"text":"there"}'
const ENDED = '{"type":"turn_ended","seq":5,"turnId":"t1","status":"completed"}'

describe('parseSseText', () => {
  it('parses complete frames and keeps the unterminated tail as carry', () => {
    const buffer = `id: 3\ndata: ${SNAPSHOT}\n\nid: 4\ndata: ${DELTA.slice(0, 10)}`
    const { frames, rest, sawDone } = parseSseText(buffer)
    expect(frames).toHaveLength(1)
    expect(frames[0].type).toBe('snapshot')
    expect(rest).toBe(`id: 4\ndata: ${DELTA.slice(0, 10)}`)
    expect(sawDone).toBe(false)
  })

  it('reassembles a frame torn across chunks via the carry', () => {
    const first = parseSseText(`id: 4\ndata: ${DELTA.slice(0, 12)}`)
    expect(first.frames).toHaveLength(0)
    const second = parseSseText(first.rest + DELTA.slice(12) + '\n\n')
    expect(second.frames).toHaveLength(1)
    expect(second.frames[0]).toMatchObject({ type: 'text_delta', text: 'there' })
  })

  it('skips ping comments, detects [DONE], and passes unknown frame types through', () => {
    const buffer = `: ping\n\ndata: {"type":"future_frame","seq":9}\n\ndata: [DONE]\n\n`
    const { frames, sawDone } = parseSseText(buffer)
    expect(frames).toHaveLength(1)
    expect(frames[0].type).toBe('future_frame')
    expect(sawDone).toBe(true)
  })

  it('throws on malformed JSON in a complete data line (never silently dropped)', () => {
    expect(() => parseSseText('data: {"type":"text_delta",\n\n')).toThrow()
  })
})

describe('the known-frame narrowing (a cast is not a parse)', () => {
  const parseOne = (json: string) => parseSseText(`data: ${json}\n\n`).frames

  it('drops a step frame with no item — a step frame IS its item', () => {
    // The blanket cast promised `item: StepItem` and handed consumers `undefined`, which
    // throws at render INSIDE the stream reader and reads to the user as a dropped socket.
    expect(parseOne('{"type":"step","seq":2,"toolCallId":"t1","phase":"started"}')).toEqual([])
  })

  it('drops a plan_options frame whose item is empty — an unclickable card is worse than none', () => {
    // The resolve endpoint is addressed BY toolCallId; without one the card's buttons are dead.
    expect(parseOne('{"type":"plan_options","seq":3,"item":{}}')).toEqual([])
    expect(parseOne('{"type":"plan_options","seq":3}')).toEqual([])
  })

  it('narrows a well-formed step frame and carries `hidden` through', () => {
    const [frame] = parseOne(
      '{"type":"step","seq":2,"toolCallId":"t1","phase":"finished","item":' +
        '{"type":"step","seq":2,"mode":"ask","tool":"read_file","label":"Read app/page.tsx",' +
        '"state":"ok","hidden":true,"detail":{"args":"{}","result":"ok"}}}',
    )
    expect(frame).toMatchObject({
      type: 'step',
      phase: 'finished',
      item: { tool: 'read_file', state: 'ok', hidden: true, detail: { result: 'ok' } },
    })
  })

  it('fails SAFE on unrecognized enum values rather than passing them through', () => {
    const [step] = parseOne(
      '{"type":"step","seq":1,"toolCallId":"t1","phase":"nonsense","item":' +
        '{"type":"step","seq":1,"tool":"x","label":"y","state":"nonsense"}}',
    )
    expect(step).toMatchObject({ phase: 'started', item: { state: 'pending', hidden: false } })
    // A terminal with an unreadable status is still a terminal — never lost, read as failed.
    const [ended] = parseOne('{"type":"turn_ended","seq":9,"turnId":"t","status":"nonsense"}')
    expect(ended).toMatchObject({ type: 'turn_ended', status: 'failed' })
    // An unreadable snapshot status reads as idle, and its bad steps are dropped, not spread.
    const [snap] = parseOne('{"type":"snapshot","seq":1,"turnStatus":"nonsense","steps":["x"]}')
    expect(snap).toMatchObject({ type: 'snapshot', turnStatus: 'idle', steps: [], textSoFar: '' })
  })
})

function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return new Response(body, { status: 200 })
}

describe('the compile frame (R17/R18) — an absent signal is never good news', () => {
  const parseOne = (json: string) => parseSseText(`data: ${json}\n\n`).frames

  it('parses each of the four states', () => {
    for (const state of ['building', 'clean', 'failed', 'unknown']) {
      expect(parseOne(`{"type":"compile","seq":1,"state":"${state}"}`)).toEqual([
        { type: 'compile', seq: 1, state },
      ])
    }
  })

  it('narrows a state string it does not recognise to unknown, never to clean', () => {
    // The container and this bundle ship separately and can be a release apart in either
    // direction. A value one of them has not heard of is a real state — and the pane HOLDS
    // its cover on `unknown`, so this is the difference between covering a red screen and
    // uncovering over one.
    expect(parseOne('{"type":"compile","seq":1,"state":"recompiling"}')).toEqual([
      { type: 'compile', seq: 1, state: 'unknown' },
    ])
    expect(parseOne('{"type":"compile","seq":1}')).toEqual([
      { type: 'compile', seq: 1, state: 'unknown' },
    ])
  })

  it('is a KNOWN frame, so it is not swallowed by the forward-compat escape hatch', () => {
    // The both-places trap: a type listed in the union but missing from KNOWN_FRAME_TYPES
    // still parses — as an unknown frame — and the consumer's `isKnownFrame` guard drops it
    // silently. Mirrors the server's own `_KNOWN_FRAME_TAGS` assertion.
    const [frame] = parseOne('{"type":"compile","seq":1,"state":"failed"}')
    expect(isKnownFrame(frame)).toBe(true)
  })
})

describe('readTurnStream', () => {
  it('delivers frames split across chunks and resolves completed on [DONE]', async () => {
    const whole = `id: 3\ndata: ${SNAPSHOT}\n\nid: 4\ndata: ${DELTA}\n\nid: 5\ndata: ${ENDED}\n\ndata: [DONE]\n\n`
    const cut = 40 // tears the snapshot frame mid-JSON
    const fetchFn = vi.fn(async () => streamResponse([whole.slice(0, cut), whole.slice(cut)]))
    const seen: TurnFrame[] = []
    const outcome = await readTurnStream({
      conversationId: 'c1',
      signal: new AbortController().signal,
      onFrame: (frame) => seen.push(frame),
      deps: { fetchImpl: fetchFn },
    })
    expect(outcome).toBe('completed')
    expect(seen.map((frame) => frame.type)).toEqual(['snapshot', 'text_delta', 'turn_ended'])
  })

  it('resolves truncated when the socket closes without [DONE]', async () => {
    const fetchFn = vi.fn(async () => streamResponse([`id: 3\ndata: ${SNAPSHOT}\n\n`]))
    const outcome = await readTurnStream({
      conversationId: 'c1',
      signal: new AbortController().signal,
      onFrame: () => undefined,
      deps: { fetchImpl: fetchFn },
    })
    expect(outcome).toBe('truncated')
  })

  it('passes cursor + turn as query params for a gap-free resume', async () => {
    const fetchFn = vi.fn(async () => streamResponse(['data: [DONE]\n\n']))
    await readTurnStream({
      conversationId: 'c1',
      cursor: 7,
      turnId: 't1',
      signal: new AbortController().signal,
      onFrame: () => undefined,
      deps: { fetchImpl: fetchFn },
    })
    const url = (fetchFn.mock.calls[0] as unknown[])[0] as string
    expect(url).toContain('cursor=7')
    expect(url).toContain('turn=t1')
  })

  it('resolves stalled when no bytes arrive within the stall window', async () => {
    const body = new ReadableStream<Uint8Array>({ start() {} }) // never produces
    const fetchFn = vi.fn(async () => new Response(body, { status: 200 }))
    const outcome = await readTurnStream({
      conversationId: 'c1',
      signal: new AbortController().signal,
      onFrame: () => undefined,
      deps: { fetchImpl: fetchFn },
      stallTimeoutMs: 20,
    })
    expect(outcome).toBe('stalled')
  })

  it('resolves stalled when the REQUEST itself never answers (#137)', async () => {
    // THE HUNG-SUBSCRIBE HOLE. The watchdog used to guard only `reader.read()` — i.e. only
    // after response HEADERS arrived. A server that accepted the connection and then never
    // answered left this promise pending FOREVER, and `BuilderPage`'s `endGenerating` sits
    // after the await, so `generatingChatId` was never cleared: the composer kept animating
    // "Setting up your sandbox… running Nm Ns" with a live Stop button (and a disabled mode
    // toggle) on a turn the server had already failed in under a second.
    const fetchFn = vi.fn(() => new Promise<Response>(() => {})) // accepted, never answers
    const outcome = await readTurnStream({
      conversationId: 'c1',
      signal: new AbortController().signal,
      onFrame: () => undefined,
      deps: { fetchImpl: fetchFn },
      stallTimeoutMs: 20,
    })
    expect(outcome).toBe('stalled')
  })

  it('resolves aborted when the caller aborts during the REQUEST (#137)', async () => {
    // The abort arm of the same window: a navigation away mid-subscribe must settle the
    // promise, not leave the page pinned to a request that will never answer.
    const controller = new AbortController()
    const fetchFn = vi.fn(() => new Promise<Response>(() => {}))
    const pending = readTurnStream({
      conversationId: 'c1',
      signal: controller.signal,
      onFrame: () => undefined,
      deps: { fetchImpl: fetchFn },
      stallTimeoutMs: 5_000,
    })
    controller.abort()
    await expect(pending).resolves.toBe('aborted')
  })

  it('resolves aborted when the caller aborts', async () => {
    const controller = new AbortController()
    const body = new ReadableStream<Uint8Array>({ start() {} })
    const fetchFn = vi.fn(async () => new Response(body, { status: 200 }))
    const pending = readTurnStream({
      conversationId: 'c1',
      signal: controller.signal,
      onFrame: () => undefined,
      deps: { fetchImpl: fetchFn },
      stallTimeoutMs: 5_000,
    })
    controller.abort()
    // The abort surfaces through the raced reader.read() rejection.
    await expect(pending).resolves.toBe('aborted')
  })
})

describe('the cross-repo timing contract', () => {
  it('pins the stall window to 4x the server keepalive (15s)', () => {
    // Server side: `turns.py KEEPALIVE_SECONDS = 15.0`, pinned by
    // `test_turn_stream.py::test_keepalive_budget_stays_pinned_under_the_client_stall_window`.
    expect(TURN_STREAM_STALL_TIMEOUT_MS).toBe(60_000)
    expect(TURN_STREAM_STALL_TIMEOUT_MS).toBe(4 * 15_000)
  })
})

describe('startTurn', () => {
  it('posts the message shape and returns the turn id', async () => {
    const fetchFn = vi.fn(async () =>
      new Response(JSON.stringify({ turnId: 't9' }), { status: 202 })
    )
    const result = await startTurn(
      'c1',
      { text: 'hello' },
      { fetchImpl: fetchFn }
    )
    expect(result.turnId).toBe('t9')
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit]
    // F2: the edge rewrite is `^/api → /v1`, so the client base must be `/api/...` (un-prefixed).
    // A `/api/v1/...` base doubled to `/v1/v1/...` → 404 for every turn call (the P0 this pins).
    expect(url).toBe('/api/conversations/c1/turns')
    expect(JSON.parse(init.body as string)).toEqual({
      message: { text: 'hello', attachmentTexts: [], attachmentIds: [] },
    })
  })

  it('maps an error envelope to a typed TurnStartError', async () => {
    const fetchFn = vi.fn(async () =>
      new Response(JSON.stringify({ error: { message: 'A turn is already running.' } }), {
        status: 409,
      })
    )
    await expect(
      startTurn('c1', { text: 'hi' }, { fetchImpl: fetchFn })
    ).rejects.toMatchObject({ status: 409, message: 'A turn is already running.' })
    expect(new TurnStartError(409, 'x')).toBeInstanceOf(Error)
  })
})

// F2 REGRESSION GUARD. The edge/nginx rewrite is `^/api → /v1`. If ANY of the six turn-transport
// call sites keeps a `/api/v1/...` base it doubles to `/v1/v1/...` → 404 for every turn / mode /
// build / events call — the P0 that broke the whole unified-chat flow. Pin every call site to the
// un-prefixed `/api/conversations/...` base so the doubling can never come back silently.
describe('base-path contract (F2 regression guard) — every call hits /api/conversations, never /api/v1', () => {
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
  const urlOf = (fetchFn: ReturnType<typeof vi.fn>) => (fetchFn.mock.calls[0] as unknown[])[0] as string
  const expectUnPrefixed = (url: string) => {
    expect(url.startsWith('/api/conversations/')).toBe(true)
    expect(url).not.toContain('/api/v1/')
  }

  it('startTurn → /api/conversations/{id}/turns', async () => {
    const fetchFn = vi.fn(async () => json({ turnId: 't1' }, 202))
    await startTurn('c1', { text: 'hi' }, { fetchImpl: fetchFn })
    expectUnPrefixed(urlOf(fetchFn))
  })

  it('stopTurn → /api/conversations/{id}/turns/{turnId}/stop', async () => {
    const fetchFn = vi.fn(async () => json({ status: 'stopping' }))
    await stopTurn('c1', 't1', { fetchImpl: fetchFn })
    expectUnPrefixed(urlOf(fetchFn))
  })

  it('switchMode → /api/conversations/{id}/mode', async () => {
    const fetchFn = vi.fn(async () => json({ mode: 'plan' }))
    await switchMode('c1', 'plan', { fetchImpl: fetchFn })
    expectUnPrefixed(urlOf(fetchFn))
  })

  it('buildFromPlan → /api/conversations/{id}/plan-options/{toolCallId}/build', async () => {
    const fetchFn = vi.fn(async () => json({ outcome: 'started' }))
    await buildFromPlan('c1', 'tc1', {}, { fetchImpl: fetchFn })
    expectUnPrefixed(urlOf(fetchFn))
  })

  it('resolvePlanOptions → /api/conversations/{id}/plan-options/{toolCallId}/resolve', async () => {
    const fetchFn = vi.fn(async () => json({ state: 'refine', alreadyResolved: false }))
    await resolvePlanOptions('c1', 'tc1', { fetchImpl: fetchFn })
    expectUnPrefixed(urlOf(fetchFn))
  })

  it('readTurnStream → /api/conversations/{id}/events', async () => {
    const fetchFn = vi.fn(async () => streamResponse(['data: [DONE]\n\n']))
    await readTurnStream({
      conversationId: 'c1',
      signal: new AbortController().signal,
      onFrame: () => undefined,
      deps: { fetchImpl: fetchFn },
    })
    expectUnPrefixed(urlOf(fetchFn))
  })
})

// F1 REGRESSION GUARD (turn transport). These five MUTATING calls ride the signed double-submit
// X-CSRF-Token, and every one of their routes enforces RequireCsrf server-side — so a dropped header
// would 403 every mode-switch / turn-start in prod (the P0 class) while the base-path guard above
// stays fully green. Since U1 the header comes from `authFetch` rather than a second copy in this
// module; the assertion is unchanged because the observable contract is. The read path
// (readTurnStream) is a safe GET and carries none.
describe('CSRF double-submit (F1 regression guard) — every MUTATING turn call rides X-CSRF-Token', () => {
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
  const headersOf = (fetchFn: ReturnType<typeof vi.fn>) =>
    ((fetchFn.mock.calls[0] as unknown[])[1] as RequestInit).headers as Record<string, string>

  beforeEach(() => {
    document.cookie = 'csrf=signed-turn-csrf'
  })
  afterEach(() => {
    document.cookie = 'csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
  })

  it('startTurn rides X-CSRF-Token', async () => {
    const fetchFn = vi.fn(async () => json({ turnId: 't1' }, 202))
    await startTurn('c1', { text: 'hi' }, { fetchImpl: fetchFn })
    expect(headersOf(fetchFn)['X-CSRF-Token']).toBe('signed-turn-csrf')
  })

  it('stopTurn rides X-CSRF-Token', async () => {
    const fetchFn = vi.fn(async () => json({ status: 'stopping' }))
    await stopTurn('c1', 't1', { fetchImpl: fetchFn })
    expect(headersOf(fetchFn)['X-CSRF-Token']).toBe('signed-turn-csrf')
  })

  it('switchMode rides X-CSRF-Token', async () => {
    const fetchFn = vi.fn(async () => json({ mode: 'plan' }))
    await switchMode('c1', 'plan', { fetchImpl: fetchFn })
    expect(headersOf(fetchFn)['X-CSRF-Token']).toBe('signed-turn-csrf')
  })

  it('buildFromPlan rides X-CSRF-Token', async () => {
    const fetchFn = vi.fn(async () => json({ outcome: 'started' }))
    await buildFromPlan('c1', 'tc1', {}, { fetchImpl: fetchFn })
    expect(headersOf(fetchFn)['X-CSRF-Token']).toBe('signed-turn-csrf')
  })

  it('resolvePlanOptions rides X-CSRF-Token', async () => {
    const fetchFn = vi.fn(async () => json({ state: 'refine', alreadyResolved: false }))
    await resolvePlanOptions('c1', 'tc1', { fetchImpl: fetchFn })
    expect(headersOf(fetchFn)['X-CSRF-Token']).toBe('signed-turn-csrf')
  })

  it('readTurnStream is a safe GET — carries NO X-CSRF-Token', async () => {
    const fetchFn = vi.fn(async () => streamResponse(['data: [DONE]\n\n']))
    await readTurnStream({
      conversationId: 'c1',
      signal: new AbortController().signal,
      onFrame: () => undefined,
      deps: { fetchImpl: fetchFn },
    })
    const init = (fetchFn.mock.calls[0] as unknown[])[1] as RequestInit | undefined
    expect((init?.headers as Record<string, string> | undefined)?.['X-CSRF-Token']).toBeUndefined()
  })
})

// N11 REGRESSION GUARD (U1). Before U1 this module called raw `fetch` at all six sites and had no
// 401 handling at all, so an expired JWT did not degrade the chat — it KILLED it: start, stop,
// mode-switch, Build-it, plan-resolve and the SSE reader every one died where the rest of the app
// quietly refreshed and retried. Routing them through `authFetch` is the whole fix, so pin it at
// every site: a single call site slipping back to raw `fetch` reintroduces the dead transport for
// exactly one action, which is precisely how this shipped unnoticed the first time.
describe('session expiry recovery (N11) — every turn call refreshes once and retries', () => {
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
  const unauthorized = () => new Response(JSON.stringify({ detail: 'Not authenticated' }), { status: 401 })

  /** The expired-session shape: the first attempt 401s, the post-refresh retry succeeds. */
  const expiredThen = (ok: () => Response) => {
    let attempt = 0
    return vi.fn(async () => (attempt++ === 0 ? unauthorized() : ok()))
  }

  /** Every call, reduced to `() => Promise<unknown>` so the two arms can share one table. */
  const CALLS: ReadonlyArray<{
    name: string
    ok: () => Response
    run: (deps: { fetchImpl: typeof fetch; refresh: () => Promise<boolean> }) => Promise<unknown>
  }> = [
    {
      name: 'startTurn',
      ok: () => json({ turnId: 't1' }, 202),
      run: (deps) => startTurn('c1', { text: 'hi' }, deps),
    },
    {
      name: 'stopTurn',
      ok: () => json({ status: 'stopping' }),
      run: (deps) => stopTurn('c1', 't1', deps),
    },
    {
      name: 'switchMode',
      ok: () => json({ mode: 'write' }),
      run: (deps) => switchMode('c1', 'write', deps),
    },
    {
      name: 'buildFromPlan',
      ok: () => json({ outcome: 'started' }),
      run: (deps) => buildFromPlan('c1', 'tc1', {}, deps),
    },
    {
      name: 'resolvePlanOptions',
      ok: () => json({ state: 'refine', alreadyResolved: false }),
      run: (deps) => resolvePlanOptions('c1', 'tc1', deps),
    },
    {
      name: 'readTurnStream',
      ok: () => streamResponse(['data: [DONE]\n\n']),
      run: (deps) =>
        readTurnStream({
          conversationId: 'c1',
          signal: new AbortController().signal,
          onFrame: () => undefined,
          deps,
        }),
    },
  ]

  it.each(CALLS)('$name recovers: 401 → refresh → retry → resolved', async ({ ok, run }) => {
    const fetchImpl = expiredThen(ok)
    const refresh = vi.fn(async () => true)
    await expect(run({ fetchImpl: fetchImpl as unknown as typeof fetch, refresh })).resolves.toBeDefined()
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })

  it.each(CALLS)('$name surfaces a typed error when the refresh FAILS — never a silent success', async ({ ok, run }) => {
    // The dangerous half of a recovery path: a dead session must reach the caller as a rejection,
    // not as an empty-but-resolved promise the UI renders as a successful no-op.
    const fetchImpl = expiredThen(ok)
    const refresh = vi.fn(async () => false)
    await expect(run({ fetchImpl: fetchImpl as unknown as typeof fetch, refresh })).rejects.toThrow()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })

  it('readTurnStream still reassembles a torn frame after a 401-refresh-retry', async () => {
    // The wrapper takes the REQUEST only. If it had swallowed the body, the carry buffer would be
    // reading someone else's stream — so prove the reassembly path survives the retry, not just the
    // status code.
    const whole = `id: 3\ndata: ${SNAPSHOT}\n\nid: 4\ndata: ${DELTA}\n\ndata: [DONE]\n\n`
    const cut = 40 // tears the snapshot frame mid-JSON
    const fetchImpl = expiredThen(() => streamResponse([whole.slice(0, cut), whole.slice(cut)]))
    const seen: TurnFrame[] = []
    const outcome = await readTurnStream({
      conversationId: 'c1',
      signal: new AbortController().signal,
      onFrame: (frame) => seen.push(frame),
      deps: { fetchImpl: fetchImpl as unknown as typeof fetch, refresh: async () => true },
    })
    expect(outcome).toBe('completed')
    expect(seen.map((frame) => frame.type)).toEqual(['snapshot', 'text_delta'])
  })

  it('the retried mutating call carries the POST-refresh CSRF token (the KTD-9 pairing)', async () => {
    // U1 is two halves and they only work together: routing through authFetch without the
    // per-attempt CSRF read would trade every 401 for a 403. Pin the pairing from this side too —
    // api.test.js owns the wrapper-level proof.
    document.cookie = 'csrf=before-refresh'
    const fetchImpl = expiredThen(() => json({ mode: 'write' }))
    const refresh = vi.fn(async () => {
      document.cookie = 'csrf=after-refresh' // /auth/refresh rotates the cookie
      return true
    })
    await switchMode('c1', 'write', { fetchImpl: fetchImpl as unknown as typeof fetch, refresh })
    const headersOfCall = (i: number) =>
      ((fetchImpl.mock.calls[i] as unknown[])[1] as RequestInit).headers as Record<string, string>
    expect(headersOfCall(0)['X-CSRF-Token']).toBe('before-refresh')
    expect(headersOfCall(1)['X-CSRF-Token']).toBe('after-refresh')
    document.cookie = 'csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
  })
})
