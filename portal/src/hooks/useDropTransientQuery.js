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
 * Guarded per chat id, because the send path may call this on every turn.
 *
 * Both chat pages need this and neither owns it, so it lives here rather than as two
 * byte-identical copies drifting apart. `ChatRoute` reads the query the pages clear — keeping
 * the clear in one place means the concept lives in two files rather than three.
 *
 * @returns {(chatId: string) => void}
 */
export function useDropTransientQuery() {
  const navigate = useNavigate()
  const location = useLocation()
  const cleanedRef = useRef(null)

  return useCallback(
    (chatId) => {
      if (cleanedRef.current === chatId || !location.search) return
      cleanedRef.current = chatId
      navigate(`/chat/${chatId}`, { replace: true, state: location.state })
    },
    [navigate, location.search, location.state],
  )
}
