/**
 * How a chat reads a failed conversation write.
 *
 * "Could not save your message" is the right thing to say about a network blip and the
 * wrong thing to say about a project someone deleted in another tab — the first invites
 * a retry, the second makes retrying impossible. Both used to render identically.
 */
import { ApiError } from './apiError'

/**
 * The conversation (or the project it hangs off) no longer exists server-side. Retrying
 * cannot help; the chat must leave. Distinct from a save failure the user can recover from.
 */
export function isConversationGone(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404
}

/**
 * The append route's TRANSIENT 409: a concurrent writer took the seq and NOTHING was stored.
 * Retrying is the correct response.
 *
 * Keyed on the server's `code`, never on the bare status. The route has two 409s that mean
 * opposite things, and telling them apart matters more than it looks: read this one as the
 * permanent "already saved" and we tell the user their message landed when it did not, then
 * send them to reload — which throws away the very text the retry needed.
 */
function isSeqConflict(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && err.code === 'message_seq_conflict'
}

/**
 * The user's turn already landed — a duplicate `message._id`, not a lost write. PERMANENT:
 * retrying cannot help. Any 409 WITHOUT the transient code is this one.
 */
function isDuplicateMessage(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && !isSeqConflict(err)
}

/**
 * User-facing copy for a failed append/patch.
 *
 * A `400` is the server telling us the request itself was malformed — most often a
 * missing or unowned `header.projectId`, which is a client bug worth showing verbatim
 * rather than hiding behind "check your connection".
 */
export function describeSaveFailure(err: unknown, fallback = 'Could not save your message. Check your connection and try again.'): string {
  if (isConversationGone(err)) return 'This project was deleted. Taking you back to your projects.'
  // Order matters: the transient conflict is a 409 too, and it is the opposite advice.
  if (isSeqConflict(err)) return 'Your message didn’t save — send it again.'
  if (isDuplicateMessage(err)) return 'That message was already saved. Reload to see the latest transcript.'
  if (err instanceof ApiError && err.status === 400) return err.message
  return fallback
}

/**
 * Why a mode switch failed.
 *
 * This whole surface used to be one hardcoded string — "Finish the current step before
 * switching modes" — mapped onto EVERY rejection, because the catch was written believing a
 * race past the disabled pill was the only way to reach it. That is true of the server's 409
 * and false of everything else, and the failure it lied about most is the one that stranded
 * people: an expired session is not a step, and finishing nothing fixes it. Combined with a
 * thread already in Write, the false advice produced a chat with no escape at all (N12) —
 * the user could neither send nor switch out, and the only message on screen named a cause
 * that did not exist.
 *
 * Narrowest-case-first, per `.claude/rules/fail-first.md`.
 */
export function describeModeSwitchFailure(err: unknown): string {
  // The ONE case the original copy is true for. The server stamps the running turn's rows
  // with the conversation's mode, so a mid-turn switch would retroactively mislabel work
  // already in flight — it answers 409 and the pill is disabled to match (KTD-4).
  if (err instanceof ApiError && err.status === 409) return 'Finish the current step before switching modes.'
  // 401 = the session lapsed; 403 = its CSRF token did. Both are "your session", and both are
  // fixed by reloading rather than by waiting.
  if (err instanceof ApiError && (err.status === 401 || err.status === 403))
    return 'Your session expired. Reload the page to sign back in.'
  if (isConversationGone(err)) return 'This chat no longer exists. Taking you back to your projects.'
  return 'Could not switch modes. Please try again.'
}
