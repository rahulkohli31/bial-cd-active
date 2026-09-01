import { describe, it, expect, vi } from 'vitest'
import { subscribeBuildFeed, toProgressEnvelope } from '../buildSessionEvents'
import type { BuildFeedError, EventSourceFactory } from '../buildSessionEvents'
import { FakeEventSource } from '../buildSessionMock'
import type { ProgressEnvelope } from '../buildSessionTypes'

function setup(depsOverride: { maxReconnects?: number } = {}) {
  const fake = new FakeEventSource('unused')
  const factory: EventSourceFactory = () => fake
  const envelopes: ProgressEnvelope[] = []
  const errors: BuildFeedError[] = []
  let opens = 0
  const sub = subscribeBuildFeed(
    's1',
    {
      onEnvelope: (e) => envelopes.push(e),
      onError: (e) => errors.push(e),
      onOpen: () => {
        opens += 1
      },
    },
    { eventSourceFactory: factory, ...depsOverride },
  )
  return { fake, envelopes, errors, sub, opens: () => opens }
}

const STEP: ProgressEnvelope = { type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding…', state: 'started', hidden: false }
const ERROR: ProgressEnvelope = { type: 'error', seq: 3, source: 'tsc', title: 'Type error', cleaned_stack: 'app/page.tsx(1,1)' }
const PREVIEW: ProgressEnvelope = { type: 'preview_ready', seq: 4, preview_url: 'https://app.example.azurecontainerapps.io/' }
const ESCALATION: ProgressEnvelope = { type: 'escalation', seq: 5, reason: 'self_heal_budget_exhausted', detail: 'gave up', last_error: null }
const QUOTA: ProgressEnvelope = { type: 'quota_exceeded', seq: 6, limit: 100, used: 100, resets_at: '2026-07-15T18:30:00Z' }
const ENDED: ProgressEnvelope = { type: 'ended', seq: 7, status: 'ended', preview_url: null, snapshot_committed: true, reason: 'completed' }

describe('buildSessionEvents — dispatch (C7 §3)', () => {
  it('produces the correct typed object for each of the 6 envelope types', () => {
    const { fake, envelopes } = setup()
    fake.open()
    ;[STEP, ERROR, PREVIEW, ESCALATION, QUOTA].forEach((e) => fake.emitEnvelope(e))
    expect(envelopes).toEqual([STEP, ERROR, PREVIEW, ESCALATION, QUOTA])

    fake.emitEnvelope(ENDED) // terminal — the 6th type
    expect(envelopes).toHaveLength(6)
    expect(envelopes[5]).toEqual(ENDED)
    expect(fake.closeCalls).toBe(1) // ended closes the feed
  })

  it('drops a frame with an unknown type without throwing (defensive runtime drop)', () => {
    const { fake, envelopes, errors } = setup()
    fake.open()
    expect(() => fake.emit(JSON.stringify({ type: 'nonsense', seq: 1 }))).not.toThrow()
    expect(envelopes).toHaveLength(0)
    expect(errors).toHaveLength(0)
  })

  it('drops a retired `log` frame (U29 — the server can no longer emit one) without throwing, and a known frame still lands right after it (liveness)', () => {
    // FLIPPED: `log` used to be a recognized C7 member; U29 removed it end to end because no
    // production BRAIN path had ever emitted one. The wire-level consumer must now treat it
    // exactly like any other unrecognized `type` — dropped defensively, never a throw — and a
    // frame arriving right after it must still dispatch normally (this is not a dead transport).
    const { fake, envelopes, errors } = setup()
    fake.open()
    expect(() =>
      fake.emit(JSON.stringify({ type: 'log', seq: 2, source: 'exec', stream: 'stderr', text: 'warn: x' })),
    ).not.toThrow()
    expect(envelopes).toHaveLength(0)
    fake.emitEnvelope(STEP)
    expect(envelopes).toEqual([STEP]) // liveness: a known frame still renders
    expect(errors).toHaveLength(0)
  })

  it('skips a malformed (non-JSON) data line — parity with the chat relay skip-malformed rule (C3 §4.2)', () => {
    const { fake, envelopes } = setup()
    fake.open()
    fake.emit('not-json {oops')
    fake.emitEnvelope(STEP)
    expect(envelopes).toEqual([STEP]) // the good frame still lands
  })
})

describe('buildSessionEvents — terminal close (C3 §4.3)', () => {
  it('recognizes the [DONE] sentinel AHEAD of the malformed-skip rule and closes', () => {
    const { fake, envelopes } = setup()
    fake.open()
    fake.emit('[DONE]') // not JSON, but must NOT be treated as malformed — it is terminal
    expect(fake.closeCalls).toBe(1)
    expect(envelopes).toHaveLength(0)
  })

  it('after ended + [DONE], a further pushed frame is NOT dispatched (no zombie reconnect against an ended session)', () => {
    const { fake, envelopes } = setup()
    fake.open()
    fake.emitEnvelope(ENDED)
    fake.emit('[DONE]')
    fake.emitEnvelope(STEP) // arrives after terminal
    expect(envelopes).toHaveLength(1)
    expect(envelopes[0]).toEqual(ENDED)
  })
})

describe('buildSessionEvents — error arm, fail closed (C3 §4.1, KTD-1)', () => {
  it('a never-opened admission failure surfaces a terminal error and stops (readyState CLOSED)', () => {
    const { fake, errors, envelopes } = setup()
    fake.failNeverOpened() // 401/404 — the stream never opened
    expect(errors).toEqual([{ kind: 'admission', message: expect.any(String) }])
    expect(fake.closeCalls).toBe(1)

    fake.emitEnvelope(STEP) // anything after the terminal error is ignored
    expect(envelopes).toHaveLength(0)
  })

  it('a bounded reconnect gives up after the cap rather than looping forever (readyState CONNECTING)', () => {
    const { fake, errors } = setup({ maxReconnects: 2 })
    fake.open() // opened at least once → transient drops are reconnect attempts
    fake.dropAfterOpen() // 1
    fake.dropAfterOpen() // 2
    expect(errors).toHaveLength(0) // still within budget
    fake.dropAfterOpen() // 3 > 2 → exhausted
    expect(errors).toEqual([{ kind: 'reconnect_exhausted', message: expect.any(String) }])
    expect(fake.closeCalls).toBe(1)
  })

  it('a flapping connection (open→drop, open→drop, …) still exhausts the budget — onopen alone never resets it (finding #14)', () => {
    vi.useFakeTimers()
    try {
      const { fake, errors } = setup({ maxReconnects: 2 })
      fake.open()
      // Each cycle re-opens and immediately drops. Before the fix, every open() zeroed the
      // budget, so this loop could flap forever without ever tripping reconnect_exhausted.
      fake.dropAfterOpen() // 1
      fake.open()
      fake.dropAfterOpen() // 2
      expect(errors).toHaveLength(0) // still within budget
      fake.open()
      fake.dropAfterOpen() // 3 > 2 → exhausted, despite the re-opens in between
      expect(errors).toEqual([{ kind: 'reconnect_exhausted', message: expect.any(String) }])
      expect(fake.closeCalls).toBe(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a connection that stays open through the stability window earns its budget back', () => {
    vi.useFakeTimers()
    try {
      const { fake, errors } = setup({ maxReconnects: 2 })
      fake.open()
      fake.dropAfterOpen() // 1
      fake.dropAfterOpen() // 2 — budget spent
      fake.open()
      vi.advanceTimersByTime(10_000) // survives the stability window → budget resets
      fake.dropAfterOpen() // 1 again
      fake.dropAfterOpen() // 2
      expect(errors).toHaveLength(0) // a genuinely-recovered stream is not punished for old drops
      fake.dropAfterOpen() // 3 > 2 → exhausted
      expect(errors).toEqual([{ kind: 'reconnect_exhausted', message: expect.any(String) }])
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('buildSessionEvents — quota + resume (C7 §8, C3 §4.2)', () => {
  it('dispatches the quota pair quota_exceeded → ended in seq order', () => {
    const { fake, envelopes } = setup()
    fake.open()
    fake.emitEnvelope(QUOTA)
    fake.emitEnvelope({ type: 'ended', seq: 7, status: 'ended', preview_url: null, snapshot_committed: true, reason: 'quota_exceeded' })
    expect(envelopes.map((e) => e.type)).toEqual(['quota_exceeded', 'ended'])
  })

  it('on reconnect replays seq > last_seq in order; a malformed line in the replay is skipped; lastSeq stays monotonic', () => {
    const { fake, envelopes, sub } = setup()
    fake.open()
    fake.emitEnvelope(STEP) // seq 1
    fake.emitEnvelope({ type: 'step', seq: 2, name: 'install_deps', label: 'Installing…', state: 'started', hidden: false }) // seq 2
    expect(sub.lastSeq()).toBe(2)

    // …a transient drop, then the server replays seq > 2, with one torn line among them.
    fake.emit('garbage')
    fake.emitEnvelope(ERROR) // seq 3
    fake.emitEnvelope(PREVIEW) // seq 4
    expect(envelopes.map((e) => e.seq)).toEqual([1, 2, 3, 4])
    expect(sub.lastSeq()).toBe(4)
  })
})

describe('toProgressEnvelope — parse at the boundary', () => {
  it('fails closed: an ended with an unrecognized status is treated as failed, never lost', () => {
    const env = toProgressEnvelope({ type: 'ended', seq: 9, status: 'weird', preview_url: null, snapshot_committed: false, reason: 'build_failed' })
    expect(env).toEqual({ type: 'ended', seq: 9, status: 'failed', preview_url: null, snapshot_committed: false, reason: 'build_failed' })
  })

  it('drops a preview_ready with no url (unusable — the hook seeds from getStatus instead)', () => {
    expect(toProgressEnvelope({ type: 'preview_ready', seq: 4, preview_url: '' })).toBeNull()
    expect(toProgressEnvelope({ type: 'preview_ready', seq: 4 })).toBeNull()
  })

  it('drops a frame with a non-numeric seq', () => {
    expect(toProgressEnvelope({ type: 'step', seq: 'nope', name: 'x', label: 'y', state: 'ok' })).toBeNull()
  })

  it('carries a step`s `hidden` flag through the parse (F3: else the live hidden-step filter is a no-op)', () => {
    // The backend serializes `hidden:true` for read-only/housekeeping steps; if the parser drops it,
    // the live `!env.hidden` filter keeps every step and the LIVE feed shows what reload hides.
    const hidden = toProgressEnvelope({ type: 'step', seq: 5, name: 'inspect', label: 'x', state: 'ok', hidden: true })
    expect(hidden).toMatchObject({ type: 'step', seq: 5, hidden: true })
    // A visible step parses to hidden:false (never undefined), so the boolean filter is well-defined.
    const visible = toProgressEnvelope({ type: 'step', seq: 6, name: 'install', label: 'y', state: 'ok' })
    expect(visible).toMatchObject({ type: 'step', hidden: false })
  })
})
