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
 * Both pages need this and neither owns it, so it lives here rather than as two byte-identical
 * copies drifting apart.
 *
 * @returns {(chatId: string) => void}
 */
export function useDropTransientQuery() {
  const navigate = useNavigate()
  const location = useLocation()
  const cleanedRef = useRef(null)

  // The latest location, readable from inside a stale closure.
  const locationRef = useRef(location)
  locationRef.current = location

  return useCallback(
    (chatId) => {
      const current = locationRef.current
      if (cleanedRef.current === chatId || !current.search) return
      // The user navigated away while the append was in flight. Their URL is not ours to rewrite.
      if (current.pathname !== `/chat/${chatId}`) return
      cleanedRef.current = chatId
      navigate(`/chat/${chatId}`, { replace: true, state: current.state })
    },
    [navigate],
  )
}
