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
import { useCallback, useEffect } from 'react'
import { Search, Trash2 } from 'lucide-react'
import { useKeysetList } from '../../hooks/useKeysetList'
import { fetchDeletedProjects, type DeletedProjectRow } from '../../utils/admin'

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

export default function DeletedProjectsPanel({
  onToast,
}: DeletedProjectsPanelProps): React.JSX.Element {
  const fetchPage = useCallback(
    async ({ cursor, q, limit }: { cursor: string | null; q: string; limit: number }) => {
      const page = await fetchDeletedProjects({ cursor, q, limit })
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

  // A FIRST-PAGE failure has no rows to preserve, so it takes the whole panel and offers a
  // real retry. A later page's failure is handled underneath the rows instead — see below.
  if (error && items.length === 0) {
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
      <div className="flex items-center gap-3">
        <div className="relative flex-1 max-w-md">
          <Search
            size={15}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-neutral"
          />
          <input
            value={q}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search deletions"
            placeholder="Search by project, person, or reason…"
            className="w-full rounded-xl border border-bial-border py-2 pl-9 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
          />
        </div>
      </div>

      {items.length === 0 && !loading ? (
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
                <h3 className="text-sm font-bold text-tertiary">{row.projectName}</h3>
                <span className="text-xs text-neutral tabular-nums">{formatWhen(row.deletedAt)}</span>
              </div>

              <p className="mt-1 text-xs text-neutral">
                Deleted by <span className="font-semibold text-tertiary">{row.deletedByName}</span>
                {' · owned by '}
                {row.ownerEmail}
              </p>

              {/* The reason, given the weight it was collected with. It is the only part of
                  this row a person actually wrote. */}
              <blockquote className="mt-2 border-l-2 border-bial-border pl-3 text-sm text-tertiary">
                {row.remark}
              </blockquote>

              <p className="mt-2 text-[11px] text-neutral">Went with it: {whatWentWithIt(row)}</p>
            </li>
          ))}
        </ul>
      )}

      {/* A page-two failure leaves the rows already on screen intact and says so underneath,
          rather than replacing a useful list with an error. */}
      {error && items.length > 0 && (
        <p role="alert" className="text-xs font-medium text-danger">
          {error.message}
        </p>
      )}

      {hasMore && (
        <button
          type="button"
          onClick={loadMore}
          disabled={loading}
          className="w-full rounded-xl border border-bial-border py-2 text-sm font-semibold text-tertiary transition hover:bg-bial-bg disabled:opacity-50"
        >
          {loading ? 'Loading…' : 'Load more'}
        </button>
      )}
    </div>
  )
}
