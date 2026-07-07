import { useState, useEffect, useCallback } from 'react'
import { Pencil, X, AlertCircle, Loader2, RefreshCw } from 'lucide-react'
import { fetchUsers, updateUserLimits } from '../../utils/admin'

// The model's real context window — a per-conversation hard limit can be
// lowered below this but never raised past it. Mirrors server/limits.js
// (the server is the real boundary; this is a friendly client-side guard).
const MODEL_CONTEXT_WINDOW = 200_000

const fmt = (n) => Number(n).toLocaleString('en-US')

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
      await updateUserLimits(user.userId, patch)
      onToast(`Limits updated for ${user.displayName || user.email}`)
      onSaved()
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
 * Admin "Users & Limits" panel — lists every portal user with their effective
 * usage limits and lets an admin raise/reset a user's plan. Backed by the real
 * /api/admin endpoints (admin-gated server-side); mounts when the tab opens.
 */
export default function UsersLimitsPanel({ onToast }) {
  const [users, setUsers] = useState([])
  const [defaults, setDefaults] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [editing, setEditing] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const { users: list, defaults: def } = await fetchUsers()
      setUsers(list)
      setDefaults(def)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (loading) {
    return (
      <div className="flex items-center justify-center gap-2 py-16 text-neutral text-sm">
        <Loader2 size={16} className="animate-spin" /> Loading users…
      </div>
    )
  }

  if (error) {
    return (
      <div className="text-center py-16">
        <AlertCircle size={20} className="text-red-500 mx-auto mb-3" />
        <p className="text-sm text-tertiary font-semibold">Couldn’t load users</p>
        <p className="text-xs text-neutral mt-1">{error}</p>
        <button
          onClick={load}
          className="mt-4 inline-flex items-center gap-1.5 px-4 py-2 rounded-xl border border-bial-border text-sm font-medium text-tertiary hover:bg-bial-bg transition"
        >
          <RefreshCw size={14} /> Retry
        </button>
      </div>
    )
  }

  return (
    <>
      <p className="text-xs text-neutral mb-4">
        Each user starts on the standard plan. Raise a user’s limits here to approve a higher plan, or reset a field to
        fall back to the default.
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-bial-border">
              <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">User</th>
              <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Role</th>
              <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Daily tokens</th>
              <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Per-conv warn</th>
              <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Per-conv max</th>
              <th className="pb-3 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-bial-border">
            {users.map((u) => (
              <tr key={u.userId} data-testid={`row-${u.email}`} className="hover:bg-bial-bg/50 transition">
                <td className="py-3 pr-6">
                  <p className="font-semibold text-tertiary whitespace-nowrap">{u.displayName || u.email}</p>
                  <p className="text-[11px] text-neutral">{u.email}</p>
                </td>
                <td className="py-3 pr-6 capitalize text-neutral">{u.role}</td>
                <td className="py-3 pr-6">
                  <LimitCell value={u.effectiveLimits.dailyTokenLimit} overridden={Number.isInteger(u.limits?.dailyTokenLimit)} />
                </td>
                <td className="py-3 pr-6">
                  <LimitCell value={u.effectiveLimits.contextSoftLimit} overridden={Number.isInteger(u.limits?.contextSoftLimit)} />
                </td>
                <td className="py-3 pr-6">
                  <LimitCell value={u.effectiveLimits.contextHardLimit} overridden={Number.isInteger(u.limits?.contextHardLimit)} />
                </td>
                <td className="py-3">
                  <button
                    onClick={() => setEditing(u)}
                    data-testid={`edit-${u.email}`}
                    className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-bial-border text-neutral hover:text-primary hover:bg-bial-bg transition text-xs font-medium"
                  >
                    <Pencil size={12} /> Edit
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {editing && defaults && (
        <EditModal
          user={editing}
          defaults={defaults}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            load()
          }}
          onToast={onToast}
        />
      )}
    </>
  )
}
