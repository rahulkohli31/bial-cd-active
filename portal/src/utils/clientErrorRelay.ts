/**
 * The relay leg of the app's own client-error reporting (U13, R17 runtime half).
 *
 * A generated app can answer 200 and still die in the browser before it paints — a bad hook
 * order, a null read in a render, a rejected fetch nobody caught. Every health signal the
 * platform has runs against the SERVER, so that whole class of failure is invisible to it, and
 * "Build complete" goes out over a blank page. The app has always captured its own
 * `window.onerror` / `unhandledrejection` / `console.*`
 * (`sandbox/template/components/bial/error-capture.tsx`) and posted them to the framing portal;
 * until now nothing listened. This is the listener.
 *
 * WHAT REACHES THE USER FROM HERE: nothing. The report goes to the build harness, where it makes
 * the health verdict not-green, and to the agent, which can act on it. The user's only visible
 * consequence is that the completion claim does not appear. A JS stack trace under a file path
 * is not a product surface, and this plan removes developer surfaces rather than adding one.
 *
 * TRUST BOUNDARY. The origin check that decides whether a message is even seen lives in
 * `LivePreview` (C8 §3) and is not repeated here — this module is reached only for messages that
 * already passed it. What it does add is SHAPE validation: passing the origin check proves where
 * the bytes came from, not what they are, and the sender is unreviewed agent-authored code
 * running next to unreviewed npm.
 */

import { authFetch } from './api'
import type { AuthFetchDeps } from './api'
import { isRecord } from './apiError'

/** The postMessage discriminator the app's capture component stamps on every report. */
const CLIENT_ERROR_TYPE = 'bial:client-error'

/* Mirrors the server's own caps (`CLIENT_ERROR_*_MAX_CHARS` in
 * `backend/src/api/v1/build_sessions/schemas.py`). Kept in step by value, not by import — there
 * is no shared-constants mechanism across Python and TypeScript in this repo. */
const MAX_SOURCE = 64
const MAX_TITLE = 1000
const MAX_STACK = 20000

/**
 * How many reports one page may relay before it stops talking.
 *
 * A crash loop is the ORDINARY shape of this input, not the exotic one: a component that throws
 * on render throws again on every re-render, and React will happily do that hundreds of times a
 * second. The server caps what it keeps per app, but a cap on the far side of the network still
 * means one broken page firing hundreds of requests a second from the user's own browser. The
 * first few reports carry the fault; the rest are copies.
 *
 * Counted per FRAMED APP rather than for the lifetime of the tab, so opening a second project —
 * or the same one after a rebuild — starts fresh. Otherwise one bad build would silence the
 * reporting for every app the user looked at afterwards.
 */
/* NB: the server keeps its own, LARGER cap under the same name
 * (`MAX_REPORTS_PER_APP = 10` in `backend/src/services/orchestrator/client_errors.py`). They are
 * not meant to match — this one bounds requests LEAVING the browser, that one bounds reports the
 * store KEEPS — and the client cap is the smaller of the two on purpose, so the throttle bites
 * before the server has to start refusing. */
export const MAX_REPORTS_PER_APP = 8

/** How many distinct scopes one relay tracks before it starts forgetting the oldest. */
const MAX_TRACKED_SCOPES = 8

/** A report as the app sends it, once we have decided it is one. */
export interface ClientErrorPayload {
  source: string
  title: string
  stack: string
}

/**
 * Is this inbound frame message a client-error report, and what does it say?
 *
 * Field-by-field narrowing, not a cast: the payload is authored by code inside the generated app,
 * so "it arrived from the right origin" says nothing about its shape. A message that is not a
 * report — or a report with no title, which is nothing to act on — returns null and is dropped.
 */
export function asClientErrorPayload(data: unknown): ClientErrorPayload | null {
  // `isRecord` rather than a hand-rolled `typeof === 'object'` check: it also excludes arrays,
  // which the naive form lets through as a record with numeric keys.
  if (!isRecord(data)) return null
  const record = data
  if (record.type !== CLIENT_ERROR_TYPE) return null
  const title = typeof record.title === 'string' ? record.title : ''
  if (title === '') return null
  return {
    // Capped HERE, not left to the server's own `max_length`. Every one of these strings is
    // written by code inside the generated app, and a server-side cap only rejects the body
    // AFTER it has been buffered — so an app that wanted to could drive the citizen's own
    // authenticated browser into posting arbitrarily large bodies at the control plane. The
    // limits mirror the server's; anything past them is noise in a diagnostic anyway.
    source: (typeof record.source === 'string' ? record.source : 'unknown').slice(0, MAX_SOURCE),
    title: title.slice(0, MAX_TITLE),
    // Absent on the `console.error` / `console.warn` arms, which are the commonest reports of
    // all — an empty stack is a normal report, not a malformed one.
    stack: (typeof record.stack === 'string' ? record.stack : '').slice(0, MAX_STACK),
  }
}

/**
 * Make a relay. The returned object's `relay` is what a frame-message handler calls; `reset`
 * starts a fresh budget and belongs at the start of a turn.
 *
 * THE BUDGET IS PER TURN, NOT PER PAGE, and that is a correctness property rather than a policy
 * one. The scope key is the framed url, which is byte-identical across repair turns on the attach
 * arm — same container, same app, same url. A page-lifetime counter therefore never reset: eight
 * crashes into a session, the relay went quiet for good, and every later verify came back green
 * by manufactured absence. Silence that the platform itself caused reads exactly like health.
 *
 * COUNTED PER SCOPE, NOT AGAINST A LAST-SEEN POINTER. A single "which app was I counting"
 * pointer is reset by flapping between two apps, so alternating reports bypass the cap entirely.
 * A small bounded map costs nothing and cannot be flapped.
 *
 * Errors are SWALLOWED, and that is the requirement rather than an oversight: this is a
 * diagnostic side-channel about an app that is already broken. A failed report must cost the
 * citizen nothing — not an error toast, not an unhandled rejection in their console, and above
 * all not the preview they are looking at.
 */
export function makeClientErrorRelay(deps: AuthFetchDeps = {}) {
  let sentByScope = new Map<string, number>()

  async function relay(projectId: string, appScope: string, data: unknown): Promise<void> {
    const payload = asClientErrorPayload(data)
    if (payload === null) return
    const sent = sentByScope.get(appScope) ?? 0
    if (sent >= MAX_REPORTS_PER_APP) return
    sentByScope.set(appScope, sent + 1)
    // Bounded: a page that framed many apps must not accumulate a counter per app forever.
    // Insertion-ordered, so the first key is the least recently FIRST-seen.
    if (sentByScope.size > MAX_TRACKED_SCOPES) {
      const oldest = sentByScope.keys().next().value
      if (oldest !== undefined) sentByScope.delete(oldest)
    }
    try {
      await authFetch(
        `/api/build-sessions/projects/${encodeURIComponent(projectId)}/client-error`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        },
        deps,
      )
    } catch {
      // See the note above: a diagnostic about a broken app may not become a second failure.
    }
  }

  /** Start a fresh budget. Called when a turn begins — see the per-turn note above. */
  function reset(): void {
    sentByScope = new Map()
  }

  return { relay, reset }
}
