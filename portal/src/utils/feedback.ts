/**
 * User-feedback submission. Thin authenticated wrapper over POST /api/feedback
 * (mirrors src/utils/admin.js). Throws an Error with a user-ready message on
 * failure so the modal can surface it inline. Dependencies are injected so it's
 * testable without a real token/network.
 */
import { authFetch } from './api.js'
import { isRecord } from './apiError'

type AuthFetchDeps = NonNullable<Parameters<typeof authFetch>[2]>

/**
 * POST a feedback message + the page it was sent from. Resolves to the JSON
 * body — untyped (`unknown`) since FeedbackModal never reads it; the call is
 * awaited only for its throw-on-failure side effect.
 *
 * NOTE (types-only migration): this hand-rolls `body.error?.message` rather
 * than the shared `readApiError` (`apiError.ts`) every already-converted
 * caller (e.g. `projectApi.ts`) uses — that reads two more error envelope
 * shapes (`{"detail": …}`, FastAPI-native 422 `detail[]`) this doesn't. Kept
 * byte-identical to pre-migration behavior on purpose; switching would change
 * the user-visible error text on some status codes, which is out of scope for
 * a types-only diff. Flagged as a follow-up for Rahul.
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
