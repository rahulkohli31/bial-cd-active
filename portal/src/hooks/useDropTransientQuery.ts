import { useCallback, useRef } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

/**
 * Drop the transient `?projectId=&kind=` query once a chat's row exists.
 *
 * A brand-new chat opens at `/chat/{clientMintedId}?projectId=…&kind=…` because the
 * conversation row does not exist until its first message is appended — there is no
 * header-only create endpoint — and the path alone cannot say which project it belongs to.
 * The query is the only carrier that survives a reload and a cold open. Once the first append
 * creates the row, `conversation.projectId` is authoritative and the query is dead weight, so
 * we rewrite to the flat `/chat/{id}` and the address the user copies is the canonical one.
 *
 * TWO GUARDS, both load-bearing:
 *  - Once per chat id, because the send path calls this on every turn.
 *  - Only while that chat is still the one on screen. This runs AFTER an `await` (the user-turn
 *    persist), so by the time it fires the user may have navigated elsewhere; a `navigate()`
 *    built from the render-time location would snap them back to the chat they left.
 *
 * AND IT DROPS THE ROUTER STATE WITH THE QUERY, because both are the same thing: a one-shot
 * hand-off from the project composer (`{prompt, mode, theme, pendingAttachments}`), consumed at
 * mount and worthless afterwards. Carrying it forward is not harmless — it is N1. Both pages call
 * `window.history.replaceState` before firing the hand-off, but that rewrites the browser entry
 * WITHOUT emitting a popstate, so react-router's in-memory `location.state` survives it; this
 * hook then wrote that survivor straight back into history. One reload later the prompt was
 * still there, the fire-once ref had died with the mount, and the opening turn ran a second time
 * — billed again, on a thread the user was only re-reading.
 *
 * Nothing needs it to survive: `initialPrompt`, `theme`, `uploadedFiles` and `mode` are all read
 * at MOUNT, `pendingAttachments` is read inside `fireHandoffPrompt` which runs BEFORE this, and
 * after a reload the server's saved header is authoritative for every one of them.
 *
 * Both pages need this and neither owns it, so it lives here rather than as two byte-identical
 * copies drifting apart.
 *
 */
export function useDropTransientQuery(): (chatId: string) => void {
  const navigate = useNavigate()
  const location = useLocation()
  const cleanedRef = useRef<string | null>(null)

  // The latest location, readable from inside a stale closure.
  const locationRef = useRef(location)
  locationRef.current = location

  return useCallback(
    (chatId: string) => {
      const current = locationRef.current
      if (cleanedRef.current === chatId || !current.search) return
      // The user navigated away while the append was in flight. Their URL is not ours to rewrite.
      if (current.pathname !== `/chat/${chatId}`) return
      cleanedRef.current = chatId
      navigate(`/chat/${chatId}`, { replace: true, state: null })
    },
    [navigate],
  )
}
