/**
 * The one place that reads a backend error message.
 *
 * The control-plane emits THREE error envelopes and deliberately does not unify
 * them, so every caller that does `body.error?.message || fallback` is blind to
 * two thirds of them — including the `{"detail": …}` shape used by 401, the
 * 403 suspension gate, the 403 super-admin gate, and 500:
 *
 *   1. `{"error": {"message", "code"?}}`      most domains, pagination 422s,
 *                                             rate limits, the daily-token 429
 *   2. `{"detail": "string"}`                 401, 403 "Account suspended",
 *                                             403 "Super-admin privileges required.", 500
 *   3. `{"detail": [{type, loc, msg}]}`       FastAPI-native 422 from Pydantic
 *                                             body validation
 *
 * Bodies arrive as `unknown` (they are untrusted network input) and are narrowed
 * with type guards — never cast, never `any`.
 */

/** An HTTP failure carrying the status and the backend's own error code, so callers can branch on 409 / 429 / 503 without re-parsing the body. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string | null
  /** The whole `error` object the backend sent, for the codes that carry more than a message.
   *  `sandbox_reclaim_blocked` is the first: its `projectId`/`projectName`/`dirty` are what let
   *  the client name the project holding the workspace and offer to save it. Reading them off
   *  the error keeps the branch in one place instead of re-fetching the body at each call
   *  site. `null` when the response carried no structured error. */
  readonly details: Record<string, unknown> | null

  constructor(
    message: string,
    status: number,
    code: string | null = null,
    details: Record<string, unknown> | null = null,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

/** A JSON object (not an array, not null) — the shape every envelope guard starts from. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * A nullable string field off an untrusted body: absent, null, or the wrong type all
 * read as `null`.
 *
 * Lives here beside `isRecord` because it is the same kind of thing — the narrowing
 * every typed client starts from — and because two clients had written it identically,
 * under the same name, in the same feature. A field that is genuinely REQUIRED does not
 * use this: it throws at its own boundary (`readString`), because a missing required
 * field is a server contract break, not an absent value.
 */
export function optionalString(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0
}

/** Flatten a FastAPI/Pydantic `detail[]` into one human sentence, or null when it carries nothing readable. */
function flattenValidationDetail(detail: readonly unknown[]): string | null {
  const messages = detail.filter(isRecord).reduce<string[]>((acc, entry) => {
    if (nonEmptyString(entry.msg)) acc.push(entry.msg)
    return acc
  }, [])
  return messages.length > 0 ? messages.join('; ') : null
}

/**
 * The message to show a user, whichever envelope the backend chose.
 * Resolution order: `error.message` → `detail[].msg` → `detail` (string) → `` `${fallback} (${status}).` ``
 */
export function extractApiMessage(body: unknown, status: number, fallback: string): string {
  const fallbackMessage = `${fallback} (${status}).`
  if (!isRecord(body)) return fallbackMessage

  const { error, detail } = body

  if (isRecord(error) && nonEmptyString(error.message)) return error.message
  if (Array.isArray(detail)) return flattenValidationDetail(detail) ?? fallbackMessage
  if (nonEmptyString(detail)) return detail

  return fallbackMessage
}

/** The backend's machine-readable error code (`daily_token_limit_exceeded`, …), or null. Only envelope 1 carries one. */
export function extractApiCode(body: unknown): string | null {
  if (!isRecord(body)) return null
  const { error } = body
  return isRecord(error) && nonEmptyString(error.code) ? error.code : null
}

/**
 * True only for the mid-session suspension gate.
 *
 * Matching on `403` alone would also swallow CSRF failures and the super-admin
 * gate, both of which the caller must handle itself. The exact-copy match is
 * fragile by nature; it is the contract we have, and it is pinned by a test so a
 * backend copy change fails loudly in CI rather than degrading in production.
 */
export function isSuspended(body: unknown, status: number): boolean {
  return status === 403 && isRecord(body) && body.detail === 'Account suspended'
}

/** Read a non-2xx `Response` into an `ApiError`. A body that is not JSON degrades to the fallback message. */
export async function readApiError(res: Response, fallback: string): Promise<ApiError> {
  const body: unknown = await res.json().catch(() => null)
  const error = isRecord(body) && isRecord(body.error) ? body.error : null
  return new ApiError(
    extractApiMessage(body, res.status, fallback),
    res.status,
    extractApiCode(body),
    error,
  )
}
