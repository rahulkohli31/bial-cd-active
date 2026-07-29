/**
 * The composer draft, per conversation (G3).
 *
 * Under the mode-free composer contract the user is invited to keep typing while the assistant
 * works — so the text they type has to survive the three things that used to destroy it: a
 * reload, a chat switch, and a refinement chip. Two of those are this module's job.
 *
 * SEMANTICS, stated because they are user-visible:
 *  - `sessionStorage`, not `localStorage`: a draft is tab-scoped work-in-progress, and dying with
 *    the tab is the correct lifetime. `localStorage` would accumulate every abandoned half-thought
 *    forever, across every conversation the user ever opened.
 *  - Keyed per conversation, so switching chats shows each one its own draft rather than leaking
 *    A's text into B.
 *  - LAST WRITER WINS across tabs. Two tabs on one conversation share the key and the later write
 *    stands. Accepted deliberately: the alternative is a merge UI for a text box.
 *  - Cleared on a SUCCESSFUL send, never on failure — a failed send is exactly when the text is
 *    worth most, and an uncleared draft would otherwise re-populate the composer with the message
 *    the user just sent, which is easy to send twice by accident.
 *
 * Storage access is wrapped because `sessionStorage` genuinely throws rather than degrading —
 * Safari's private mode on quota, and any embedding that blocks storage access. Losing a draft is
 * not worth taking the chat down with it, so the failure has a defined meaning here: no
 * persistence, everything else unaffected. This is the documented-optional case in
 * `.claude/rules/fail-first.md`, not a swallowed error.
 */

const key = (conversationId) => `draft:${conversationId}`

/**
 * @param {string | null | undefined} conversationId
 * @returns {string} the saved draft, or `''` when there is none (or storage is unavailable)
 */
export function readDraft(conversationId) {
  if (!conversationId) return ''
  try {
    return sessionStorage.getItem(key(conversationId)) ?? ''
  } catch {
    return ''
  }
}

/**
 * @param {string | null | undefined} conversationId
 * @param {string} text
 */
export function writeDraft(conversationId, text) {
  if (!conversationId) return
  try {
    if (text) sessionStorage.setItem(key(conversationId), text)
    else sessionStorage.removeItem(key(conversationId))
  } catch {
    // No persistence this session. The composer still holds the text in React state.
  }
}

/** @param {string | null | undefined} conversationId */
export function clearDraft(conversationId) {
  writeDraft(conversationId, '')
}
