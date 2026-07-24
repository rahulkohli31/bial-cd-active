/**
 * The U10 stream reader's transport disciplines: carry-buffered reassembly of torn
 * frames, distinct outcomes (completed / truncated / stalled / aborted), and the
 * cross-repo keepalive⁄stall inequality pin.
 */

import { describe, expect, it, vi } from 'vitest'

import {
  TURN_STREAM_STALL_TIMEOUT_MS,
  TurnStartError,
  buildFromPlan,
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
      fetchFn: fetchFn as unknown as typeof fetch,
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
      fetchFn: fetchFn as unknown as typeof fetch,
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
      fetchFn: fetchFn as unknown as typeof fetch,
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
      fetchFn: fetchFn as unknown as typeof fetch,
      stallTimeoutMs: 20,
    })
    expect(outcome).toBe('stalled')
  })

  it('resolves aborted when the caller aborts', async () => {
    const controller = new AbortController()
    const body = new ReadableStream<Uint8Array>({ start() {} })
    const fetchFn = vi.fn(async () => new Response(body, { status: 200 }))
    const pending = readTurnStream({
      conversationId: 'c1',
      signal: controller.signal,
      onFrame: () => undefined,
      fetchFn: fetchFn as unknown as typeof fetch,
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
      fetchFn as unknown as typeof fetch
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
      startTurn('c1', { text: 'hi' }, fetchFn as unknown as typeof fetch)
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
    await startTurn('c1', { text: 'hi' }, fetchFn as unknown as typeof fetch)
    expectUnPrefixed(urlOf(fetchFn))
  })

  it('stopTurn → /api/conversations/{id}/turns/{turnId}/stop', async () => {
    const fetchFn = vi.fn(async () => json({ status: 'stopping' }))
    await stopTurn('c1', 't1', fetchFn as unknown as typeof fetch)
    expectUnPrefixed(urlOf(fetchFn))
  })

  it('switchMode → /api/conversations/{id}/mode', async () => {
    const fetchFn = vi.fn(async () => json({ mode: 'plan' }))
    await switchMode('c1', 'plan', fetchFn as unknown as typeof fetch)
    expectUnPrefixed(urlOf(fetchFn))
  })

  it('buildFromPlan → /api/conversations/{id}/plan-options/{toolCallId}/build', async () => {
    const fetchFn = vi.fn(async () => json({ outcome: 'started' }))
    await buildFromPlan('c1', 'tc1', {}, fetchFn as unknown as typeof fetch)
    expectUnPrefixed(urlOf(fetchFn))
  })

  it('resolvePlanOptions → /api/conversations/{id}/plan-options/{toolCallId}/resolve', async () => {
    const fetchFn = vi.fn(async () => json({ state: 'refine', alreadyResolved: false }))
    await resolvePlanOptions('c1', 'tc1', fetchFn as unknown as typeof fetch)
    expectUnPrefixed(urlOf(fetchFn))
  })

  it('readTurnStream → /api/conversations/{id}/events', async () => {
    const fetchFn = vi.fn(async () => streamResponse(['data: [DONE]\n\n']))
    await readTurnStream({
      conversationId: 'c1',
      signal: new AbortController().signal,
      onFrame: () => undefined,
      fetchFn: fetchFn as unknown as typeof fetch,
    })
    expectUnPrefixed(urlOf(fetchFn))
  })
})
