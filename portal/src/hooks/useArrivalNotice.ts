import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

/**
 * THE ONE SENTENCE A BOUNCE CARRIES, READ ONCE AND THEN SCRUBBED.
 *
 * It rides ROUTER STATE, not the query string. A query survives a copy, a bookmark and a share,
 * and "that application is no longer available" pinned to a shareable `?notice=…` is a sentence
 * about a bounce the next reader never made. Router state travels only on the one navigation that
 * set it — and a `?notice=` would also be copied forward by a list page's own parameter writer,
 * which preserves the keys it does not own, and would then outlive the reload meant to clear it.
 *
 * BUT IT SURVIVES MORE THAN THAT NAVIGATION UNLESS IT IS TAKEN AWAY. React Router keeps this in
 * `window.history.state`, which the browser restores on RELOAD and replays on BACK — so without
 * the replace below, refreshing the list re-announces something the reader dealt with ten minutes
 * ago, and stepping back onto the list later does it again. Reading the sentence into component
 * state and replacing the entry with a stateless one is what makes this a one-shot. The replace
 * cannot loop: the re-run reads a `notice` that is no longer there. It carries `location.search`
 * through verbatim, so the page, size and query a reader arrived with all survive.
 *
 * THE TEXT ARRIVES AFTER ITS REGION, which is why this is an effect rather than a `useState`
 * initialiser: a live region inserted together with its text is missed entirely by several
 * reader-and-browser combinations, so the caller mounts an empty region on every render and the
 * sentence lands inside it a tick later.
 */
export function useArrivalNotice(): { notice: string | null; dismiss: () => void } {
  const navigate = useNavigate()
  const location = useLocation()
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    const carried = (location.state as { notice?: unknown } | null)?.notice
    if (typeof carried !== 'string' || carried.length === 0) return
    setNotice(carried)
    navigate(`${location.pathname}${location.search}`, { replace: true, state: null })
  }, [location.pathname, location.search, location.state, navigate])

  return { notice, dismiss: () => setNotice(null) }
}
