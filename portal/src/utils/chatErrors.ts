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
 * Why provisioning or saving the project's APP failed.
 *
 * Deliberately separate from `describeSaveFailure`, not merged with it: the same status code
 * carries a different domain meaning on each surface. A 409 on a conversation write means the
 * message already landed; a 409 on provision means the project's app belongs to another user
 * — unrecoverable, and nothing like "could not be saved".
 */
export function describeAppFailure(err: unknown): string {
  if (isConversationGone(err)) return 'This project was deleted. Taking you back to your projects.'
  if (err instanceof ApiError && err.status === 409) return "This project's app belongs to another user, so it can't be updated here."
  if (err instanceof ApiError && err.status === 422) return 'Could not create this app. Reopen the project and try again.'
  return 'Your generated app could not be saved.'
}
