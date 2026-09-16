/**
 * THE LAST `/projects` ADDRESS THIS TAB VISITED — the half of keeping the list's state addressable
 * that the URL alone cannot cover.
 *
 * `page`, `pageSize` and `q` live in the URL, so opening `/projects?page=2&q=ramp`, pressing
 * the browser's own Back, reloading, or pasting the address all land on the same view. What that
 * does NOT cover is the product's OWN "back to the list" controls — the workspace toolbar's back
 * chevron, the navigation's destinations and its brand mark — because none of them IS
 * `/projects`: they are
 * components mounted on a *different* address (`/projects/:id`, `/chat/:id`) that have to name a
 * destination without ever having read the list's own query string themselves. Before this,
 * both hardcoded a bare `/projects`, so leaving a filtered, paged list and pressing either control
 * bounced back to page one with the search cleared — the address bar became addressable in one
 * direction (reading it in) and stayed silent in the other (writing it back out).
 *
 * `AppShell` frames every route, so it is the one place already positioned to WRITE this on every
 * render where the address bar reads `/projects`. `WorkspaceShell`'s back control, the navigation's
 * list entry and its brand link all READ it when constructing a destination — at CLICK time, so a
 * search typed a moment ago is what they land on.
 *
 * A READER WITHOUT A WRITER IS DEAD CODE THAT LOOKS ALIVE: every read still resolves, every test
 * that only exercises a read still passes, and the list silently reopens at page one with the
 * search cleared. That is why the write has a scenario of its own in `AppShell.test.tsx`.
 *
 * `sessionStorage`, for the same reason `chatProjectMemory.ts` gives: this is tab-scoped knowledge
 * about where the citizen was looking, not a record worth keeping past the tab. Storage access is
 * wrapped because it genuinely throws rather than degrading — Safari's private mode on quota, or
 * an embedding that blocks storage access — and the defined meaning of that failure is: no memory,
 * so a route-back link falls back to the bare list it always went to before this existed.
 */

const KEY = 'projectsListSearch'

/** The last `/projects` search string this tab saw (e.g. `?page=2&q=ramp`), or `''` when there is
 *  none to remember — a route-back link appends this straight onto `/projects`. */
export function recallProjectsSearch(): string {
  try {
    return sessionStorage.getItem(KEY) ?? ''
  } catch {
    return ''
  }
}

/** Record the CURRENT `/projects` address's search string. Called only while the address bar is
 *  actually `/projects` — see `AppShell`'s own effect. */
export function rememberProjectsSearch(search: string): void {
  try {
    sessionStorage.setItem(KEY, search)
  } catch {
    // No memory this session. Route-back links fall back to the bare list, exactly as before.
  }
}

/**
 * The projects-list address, carrying whatever page/search/page-size the citizen last had.
 *
 * Two callers assembled `` `/projects${recallProjectsSearch()}` `` independently — the workspace
 * back control and the navbar brand. That is a route name and a concatenation rule spelled twice,
 * and nothing fails if only one is edited. One export instead.
 */
export function projectsListHref(): string {
  return `/projects${recallProjectsSearch()}`
}
