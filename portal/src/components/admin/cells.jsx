/**
 * Formatting + small badge/pill components shared between UsersLimitsPanel and its
 * TanStack Table column defs (columns.tsx). Hoisted out of the panel to avoid a
 * panel <-> columns circular import.
 */
export const fmt = (n) => Number(n).toLocaleString('en-US')
export const roleLabel = (role) => (role === 'super_admin' ? 'Super admin' : 'Citizen')

/** One numeric limit cell: the effective value + a "default" pill when not overridden.
 * `value` defaults to 0 (matching the column's accessorFn) so a row missing
 * `effectiveLimits` renders "0", not the literal string "NaN" that `fmt(undefined)`
 * produces while sorting has already treated the same row as a zero. */
export function LimitCell({ value, overridden }) {
  return (
    <div className="flex items-center gap-1.5 whitespace-nowrap">
      <span className="text-tertiary font-medium tabular-nums">{fmt(value ?? 0)}</span>
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
export function SuspensionBadge({ email, suspendedAt }) {
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
