/**
 * Share a project with a colleague (#198 R1-R7, R12, R28) — a dialog reached from the
 * project's own workspace, never a route of its own.
 *
 * THE COLLEAGUE SEARCH HAS FOUR NAMED STATES (R28), never a raw error code on screen:
 * below 3 characters, in flight, no matches, and rate-limited. All four render as a plain
 * sentence under the search box, the same `role="status"` treatment `ProjectsPage`'s own
 * wait region uses, so a screen reader hears each one land.
 *
 * THE GRANT IS "CAN USE", NEVER "VIEW ONLY" (R6, Key Decision 3) — a colleague who opens a
 * shared project can interact with the real, running app, and anything they enter is saved
 * into the project's actual data (R7). Both sentences are said here, once, rather than left
 * for the recipient to discover after the fact.
 */
import { useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog'
import { BusyGlyph } from '../ui/Waiting'
import { ApiError } from '../../utils/apiError'
import {
  listProjectShares,
  searchColleagues,
  shareProject,
  unshareProject,
  type Colleague,
  type ProjectShare,
} from '../../utils/sharingApi'

const MIN_QUERY_CHARS = 3
const DEBOUNCE_MS = 300

export interface SharePanelProps {
  projectId: string
  projectName: string
  onClose: () => void
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback
}

export default function SharePanel({ projectId, projectName, onClose }: SharePanelProps): React.JSX.Element {
  const [shares, setShares] = useState<ProjectShare[]>([])
  const [sharesLoading, setSharesLoading] = useState(true)
  const [sharesError, setSharesError] = useState<string | null>(null)

  const [query, setQuery] = useState('')
  const [results, setResults] = useState<Colleague[]>([])
  const [searching, setSearching] = useState(false)
  // ONE OF THE FOUR NAMED STATES (R28), or null once results are showing. Never a raw
  // `err.message` from the network — the two failure arms below write their own sentence,
  // so a colleague search never puts backend prose on screen.
  const [searchNotice, setSearchNotice] = useState<string | null>(null)

  const [sharingId, setSharingId] = useState<string | null>(null)
  const [revokingId, setRevokingId] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const requestIdRef = useRef(0)

  function loadShares(): void {
    setSharesLoading(true)
    listProjectShares(projectId)
      .then((rows) => {
        setShares(rows)
        setSharesError(null)
      })
      .catch((err: unknown) => setSharesError(errorMessage(err, 'Could not load the current shares.')))
      .finally(() => setSharesLoading(false))
  }

  useEffect(() => {
    loadShares()
    // `projectId` is the only input this effect reads that can legitimately change under
    // this component; `loadShares` is a stable closure over it, not a second dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  useEffect(
    () => () => {
      if (debounceRef.current !== null) clearTimeout(debounceRef.current)
    },
    [],
  )

  const sharedIds = new Set(shares.map((s) => s.sharedWithUserId))

  function onQueryChange(next: string): void {
    setQuery(next)
    if (debounceRef.current !== null) clearTimeout(debounceRef.current)
    const trimmed = next.trim()
    // STATE 1 OF 4: below the minimum. Said plainly rather than left silent — a box that
    // shows nothing at all while you type "Al" looks broken, not merely unready.
    if (trimmed.length < MIN_QUERY_CHARS) {
      setResults([])
      setSearching(false)
      setSearchNotice(trimmed.length === 0 ? null : `Type at least ${MIN_QUERY_CHARS} characters to search.`)
      return
    }
    // STATE 2 OF 4: in flight.
    setSearching(true)
    setSearchNotice(null)
    debounceRef.current = setTimeout(() => {
      debounceRef.current = null
      const myId = ++requestIdRef.current
      searchColleagues(trimmed)
        .then((colleagues) => {
          if (requestIdRef.current !== myId) return // a newer keystroke's search already won
          setResults(colleagues)
          // STATE 3 OF 4: no matches.
          setSearchNotice(colleagues.length === 0 ? 'No matching colleagues.' : null)
        })
        .catch((err: unknown) => {
          if (requestIdRef.current !== myId) return
          setResults([])
          // STATE 4 OF 4: rate-limited — named as a wait, never as the server's own 429 text.
          setSearchNotice(
            err instanceof ApiError && err.status === 429
              ? 'Too many searches. Please wait a moment and try again.'
              : 'Could not search colleagues right now.',
          )
        })
        .finally(() => {
          if (requestIdRef.current === myId) setSearching(false)
        })
    }, DEBOUNCE_MS)
  }

  function onShare(colleague: Colleague): void {
    setSharingId(colleague.id)
    setActionError(null)
    shareProject(projectId, colleague.id)
      .then(() => {
        loadShares()
        setResults((prev) => prev.filter((c) => c.id !== colleague.id))
      })
      .catch((err: unknown) => setActionError(errorMessage(err, 'Could not share this project.')))
      .finally(() => setSharingId(null))
  }

  function onRevoke(share: ProjectShare): void {
    setRevokingId(share.sharedWithUserId)
    setActionError(null)
    unshareProject(projectId, share.sharedWithUserId)
      .then(() => setShares((prev) => prev.filter((s) => s.id !== share.id)))
      .catch((err: unknown) => setActionError(errorMessage(err, 'Could not revoke this share.')))
      .finally(() => setRevokingId(null))
  }

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      <DialogContent
        hideClose
        overlayClassName="bg-slate-900/15 backdrop-blur-[3px] [-webkit-backdrop-filter:blur(3px)]"
        className="font-manrope bg-white rounded-2xl shadow-2xl w-full max-w-md p-6 gap-0 border-0"
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <DialogTitle className="text-base font-bold text-tertiary truncate">
              Share &ldquo;{projectName}&rdquo;
            </DialogTitle>
            {/* R6 + R7, said once, plainly, before anyone is added. */}
            <p className="text-xs text-neutral mt-1 leading-relaxed">
              Anyone you add can open and use this app. Anything they enter is saved into the
              project&rsquo;s real data.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="flex-shrink-0 p-1.5 text-neutral hover:text-tertiary rounded-lg hover:bg-bial-bg transition"
          >
            <X size={18} />
          </button>
        </div>

        <label className="block mt-5">
          <span className="text-xs font-semibold text-tertiary">Add a colleague</span>
          <input
            autoFocus
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            placeholder="Search by name or email"
            className="mt-1.5 w-full border border-bial-border rounded-xl px-3 py-2.5 text-sm text-tertiary placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary"
          />
        </label>

        {/* THE ONE REGION FOR ALL FOUR NAMED STATES (R28) — mounted unconditionally so a
            reader hears each one land, matching the wait-region convention `ProjectsPage`
            already establishes for this codebase. */}
        <div role="status" aria-live="polite" data-testid="colleague-search-status" className="mt-2 min-h-[1.125rem]">
          {searching ? (
            <p className="text-xs text-neutral flex items-center gap-1.5">
              <BusyGlyph size={12} /> Searching…
            </p>
          ) : searchNotice ? (
            <p className="text-xs text-neutral">{searchNotice}</p>
          ) : null}
        </div>

        {results.length > 0 && (
          <ul className="mt-1 flex flex-col gap-0.5 max-h-40 overflow-y-auto">
            {results.map((colleague) => {
              const alreadyShared = sharedIds.has(colleague.id)
              const busy = sharingId === colleague.id
              return (
                <li
                  key={colleague.id}
                  className="flex items-center justify-between gap-2 px-2 py-1.5 rounded-lg hover:bg-bial-bg"
                >
                  <div className="min-w-0">
                    <p className="text-sm text-tertiary truncate">
                      {colleague.displayName || colleague.emailLocalPart}
                    </p>
                    <p className="text-[11px] text-neutral truncate">{colleague.emailLocalPart}</p>
                  </div>
                  <button
                    type="button"
                    aria-disabled={alreadyShared || busy}
                    onClick={() => {
                      if (!alreadyShared && !busy) onShare(colleague)
                    }}
                    className="flex-shrink-0 text-xs font-semibold text-primary hover:underline aria-disabled:opacity-50 aria-disabled:no-underline aria-disabled:cursor-not-allowed"
                  >
                    {alreadyShared ? 'Already shared' : busy ? <BusyGlyph size={12} /> : 'Share'}
                  </button>
                </li>
              )
            })}
          </ul>
        )}

        {actionError !== null && (
          <div role="alert" className="mt-3 bg-red-50 border border-red-200 rounded-xl px-3 py-2.5">
            <p className="text-xs text-red-600">{actionError}</p>
          </div>
        )}

        <div className="mt-5">
          <p className="text-xs font-semibold text-tertiary">Who can use this project</p>
          {sharesLoading ? (
            <p className="text-xs text-neutral mt-2 flex items-center gap-1.5">
              <BusyGlyph size={12} /> Loading…
            </p>
          ) : sharesError !== null ? (
            <p className="text-xs text-danger mt-2">{sharesError}</p>
          ) : shares.length === 0 ? (
            <p className="text-xs text-neutral/70 italic mt-2">Not shared with anyone yet.</p>
          ) : (
            <ul className="mt-2 flex flex-col gap-0.5 max-h-48 overflow-y-auto">
              {shares.map((share) => {
                const busy = revokingId === share.sharedWithUserId
                return (
                  <li
                    key={share.id}
                    className="flex items-center justify-between gap-2 px-2 py-1.5 rounded-lg hover:bg-bial-bg"
                  >
                    <div className="min-w-0">
                      <p className="text-sm text-tertiary truncate">
                        {share.sharedWithDisplayName || share.sharedWithEmailLocalPart}
                      </p>
                      {/* "Can use", never "view only" (R6, Key Decision 3). */}
                      <p className="text-[11px] text-neutral truncate">Can use</p>
                    </div>
                    <button
                      type="button"
                      aria-disabled={busy}
                      onClick={() => {
                        if (!busy) onRevoke(share)
                      }}
                      className="flex-shrink-0 text-xs font-semibold text-neutral hover:text-danger aria-disabled:opacity-50"
                    >
                      {busy ? <BusyGlyph size={12} /> : 'Remove'}
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
