import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ColumnDef, RowSelectionState } from '@tanstack/react-table'
import { AlertCircle } from 'lucide-react'
import { BusyGlyph } from '../ui/Waiting'
import { Checkbox } from '../ui/checkbox'
import { Label } from '../ui/label'
import { fetchUsers, bulkUpdateUserLimits } from '../../utils/admin'
import type { UserLimitsOut } from '../../utils/admin'
import { useKeysetList } from '../../hooks/useKeysetList'
import type { KeysetPage } from '../../hooks/useKeysetList'
import { fmt, roleLabel } from './columns'
import AdminDataTable from './AdminDataTable'

// A plain suggestion list, not an enforced enum — the endpoint that consumes a value
// still accepts any positive integer up to MAX_DAILY_TOKEN_LIMIT (custom values stay
// legal). No backend-side twin: mirroring this list in services/usage/limits.py would have
// zero consumers there and would exist only to be kept in sync by hand with nothing reading it.
const SUGGESTED_DAILY_TOKEN_LIMITS = [250_000, 500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000]
const CUSTOM_VALUE = 'custom'

const FETCH_PAGE_SIZE = 100
// Same ceiling UsersLimitsPanel uses for its own background bulk-load — past this
// many rows, "selected users" mode stops loading and says so rather than quietly
// firing hundreds of sequential requests. "All users" mode is unaffected: it never
// enumerates ids client-side, so it covers everyone regardless of this cap.
const MAX_LOADED_USERS = 2000

type Mode = 'all' | 'selected'

export interface GlobalLimitsPanelProps {
  onToast: (msg: string) => void
}

/**
 * Admin "Global Limits" panel — apply one daily-token-limit value to either
 * every user system-wide or a hand-picked subset, in one bulk request. "All
 * users" sends `userIds: null` (backend resolves the roster, covering
 * everyone); "Selected users" checks rows off the same background-loaded
 * roster `UsersLimitsPanel` uses, held as TanStack row selection keyed by user id.
 * The value picker is one control: a preset fills the number input, which stays
 * hand-editable before applying.
 */
export default function GlobalLimitsPanel({ onToast }: GlobalLimitsPanelProps) {
  const [mode, setMode] = useState<Mode>('all')
  const [preset, setPreset] = useState<string>(String(SUGGESTED_DAILY_TOKEN_LIMITS[2]))
  const [customValue, setCustomValue] = useState('')
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({})
  const [confirming, setConfirming] = useState(false)
  const [applying, setApplying] = useState(false)
  const [applyError, setApplyError] = useState<string | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    abortRef.current = controller
    return () => controller.abort()
  }, [])
  // Guards the post-await setState calls in `apply()` — switching admin tabs mid-apply
  // unmounts this panel (`AdminPage.tsx` mounts/unmounts each tab), but the POST has no
  // AbortSignal and keeps running. Without this, a failure after unmount is a silent
  // dead-instance no-op: `onToast` (the parent's callback) still fires on success, so
  // success stays visible while failure vanishes without trace — for an action the UI
  // itself labels "can't be undone automatically".
  const isMountedRef = useRef(true)
  useEffect(() => {
    isMountedRef.current = true
    return () => { isMountedRef.current = false }
  }, [])

  const fetchPage = useCallback(
    async ({ cursor, q, limit }: { cursor: string | null; q: string; limit: number }): Promise<KeysetPage<UserLimitsOut>> => {
      const page = await fetchUsers({ cursor, q, limit, signal: abortRef.current?.signal })
      return { items: page.users, nextCursor: page.nextCursor, hasMore: page.hasMore }
    },
    [],
  )

  const { items: users, q, appliedQuery, loading, hasMore, error, loadMore, setQuery, refresh } = useKeysetList<
    UserLimitsOut,
    KeysetPage<UserLimitsOut>
  >({ fetchPage, pageSize: FETCH_PAGE_SIZE })

  const isAbortError = error?.name === 'AbortError'

  // Same background-load-to-completion pattern as UsersLimitsPanel: keeps chaining
  // loadMore() for the current search so "select all loaded rows" actually means
  // all matching rows, not just the first page. Gated on `mode === 'selected'`, and
  // `mode` DEFAULTS TO 'all' above — so for an admin who only ever uses "All users"
  // (which needs no roster at all; the backend resolves it), this chain never starts
  // in the first place. It only fires once the admin actively switches to "Selected
  // users", and stops as soon as they switch away — it re-runs from scratch on every
  // remount of that mode, since `AdminPage.tsx` unmounts each tab.
  useEffect(() => {
    if (
      mode === 'selected' &&
      !loading &&
      hasMore &&
      (!error || isAbortError) &&
      (appliedQuery === null || appliedQuery === q) &&
      users.length < MAX_LOADED_USERS
    ) {
      loadMore()
    }
  }, [mode, loading, hasMore, error, isAbortError, appliedQuery, q, users.length, loadMore])

  // A failed background page must never silently vanish, and a truncated roster must
  // never look complete: ported verbatim from `UsersLimitsPanel` (613-646), whose two
  // banners cover exactly the gap this panel used to leave — the error branch below
  // was gated on `users.length === 0` and the loading caption on `!error`, so a
  // page-3-of-20 failure rendered NOTHING (no banner, no retry, no spinner), and
  // "Select all loaded" would then silently apply an irreversible fleet-wide change to
  // a roster the admin didn't know was incomplete.
  const isPartial = hasMore && !!error && !isAbortError
  const isCapped = hasMore && (!error || isAbortError) && users.length >= MAX_LOADED_USERS
  // A refresh (post-apply, or a retry) can fail AFTER the roster has already fully
  // drained (`hasMore === false`, the normal steady state at BIAL's size) — distinct
  // from `isPartial`, which only covers a failure while more pages are still owed.
  // `useKeysetList` only writes `hasMore` on a SUCCESSFUL fetch, so a failed refresh
  // here sets `error` but leaves `hasMore` at its already-`false` value, and every
  // other disclosure branch requires something that isn't true (isPartial/isCapped
  // need `hasMore`; the empty-error block needs `users.length === 0`) — so without
  // this, the admin sees a success toast next to a "Current daily tokens" column
  // that's silently gone stale, with no error and no retry anywhere.
  const isStaleAfterFailedRefresh = !hasMore && !!error && !isAbortError && users.length > 0

  const isCustom = preset === CUSTOM_VALUE
  // Plain digits only — NOT `Number.isInteger(Number(raw))`, which also accepts
  // scientific notation ("1e3"), leading/trailing whitespace, and a leading "+".
  // An admin typo like "1e3" would otherwise silently parse to a valid-looking
  // 1,000 instead of the 1,000,000 they meant, with nothing distinguishing the
  // parsed value from the raw text before it's sent.
  const isPlainPositiveInteger = (raw: string): boolean => /^[1-9]\d*$/.test(raw)
  const rawValue = isCustom ? customValue : preset
  const valueIsValid = isPlainPositiveInteger(rawValue)
  const value = valueIsValid ? Number(rawValue) : NaN

  const selectedIds = Object.keys(rowSelection).filter((id) => rowSelection[id])
  const selectedCount = selectedIds.length
  const targetCount = mode === 'all' ? null : selectedCount

  // Closing the confirm step on any value/preset edit keeps the reviewed action and the
  // applied action provably identical — without this, switching the preset dropdown to
  // "Custom…" while `confirming` was true left the banner reading "Set the daily limit
  // to NaN" (customValue starts '') with "Yes, apply" still clickable.
  const setPresetAndClose = (next: string) => {
    setPreset(next)
    if (next !== CUSTOM_VALUE) setCustomValue(next)
    setConfirming(false)
  }
  const setCustomValueAndClose = (next: string) => {
    setCustomValue(next)
    setPreset(CUSTOM_VALUE)
    setConfirming(false)
  }

  // The selection is otherwise never reconciled with the roster: `useKeysetList` clears
  // `items` on a query change, but nothing pruned the selection to match — so the header
  // and confirm banner could read "42 selected" while the visible table showed zero
  // ticked rows, targeting users the admin could no longer see or review.
  useEffect(() => {
    setRowSelection({})
  }, [appliedQuery])

  // "Select all" ticks every LOADED row, on every page, and merges with picks already made;
  // unticking it clears only the loaded rows. It stays off while a background page has failed.
  const columns = useMemo<ColumnDef<UserLimitsOut>[]>(
    () => [
      {
        id: 'select',
        meta: { className: 'w-8' },
        header: ({ table }) => (
          <Checkbox
            aria-label="Select all loaded users"
            checked={table.getIsAllRowsSelected()}
            disabled={isPartial}
            onCheckedChange={(checked) => table.toggleAllRowsSelected(checked === true)}
          />
        ),
        cell: ({ row }) => (
          <Checkbox
            aria-label={`Select ${row.original.displayName || row.original.email}`}
            checked={row.getIsSelected()}
            onCheckedChange={(checked) => row.toggleSelected(checked === true)}
          />
        ),
      },
      {
        id: 'user',
        accessorFn: (u) => u.displayName || u.email,
        header: 'User',
        cell: ({ row }) => (
          <>
            <p className="font-medium text-tertiary">{row.original.displayName || row.original.email}</p>
            <p className="text-xs text-neutral">{row.original.email}</p>
          </>
        ),
      },
      { id: 'role', accessorFn: (u) => u.role, header: 'Role', cell: ({ row }) => roleLabel(row.original.role) },
      {
        id: 'dailyTokenLimit',
        accessorFn: (u) => u.effectiveLimits.dailyTokenLimit ?? 0,
        header: 'Current daily tokens',
        cell: ({ row }) => <span className="tabular-nums">{fmt(row.original.effectiveLimits.dailyTokenLimit ?? 0)}</span>,
      },
    ],
    [isPartial],
  )

  const canApply =
    valueIsValid && (mode === 'all' || selectedCount > 0) && !applying && !(mode === 'selected' && isPartial)

  const apply = async () => {
    setApplying(true)
    setApplyError(null)
    try {
      const result = await bulkUpdateUserLimits(
        value,
        mode === 'all' ? undefined : selectedIds,
        {},
      )
      if (!isMountedRef.current) return
      onToast(`Daily limit updated for ${fmt(result.updatedCount)} user${result.updatedCount === 1 ? '' : 's'}`)
      setConfirming(false)
      setRowSelection({})
      // The "Current daily tokens" column otherwise keeps showing pre-apply values
      // until the admin switches tabs and back — immediately after an action whose
      // entire purpose was to change that column. `refresh()` reloads page 1 under the
      // current query; the auto-chain effect above picks up the rest.
      refresh()
    } catch (e) {
      if (!isMountedRef.current) return
      setApplyError(e instanceof Error ? e.message : String(e))
    } finally {
      if (isMountedRef.current) setApplying(false)
    }
  }

  return (
    <>
      <p className="text-xs text-neutral mb-4">
        Set the daily token limit for many users at once — apply it to everyone, or tick the
        specific users you want to change.
      </p>

      {/* Mode toggle */}
      <div className="flex gap-2 mb-4" role="radiogroup" aria-label="Apply to">
        <button
          type="button"
          role="radio"
          aria-checked={mode === 'selected'}
          data-testid="mode-selected"
          onClick={() => {
            setMode('selected')
            setConfirming(false)
          }}
          className={`px-3 py-1.5 rounded-xl text-sm font-medium border transition ${
            mode === 'selected'
              ? 'bg-primary text-white border-primary'
              : 'border-bial-border text-tertiary hover:bg-bial-bg'
          }`}
        >
          Selected users
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={mode === 'all'}
          data-testid="mode-all"
          onClick={() => {
            setMode('all')
            setConfirming(false)
          }}
          className={`px-3 py-1.5 rounded-xl text-sm font-medium border transition ${
            mode === 'all'
              ? 'bg-primary text-white border-primary'
              : 'border-bial-border text-tertiary hover:bg-bial-bg'
          }`}
        >
          All users
        </button>
      </div>

      {/* Value picker */}
      <div className="flex flex-wrap items-end gap-3 mb-5">
        <div>
          <Label htmlFor="glp-preset" className="block text-[10px] font-bold uppercase tracking-wider text-neutral mb-1.5">
            Daily token limit
          </Label>
          <select
            id="glp-preset"
            data-testid="preset-select"
            value={preset}
            onChange={(e) => setPresetAndClose(e.target.value)}
            className="border border-bial-border rounded-xl px-3 py-2 text-sm text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition"
          >
            {SUGGESTED_DAILY_TOKEN_LIMITS.map((v) => (
              <option key={v} value={v}>
                {fmt(v)}
              </option>
            ))}
            <option value={CUSTOM_VALUE}>Custom…</option>
          </select>
        </div>
        <div>
          <Label htmlFor="glp-custom" className="block text-[10px] font-bold uppercase tracking-wider text-neutral mb-1.5">
            Exact value
          </Label>
          <input
            id="glp-custom"
            type="number"
            min="1"
            data-testid="custom-value"
            value={isCustom ? customValue : preset}
            onChange={(e) => setCustomValueAndClose(e.target.value)}
            className="w-40 border border-bial-border rounded-xl px-3 py-2 text-sm text-tertiary tabular-nums focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition"
          />
          {/* `1e6` etc. is a legal `<input type="number">` value, so the browser keeps it in
              `.value` while `valueIsValid` silently goes false and Apply just greys out with
              no explanation — closing the loop on the exact typo this guard exists to catch. */}
          {isCustom && rawValue !== '' && !valueIsValid && (
            <p data-testid="glp-value-hint" className="mt-1 text-[11px] text-danger">
              Digits only — no decimals, spaces, or scientific notation.
            </p>
          )}
        </div>
      </div>

      {mode === 'selected' && (
        <>
          {/* A failed background page must never silently vanish (fail-first), and it must
              never look like the whole roster is in when it isn't — shown above the table,
              not tucked below it. Ported from the partial-roster banner in
              `UsersLimitsPanel.tsx`. Gated on `users.length > 0`: on a FIRST-page failure
              `isPartial` can also be true (hasMore starts `true` and is never reset on
              failure), and the dedicated empty-error block below already covers that
              zero-users case — without this gate both rendered at
              once, and this banner's Retry was disabled forever (appliedQuery stays `null`,
              never equal to `q`). */}
          {isPartial && users.length > 0 && (
            <div data-testid="loadmore-error" className="mb-4 flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-xl px-3 py-2.5">
              <AlertCircle size={14} className="text-amber-600 flex-shrink-0 mt-0.5" />
              <p className="text-xs text-amber-700 flex-1">
                Only {fmt(users.length)} users loaded — "Select all loaded" and Apply are disabled
                until this is resolved. {error?.message}
              </p>
              <button
                onClick={() => appliedQuery === q && loadMore()}
                disabled={appliedQuery !== q}
                title={appliedQuery !== q ? 'A new search is in progress — this re-enables once it lands.' : undefined}
                className="flex-none underline font-medium text-amber-800 hover:text-amber-900 disabled:opacity-50 disabled:cursor-not-allowed disabled:no-underline"
              >
                Retry
              </button>
            </div>
          )}

          {isCapped && users.length > 0 && (
            <p className="mb-4 text-xs text-neutral bg-bial-bg border border-bial-border rounded-xl px-3 py-2.5">
              Showing the first {fmt(MAX_LOADED_USERS)} users — refine your search to narrow the results.
            </p>
          )}

          {isStaleAfterFailedRefresh && (
            <div data-testid="refresh-error" className="mb-4 flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-xl px-3 py-2.5">
              <AlertCircle size={14} className="text-amber-600 flex-shrink-0 mt-0.5" />
              <p className="text-xs text-amber-700 flex-1">
                Couldn't refresh the list — the values shown may be out of date. {error?.message}
              </p>
              {/* refresh() always reads the live query ref directly, unlike loadMore()'s
                  cursor — no appliedQuery === q guard needed here. */}
              <button
                onClick={() => refresh()}
                className="flex-none underline font-medium text-amber-800 hover:text-amber-900"
              >
                Retry
              </button>
            </div>
          )}

          {users.length === 0 && (!error || isAbortError) && (appliedQuery === null || isAbortError) ? (
            <div className="flex items-center justify-center gap-2 py-16 text-neutral text-sm">
              <BusyGlyph size={16} /> Loading users…
            </div>
          ) : error && !isAbortError && users.length === 0 ? (
            <div className="text-center py-16">
              <AlertCircle size={20} className="text-red-500 mx-auto mb-3" />
              <p className="text-sm text-tertiary font-semibold">Couldn’t load users</p>
              <p data-testid="glp-load-error" className="text-xs text-neutral mt-1">
                {error.message}
              </p>
              <button
                onClick={() => loadMore()}
                className="mt-4 inline-flex items-center gap-1.5 px-4 py-2 rounded-xl border border-bial-border text-sm font-medium text-tertiary hover:bg-bial-bg transition"
              >
                Retry
              </button>
            </div>
          ) : (
            <AdminDataTable<UserLimitsOut>
              columns={columns}
              rows={users}
              getRowId={(u) => u.userId}
              serverSearch={{ value: q, onChange: setQuery }}
              searchLabel="Search users"
              searchPlaceholder="Search name or email…"
              emptyMessage={appliedQuery ? `No users match “${appliedQuery}”.` : 'No users yet.'}
              loading={loading}
              rowSelection={rowSelection}
              onRowSelectionChange={setRowSelection}
              toolbarStart={() => <span className="text-xs text-neutral">{fmt(selectedCount)} selected</span>}
            />
          )}

          {hasMore && !error && !isCapped && (
            <p className="mt-3 flex items-center gap-1.5 text-xs text-neutral">
              <BusyGlyph size={12} /> Loading more users…
            </p>
          )}
        </>
      )}

      {mode === 'all' && (
        <p data-testid="all-users-summary" className="text-sm text-tertiary bg-bial-bg border border-bial-border rounded-xl px-3 py-2.5 mb-2">
          This will set the daily limit for <strong>every current, active user, system-wide</strong>.
          Suspended users are excluded and won't be updated. It's a one-time apply, not a standing
          policy — anyone who joins afterward starts on the standard plan and needs a re-apply to
          pick up this value.
        </p>
      )}

      {applyError && (
        <div data-testid="apply-error" className="mt-4 flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl px-3 py-2.5">
          <AlertCircle size={14} className="text-red-500 flex-shrink-0 mt-0.5" />
          <p className="text-xs text-red-600">{applyError}</p>
        </div>
      )}

      <div className="mt-5">
        {!confirming ? (
          <button
            type="button"
            data-testid="glp-apply"
            disabled={!canApply}
            onClick={() => setConfirming(true)}
            className="px-4 py-2.5 rounded-xl font-semibold text-sm bg-primary text-white hover:bg-primary/90 disabled:opacity-50 transition"
          >
            Apply
          </button>
        ) : (
          <div className="flex flex-wrap items-center gap-3 bg-amber-50 border border-amber-200 rounded-xl px-4 py-3">
            <p className="text-sm text-amber-900">
              Set the daily limit to <strong>{fmt(value)}</strong> for{' '}
              <strong>{mode === 'all' ? 'every user' : `${fmt(targetCount ?? 0)} selected user${targetCount === 1 ? '' : 's'}`}</strong>?
              This can't be undone automatically.
            </p>
            <div className="flex gap-2 ml-auto">
              <button
                type="button"
                data-testid="glp-confirm"
                disabled={applying || !canApply}
                onClick={apply}
                className="flex items-center gap-2 px-3 py-1.5 rounded-lg font-semibold text-xs bg-primary text-white hover:bg-primary/90 disabled:opacity-50 transition"
              >
                {applying && <BusyGlyph size={13} />}
                Yes, apply
              </button>
              <button
                type="button"
                disabled={applying}
                onClick={() => setConfirming(false)}
                className="px-3 py-1.5 rounded-lg font-semibold text-xs border border-bial-border text-tertiary hover:bg-white disabled:opacity-50 transition"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </>
  )
}
