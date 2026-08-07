import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertCircle, Loader2, Search } from 'lucide-react'
import { fetchUsers, bulkUpdateUserLimits } from '../../utils/admin'
import type { UserLimitsOut } from '../../utils/admin'
import { useKeysetList } from '../../hooks/useKeysetList'
import type { KeysetPage } from '../../hooks/useKeysetList'
import { fmt, roleLabel } from './columns'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '../ui/table'

// Mirrors the backend's SUGGESTED_DAILY_TOKEN_LIMITS (services/usage/limits.py) — a
// plain suggestion list, not an enforced enum. Keep in sync by hand.
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
 * Admin "Global Limits" panel — apply one daily-token-limit value to either every
 * user system-wide or a hand-picked subset, in a single bulk request.
 *
 * Two explicit modes, not a hybrid "select all that match, except these": "All
 * users" sends `userIds: null` straight to the backend (which resolves the roster
 * itself, so it covers literally everyone, not just what's loaded here); "Selected
 * users" renders a checkbox table over the same background-loaded roster
 * `UsersLimitsPanel` uses, and only ever sends the ids actually ticked.
 *
 * The value picker is one control, not two disconnected ones: picking a preset from
 * the dropdown fills the number input, which the admin can still hand-edit before
 * applying.
 */
export default function GlobalLimitsPanel({ onToast }: GlobalLimitsPanelProps) {
  const [mode, setMode] = useState<Mode>('selected')
  const [preset, setPreset] = useState<string>(String(SUGGESTED_DAILY_TOKEN_LIMITS[2]))
  const [customValue, setCustomValue] = useState('')
  const [selected, setSelected] = useState<Record<string, true>>({})
  const [confirming, setConfirming] = useState(false)
  const [applying, setApplying] = useState(false)
  const [applyError, setApplyError] = useState<string | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    abortRef.current = controller
    return () => controller.abort()
  }, [])

  const fetchPage = useCallback(
    async ({ cursor, q, limit }: { cursor: string | null; q: string; limit: number }): Promise<KeysetPage<UserLimitsOut>> => {
      const page = await fetchUsers({ cursor, q, limit, signal: abortRef.current?.signal })
      return { items: page.users, nextCursor: page.nextCursor, hasMore: page.hasMore }
    },
    [],
  )

  const { items: users, q, appliedQuery, loading, hasMore, error, loadMore, setQuery } = useKeysetList<
    UserLimitsOut,
    KeysetPage<UserLimitsOut>
  >({ fetchPage, pageSize: FETCH_PAGE_SIZE })

  const isAbortError = error?.name === 'AbortError'

  // Same background-load-to-completion pattern as UsersLimitsPanel: keeps chaining
  // loadMore() for the current search so "select all loaded rows" actually means
  // all matching rows, not just the first page.
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

  const selectedIds = useMemo(() => Object.keys(selected), [selected])
  const selectedCount = selectedIds.length
  const targetCount = mode === 'all' ? null : selectedCount

  const toggleOne = (userId: string) =>
    setSelected((prev) => {
      const next = { ...prev }
      if (next[userId]) delete next[userId]
      else next[userId] = true
      return next
    })

  const allLoadedSelected = users.length > 0 && users.every((u) => selected[u.userId])
  const toggleAllLoaded = () => {
    if (allLoadedSelected) {
      setSelected({})
    } else {
      setSelected(Object.fromEntries(users.map((u) => [u.userId, true as const])))
    }
  }

  const canApply =
    valueIsValid && (mode === 'all' || selectedCount > 0) && !applying

  const apply = async () => {
    setApplying(true)
    setApplyError(null)
    try {
      const result = await bulkUpdateUserLimits(
        value,
        mode === 'all' ? undefined : selectedIds,
        {},
      )
      onToast(`Daily limit updated for ${fmt(result.updatedCount)} user${result.updatedCount === 1 ? '' : 's'}`)
      setConfirming(false)
      setSelected({})
    } catch (e) {
      setApplyError(e instanceof Error ? e.message : String(e))
    } finally {
      setApplying(false)
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
          <label htmlFor="glp-preset" className="block text-[10px] font-bold uppercase tracking-wider text-neutral mb-1.5">
            Daily token limit
          </label>
          <select
            id="glp-preset"
            data-testid="preset-select"
            value={preset}
            onChange={(e) => {
              const next = e.target.value
              setPreset(next)
              if (next !== CUSTOM_VALUE) setCustomValue(next)
            }}
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
          <label htmlFor="glp-custom" className="block text-[10px] font-bold uppercase tracking-wider text-neutral mb-1.5">
            Exact value
          </label>
          <input
            id="glp-custom"
            type="number"
            min="1"
            data-testid="custom-value"
            value={isCustom ? customValue : preset}
            onChange={(e) => {
              setCustomValue(e.target.value)
              setPreset(CUSTOM_VALUE)
            }}
            className="w-40 border border-bial-border rounded-xl px-3 py-2 text-sm text-tertiary tabular-nums focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition"
          />
        </div>
      </div>

      {mode === 'selected' && (
        <>
          <div className="flex flex-wrap items-center gap-3 mb-4">
            <div className="relative max-w-xs flex-1 min-w-[180px]">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral" />
              <input
                type="search"
                data-testid="glp-search"
                value={q}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search name or email…"
                className="w-full pl-9 pr-3 py-2 text-sm border border-bial-border rounded-xl text-tertiary placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition"
              />
            </div>
            <span className="text-xs text-neutral">{fmt(selectedCount)} selected</span>
          </div>

          {users.length === 0 && (!error || isAbortError) && (loading || appliedQuery === null) ? (
            <div className="flex items-center justify-center gap-2 py-16 text-neutral text-sm">
              <Loader2 size={16} className="animate-spin" /> Loading users…
            </div>
          ) : error && !isAbortError && users.length === 0 ? (
            <div className="text-center py-16">
              <AlertCircle size={20} className="text-red-500 mx-auto mb-3" />
              <p data-testid="glp-load-error" className="text-xs text-neutral mt-1">
                {error.message}
              </p>
            </div>
          ) : users.length === 0 ? (
            <div className="text-center py-16 text-sm text-neutral">
              {appliedQuery ? `No users match “${appliedQuery}”.` : 'No users yet.'}
            </div>
          ) : (
            <Table>
              <TableHeader>
                <tr className="border-b border-bial-border">
                  <TableHead className="w-8">
                    <input
                      type="checkbox"
                      aria-label="Select all loaded users"
                      data-testid="select-all-loaded"
                      className="accent-primary w-3.5 h-3.5"
                      checked={allLoadedSelected}
                      onChange={toggleAllLoaded}
                    />
                  </TableHead>
                  <TableHead>User</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead>Current daily tokens</TableHead>
                </tr>
              </TableHeader>
              <TableBody>
                {users.map((u) => (
                  <TableRow key={u.userId} data-testid={`glp-row-${u.email}`} className="hover:bg-bial-bg/50">
                    <TableCell>
                      <input
                        type="checkbox"
                        aria-label={`Select ${u.displayName || u.email}`}
                        data-testid={`select-${u.email}`}
                        className="accent-primary w-3.5 h-3.5"
                        checked={!!selected[u.userId]}
                        onChange={() => toggleOne(u.userId)}
                      />
                    </TableCell>
                    <TableCell>
                      <p className="font-medium text-tertiary">{u.displayName || u.email}</p>
                      <p className="text-xs text-neutral">{u.email}</p>
                    </TableCell>
                    <TableCell>{roleLabel(u.role)}</TableCell>
                    <TableCell className="tabular-nums">{fmt(u.effectiveLimits.dailyTokenLimit ?? 0)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}

          {hasMore && !error && (
            <p className="mt-3 flex items-center gap-1.5 text-xs text-neutral">
              <Loader2 size={12} className="animate-spin" /> Loading more users…
            </p>
          )}
        </>
      )}

      {mode === 'all' && (
        <p data-testid="all-users-summary" className="text-sm text-tertiary bg-bial-bg border border-bial-border rounded-xl px-3 py-2.5 mb-2">
          This will set the daily limit for <strong>every user, system-wide</strong>.
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
                disabled={applying}
                onClick={apply}
                className="flex items-center gap-2 px-3 py-1.5 rounded-lg font-semibold text-xs bg-primary text-white hover:bg-primary/90 disabled:opacity-50 transition"
              >
                {applying && <Loader2 size={13} className="animate-spin" />}
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
