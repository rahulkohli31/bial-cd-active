import { useState, useCallback, useRef, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { getAccessToken, refreshAccessToken, clearSession, getStoredUser, SIGNOUT_REASONS, handleSuspendedSession } from '../utils/auth.js'
import { isSuspended, ApiError } from '../utils/apiError'
import { notifyUsageChanged } from '../utils/usage.js'

// Backstop for the silent truncation in truncateMessages. Raised from the old
// 50k cost-control cap to sit under the 1M model window minus the 64k
// max_tokens output budget, so an allowed near-limit send still fits the window
// (no API "prompt too long"). The user-facing guardrail (CONTEXT_* below) warns
// at 150k and blocks at 200k; this backstop only trims for a user who pushed
// past the visible 150k warning.
const INPUT_TOKEN_BUDGET = 180_000
const CHARS_PER_TOKEN = 4
// Flat nominal budget cost for one attachment block. The real token cost is
// counted server-side; this only keeps the client-side history estimate from
// either crashing on an array or under-counting a multi-MB file as ~2 tokens.
const NOMINAL_FILE_TOKENS = 1_600
// A deck (.pptx) is sent as a vision PDF: roughly per-page (text + image) tokens.
// Heuristic only, to drive the warn/block UI — never to gate the API call. The
// deck is STICKY and prompt-cached, so the FIRST turn pays the full (cache-write)
// cost and each sticky follow-up re-reads at ~0.1x; the estimate reflects that
// rather than billing the full deck on every turn (which would over-warn badly).
const DECK_TOKENS_PER_PAGE = 3_000
const DECK_CACHED_FACTOR = 0.1

// content is `string | ContentBlock[]`. Never call `.length` on a non-string:
// for an array, sum the text blocks and add a flat per-file nominal (NOT the
// element count) for each image/document block.
function estimateContentTokens(content) {
  if (typeof content === 'string') return Math.ceil(content.length / CHARS_PER_TOKEN)
  if (!Array.isArray(content)) return 0
  let tokens = 0
  for (const block of content) {
    if (block?.type === 'text') tokens += Math.ceil((block.text || '').length / CHARS_PER_TOKEN)
    else tokens += NOMINAL_FILE_TOKENS
  }
  return tokens
}

export function estimateTokens(messages) {
  return messages.reduce((sum, m) => sum + estimateContentTokens(m.content), 0)
}

export function truncateMessages(messages) {
  if (estimateTokens(messages) <= INPUT_TOKEN_BUDGET) return messages
  const [first, ...rest] = messages
  while (rest.length > 1 && estimateTokens([first, ...rest]) > INPUT_TOKEN_BUDGET) {
    rest.shift()
  }
  return [first, ...rest]
}

// Conversation context-length guardrail, anchored to the 200k Opus 4.7 window.
// The chat surfaces warn at SOFT (non-blocking banner + "new chat" CTA) and
// hard-block at HARD (banner + disabled send). Both sit clear of the silent
// truncation backstop so an un-warned user is never surprised by a dropped turn.
export const CONTEXT_SOFT_LIMIT = 150_000
export const CONTEXT_HARD_LIMIT = 200_000

/**
 * The signed-in user's effective per-conversation guardrail thresholds. The
 * login/refresh profile carries server-resolved `limits` (the standard plan
 * unless an admin raised them); fall back to the constants above when absent
 * (e.g. a session minted before this feature). Mirrors the server's soft < hard
 * clamp defensively so the warn banner can never sit at or above the hard stop.
 */
export function getContextLimits() {
  const lim = getStoredUser()?.limits || {}
  const hard = Number.isInteger(lim.contextHardLimit) && lim.contextHardLimit > 0 ? lim.contextHardLimit : CONTEXT_HARD_LIMIT
  let soft = Number.isInteger(lim.contextSoftLimit) && lim.contextSoftLimit > 0 ? lim.contextSoftLimit : CONTEXT_SOFT_LIMIT
  if (soft >= hard) soft = Math.max(1, hard - 1)
  return { soft, hard }
}

/**
 * Estimate a conversation's input size the way assembleApiMessages actually
 * sends it, reading the neutral `parts[]` message model. Mirrors the
 * sticky/newest-only split in assembleApiMessages —
 *  - TEXT parts (prose AND inline text-attachment parts, whose `text` holds the
 *    file content) are sent on EVERY turn, so each is counted by its character
 *    length on every turn it appears — a 200 KB inlined CSV is ~50k tokens, not a
 *    flat 1600.
 *  - FILE parts (image/PDF) send only on the newest turn, so they're counted as a
 *    flat per-file nominal there and ignored on older turns.
 * Heuristic (4 chars/token) used only to drive the warn/block UI — never to gate
 * the API call directly.
 */
export function estimateConversationTokens(messages, systemText = '') {
  if (!Array.isArray(messages)) return 0
  const systemTokens = Math.ceil((systemText?.length || 0) / CHARS_PER_TOKEN)
  const lastIdx = messages.length - 1
  const seenDecks = new Set() // first occurrence pays full; later (sticky) ~0.1x
  let tokens = 0
  messages.forEach((m, i) => {
    for (const p of m?.parts || []) {
      if (p?.type === 'text') {
        tokens += Math.ceil((p.text || '').length / CHARS_PER_TOKEN)
      } else if (p?.type === 'file' && p.kind === 'office') {
        // Office extracted text is sticky (re-sent every turn), so it counts on
        // EVERY turn by its real length — not the nominal one-turn binary cost.
        tokens += Math.ceil((p.text || '').length / CHARS_PER_TOKEN)
      } else if (p?.type === 'file' && p.kind === 'deck') {
        // Deck = a sticky, cached vision PDF: full per-page cost on its first turn
        // (cache write), ~0.1x on each later turn (cache read). De-dup by file_id.
        const pages = Number.isInteger(p.pageCount) && p.pageCount > 0 ? p.pageCount : 1
        const full = pages * DECK_TOKENS_PER_PAGE
        const key = p.pdfFileId || p.attachmentId
        if (seenDecks.has(key)) {
          tokens += Math.ceil(full * DECK_CACHED_FACTOR)
        } else {
          seenDecks.add(key)
          tokens += full
        }
      } else if (p?.type === 'file' && i === lastIdx) {
        tokens += NOMINAL_FILE_TOKENS
      }
    }
  })
  return tokens + systemTokens
}

const AUTH_FAILED = 'AUTH_REFRESH_FAILED'

// The mid-stream idle watchdog (F1). A dead-but-unclosed SSE socket makes `reader.read()` never
// resolve, so the read loop — and the caller's `await sendMessage` — would hang forever with the
// spinner stuck. If NO byte arrives for this long once the stream is flowing, treat the socket as
// dead and surface an error (never a silent truncated reply). It MUST out-wait the server's
// keepalive cadence with margin: once the FIRST byte has arrived the relay emits a `: ping` comment
// every ~15s (backend claude/router.py `_KEEPALIVE_SECONDS`) — including during a MID-STREAM
// server→model retry backoff — so while the server is alive a byte always lands well inside this
// window; only a dead socket trips it. Keep it comfortably above 3× the cadence so a couple of
// delayed pings never false-fail. (The keepalive runs inside the generator, so it does NOT cover
// the pre-first-byte wait — that window is bounded separately by FIRST_BYTE_TIMEOUT_MS below.)
export const STREAM_STALL_TIMEOUT_MS = 60_000

// The pre-first-byte watchdog (F1). The idle watchdog above only wraps `reader.read()`, reached
// only AFTER response headers arrive; the initial POST that awaits the first token has no bound of
// its own, so a browser→server socket that half-closes before headers (a proxy/LB idle-drop during
// the first-token wait) would hang the spinner with no timeout. This caps that window. It is far
// more generous than the mid-stream watchdog because the server legitimately blocks here on the
// WHOLE first model turn: the relay awaits the first delta before committing to a response
// (claude/router.py `_stream` — headers are not even sent until then), and the `: ping` keepalive
// only starts after that, so nothing resets this timer while the server retries.
//
// Sized ABOVE the server's own worst case so patience wins over a false failure (plan Decision 3:
// the turn bills server-side regardless, so a premature client abort + regenerate double-bills).
// Server worst case, from backend FoundryConfig (src/config.py): (connect 10s + read 120s) ×
// (max_retries 2 + 1) = 390s, plus Retry-After backoff between attempts. 420s > 390s with margin;
// a test pins this derivation so a backend retune fails loudly here instead of silently
// re-opening the gap.
export const FIRST_BYTE_TIMEOUT_MS = 420_000

/** A distinct, NON-abort stall signal. Named so the reader's abort-swallow can tell it apart from a
 * genuine navigation/unmount abort and re-throw it (an abort returns the partial text; a stall must
 * surface the error banner). */
export class StreamStalledError extends Error {
  constructor() {
    super('The response stalled. Check your connection and try again.')
    this.name = 'StreamStalledError'
  }
}

/** The server closed the stream WITHOUT its terminal `[DONE]` sentinel — a mid-stream relay failure
 * (`_END_FAIL`) truncated the reply. Distinct + non-abort so the caller surfaces the error banner +
 * Regenerate instead of persisting the partial as if it were a complete answer. */
export class StreamIncompleteError extends Error {
  constructor() {
    super('The response was cut off before it finished. Please try again.')
    this.name = 'StreamIncompleteError'
  }
}

/** Race a promise against a timeout that rejects with `StreamStalledError`. The timer is armed
 * per call and cleared on every settle. Bounds both the pre-first-byte POST (the reader watchdog
 * only covers post-header reads) and each `reader.read()` — where ANY received byte (a delta, a
 * `[DONE]`, or a `: ping` keepalive) resets the window, since the byte arriving is what proves
 * the socket is alive, not a text delta specifically. `onTimeout` runs BEFORE the rejection —
 * the stall path uses it to abort the dead underlying request (F9). */
async function withTimeout(promise, timeoutMs, onTimeout) {
  let timer
  const stall = new Promise((_, reject) => {
    timer = setTimeout(() => {
      onTimeout?.()
      reject(new StreamStalledError())
    }, timeoutMs)
  })
  try {
    return await Promise.race([promise, stall])
  } finally {
    clearTimeout(timer)
  }
}

/**
 * POST /api/claude with a Bearer access token and consume the SSE stream.
 *
 * If the *initial* response is 401 (BEFORE the stream starts), refresh the
 * access token once and retry. A 401 is never retried once `getReader()` has
 * begun — auth is checked once at admission. Dependencies are injected so this
 * is testable without a React render. Returns the accumulated text.
 */
export async function fetchClaudeStream({
  body,
  onChunk,
  signal,
  abort,
  fetchImpl = fetch,
  getToken = getAccessToken,
  refresh = refreshAccessToken,
}) {
  const post = (token) =>
    fetchImpl('/api/claude', {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(body),
      signal,
    })

  // F9 — a stall must ABORT the dead request, not abandon it: without the abort the browser keeps
  // the socket open and the server keeps generating (and billing) into it. Order is load-bearing:
  // the flag is set BEFORE the abort so the abort-swallow branches (which see `signal.aborted`
  // flip true) can tell this deliberate teardown from a genuine navigation/unmount abort and let
  // the stall surface as an ERROR rather than a silent partial.
  let stalled = false
  const stallOut = () => {
    stalled = true
    abort?.()
  }

  // Bound the pre-first-byte wait: a socket that half-closes before headers arrive would otherwise
  // hang the POST forever (the reader watchdog only covers reads AFTER headers).
  let response = await withTimeout(post(getToken()), FIRST_BYTE_TIMEOUT_MS, stallOut)

  // Pre-stream 401 only: refresh once, then retry. refreshAccessToken() returns a
  // SUCCESS BOOLEAN in the cookie-session model (not a bearer token), so the retry
  // carries NO Authorization header — the refreshed session cookie rides along
  // automatically; templating the boolean would send a literal `Bearer true`.
  if (response.status === 401) {
    const refreshed = await refresh()
    if (!refreshed) {
      const err = new Error('Your session has expired. Please sign in again.')
      err.code = AUTH_FAILED
      throw err
    }
    response = await withTimeout(post(), FIRST_BYTE_TIMEOUT_MS, stallOut)
  }

  if (!response.ok) {
    if (response.status === 401) {
      const err = new Error('Your session has expired. Please sign in again.')
      err.code = AUTH_FAILED
      throw err
    }
    const errBody = await response.json().catch(() => ({}))
    // Mid-session suspension, checked on the PRE-STREAM response (mirrors the
    // 429 daily-limit interceptor below). `current_user` runs before the first
    // SSE byte, so a suspended user's 403 arrives here — the reader is never
    // opened. Tear the session down and hard-bounce to the login banner.
    if (isSuspended(errBody, response.status)) {
      handleSuspendedSession()
      throw new ApiError('Account suspended', 403)
    }
    // Daily token limit: surface a user-ready message (the existing setError
    // path renders it). A 429 WITHOUT the known code falls through to the
    // generic error so other rate limits keep their server message.
    if (response.status === 429 && errBody.error?.code === 'daily_token_limit_exceeded') {
      const limit = errBody.error?.limit
      const contact = ' If you need a higher limit, please contact your administrator to enable a higher plan.'
      throw new Error(
        limit
          ? `You've hit your daily limit of ${limit.toLocaleString('en-US')} tokens. It resets at midnight IST.${contact}`
          : `You've hit your daily token limit. It resets at midnight IST.${contact}`,
      )
    }
    throw new Error(errBody.error?.message || `API error ${response.status}`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let fullText = ''
  // The relay ALWAYS ends a successful stream with `data: [DONE]`; a clean EOF WITHOUT it means the
  // server closed on a mid-stream failure (`_END_FAIL`), i.e. a truncated reply. Track the sentinel
  // so we can reject that partial instead of returning it as if it were complete (F1).
  let sawDone = false
  // F14 — SSE frames are NOT aligned to read() chunks: a `data: [DONE]` (or a delta frame's JSON)
  // can be torn across two reads, and parsing per-chunk read the torn halves as garbage — a false
  // StreamIncompleteError on a stream the server finished cleanly (whose retry double-bills).
  // Carry the trailing partial line across reads and parse only COMPLETE lines.
  let carry = ''

  const handleLine = (line) => {
    if (!line.startsWith('data: ')) return
    const data = line.slice(6)
    if (data === '[DONE]') {
      sawDone = true
      return
    }
    try {
      const parsed = JSON.parse(data)
      const delta = parsed.delta?.text || ''
      if (delta) {
        fullText += delta
        onChunk?.(delta, fullText)
      }
    } catch {
      // skip malformed SSE lines
    }
  }

  try {
    while (true) {
      // The idle watchdog wraps the read: a `: ping` keepalive line resets the timer at THIS byte
      // (it is skipped by the `startsWith('data: ')` filter, which is fine — the byte already
      // proved the socket alive here), so only a truly dead socket ever trips the stall. On a
      // stall the underlying request is aborted too (F9, via `stallOut`) — the server must see
      // the disconnect, not keep streaming into a socket nobody reads.
      const { done, value } = await withTimeout(reader.read(), STREAM_STALL_TIMEOUT_MS, stallOut)
      if (done) break
      // `stream: true` holds a split multi-byte character across reads, exactly as `carry`
      // holds a split line.
      const lines = (carry + decoder.decode(value, { stream: true })).split('\n')
      carry = lines.pop() ?? ''
      for (const line of lines) handleLine(line)
    }
    // Flush the tail: the final frame may arrive without a trailing newline (and the decoder may
    // hold a final partial character) — parse it as one complete line, else a chunk-torn terminal
    // `data: [DONE]` reads as truncation.
    carry += decoder.decode()
    if (carry) handleLine(carry)
  } catch (err) {
    // A STALL is not an abort and not a success: release the dead socket and re-throw so
    // `sendMessage`'s outer catch routes it to the error banner (NOT a silent truncated reply).
    // Checked FIRST so it never falls into the abort-swallow below — the stall itself aborts the
    // controller (F9), so `signal.aborted` alone can no longer discriminate.
    if (err?.name === 'StreamStalledError') {
      reader.cancel().catch(() => {})
      throw err
    }
    // Aborting (logout/unmount) mid-stream is expected — return what we have. `!stalled` keeps a
    // stall-triggered AbortError (any interleaving) out of this success arm.
    if (!stalled && (err?.name === 'AbortError' || signal?.aborted)) return fullText
    throw err
  }

  // The stream ended cleanly (EOF) but WITHOUT the terminal `[DONE]` — the relay closed on a
  // mid-stream failure and truncated the reply (router.py `_END_FAIL`). Reject the partial so the
  // caller shows the error banner + Regenerate rather than persisting it as a complete answer. A
  // genuine abort took the branch above; only a real server-side truncation reaches here.
  if (!sawDone && !signal?.aborted) throw new StreamIncompleteError()
  return fullText
}

export function useClaudeAPI() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const abortRef = useRef(null)

  // Abort an in-flight stream on unmount (covers logout, which navigates away).
  useEffect(() => () => abortRef.current?.abort(), [])

  /**
   * `conversationId` is REQUIRED by `POST /v1/claude` — absent or non-uuid is a 400
   * (`_required_conversation_id`). The server resolves it to fold the project's
   * description (every kind) and the project's current app code (builder kind) into the
   * system prompt. It is not bookkeeping: without it the turn is rejected, and back when
   * it was optional it silently produced a context-less answer.
   *
   * Every caller persists its user turn BEFORE streaming, so the row exists by the time
   * the server looks it up — that ordering is what makes the FIRST turn of a new chat
   * inherit its project's description and code seed.
   */
  const sendMessage = useCallback(
    async (messages, onChunk, context, conversationId) => {
      setLoading(true)
      setError(null)
      const controller = new AbortController()
      abortRef.current = controller

      try {
        const fullText = await fetchClaudeStream({
          body: {
            model: 'claude-opus-4-7',
            max_tokens: 64000,
            // The old client-side JSX builder prompt was retired (U11). The caller (ChatPage)
            // now passes its own planning/summarize system prompt through context.systemPrompt;
            // the server folds in project context and tolerates an empty string.
            system: context?.systemPrompt || '',
            messages: truncateMessages(messages).map((m) => ({ role: m.role, content: m.content })),
            conversationId,
          },
          onChunk,
          signal: controller.signal,
          // F9 — the stall path aborts its own dead request so the server sees the disconnect.
          abort: () => controller.abort(),
        })
        setLoading(false)
        // A turn completed → server-side usage advanced; nudge the navbar badge.
        notifyUsageChanged()
        return fullText
      } catch (err) {
        setLoading(false)
        // A stall ABORTED the controller itself (F9), so it must be routed to the banner BEFORE
        // the abort-swallow — `controller.signal.aborted` is true for both, but only a genuine
        // navigation/unmount abort is a non-error.
        if (err?.name === 'StreamStalledError') {
          setError(err.message)
          return null
        }
        if (err?.name === 'AbortError' || controller.signal.aborted) return null
        if (err?.code === AUTH_FAILED) {
          // The refresh-failed path already cleared the session, but the
          // refresh-succeeded-then-retry-401 path did not — clear here too so
          // stale tokens can't keep isAuthenticated() passing and trap the user
          // on protected routes until the access token expires on its own.
          clearSession(SIGNOUT_REASONS.EXPIRED)
          navigate('/login')
          return null
        }
        setError(err.message)
        return null
      }
    },
    [navigate],
  )

  // Dismiss the error banner — the caller clears it on chat navigation so a stalled turn's banner
  // (and its "Try again") never lingers onto a DIFFERENT conversation.
  const clearError = useCallback(() => setError(null), [])

  // Abort the in-flight stream (F7 — chat switch). A genuine abort is NOT an error: the reader
  // returns its partial text and `sendMessage` resolves null without touching the banner.
  const abort = useCallback(() => abortRef.current?.abort(), [])

  return { sendMessage, loading, error, clearError, abort }
}
