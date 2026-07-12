import { useState, useEffect, useCallback } from 'react'
import { Pencil, X, AlertCircle, Loader2, Search, UserX, UserCheck, ShieldCheck } from 'lucide-react'
import { fetchUsers, updateUserLimits, deactivateUser, reactivateUser } from '../../utils/admin'
import { useKeysetList } from '../../hooks/useKeysetList'

// The model's real context window — a per-conversation hard limit can be
// lowered below this but never raised past it. Mirrors server/limits.js
// (the server is the real boundary; this is a friendly client-side guard).
const MODEL_CONTEXT_WINDOW = 200_000
const PAGE_SIZE = 25

const fmt = (n) => Number(n).toLocaleString('en-US')
const roleLabel = (role) => (role === 'super_admin' ? 'Super admin' : 'Citizen')

/** One numeric limit cell: the effective value + a "default" pill when not overridden. */
function LimitCell({ value, overridden }) {
  return (
    <div className="flex items-center gap-1.5 whitespace-nowrap">
      <span className="text-tertiary font-medium tabular-nums">{fmt(value)}</span>
      {overridden ? (
        <span className="text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded-full bg-primary/10 text-primary">
          custom
        </span>
      ) : (
        <span className="text-[9px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-full bg-gray-100 text-neutral">
          default
        </span>
      )}
    </div>
  )
}

/** Active / Suspended pill driven purely by `suspendedAt` (null = active). */
function SuspensionBadge({ email, suspendedAt }) {
  return suspendedAt ? (
    <span
      data-testid={`status-${email}`}
      className="inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-full bg-red-100 text-red-700"
    >
      Suspended
    </span>
  ) : (
    <span
      data-testid={`status-${email}`}
      className="inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-full bg-green-100 text-green-700"
    >
      Active
    </span>
  )
}

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
  const fetchPage = useCallback(async ({ cursor, q, limit }) => {
    const page = await fetchUsers({ cursor, q, limit })
    // Adapt the roster envelope (`users`) into the hook's KeysetPage shape, keeping
    // `defaults` as a sibling key so it survives on `lastPage`.
    return { items: page.users, nextCursor: page.nextCursor, hasMore: page.hasMore, defaults: page.defaults }
  }, [])

  const { items: users, q, appliedQuery, loading, hasMore, error, lastPage, loadMore, setQuery, removeLocal } = useKeysetList({
    fetchPage,
    pageSize: PAGE_SIZE,
  })
  const defaults = lastPage?.defaults ?? null

  const [editing, setEditing] = useState(null)
  // Optimistic per-row patches keyed by userId: suspension flips land here immediately
  // and a successful limits edit merges its new {limits, effectiveLimits} in, so a
  // suspend/reactivate never refetches the whole list or loses the loaded pages.
  const [overrides, setOverrides] = useState({})
  const [busyId, setBusyId] = useState(null)
  const [actionError, setActionError] = useState(null)

  // useKeysetList does not self-load; pull the first page once on mount.
  useEffect(() => {
    loadMore()
  }, [loadMore])

  const mergeOverride = (id, patch) => setOverrides((o) => ({ ...o, [id]: { ...o[id], ...patch } }))
  const dropOverride = (id) =>
    setOverrides((o) => {
      const next = { ...o }
      delete next[id]
      return next
    })

  const onDeactivate = async (u) => {
    const original = u.suspendedAt // pre-action snapshot for revert
    setActionError(null)
    setBusyId(u.userId)
    mergeOverride(u.userId, { suspendedAt: new Date().toISOString() }) // optimistic: suspended
    try {
      const resp = await deactivateUser(u.userId)
      mergeOverride(u.userId, { suspendedAt: resp.suspendedAt })
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
        mergeOverride(u.userId, { suspendedAt: original })
        setActionError(e?.message || 'Could not suspend the user.')
      }
    } finally {
      setBusyId(null)
    }
  }

  const onReactivate = async (u) => {
    const original = u.suspendedAt
    setActionError(null)
    setBusyId(u.userId)
    mergeOverride(u.userId, { suspendedAt: null }) // optimistic: active
    try {
      const resp = await reactivateUser(u.userId)
      mergeOverride(u.userId, { suspendedAt: resp.suspendedAt })
      onToast?.(`Reactivated ${u.displayName || u.email}`)
    } catch (e) {
      if (e?.status === 409) {
        // Not suspended on the server — the optimistic "active" flip already matches; stay quiet.
      } else if (e?.status === 404) {
        removeLocal((r) => r.userId === u.userId)
        dropOverride(u.userId)
      } else {
        mergeOverride(u.userId, { suspendedAt: original }) // revert to suspended
        setActionError(e?.message || 'Could not reactivate the user.')
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

      <div className="relative mb-4 max-w-xs">
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
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-bial-border">
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">User</th>
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Role</th>
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Status</th>
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Used today</th>
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Daily tokens</th>
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Per-conv warn</th>
                <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Per-conv max</th>
                <th className="pb-3 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-bial-border">
              {users.map((item) => {
                const u = { ...item, ...(overrides[item.userId] || {}) }
                const isSuper = u.role === 'super_admin'
                const suspended = u.suspendedAt != null
                const busy = busyId === u.userId
                return (
                  <tr key={u.userId} data-testid={`row-${u.email}`} className="hover:bg-bial-bg/50 transition">
                    <td className="py-3 pr-6">
                      <p className="font-semibold text-tertiary whitespace-nowrap">{u.displayName || u.email}</p>
                      <p className="text-[11px] text-neutral">{u.email}</p>
                    </td>
                    <td className="py-3 pr-6 capitalize text-neutral whitespace-nowrap">{roleLabel(u.role)}</td>
                    <td className="py-3 pr-6">
                      <SuspensionBadge email={u.email} suspendedAt={u.suspendedAt} />
                    </td>
                    <td className="py-3 pr-6 text-tertiary tabular-nums whitespace-nowrap">{fmt(u.usageToday ?? 0)}</td>
                    <td className="py-3 pr-6">
                      <LimitCell value={u.effectiveLimits?.dailyTokenLimit} overridden={Number.isInteger(u.limits?.dailyTokenLimit)} />
                    </td>
                    <td className="py-3 pr-6">
                      <LimitCell value={u.effectiveLimits?.contextSoftLimit} overridden={Number.isInteger(u.limits?.contextSoftLimit)} />
                    </td>
                    <td className="py-3 pr-6">
                      <LimitCell value={u.effectiveLimits?.contextHardLimit} overridden={Number.isInteger(u.limits?.contextHardLimit)} />
                    </td>
                    <td className="py-3">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <button
                          onClick={() => setEditing(item)}
                          data-testid={`edit-${u.email}`}
                          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-bial-border text-neutral hover:text-primary hover:bg-bial-bg transition text-xs font-medium"
                        >
                          <Pencil size={12} /> Edit
                        </button>
                        {isSuper ? (
                          // The self-and-peer guard, made visible: a super-admin is never
                          // suspendable, so no action is offered (rather than a 403 on click).
                          <span
                            data-testid={`noguard-${u.email}`}
                            title="Super-admins can’t be suspended"
                            className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium text-neutral/60"
                          >
                            <ShieldCheck size={12} /> Protected
                          </span>
                        ) : suspended ? (
                          <button
                            onClick={() => onReactivate(u)}
                            disabled={busy}
                            data-testid={`reactivate-${u.email}`}
                            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-bial-border text-green-600 hover:bg-green-50 transition text-xs font-medium disabled:opacity-50"
                          >
                            {busy ? <Loader2 size={12} className="animate-spin" /> : <UserCheck size={12} />} Reactivate
                          </button>
                        ) : (
                          <button
                            onClick={() => onDeactivate(u)}
                            disabled={busy}
                            data-testid={`deactivate-${u.email}`}
                            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-bial-border text-red-600 hover:bg-red-50 transition text-xs font-medium disabled:opacity-50"
                          >
                            {busy ? <Loader2 size={12} className="animate-spin" /> : <UserX size={12} />} Deactivate
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
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
