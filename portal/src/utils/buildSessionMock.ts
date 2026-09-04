/**
 * `FakeEventSource` — a hand-driven `EventSourceLike` test double (no `EventSource`
 * exists in jsdom). Tests emit frames / trigger the error arms on it directly and
 * inject it through the `eventSourceFactory` deps seam (KTD-6).
 *
 * This module once also carried scripted C7 envelope sequences and a canned/coordinated
 * C3 mock client, built while SESSION-API's real endpoints did not exist yet. Nothing
 * consumed that surface once the real endpoints landed (each test suite primes its own
 * `vi.fn()` client bag instead), so it was deleted — the fake transport below is the
 * only export anything imports.
 *
 * AUDIT-2026-09-03 · verified-alive: intentionally retained pending verification — see the audit record.
 */
import type { EventSourceLike } from './buildSessionEvents'
import type { ProgressEnvelope } from './buildSessionTypes'

const READY_STATE = { CONNECTING: 0, OPEN: 1, CLOSED: 2 } as const

/**
 * A test/dev double for a native `EventSource`. Starts CONNECTING; a test drives it
 * with `open()` / `emit()` / `emitEnvelope()` and the two error arms
 * (`failNeverOpened()` = admission failure, `dropAfterOpen()` = transient drop). The
 * consumer's `close()` sets CLOSED, and `closeCalls` records how many times it fired
 * (the terminal-close assertion).
 */
export class FakeEventSource implements EventSourceLike {
  readyState: number = READY_STATE.CONNECTING
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  onopen: ((ev: Event) => void) | null = null
  readonly url: string
  closeCalls = 0

  constructor(url: string) {
    this.url = url
  }

  /** Simulate the connection opening (first byte). */
  open(): void {
    this.readyState = READY_STATE.OPEN
    this.onopen?.(new Event('open'))
  }

  /** Push one raw `data:` payload (a JSON envelope string, or the `[DONE]` sentinel). */
  emit(data: string): void {
    this.onmessage?.(new MessageEvent('message', { data }))
  }

  /** Push one typed envelope, JSON-encoded exactly as the wire would carry it. */
  emitEnvelope(env: ProgressEnvelope): void {
    this.emit(JSON.stringify(env))
  }

  /** The never-opened admission failure (401/404, C3 §4.1): CLOSED + `error`, no retry. */
  failNeverOpened(): void {
    this.readyState = READY_STATE.CLOSED
    this.onerror?.(new Event('error'))
  }

  /** A transient drop after a prior open: CONNECTING + `error` (the browser would auto-retry). */
  dropAfterOpen(): void {
    this.readyState = READY_STATE.CONNECTING
    this.onerror?.(new Event('error'))
  }

  close(): void {
    this.closeCalls += 1
    this.readyState = READY_STATE.CLOSED
  }
}
