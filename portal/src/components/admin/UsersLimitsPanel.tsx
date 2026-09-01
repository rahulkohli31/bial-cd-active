import { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  flexRender,
} from '@tanstack/react-table'
import type { SortingState, ColumnFiltersState } from '@tanstack/react-table'
import { X, AlertCircle, Loader2, Search, ChevronLeft, ChevronRight } from 'lucide-react'
import { fetchUsers, updateUserLimits, deactivateUser, reactivateUser, resetUserUsage } from '../../utils/admin'
import type { LimitFields, UserLimitsOut } from '../../utils/admin'
import { ApiError } from '../../utils/apiError'
import { useKeysetList } from '../../hooks/useKeysetList'
import type { KeysetPage } from '../../hooks/useKeysetList'
import { fmt, createUserColumns } from './columns'
import type { MergedUser } from './columns'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '../ui/table'
import { Select, SelectValue, SelectTrigger, SelectContent, SelectItem } from '../ui/select'

// The model's real context window — a per-conversation hard limit can be lowered below this
// but never raised past it. The clamp is the SERVER's (`services/usage/limits.effective_context`),
// so typing a larger number here does not widen anything; this constant only keeps the hint
// truthful about what will happen.
//
// THIS COMMENT USED TO SAY the opposite of what was true — "the server is the real boundary;
// this is a friendly client-side guard" — while the server enforced nothing and the client had
// stopped enforcing anything either. Both halves are now correct: the server refuses a turn
// past the per-conversation max (`enforce_context_limit`, a 413 the citizen reads a sentence
// from), and the browser's own warning at the soft threshold is the friendly guard.
const MODEL_CONTEXT_WINDOW = 200_000
// The wire page size (how many rows one fetchUsers call asks for — capped at the
// server's MAX_PAGE_SIZE=100) is deliberately larger than the table's on-screen page
// size, so bulk-loading the roster takes 1/4 the round-trips it would at 25/request.
const FETCH_PAGE_SIZE = 100
const TABLE_PAGE_SIZE = 25
// A hard ceiling on the background bulk-load: past this many rows, the chain stops
// and the UI says so instead of quietly firing hundreds of sequential requests for
// an org-sized roster. Search still narrows the server-side candidate set first.
const MAX_LOADED_USERS = 2000

interface LimitFieldState {
  useDefault: boolean
  value: string
}

interface LimitFieldProps {
  name: string
  label: string
  hint?: string
  field: LimitFieldState
  setField: (next: LimitFieldState) => void
  // number | null | undefined: matches EditModalProps.defaults being Partial<LimitFields>
  // (a field can be genuinely absent, not just null). fmt() itself takes only `number` —
  // the placeholder below folds the absent/null case to 0 before calling it
  // (`fmt(defaultValue ?? 0)`), matching LimitCell's own `fmt(value ?? 0)` convention
  // (columns.tsx) so a missing default reads "0 (default)", never "NaN (default)".
  defaultValue: number | null | undefined
}

/** One field of the edit modal: a number input with a "Use default" toggle. */
function LimitField({ name, label, hint, field, setField, defaultValue }: LimitFieldProps) {
  return (
    <div>
      <div className="flex items-center justify-between">
        <label className="text-xs font-bold uppercase tracking-wider text-neutral">{label}</label>
        <label className="flex items-center gap-1.5 text-xs text-neutral cursor-pointer">
          <input
            type="checkbox"
            data-testid={`usedefault-${name}`}
            className="accent-primary w-3.5 h-3.5"
            checked={field.useDefault}
            onChange={(e) => setField({ ...field, useDefault: e.target.checked })}
          />
          Use default
        </label>
      </div>
      <input
        type="number"
        min="1"
        data-testid={`limit-${name}`}
        value={field.useDefault ? '' : field.value}
        disabled={field.useDefault}
        placeholder={field.useDefault ? `${fmt(defaultValue ?? 0)} (default)` : ''}
        onChange={(e) => setField({ ...field, value: e.target.value })}
        className="mt-1.5 w-full border border-bial-border rounded-lg px-3 py-2 text-sm text-tertiary tabular-nums focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary disabled:bg-gray-50 disabled:text-neutral transition"
      />
      {hint && <p className="text-[11px] text-neutral mt-1">{hint}</p>}
    </div>
  )
}

interface EditModalProps {
  user: MergedUser
  // Partial: a field can be genuinely absent (undefined), not just null — every
  // read below folds that back to null, which is init()'s and submit()'s
  // existing "no override" fallback value.
  defaults: Partial<LimitFields>
  onClose: () => void
  onSaved: (updated: { userId: string; limits: LimitFields; effectiveLimits: LimitFields }) => void
  onToast: (msg: string) => void
}

function EditModal({ user, defaults, onClose, onSaved, onToast }: EditModalProps) {
  // fallback stays number | null | undefined, matching defaults' Partial<LimitFields> —
  // NOT folded to null here. A field genuinely absent from the server envelope
  // (undefined) must still reach String() as undefined, reproducing the
  // pre-existing "undefined" string in the input exactly.
  const init = (field: keyof LimitFields, fallback: number | null | undefined): LimitFieldState => {
    const has = Number.isInteger(user.limits?.[field])
    return { useDefault: !has, value: String(has ? user.limits?.[field] : fallback) }
  }
  const [daily, setDaily] = useState<LimitFieldState>(() => init('dailyTokenLimit', defaults.dailyTokenLimit))
  const [soft, setSoft] = useState<LimitFieldState>(() => init('contextSoftLimit', defaults.contextSoftLimit))
  const [hard, setHard] = useState<LimitFieldState>(() => init('contextHardLimit', defaults.contextHardLimit))
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const submit = async () => {
    const dailyVal = daily.useDefault ? (defaults.dailyTokenLimit ?? null) : Number(daily.value)
    const softVal = soft.useDefault ? (defaults.contextSoftLimit ?? null) : Number(soft.value)
    const hardVal = hard.useDefault ? (defaults.contextHardLimit ?? null) : Number(hard.value)

    const fields: [string, number | null][] = [
      ['Daily token limit', dailyVal],
      ['Per-conversation warn', softVal],
      ['Per-conversation max', hardVal],
    ]
    for (const [name, v] of fields) {
      if (!Number.isInteger(v) || (v as number) <= 0) {
        setErr(`${name} must be a positive whole number.`)
        return
      }
    }
    // The loop above already confirmed all three pass Number.isInteger(v) && v > 0,
    // which is false for null — so they're real numbers here. TS can't see that
    // across the loop boundary, so it's spelled out rather than cast away silently.
    const dailyNum = dailyVal as number
    const softNum = softVal as number
    const hardNum = hardVal as number
    if (hardNum > MODEL_CONTEXT_WINDOW) {
      setErr(`Per-conversation max can't exceed ${fmt(MODEL_CONTEXT_WINDOW)} (the model's context window).`)
      return
    }
    if (softNum >= hardNum) {
      setErr('Per-conversation warn must be less than the max.')
      return
    }

    const patch: LimitFields = {
      dailyTokenLimit: daily.useDefault ? null : dailyNum,
      contextSoftLimit: soft.useDefault ? null : softNum,
      contextHardLimit: hard.useDefault ? null : hardNum,
    }
    setSaving(true)
    setErr(null)
    try {
      const updated = await updateUserLimits(user.userId, patch)
      onToast(`Limits updated for ${user.displayName || user.email}`)
      onSaved(updated)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
        <div className="flex items-start justify-between">
          <div>
            <h3 className="text-base font-bold text-tertiary">Edit limits</h3>
            <p className="text-sm text-neutral mt-0.5">{user.displayName || user.email}</p>
          </div>
          <button onClick={onClose} className="p-1.5 text-neutral hover:text-tertiary rounded-lg hover:bg-bial-bg transition">
            <X size={18} />
          </button>
        </div>

        <div className="mt-5 space-y-4">
          <LimitField
            name="daily"
            label="Daily token limit"
            hint="Total input + output tokens per day (resets midnight IST)."
            field={daily}
            setField={setDaily}
            defaultValue={defaults.dailyTokenLimit}
          />
          <LimitField
            name="soft"
            label="Per-conversation warn"
            hint="Warn the user their chat is getting long at this many tokens. A warning only — it does not stop them sending."
            field={soft}
            setField={setSoft}
            defaultValue={defaults.contextSoftLimit}
          />
          <LimitField
            name="hard"
            label="Per-conversation max"
            hint={`Hard stop for a single chat: past this the server refuses the next message and tells the user to start a new chat. Max ${fmt(MODEL_CONTEXT_WINDOW)} (model window).`}
            field={hard}
            setField={setHard}
            defaultValue={defaults.contextHardLimit}
          />
        </div>

        {/* Propagation reality (docs/solutions: per-user-limits daily-vs-context). The two
            per-conversation numbers now propagate DIFFERENTLY and the difference is the user's
            to know about: the hard stop is read from the database on every send, so it bites
            immediately; the warning threshold rides the profile the browser cached at sign-in,
            so it follows on their next reload. Never imply either is instant when it is not. */}
        <p className="text-[11px] text-neutral mt-4 leading-relaxed">
          The <strong className="text-tertiary">daily token limit</strong> and the{' '}
          <strong className="text-tertiary">per-conversation max</strong> take effect on the user’s next message. The{' '}
          <strong className="text-tertiary">per-conversation warn</strong> only takes effect after the user reloads the app.
        </p>

        {err && (
          <div data-testid="limit-error" className="mt-4 flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl px-3 py-2.5">
            <AlertCircle size={14} className="text-red-500 flex-shrink-0 mt-0.5" />
            <p className="text-xs text-red-600">{err}</p>
          </div>
        )}

        <div className="flex gap-3 mt-5">
          <button
            onClick={submit}
            disabled={saving}
            data-testid="save-limits"
            className="flex-1 flex items-center justify-center gap-2 py-2.5 rounded-xl font-semibold text-sm bg-primary text-white hover:bg-primary/90 disabled:opacity-50 transition"
          >
            {saving && <Loader2 size={15} className="animate-spin" />}
            Save limits
          </button>
          <button
            onClick={onClose}
            disabled={saving}
            className="flex-1 py-2.5 rounded-xl font-semibold text-sm border border-bial-border text-tertiary hover:bg-bial-bg disabled:opacity-50 transition"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}

export interface UsersLimitsPanelProps {
  onToast: (msg: string) => void
}

interface UsersKeysetPage extends KeysetPage<UserLimitsOut> {
  defaults: Partial<LimitFields>
}

/**
 * Admin "Users & Limits" panel — the super-admin roster. Server-side keyset
 * pagination + search (via useKeysetList over fetchUsers, whose item key is
 * `users`, not `items`; `defaults` rides on the resolved page as `lastPage.defaults`),
 * a per-user usage-today + suspension column, a raise/reset-limits modal, and
 * deactivate / reactivate / reset-usage row actions with optimistic state +
 * reconcile-on-failure.
 *
 * Sorting, role/status filtering, and pagination are TanStack Table (client-side)
 * over the search-scoped roster: since the backend has no sort/role/status params
 * and no offset pagination (keyset only), the panel keeps chaining `loadMore()` in
 * the background until the current search's full result set is loaded, so a column
 * sort is never silently wrong about rows that haven't loaded yet. The search box
 * itself stays server-side (`q`) exactly as before — that's not a TanStack concern.
 *
 * The self-and-peer-super-admin guard (a super-admin is never suspendable, and the
 * caller is always a super-admin) is surfaced as a MISSING action on an ACTIVE
 * super-admin's row — a visible affordance, not a 403 discovered after the click.
 * A SUSPENDED super-admin is the one exception: role is derived at read time from
 * the env allowlist (ADR-0005), so that state is reachable with no 403 bypass (e.g.
 * suspended as a citizen, later added to the allowlist), and the server's
 * reactivate_user has no super-admin guard — so suspended wins over the guard and a
 * live Reactivate is offered instead of a permanent "Protected" dead end
 * (columns.tsx). RBAC is still enforced server-side; this is purely UI.
 */
export default function UsersLimitsPanel({ onToast }: UsersLimitsPanelProps) {
  // One controller per mount, aborted on unmount, so the background bulk-load chain
  // doesn't keep firing requests after the admin tabs away (AdminPage unmounts this
  // panel on tab switch) or navigates elsewhere. `fetchPage` reads `.signal` via
  // closure on every call — `useKeysetList` itself is untouched, it stays blind to
  // cancellation and just calls whatever `fetchPage` the caller supplied.
  //
  // The controller is created INSIDE the effect, not lazily on the ref at render time
  // (`if (!abortRef.current) abortRef.current = new AbortController()`) — StrictMode's
  // dev-only mount→cleanup→remount simulation would abort that one lazily-created
  // controller on the simulated unmount and then never replace it (the ref is already
  // non-null on the simulated remount), permanently dooming every real fetch with
  // "signal is aborted without reason". Creating a fresh controller in the effect body
  // itself means the simulated remount's effect run creates a second, live one.
  const abortRef = useRef<AbortController | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    abortRef.current = controller
    return () => controller.abort()
  }, [])

  const fetchPage = useCallback(async ({ cursor, q, limit }: { cursor: string | null; q: string; limit: number }): Promise<UsersKeysetPage> => {
    const page = await fetchUsers({ cursor, q, limit, signal: abortRef.current?.signal })
    // Adapt the roster envelope (`users`) into the hook's KeysetPage shape, keeping
    // `defaults` as a sibling key so it survives on `lastPage`.
    return { items: page.users, nextCursor: page.nextCursor, hasMore: page.hasMore, defaults: page.defaults }
  }, [])

  const { items: users, q, appliedQuery, loading, hasMore, error, lastPage, loadMore, setQuery, removeLocal } = useKeysetList<
    UserLimitsOut,
    UsersKeysetPage
  >({
    fetchPage,
    pageSize: FETCH_PAGE_SIZE,
  })
  const defaults = lastPage?.defaults ?? null

  const [editing, setEditing] = useState<MergedUser | null>(null)
  // Optimistic per-row patches keyed by userId: suspension flips land here immediately
  // and a successful limits edit merges its new {limits, effectiveLimits} in, so a
  // suspend/reactivate never refetches the whole list or loses the loaded pages.
  const [overrides, setOverrides] = useState<Record<string, Partial<MergedUser>>>({})
  const [busyId, setBusyId] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const [sorting, setSorting] = useState<SortingState>([])
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([])
  const [pagination, setPagination] = useState({ pageIndex: 0, pageSize: TABLE_PAGE_SIZE })

  // An AbortError (the unmount-cancellation controller above firing) is not a REAL
  // failure — it's this component's own cancellation, not the server's. Most visibly:
  // React StrictMode's dev-only mount→cleanup→remount simulation aborts the very
  // request the first (simulated) mount's effects kicked off, and by the time that
  // rejection is processed the remount's effects have already run and created a
  // fresh, live controller — so the "failure" is stale before it's even observed.
  // Treated as a real error, it would prevent auto-retry the same way a genuine
  // failure correctly does, permanently stranding the panel on "Couldn't load users
  // / signal is aborted without reason" for a cancellation nothing actually asked to
  // surface. `error.name` is 'AbortError' whether the abort came from AbortController
  // or fetch() itself, per the standard.
  const isAbortError = error?.name === 'AbortError'

  // useKeysetList does not self-load, and the backend has no offset/sort/filter
  // params — so this chains loadMore() to completion for the CURRENT search: it
  // re-fires whenever `loading`/`hasMore` change (i.e. after every successful page),
  // and stops on `hasMore === false`, a REAL failed page (`error` truthy and not an
  // abort, so a stuck request never retries in a tight loop), or the
  // MAX_LOADED_USERS ceiling. An abort is the one error that DOES retry — see above.
  //
  // The `appliedQuery === null || appliedQuery === q` guard closes a race: `q`
  // updates synchronously on every keystroke but `appliedQuery` (and the hook's
  // internal cursor) only catches up once the debounced search actually lands. Without
  // this guard, a background page landing mid-keystroke calls loadMore() with the
  // OLD cursor and the NEW query text — a mismatched slab gets appended under the new
  // search. `appliedQuery === null` is the one-time exception: nothing has landed yet,
  // so there is no cursor to race against, and blocking here would stall the very
  // first page load.
  useEffect(() => {
    if (
      !loading &&
      hasMore &&
      (!error || isAbortError) &&
      (appliedQuery === null || appliedQuery === q) &&
      users.length < MAX_LOADED_USERS
    ) {
      loadMore()
    }
  }, [loading, hasMore, error, isAbortError, appliedQuery, q, users.length, loadMore])

  // The search box lives outside TanStack's own state — a new search's results
  // must not leave the table stranded on a page index from the previous query.
  useEffect(() => {
    setPagination((p) => (p.pageIndex === 0 ? p : { ...p, pageIndex: 0 }))
  }, [appliedQuery])

  const mergeOverride = (id: string, patch: Partial<MergedUser>) => setOverrides((o) => ({ ...o, [id]: { ...o[id], ...patch } }))
  const dropOverride = (id: string) =>
    setOverrides((o) => {
      const next = { ...o }
      delete next[id]
      return next
    })

  const onDeactivate = async (u: MergedUser) => {
    const original = u.suspendedAt // pre-action snapshot for revert
    setActionError(null)
    setBusyId(u.userId)
    mergeOverride(u.userId, { suspendedAt: new Date().toISOString() }) // optimistic: suspended
    try {
      const resp = await deactivateUser(u.userId)
      mergeOverride(u.userId, { suspendedAt: resp.suspendedAt })
      onToast?.(`Suspended ${u.displayName || u.email}`)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // Another admin already suspended them — the optimistic "suspended" flip already
        // matches the server, so reconcile to it and stay quiet (no error toast).
      } else if (e instanceof ApiError && e.status === 404) {
        removeLocal((r) => r.userId === u.userId) // user is gone — drop the row
        dropOverride(u.userId)
      } else {
        // 403 (a super-admin slipped past the UI guard) or any other failure: revert.
        mergeOverride(u.userId, { suspendedAt: original })
        setActionError((e instanceof Error && e.message) || 'Could not suspend the user.')
      }
    } finally {
      setBusyId(null)
    }
  }

  const onReactivate = async (u: MergedUser) => {
    const original = u.suspendedAt
    setActionError(null)
    setBusyId(u.userId)
    mergeOverride(u.userId, { suspendedAt: null }) // optimistic: active
    try {
      const resp = await reactivateUser(u.userId)
      mergeOverride(u.userId, { suspendedAt: resp.suspendedAt })
      onToast?.(`Reactivated ${u.displayName || u.email}`)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // Not suspended on the server — the optimistic "active" flip already matches; stay quiet.
      } else if (e instanceof ApiError && e.status === 404) {
        removeLocal((r) => r.userId === u.userId)
        dropOverride(u.userId)
      } else {
        mergeOverride(u.userId, { suspendedAt: original }) // revert to suspended
        setActionError((e instanceof Error && e.message) || 'Could not reactivate the user.')
      }
    } finally {
      setBusyId(null)
    }
  }

  // Idempotent server-side (no 409) — resetting an already-zero day is a no-op — so
  // unlike deactivate/reactivate there is no "conflicting state" branch to reconcile.
  //
  // instanceof ApiError narrowing (PR #93 review finding 5) extended here too, past
  // this action's original merge: release/1.5.0 added onResetUsage with the same
  // duck-typed `e?.status === 404` the review flagged on deactivate/reactivate.
  // Leaving this one site unnarrowed while the other two were fixed would be worse
  // than not fixing any of them — disclosed as a delta alongside the other four in
  // the PR body, not silent.
  const onResetUsage = async (u: MergedUser) => {
    const original = u.usageToday
    setActionError(null)
    setBusyId(u.userId)
    mergeOverride(u.userId, { usageToday: 0 }) // optimistic
    try {
      const resp = await resetUserUsage(u.userId)
      mergeOverride(u.userId, { usageToday: resp.usageToday })
      onToast?.(`Reset today's usage for ${u.displayName || u.email}`)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        removeLocal((r) => r.userId === u.userId)
        dropOverride(u.userId)
      } else {
        mergeOverride(u.userId, { usageToday: original }) // revert
        setActionError((e instanceof Error && e.message) || "Could not reset the user's usage.")
      }
    } finally {
      setBusyId(null)
    }
  }

  // The array TanStack sorts/filters/paginates — merging overrides here (rather than
  // per-cell as before) means an optimistic suspend/reactivate is immediately visible
  // to sort order and to an active Status filter.
  const mergedUsers: MergedUser[] = useMemo(
    () => users.map((u) => ({ ...u, ...(overrides[u.userId] || {}) })),
    [users, overrides],
  )

  const columns = useMemo(
    () => createUserColumns({ onEdit: setEditing, onDeactivate, onReactivate, onResetUsage, busyId }),
    // onDeactivate/onReactivate/onResetUsage are plain closures recreated every render
    // (same as before this rebuild) — memoizing on their identity would defeat the
    // memo, so this intentionally tracks only what actually changes column rendering.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [busyId],
  )

  const table = useReactTable({
    data: mergedUsers,
    columns,
    state: { sorting, columnFilters, pagination },
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    onPaginationChange: setPagination,
    // `mergedUsers`'s identity changes on every background page append AND every
    // optimistic suspend/reactivate/limits-save, not just on a real sort/filter
    // change — the library's default (on) would silently bounce the user back to
    // page 1 on a plain Deactivate click. Page-1 resets are instead driven
    // explicitly below, only for the changes that should actually cause one.
    autoResetPageIndex: false,
    // Stable per-row identity (not the row's array index) so a 404 removeLocal
    // mid-page doesn't shift every later row's key and remount them, dropping focus.
    getRowId: (row) => row.userId,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
  })

  // A new sort or filter starts the user back at page 1 — explicit, since
  // autoResetPageIndex is off above (search-driven resets are the effect below,
  // keyed on appliedQuery instead: search lives outside TanStack's own state).
  useEffect(() => {
    setPagination((p) => (p.pageIndex === 0 ? p : { ...p, pageIndex: 0 }))
  }, [sorting, columnFilters])

  // Clamp a stranded pageIndex after the row count shrinks out from under it (a 404
  // removeLocal drops a row mid-page, or a filter narrows the set) — never leave the
  // view parked on a page that no longer exists.
  const pageCount = table.getPageCount()
  useEffect(() => {
    if (pageCount > 0 && pagination.pageIndex > pageCount - 1) {
      setPagination((p) => ({ ...p, pageIndex: pageCount - 1 }))
    }
  }, [pageCount, pagination.pageIndex])

  // Spinner covers the in-flight first fetch, the pre-fetch tick before the mount
  // effect fires (appliedQuery still null), AND an in-flight retry-after-abort —
  // otherwise a StrictMode-cancelled first request would flash "Couldn't load
  // users" before the auto-chain's silent retry (above) lands.
  if (users.length === 0 && (!error || isAbortError) && (loading || appliedQuery === null || isAbortError)) {
    return (
      <div className="flex items-center justify-center gap-2 py-16 text-neutral text-sm">
        <Loader2 size={16} className="animate-spin" /> Loading users…
      </div>
    )
  }

  if (error && !isAbortError && users.length === 0) {
    return (
      <div className="text-center py-16">
        <AlertCircle size={20} className="text-red-500 mx-auto mb-3" />
        <p className="text-sm text-tertiary font-semibold">Couldn’t load users</p>
        <p data-testid="users-load-error" className="text-xs text-neutral mt-1">
          {error.message}
        </p>
        <button
          onClick={loadMore}
          className="mt-4 inline-flex items-center gap-1.5 px-4 py-2 rounded-xl border border-bial-border text-sm font-medium text-tertiary hover:bg-bial-bg transition"
        >
          Retry
        </button>
      </div>
    )
  }

  const roleFilter = (table.getColumn('role')?.getFilterValue() as string | undefined) ?? 'all'
  const statusFilter = (table.getColumn('status')?.getFilterValue() as string | undefined) ?? 'all'
  const rows = table.getRowModel().rows
  // The background chain stopped on a failed page with more still unfetched: sort
  // order, filters, and "Page N of M" are all silently answering from a PARTIAL
  // roster until this retries — surfaced above the table, not as 12px of text below it.
  // An abort isn't a real failure (see isAbortError above) — it self-heals via the
  // auto-chain effect's own retry, so it never reaches this "give up" banner.
  const isPartial = hasMore && !!error && !isAbortError
  const isCapped = hasMore && (!error || isAbortError) && users.length >= MAX_LOADED_USERS

  return (
    <>
      <p className="text-xs text-neutral mb-4">
        Each user starts on the standard plan. Raise a user’s limits to approve a higher plan, or suspend a user to
        block them immediately.
      </p>

      <div className="flex flex-wrap items-center gap-3 mb-4">
        <div className="relative max-w-xs flex-1 min-w-[180px]">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral" />
          <input
            type="search"
            data-testid="users-search"
            value={q}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search name or email…"
            className="w-full pl-9 pr-3 py-2 text-sm border border-bial-border rounded-xl text-tertiary placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition"
          />
        </div>

        <Select
          value={roleFilter}
          onValueChange={(v: string) => table.getColumn('role')?.setFilterValue(v === 'all' ? undefined : v)}
        >
          <SelectTrigger data-testid="role-filter" className="w-[150px]">
            <SelectValue placeholder="All roles" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All roles</SelectItem>
            <SelectItem value="citizen">Citizen</SelectItem>
            <SelectItem value="super_admin">Super admin</SelectItem>
          </SelectContent>
        </Select>

        <Select
          value={statusFilter}
          onValueChange={(v: string) => table.getColumn('status')?.setFilterValue(v === 'all' ? undefined : v)}
        >
          <SelectTrigger data-testid="status-filter" className="w-[150px]">
            <SelectValue placeholder="All statuses" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            <SelectItem value="active">Active</SelectItem>
            <SelectItem value="suspended">Suspended</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {actionError && (
        <div data-testid="action-error" className="mb-4 flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl px-3 py-2.5">
          <AlertCircle size={14} className="text-red-500 flex-shrink-0 mt-0.5" />
          <p className="text-xs text-red-600 flex-1">{actionError}</p>
          <button onClick={() => setActionError(null)} className="text-red-400 hover:text-red-600">
            <X size={14} />
          </button>
        </div>
      )}

      {/* A failed background page must never silently vanish (fail-first), and it must
          never look like the whole roster is in and correctly sorted/filtered when it
          isn't — shown above the table, not tucked below it. */}
      {isPartial && (
        <div data-testid="loadmore-error" className="mb-4 flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-xl px-3 py-2.5">
          <AlertCircle size={14} className="text-amber-600 flex-shrink-0 mt-0.5" />
          <p className="text-xs text-amber-700 flex-1">
            Only {fmt(users.length)} users loaded — sorting, filtering, and paging only reflect what's loaded so far.{' '}
            {error?.message}
          </p>
          {/* No loading/"retrying…" state to wire up here: runFetch clears `error`
              (hence isPartial, hence this whole banner) in the same render pass that
              `loading` flips true, so that state is never reachable — the pager's
              "Loading more users…" caption takes over as the in-flight signal instead.
              A duplicate click is a no-op regardless, guarded by useKeysetList's own
              loadingRef check inside loadMore().
              What IS reachable: the banner can still be showing (isPartial only checks
              hasMore && error) after the user has typed a NEW search that hasn't landed
              yet — appliedQuery hasn't caught up to q. loadMore() reads cursorRef/qRef
              directly with no such awareness, so an unguarded click here would send the
              OLD query's cursor under the NEW query text: the exact stale-cursor append
              the auto-chain effect above is gated against. Mirror that same gate here. */}
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

      {isCapped && (
        <p className="mb-4 text-xs text-neutral bg-bial-bg border border-bial-border rounded-xl px-3 py-2.5">
          Showing the first {fmt(MAX_LOADED_USERS)} users — refine your search to narrow the results.
        </p>
      )}

      {users.length === 0 ? (
        // Read appliedQuery, not q: the live input runs 300ms ahead of the rows, so a
        // just-cleared search would claim the roster is empty while its refetch is still
        // in flight.
        <div className="text-center py-16 text-sm text-neutral">
          {appliedQuery ? `No users match “${appliedQuery}”.` : 'No users yet.'}
        </div>
      ) : rows.length === 0 ? (
        <div className="text-center py-16 text-sm text-neutral">No users match the selected filters.</div>
      ) : (
        <Table>
          <TableHeader>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id} className="border-b border-bial-border">
                {headerGroup.headers.map((header) => {
                  const sortDir = header.column.getIsSorted()
                  const ariaSort = !header.column.getCanSort()
                    ? undefined
                    : sortDir === 'asc'
                      ? 'ascending'
                      : sortDir === 'desc'
                        ? 'descending'
                        : 'none'
                  return (
                    <TableHead key={header.id} aria-sort={ariaSort} className={header.column.columnDef.meta?.className}>
                      {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                    </TableHead>
                  )
                })}
              </tr>
            ))}
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.id} data-testid={`row-${row.original.email}`} className="hover:bg-bial-bg/50">
                {row.getVisibleCells().map((cell) => (
                  <TableCell key={cell.id} className={cell.column.columnDef.meta?.className}>
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {users.length > 0 && (
        <div className="mt-5 flex items-center justify-between text-xs text-neutral">
          <div className="flex items-center gap-1.5">
            {hasMore && !error && (
              <>
                <Loader2 size={12} className="animate-spin" /> Loading more users…
              </>
            )}
          </div>
          <div className="flex items-center gap-3">
            <span>
              {fmt(users.length)} loaded · Page {table.getState().pagination.pageIndex + 1} of{' '}
              {Math.max(table.getPageCount(), 1)}
            </span>
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                data-testid="users-prev-page"
                onClick={() => table.previousPage()}
                disabled={!table.getCanPreviousPage()}
                className="p-1.5 rounded-lg border border-bial-border text-tertiary hover:bg-bial-bg disabled:opacity-50 disabled:cursor-not-allowed transition"
              >
                <ChevronLeft size={14} />
              </button>
              <button
                type="button"
                data-testid="users-next-page"
                onClick={() => table.nextPage()}
                disabled={!table.getCanNextPage()}
                className="p-1.5 rounded-lg border border-bial-border text-tertiary hover:bg-bial-bg disabled:opacity-50 disabled:cursor-not-allowed transition"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          </div>
        </div>
      )}

      {editing && defaults && (
        <EditModal
          user={{ ...editing, ...(overrides[editing.userId] || {}) }}
          defaults={defaults}
          onClose={() => setEditing(null)}
          onSaved={(updated) => {
            setEditing(null)
            if (updated) mergeOverride(updated.userId, { limits: updated.limits, effectiveLimits: updated.effectiveLimits })
          }}
          onToast={onToast}
        />
      )}
    </>
  )
}
