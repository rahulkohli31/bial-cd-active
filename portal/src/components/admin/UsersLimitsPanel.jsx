import { useState, useEffect, useRef, useCallback } from 'react'
import { X, AlertCircle, Loader2 } from 'lucide-react'
import { fetchUsers, updateUserLimits, deactivateUser, reactivateUser, approveUser } from '../../utils/admin'
import { useKeysetList } from '../../hooks/useKeysetList'
import { getUserColumns } from './users-table/columns'
import { UsersDataTable } from './users-table/data-table'
import { UsersTableToolbar } from './users-table/toolbar'

// The model's real context window — a per-conversation hard limit can be
// lowered below this but never raised past it. Mirrors server/limits.js
// (the server is the real boundary; this is a friendly client-side guard).
const MODEL_CONTEXT_WINDOW = 200_000
const PAGE_SIZE = 25

const fmt = (n) => Number(n).toLocaleString('en-US')

/** One field of the edit modal: a number input with a "Use default" toggle. */
function LimitField({ name, label, hint, field, setField, defaultValue }) {
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
        placeholder={field.useDefault ? `${fmt(defaultValue)} (default)` : ''}
        onChange={(e) => setField({ ...field, value: e.target.value })}
        className="mt-1.5 w-full border border-bial-border rounded-lg px-3 py-2 text-sm text-tertiary tabular-nums focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary disabled:bg-gray-50 disabled:text-neutral transition"
      />
      {hint && <p className="text-[11px] text-neutral mt-1">{hint}</p>}
    </div>
  )
}

function EditModal({ user, defaults, onClose, onSaved, onToast }) {
  const init = (field, fallback) => {
    const has = Number.isInteger(user.limits?.[field])
    return { useDefault: !has, value: String(has ? user.limits[field] : fallback) }
  }
  const [daily, setDaily] = useState(() => init('dailyTokenLimit', defaults.dailyTokenLimit))
  const [soft, setSoft] = useState(() => init('contextSoftLimit', defaults.contextSoftLimit))
  const [hard, setHard] = useState(() => init('contextHardLimit', defaults.contextHardLimit))
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState(null)

  const submit = async () => {
    const dailyVal = daily.useDefault ? defaults.dailyTokenLimit : Number(daily.value)
    const softVal = soft.useDefault ? defaults.contextSoftLimit : Number(soft.value)
    const hardVal = hard.useDefault ? defaults.contextHardLimit : Number(hard.value)

    for (const [name, v] of [
      ['Daily token limit', dailyVal],
      ['Per-conversation warn', softVal],
      ['Per-conversation max', hardVal],
    ]) {
      if (!Number.isInteger(v) || v <= 0) {
        setErr(`${name} must be a positive whole number.`)
        return
      }
    }
    if (hardVal > MODEL_CONTEXT_WINDOW) {
      setErr(`Per-conversation max can't exceed ${fmt(MODEL_CONTEXT_WINDOW)} (the model's context window).`)
      return
    }
    if (softVal >= hardVal) {
      setErr('Per-conversation warn must be less than the max.')
      return
    }

    const patch = {
      dailyTokenLimit: daily.useDefault ? null : dailyVal,
      contextSoftLimit: soft.useDefault ? null : softVal,
      contextHardLimit: hard.useDefault ? null : hardVal,
    }
    setSaving(true)
    setErr(null)
    try {
      const updated = await updateUserLimits(user.userId, patch)
      onToast(`Limits updated for ${user.displayName || user.email}`)
      onSaved(updated)
    } catch (e) {
      setErr(e.message)
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
            hint="Show the “getting long” banner at this many tokens."
            field={soft}
            setField={setSoft}
            defaultValue={defaults.contextSoftLimit}
          />
          <LimitField
            name="hard"
            label="Per-conversation max"
            hint={`Hard stop for a single chat. Max ${fmt(MODEL_CONTEXT_WINDOW)} (model window).`}
            field={hard}
            setField={setHard}
            defaultValue={defaults.contextHardLimit}
          />
        </div>

        {/* Propagation reality (docs/solutions: per-user-limits daily-vs-context): the daily
            limit is a live server read; the context limits ride the cached profile. Never
            imply a context change is instant. */}
        <p className="text-[11px] text-neutral mt-4 leading-relaxed">
          The <strong className="text-tertiary">daily token limit</strong> takes effect on the user’s next request. The{' '}
          <strong className="text-tertiary">per-conversation limits</strong> only take effect after the user reloads the app.
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

/**
 * Admin "Users & Limits" panel — the super-admin roster. Server-side keyset
 * pagination + search (via useKeysetList over fetchUsers, whose item key is
 * `users`, not `items`; `defaults` rides on the resolved page as `lastPage.defaults`),
 * a per-user usage-today + suspension column, a raise/reset-limits modal, and
 * deactivate / reactivate row actions with optimistic state + reconcile-on-failure.
 *
 * The self-and-peer-super-admin guard (a super-admin is never suspendable, and the
 * caller is always a super-admin) is surfaced as a MISSING action on super-admin rows
 * — a visible affordance, not a 403 discovered after the click. RBAC is still enforced
 * server-side; this is purely UI.
 */
export default function UsersLimitsPanel({ onToast }) {
  const [statusFilter, setStatusFilter] = useState('all')

  const fetchPage = useCallback(
    async ({ cursor, q, limit }) => {
      const page = await fetchUsers({ cursor, q, limit, status: statusFilter === 'all' ? undefined : statusFilter })
      // Adapt the roster envelope (`users`) into the hook's KeysetPage shape, keeping
      // `defaults` as a sibling key so it survives on `lastPage`.
      return { items: page.users, nextCursor: page.nextCursor, hasMore: page.hasMore, defaults: page.defaults }
    },
    [statusFilter],
  )

  const { items: users, q, appliedQuery, loading, hasMore, error, lastPage, loadMore, setQuery, refresh, removeLocal } = useKeysetList({
    fetchPage,
    pageSize: PAGE_SIZE,
  })
  const defaults = lastPage?.defaults ?? null

  const [editing, setEditing] = useState(null)
  // Optimistic per-row patches keyed by userId: suspension/approval flips land here
  // immediately and a successful limits edit merges its new {limits, effectiveLimits}
  // in, so an action never refetches the whole list or loses the loaded pages.
  const [overrides, setOverrides] = useState({})
  const [busyId, setBusyId] = useState(null)
  const [actionError, setActionError] = useState(null)

  // useKeysetList does not self-load; pull the first page once on mount.
  useEffect(() => {
    loadMore()
  }, [loadMore])

  // Changing the status filter must reload page 1 under the NEW filter while
  // keeping the search query intact — refresh() does exactly that (reset()
  // would also wipe q). Compares against the LAST SEEN value rather than a
  // "have we ever run" boolean flag: a flag that flips permanently on first run
  // doesn't survive React 18 StrictMode's dev-only double-invoke of a fresh
  // mount's effects (the ref stays flipped from the first invocation, so the
  // second sees it already flipped and calls refresh() anyway — an extra fetch
  // on every mount in development). Comparing to the previous VALUE is instead
  // naturally idempotent: both invocations of the mount pair see the same
  // (starting) statusFilter and skip, since neither one mutates the ref on the
  // "unchanged" path — only a genuine change does.
  const prevStatusFilterRef = useRef(statusFilter)
  useEffect(() => {
    if (prevStatusFilterRef.current === statusFilter) return
    prevStatusFilterRef.current = statusFilter
    refresh()
  }, [statusFilter, refresh])

  const mergeOverride = (id, patch) => setOverrides((o) => ({ ...o, [id]: { ...o[id], ...patch } }))
  const dropOverride = (id) =>
    setOverrides((o) => {
      const next = { ...o }
      delete next[id]
      return next
    })

  // A successful (or optimistic) status change must not leave a row sitting in a
  // now-mismatched filtered view — e.g. approving a row while filtered to "Pending"
  // must drop it from that list immediately, not just flip its badge in place.
  // Safe to call at both the optimistic and the confirmed step: once a row has been
  // removed, a second removeLocal for the same id is a harmless no-op.
  const applyStatusPatch = (u, patch) => {
    if (statusFilter !== 'all' && patch.status !== statusFilter) {
      removeLocal((r) => r.userId === u.userId)
      dropOverride(u.userId)
    } else {
      mergeOverride(u.userId, patch)
    }
  }

  const onDeactivate = async (u) => {
    const original = { suspendedAt: u.suspendedAt, status: u.status } // pre-action snapshot for revert
    setActionError(null)
    setBusyId(u.userId)
    applyStatusPatch(u, { suspendedAt: new Date().toISOString(), status: 'disabled' }) // optimistic
    try {
      const resp = await deactivateUser(u.userId)
      applyStatusPatch(u, { suspendedAt: resp.suspendedAt, status: 'disabled' })
      onToast?.(`Suspended ${u.displayName || u.email}`)
    } catch (e) {
      if (e?.status === 409) {
        // Another admin already suspended them — the optimistic "suspended" flip already
        // matches the server, so reconcile to it and stay quiet (no error toast).
      } else if (e?.status === 404) {
        removeLocal((r) => r.userId === u.userId) // user is gone — drop the row
        dropOverride(u.userId)
      } else {
        // 403 (a super-admin slipped past the UI guard) or any other failure: revert.
        mergeOverride(u.userId, original)
        setActionError(e?.message || 'Could not suspend the user.')
      }
    } finally {
      setBusyId(null)
    }
  }

  const onReactivate = async (u) => {
    const original = { suspendedAt: u.suspendedAt, status: u.status }
    // Disabled wins over pending (User.status()'s own precedence) — clearing suspension
    // reveals whichever state was underneath: still-pending if never approved, else approved.
    const underlyingStatus = u.approvedAt ? 'approved' : 'pending'
    setActionError(null)
    setBusyId(u.userId)
    applyStatusPatch(u, { suspendedAt: null, status: underlyingStatus }) // optimistic
    try {
      const resp = await reactivateUser(u.userId)
      applyStatusPatch(u, { suspendedAt: resp.suspendedAt, status: underlyingStatus })
      onToast?.(`Reactivated ${u.displayName || u.email}`)
    } catch (e) {
      if (e?.status === 409) {
        // Not suspended on the server — the optimistic "active" flip already matches; stay quiet.
      } else if (e?.status === 404) {
        removeLocal((r) => r.userId === u.userId)
        dropOverride(u.userId)
      } else {
        mergeOverride(u.userId, original) // revert to suspended
        setActionError(e?.message || 'Could not reactivate the user.')
      }
    } finally {
      setBusyId(null)
    }
  }

  const onApprove = async (u) => {
    const original = { approvedAt: u.approvedAt, status: u.status }
    setActionError(null)
    setBusyId(u.userId)
    applyStatusPatch(u, { approvedAt: new Date().toISOString(), status: 'approved' }) // optimistic
    try {
      const resp = await approveUser(u.userId)
      applyStatusPatch(u, { approvedAt: resp.approvedAt, status: 'approved' })
      onToast?.(`Approved ${u.displayName || u.email}`)
    } catch (e) {
      if (e?.status === 409) {
        // Another admin already approved them — the optimistic flip already matches; stay quiet.
      } else if (e?.status === 404) {
        removeLocal((r) => r.userId === u.userId)
        dropOverride(u.userId)
      } else {
        mergeOverride(u.userId, original)
        setActionError(e?.message || 'Could not approve the user.')
      }
    } finally {
      setBusyId(null)
    }
  }

  // Spinner covers both the in-flight first fetch AND the pre-fetch tick before the
  // mount effect fires (appliedQuery still null) — otherwise the empty state flashes.
  if (users.length === 0 && !error && (loading || appliedQuery === null)) {
    return (
      <div className="flex items-center justify-center gap-2 py-16 text-neutral text-sm">
        <Loader2 size={16} className="animate-spin" /> Loading users…
      </div>
    )
  }

  if (error && users.length === 0) {
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

  return (
    <>
      <p className="text-xs text-neutral mb-4">
        Each user starts on the standard plan. Raise a user’s limits to approve a higher plan, or suspend a user to
        block them immediately.
      </p>

      <UsersTableToolbar
        q={q}
        onQueryChange={setQuery}
        statusFilter={statusFilter}
        onStatusFilterChange={setStatusFilter}
      />

      {actionError && (
        <div data-testid="action-error" className="mb-4 flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl px-3 py-2.5">
          <AlertCircle size={14} className="text-red-500 flex-shrink-0 mt-0.5" />
          <p className="text-xs text-red-600 flex-1">{actionError}</p>
          <button onClick={() => setActionError(null)} className="text-red-400 hover:text-red-600">
            <X size={14} />
          </button>
        </div>
      )}

      {users.length === 0 ? (
        // Read appliedQuery, not q: the live input runs 300ms ahead of the rows, so a
        // just-cleared search would claim the roster is empty while its refetch is still
        // in flight.
        <div className="text-center py-16 text-sm text-neutral">
          {appliedQuery ? `No users match “${appliedQuery}”.` : 'No users yet.'}
        </div>
      ) : (
        <UsersDataTable
          columns={getUserColumns({
            onEdit: (item) => setEditing(item),
            onApprove,
            onDeactivate,
            onReactivate,
            busyId,
          })}
          data={users.map((item) => ({ ...item, ...(overrides[item.userId] || {}) }))}
        />
      )}

      {/* A failed "Load more" must never silently vanish (fail-first) — surface it
          without dropping the rows already loaded. */}
      {error && users.length > 0 && (
        <p data-testid="loadmore-error" className="mt-3 text-center text-xs text-red-600">
          {error.message}
        </p>
      )}

      {hasMore && (
        <div className="mt-5 text-center">
          <button
            onClick={loadMore}
            disabled={loading}
            data-testid="load-more-users"
            className="inline-flex items-center gap-2 px-4 py-2 rounded-xl border border-bial-border text-sm font-medium text-tertiary hover:bg-bial-bg disabled:opacity-50 transition"
          >
            {loading && <Loader2 size={14} className="animate-spin" />} Load more
          </button>
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
