/**
 * User-feedback submission. Thin authenticated wrapper over POST /api/feedback
 * (mirrors src/utils/admin.js). Throws an Error with a user-ready message on
 * failure so the modal can surface it inline. Dependencies are injected so it's
 * testable without a real token/network.
 */
import { authFetch } from './api'
import type { AuthFetchDeps } from './api'
import { isRecord } from './apiError'

/**
 * POST a feedback message + the page it was sent from. Resolves to the JSON
 * body — untyped (`unknown`) since FeedbackModal never reads it; the call is
 * awaited only for its throw-on-failure side effect.
 *
 * NOTE (types-only migration): this hand-rolls `body.error?.message` rather
 * than the shared `readApiError` (`apiError.ts`) every already-converted
 * caller (e.g. `projectApi.ts`) uses — that reads two more error envelope
 * shapes (`{"detail": …}`, FastAPI-native 422 `detail[]`) this doesn't.
 * Flagged as a follow-up for Rahul.
 *
 * NOT byte-identical to pre-migration behavior (correction: an earlier
 * version of this comment claimed it was). Pre-migration, `body.error?.message`
 * dereferenced `body` unguarded before the `?.` — the optional chain starts
 * at `.error`, not at `body` itself — so a `res.json()` that resolved to the
 * literal value `null` (a valid JSON body; `.catch(() => ({}))` never fires
 * for it) threw `TypeError: Cannot read properties of null (reading 'error')`
 * out of this function instead of the intended user-ready Error. The
 * `isRecord(body)` guard added here for typing purposes changes that: a
 * `null` body now falls through cleanly to the generic
 * `Failed to submit feedback (…)` fallback instead of throwing a TypeError.
 * That's a real behavior change, and an improvement, not a preserved
 * pre-existing bug — but it does mean this file now diverges from its
 * sibling `attachmentApi.ts`, which still has the unguarded
 * `err.error?.message` and would still throw on the same input. Left
 * un-synced here; worth its own follow-up rather than folding into this PR.
 */
export async function submitFeedback(message: string, page: string, deps: AuthFetchDeps = {}): Promise<unknown> {
  const res = await authFetch(
    '/api/feedback',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, page }),
    },
    deps,
  )
  if (!res.ok) {
    const body: unknown = await res.json().catch(() => ({}))
    const errorMessage = isRecord(body) && isRecord(body.error) && typeof body.error.message === 'string' ? body.error.message : null
    throw new Error(errorMessage || `Failed to submit feedback (${res.status}).`)
  }
  return res.json()
}
