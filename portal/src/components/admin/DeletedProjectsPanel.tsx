/**
 * DELETED PROJECTS — the reader the tombstone did not have (#176).
 *
 * Deleting a project requires a written reason: 5 to 50 words, and the confirm button will not
 * arm without one (#158 §13.2). Until this panel existed, nothing could retrieve what people
 * wrote — every citizen was asked to justify destroying their own work into a table with no
 * reader. That is the shape `build_sessions/router.py` names as a defect ("no reader, no
 * retention, no runbook"), and this closes it.
 *
 * READ-ONLY, DELIBERATELY. There is nothing to act on here: the project, its app, its database
 * and its chats are already gone, and the row is a record ABOUT that, not a thing that can be
 * restored from. A control on this screen would imply otherwise.
 *
 * WHAT THE ROW SEPARATES, and why it matters more here than anywhere else: `deletedByName` is
 * a label, `deletedBy` is the account. Both are stamped server-side so they cannot disagree —
 * but an administrator reading this to answer "who deleted this" is relying on that being an
 * identity rather than something a browser typed, which it briefly was.
 *
 * Paging and search come from `useKeysetList`, the same hook `UsersLimitsPanel` uses, so an
 * empty list is read from `appliedQuery` (what the rows answer) rather than `q` (which runs
 * 300ms ahead of them) — otherwise a fresh search flashes "no deletions yet" at an admin whose
 * console is full of them.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Eye, Search, Trash2 } from 'lucide-react'
import { useKeysetList } from '../../hooks/useKeysetList'
import {
  fetchDeletedProjects,
  fetchDeletionsAudit,
  type DeletedProjectRow,
  type DeletionsAuditEvent,
} from '../../utils/admin'

/** The longest `q` the server accepts (`MAX_SEARCH_Q`). Enforced on the input too, so the most
 *  reachable way to fail a search — pasting something long — cannot happen at all. */
const MAX_SEARCH_CHARS = 200

export interface DeletedProjectsPanelProps {
  /** The console's shared toast channel. Matches `AdminPage`'s `ToastSeverity` exactly —
   *  `'problem'`, not `'error'`, which is the value the banner actually styles on. */
  onToast?: (message: string, severity?: 'ok' | 'problem') => void
}

/** "3 chats · an app · a database", or "nothing else" — what went with the project.
 *  Assembled rather than templated so a project with no children reads as a sentence
 *  instead of "0 chats, no app, no database". */
function whatWentWithIt(row: DeletedProjectRow): string {
  const parts: string[] = []
  if (row.chatsDeleted > 0) parts.push(`${row.chatsDeleted} chat${row.chatsDeleted === 1 ? '' : 's'}`)
  if (row.hadApp) parts.push('an app')
  if (row.hadDatabase) parts.push('a database')
  return parts.length === 0 ? 'nothing else' : parts.join(' · ')
}

function formatWhen(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime())
    ? '—'
    : at.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

/**
 * WHO HAS READ THIS LOG. Cross-owner reading is defensible here because it is recorded — and
 * until this strip existed the recording was unreachable, which made it a claim rather than a
 * control. Collapsed by default: it is the answer to a question an administrator asks
 * occasionally, not part of reading a deletion.
 */
function WhoHasRead(): React.JSX.Element {
  const [open, setOpen] = useState(false)
  const [events, setEvents] = useState<DeletionsAuditEvent[] | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (!open || events !== null) return
    const controller = new AbortController()
    fetchDeletionsAudit({ signal: controller.signal })
      .then(setEvents)
      .catch((caught: unknown) => {
        // An aborted request is the panel being closed, not a failure to report.
        if ((caught as Error)?.name !== 'AbortError') setFailed(true)
      })
    return () => controller.abort()
  }, [open, events])

  return (
    <div className="rounded-xl border border-bial-border bg-white">
      <button
        type="button"
        onClick={() => setOpen((was) => !was)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-xs font-semibold text-tertiary"
      >
        <Eye size={14} className="text-neutral" />
        Who has read this log
      </button>
      {open && (
        <div className="border-t border-bial-border px-4 py-3">
          {failed ? (
            <p className="text-xs text-danger">Couldn’t load the read log.</p>
          ) : events === null ? (
            <p className="text-xs text-neutral">Loading…</p>
          ) : events.length === 0 ? (
            <p className="text-xs text-neutral">Nobody has read this log yet.</p>
          ) : (
            <ul className="space-y-1.5">
              {events.map((event) => (
                <li key={event.id} className="text-xs text-neutral">
                  <span className="font-semibold text-tertiary">
                    {event.username ?? 'A deleted account'}
                  </span>
                  {' · '}
                  {formatWhen(event.createdAt)}
                  {' · '}
                  {/* The term itself is never stored — `audit.py` forbids record contents in the
                      blob — so this reports THAT a search happened and how much it returned. */}
                  {event.detail?.filtered ? 'searched' : 'read the whole log'}
                  {typeof event.count === 'number' ? ` · ${event.count} shown` : ''}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

export default function DeletedProjectsPanel({
  onToast,
}: DeletedProjectsPanelProps): React.JSX.Element {
  // THE DATE RANGE #176 ASKED FOR ("filters worth having: by owner, and by date range" — the
  // owner half is `q`). Held here rather than in `useKeysetList`, whose `fetchPage` contract is
  // `{cursor, q, limit}`; the values are read through a ref so `fetchPage` can stay a stable
  // callback, the same technique `UsersLimitsPanel` uses for its abort controller.
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const rangeRef = useRef({ from: '', to: '' })
  rangeRef.current = { from, to }

  // ONE CONTROLLER FOR THE PANEL'S LIFETIME, aborted on unmount — which is what switching tabs
  // does. Without it a superseded read still completes AND still commits an audit row for a
  // page nobody saw, so the accountability table ends up describing reads that never happened.
  const abortRef = useRef<AbortController | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    abortRef.current = controller
    return () => controller.abort()
  }, [])

  const fetchPage = useCallback(
    async ({ cursor, q, limit }: { cursor: string | null; q: string; limit: number }) => {
      const page = await fetchDeletedProjects({
        cursor,
        q,
        limit,
        deletedFrom: rangeRef.current.from || null,
        // INCLUSIVE TO THE END OF THE CHOSEN DAY. `<input type="date">` yields `2026-08-15`,
        // which parses as that day's midnight — so an admin picking the 15th as the upper bound
        // would otherwise see nothing deleted during the 15th itself.
        deletedTo: rangeRef.current.to ? `${rangeRef.current.to}T23:59:59.999999+00:00` : null,
        signal: abortRef.current?.signal,
      })
      // The hook wants `{items, nextCursor, hasMore}`; the envelope names its rows
      // `deletions`, so the mapping happens here rather than reshaping the API.
      return { ...page, items: page.deletions }
    },
    [],
  )

  const { items, q, appliedQuery, loading, hasMore, error, loadMore, setQuery, refresh } = useKeysetList<
    DeletedProjectRow,
    { items: DeletedProjectRow[]; deletions: DeletedProjectRow[]; nextCursor: string | null; hasMore: boolean }
  >({ fetchPage })

  // THE FIRST PAGE. `useKeysetList` deliberately does not fetch on mount — it fetches on
  // `loadMore`, `setQuery` or `refresh` — so the initial load is the caller's to start.
  //
  // `appliedQuery === null` means nothing has landed yet, and it stops being null the moment
  // the first page resolves, so this fires exactly once. `error` is in the guard for the case
  // that would otherwise be a tight retry loop: a failed first page leaves `appliedQuery`
  // null, and without this the effect would immediately ask again, forever.
  //
  // NOT the chained `loadMore` that `UsersLimitsPanel` runs — that panel pulls the whole
  // roster in so TanStack can sort it client-side. This list is server-paged and the reader
  // asks for more explicitly.
  useEffect(() => {
    if (appliedQuery === null && !loading && !error) loadMore()
  }, [appliedQuery, loading, error, loadMore])

  // A COLD-START failure has no rows to preserve, so it takes the whole panel and offers a
  // real retry. A later page's failure is handled underneath the rows instead — see below.
  //
  // `appliedQuery === null` IS LOAD-BEARING, not a tidier spelling of the same condition.
  // Without it this branch also caught a failed SEARCH: `useKeysetList` clears `items` inside
  // the debounce BEFORE fetching, so any failing search lands on `items.length === 0` — and
  // this return does not render the search box, which lives in the other one. The admin was
  // left with a single "Try again" that re-issues `runFetch(null, qRef.current)`, i.e. the same
  // failing query for ever, with no rendered input to edit or clear it in. The only escape was
  // leaving the tab. Gated on `appliedQuery` it fires only before anything has ever loaded,
  // which is the case that genuinely has nothing to preserve — and it agrees with the mount
  // effect above, which keys off exactly the same value.
  if (error && items.length === 0 && appliedQuery === null) {
    return (
      <div className="rounded-xl border border-bial-border bg-white p-6">
        <p className="text-sm text-danger">{error.message}</p>
        <button
          type="button"
          onClick={() => {
            onToast?.('Retrying…')
            refresh()
          }}
          className="mt-2 text-xs font-semibold text-primary hover:underline"
        >
          Try again
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 max-w-md">
          <Search
            size={15}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-neutral"
          />
          {/* `maxLength` is the server's own ceiling. Enforced here as well so the most
              reachable way to fail a search — pasting something long — cannot be typed at
              all, rather than being answered with an error to recover from. */}
          <input
            value={q}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search deletions"
            placeholder="Search by project, person, or reason…"
            maxLength={MAX_SEARCH_CHARS}
            className="w-full rounded-xl border border-bial-border py-2 pl-9 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
          />
        </div>

        {/* THE DATE RANGE. Changing either bound must invalidate the accumulated cursor and
            rows exactly as changing the query does — a cursor from a different filter is
            meaningless. `setQuery(q)` with the value UNCHANGED is how: it re-enters
            `useKeysetList`'s debounce path, which rewinds the cursor, clears the rows and
            refetches. It reads like a no-op and is not one. */}
        <label className="flex items-center gap-1.5 text-xs text-neutral">
          From
          <input
            type="date"
            value={from}
            onChange={(e) => {
              setFrom(e.target.value)
              setQuery(q)
            }}
            aria-label="Deleted on or after"
            className="rounded-lg border border-bial-border px-2 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-primary/30"
          />
        </label>
        <label className="flex items-center gap-1.5 text-xs text-neutral">
          To
          <input
            type="date"
            value={to}
            onChange={(e) => {
              setTo(e.target.value)
              setQuery(q)
            }}
            aria-label="Deleted on or before"
            className="rounded-lg border border-bial-border px-2 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-primary/30"
          />
        </label>
      </div>

      <WhoHasRead />

      {/* `appliedQuery !== null` keeps this off the screen before the first fetch has landed.
          The mount effect runs AFTER the first commit, so without it there is a real frame in
          which an administrator with a full log is told nothing has ever been deleted. */}
      {items.length === 0 && !loading && appliedQuery !== null ? (
        <div className="rounded-xl border border-bial-border bg-white p-8 text-center">
          <Trash2 size={20} className="mx-auto text-neutral/50" />
          {/* Read from `appliedQuery`, never `q`: the two disagree for the length of the
              debounce, and deciding on `q` tells an admin mid-keystroke that nothing has
              ever been deleted. */}
          <p className="mt-2 text-sm text-neutral">
            {appliedQuery
              ? `No deletions match “${appliedQuery}”.`
              : 'No projects have been deleted yet.'}
          </p>
        </div>
      ) : (
        <ul className="space-y-3">
          {items.map((row) => (
            <li key={row.id} className="rounded-xl border border-bial-border bg-white p-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h3 className="text-sm font-bold text-tertiary break-words">{row.projectName}</h3>
                <span className="text-xs text-neutral tabular-nums">{formatWhen(row.deletedAt)}</span>
              </div>

              <p className="mt-1 text-xs text-neutral">
                Deleted by <span className="font-semibold text-tertiary">{row.deletedByName}</span>
                {' · owned by '}
                <span className="break-words">{row.ownerEmail}</span>
              </p>

              {/* The reason, given the weight it was collected with. It is the only part of
                  this row a person actually wrote.

                  `whitespace-pre-wrap break-words` because it is the one field on this screen
                  that MUST survive intact. The input is a resize-y textarea, so newlines are in
                  scope and without `pre-wrap` a multi-line reason collapses to a run-on. And the
                  word counter gating it counts `value.split()`, so a single ~1990-character
                  token passes both the 50-word bound and the 2000-char backstop — inside a card
                  the page sets `overflow-hidden` on, that clipped away silently: no scrollbar,
                  no title, no signal to the admin that anything was missing. The pattern is
                  `AppRegistryPanel`'s, used on citizen free text for exactly this reason. */}
              <blockquote className="mt-2 border-l-2 border-bial-border pl-3 text-sm text-tertiary whitespace-pre-wrap break-words">
                {row.remark}
              </blockquote>

              <p className="mt-2 text-[11px] text-neutral">Went with it: {whatWentWithIt(row)}</p>
            </li>
          ))}
        </ul>
      )}

      {/* Any failure that is not a cold start says so HERE, underneath the search row, rather
          than replacing a useful screen with an error card.

          NOT gated on `items.length > 0` any more, and that matters: a failed SEARCH clears the
          rows inside the debounce before fetching, so gating on rows meant the one case that
          most needed an explanation — the search that just failed and emptied the list — was the
          one that rendered none. The admin saw an empty list and no reason for it. The cold
          start is already handled by the early return above, so reaching here at all means there
          is a screen worth keeping. */}
      {error && (
        <p role="alert" className="text-xs font-medium text-danger">
          {error.message}
        </p>
      )}

      {/* THE STALE-CURSOR GUARD. `loadMore` reads `cursorRef`/`qRef` directly, with no awareness
          of a pending debounce: `setQuery` updates `qRef` synchronously but only rewinds the
          cursor 300ms later, so a click inside that window sends the OLD filter's cursor under
          the NEW query text and appends a slab sliced from the wrong position. The screen
          self-heals when the debounce lands; the audit row does not, and on a surface justified
          by "reading is recorded" the permanent record ends up misdescribing what was read.
          `UsersLimitsPanel` installs the same gate against the same hazard — with only the
          `appliedQuery !== q` half, because its button lives inside an error banner that cannot
          coexist with `loading`. This pager is always visible, so `loading` is reachable here
          and both halves are needed. */}
      {hasMore && (
        <button
          type="button"
          onClick={loadMore}
          disabled={loading || appliedQuery !== q}
          title={
            appliedQuery !== q
              ? 'A new search is in progress — this re-enables once it lands.'
              : undefined
          }
          className="w-full rounded-xl border border-bial-border py-2 text-sm font-semibold text-tertiary transition hover:bg-bial-bg disabled:opacity-50"
        >
          {loading ? 'Loading…' : 'Load more'}
        </button>
      )}
    </div>
  )
}
