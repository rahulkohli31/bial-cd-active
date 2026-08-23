/**
 * U13 (portal half) — the relay from the framed app's error reporter to the build harness.
 *
 * The origin check that decides whether a message is seen at all lives in `LivePreview` and is
 * tested there (C8 §3). What is pinned here is everything AFTER that gate: shape narrowing on a
 * payload written by unreviewed agent-authored code, the crash-loop throttle, and the rule that a
 * failed report costs the citizen nothing.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  MAX_REPORTS_PER_APP,
  asClientErrorPayload,
  makeClientErrorRelay,
} from '../clientErrorRelay'

const A_REPORT = {
  type: 'bial:client-error',
  source: 'window.onerror',
  title: "Cannot read properties of undefined (reading 'map')",
  stack: 'at RecordsTable (app/records/page.tsx:41:19)',
  ts: 1_700_000_000_000,
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn(async () => new Response('{"recorded":true}', { status: 202 }))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('asClientErrorPayload — a valid origin is not a valid payload', () => {
  it('narrows a real report field by field', () => {
    expect(asClientErrorPayload(A_REPORT)).toEqual({
      source: 'window.onerror',
      title: A_REPORT.title,
      stack: A_REPORT.stack,
    })
  })

  it('accepts a report with no stack — the commonest kind', () => {
    // `console.error` / `console.warn` captures have a message and no stack. Requiring one would
    // drop the majority of real reports.
    expect(asClientErrorPayload({ ...A_REPORT, source: 'console.error', stack: undefined })).toEqual(
      { source: 'console.error', title: A_REPORT.title, stack: '' },
    )
  })

  it('drops anything that is not a client-error report', () => {
    // The app frame may postMessage for other reasons, and so may anything else the browser
    // routes through the same listener. The discriminator is the gate.
    for (const junk of [null, undefined, 'a string', 42, {}, { type: 'something-else' }]) {
      expect(asClientErrorPayload(junk)).toBeNull()
    }
  })

  it('drops a report with no title — there is nothing to act on', () => {
    expect(asClientErrorPayload({ ...A_REPORT, title: '' })).toBeNull()
    expect(asClientErrorPayload({ ...A_REPORT, title: 12 })).toBeNull()
  })

  it('coerces a non-string source rather than trusting or rejecting it', () => {
    // The source is a LABEL on a diagnostic, never a branch. A weird one should not cost us the
    // crash it was reporting.
    expect(asClientErrorPayload({ ...A_REPORT, source: { evil: true } })?.source).toBe('unknown')
  })
})

describe('makeClientErrorRelay — the harness hears it, the citizen does not', () => {
  it('POSTs a report to the project-scoped ingest route', async () => {
    await makeClientErrorRelay().relay('proj-1', 'https://app-a.example/', A_REPORT)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/build-sessions/projects/proj-1/client-error')
    expect(opts.method).toBe('POST')
    // The postMessage discriminator and the app's own clock are NOT forwarded: the first has done
    // its job by now, and the second is app-controlled where the server's arrival time is honest.
    expect(JSON.parse(opts.body)).toEqual({
      source: 'window.onerror',
      title: A_REPORT.title,
      stack: A_REPORT.stack,
    })
  })

  it('sends nothing for a message that is not a report', async () => {
    const { relay } = makeClientErrorRelay()
    await relay('proj-1', 'https://app-a.example/', { type: 'something-else' })
    expect(fetchMock).not.toHaveBeenCalled()
    // LIVENESS: the relay is genuinely working — a real report on the same instance does send.
    await relay('proj-1', 'https://app-a.example/', A_REPORT)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('stops talking after the cap — a crash loop is the ordinary case, not the exotic one', async () => {
    // A component that throws on render throws again on every re-render, hundreds of times a
    // second. The server caps what it KEEPS; without this cap the user's own browser still fires
    // hundreds of requests a second at it.
    const { relay } = makeClientErrorRelay()
    for (let i = 0; i < MAX_REPORTS_PER_APP + 20; i += 1) {
      await relay('proj-1', 'https://app-a.example/', A_REPORT)
    }
    expect(fetchMock).toHaveBeenCalledTimes(MAX_REPORTS_PER_APP)
  })

  it('starts fresh for a new container — one bad build must not silence the next', async () => {
    const { relay } = makeClientErrorRelay()
    for (let i = 0; i < MAX_REPORTS_PER_APP + 5; i += 1) {
      await relay('proj-1', 'https://app-a.example/', A_REPORT)
    }
    expect(fetchMock).toHaveBeenCalledTimes(MAX_REPORTS_PER_APP)

    // A rebuild or a restore gives the app a new container and a new url. The crash loop that
    // silenced the relay belonged to the old one.
    await relay('proj-1', 'https://app-b.example/', A_REPORT)
    expect(fetchMock).toHaveBeenCalledTimes(MAX_REPORTS_PER_APP + 1)
  })

  it('counts PER SCOPE, so flapping between two apps cannot reset an exhausted budget', () => {
    // A single "which app am I counting" pointer is reset by every switch, so alternating
    // reports bypass the cap entirely — which is the shape a crash loop in a page that frames
    // two apps actually takes.
    const { relay } = makeClientErrorRelay()
    return (async () => {
      for (let i = 0; i < MAX_REPORTS_PER_APP; i += 1) {
        await relay('proj-1', 'https://app-a.example/', A_REPORT)
      }
      fetchMock.mockClear()
      // Flap away and back. App B gets its own budget; app A's is still spent.
      await relay('proj-1', 'https://app-b.example/', A_REPORT)
      await relay('proj-1', 'https://app-a.example/', A_REPORT)
      expect(fetchMock).toHaveBeenCalledTimes(1)
    })()
  })

  it('starts a fresh budget on reset, so one turn cannot silence the next', async () => {
    // ★ The scope key is the framed url, and on the attach arm that url is byte-identical across
    // repair turns. Without a per-turn reset the budget was effectively per page-load: eight
    // crashes in and the relay went quiet for good, so every later verify came back green on
    // silence the platform itself had caused. Absence that we manufactured reads exactly like
    // health, which is the failure class this whole feature exists to remove.
    const { relay, reset } = makeClientErrorRelay()
    for (let i = 0; i < MAX_REPORTS_PER_APP + 3; i += 1) {
      await relay('proj-1', 'https://app-a.example/', A_REPORT)
    }
    expect(fetchMock).toHaveBeenCalledTimes(MAX_REPORTS_PER_APP)

    reset()
    await relay('proj-1', 'https://app-a.example/', A_REPORT)
    expect(fetchMock).toHaveBeenCalledTimes(MAX_REPORTS_PER_APP + 1)
  })

  it('caps the size of every string it forwards', () => {
    // The server caps these too, but its cap only rejects a body it has already buffered. These
    // strings are written by code inside the generated app.
    const huge = 'x'.repeat(50_000)
    const payload = asClientErrorPayload({ ...A_REPORT, title: huge, stack: huge, source: huge })
    expect(payload?.title.length).toBe(1000)
    expect(payload?.stack.length).toBe(20_000)
    expect(payload?.source.length).toBe(64)
  })

  it('swallows a failed report — a diagnostic must never become a second failure', async () => {
    // This is a side-channel about an app that is ALREADY broken. An unhandled rejection here
    // would land in the citizen's console; a thrown error would reach the frame-message handler,
    // which is called from a window listener.
    fetchMock.mockRejectedValue(new Error('network down'))
    await expect(
      makeClientErrorRelay().relay('proj-1', 'https://app-a.example/', A_REPORT),
    ).resolves.toBeUndefined()

    fetchMock.mockResolvedValue(new Response('nope', { status: 500 }))
    await expect(
      makeClientErrorRelay().relay('proj-1', 'https://app-a.example/', A_REPORT),
    ).resolves.toBeUndefined()
  })
})
