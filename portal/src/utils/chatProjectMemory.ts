/**
 * WHICH PROJECT A CHAT BELONGS TO, remembered per tab so the toolbar row can answer during the
 * one window where nothing else can.
 *
 * THE WINDOW, AND WHY THE URL CANNOT COVER IT. A chat's address is flat and permanent —
 * `/chat/{id}` — and its project is a breadcrumb resolved from the conversation. A brand-new chat
 * opens at `/chat/{id}?projectId=…` and `ChatRoute` rewrites that away the instant the first
 * message lands, which is exactly right for an address meant to be shared. But it means the
 * ordinary case — a reload, a bookmark, a link into an existing chat — is a bare `/chat/{id}`, and
 * for the whole of `GET /conversations/{id}` the row has no project to name and the back control
 * has none to go to. It said "Back to projects" and went there: out of the project the citizen was
 * working in, from a control that was about to say something different a moment later.
 *
 * So the answer the server gave last time is kept, keyed by chat, and stands in until it answers
 * again. It is never preferred over a live answer — `ChatRoute` reads it only while the resolution
 * is still loading and the URL carries nothing — so a chat that MOVED project shows the stale
 * breadcrumb for one fetch and no longer.
 *
 * `sessionStorage`, for the same reasons as the composer draft beside it: this is tab-scoped
 * knowledge about what the citizen is looking at right now, and dying with the tab is the correct
 * lifetime. It also means the memory cannot outlive a sign-out into someone else's session in the
 * way a `localStorage` copy would.
 *
 * Storage access is wrapped because `sessionStorage` genuinely throws rather than degrading —
 * Safari's private mode on quota, and any embedding that blocks storage access. The defined
 * meaning of that failure here is: no memory, so the row falls back to the neutral shape it had
 * before this existed. Nothing else is affected. The documented-optional case in
 * `.claude/rules/fail-first.md`, not a swallowed error.
 */

const key = (chatId: string): string => `chatProject:${chatId}`

/** The project this tab last saw the chat belong to, or `null` when it has never seen it. */
export function recallChatProject(chatId: string | null | undefined): string | null {
  if (!chatId) return null
  try {
    return sessionStorage.getItem(key(chatId))
  } catch {
    return null
  }
}

/** Record what the server (or the minting navigation) said, so the next load window can answer. */
export function rememberChatProject(
  chatId: string | null | undefined,
  projectId: string | null,
): void {
  if (!chatId || !projectId) return
  try {
    sessionStorage.setItem(key(chatId), projectId)
  } catch {
    // No memory this session. The row keeps the neutral shape during the load window.
  }
}
