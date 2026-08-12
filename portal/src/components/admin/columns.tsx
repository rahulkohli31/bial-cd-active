import type { ColumnDef, Column, Row } from '@tanstack/react-table'
import { Pencil, Loader2, UserX, UserCheck, ShieldCheck, ArrowUpDown, RotateCcw } from 'lucide-react'
import { tableHeadLabelClass } from '../ui/table'
import type { LimitFields } from '../../utils/admin'

// Formatting + small badge/pill helpers shared with UsersLimitsPanel (LimitField/
// EditModal import `fmt` from here too). Defined directly in this file — not a
// separate cells.jsx — because TanStack's ColumnDef<MergedUser> already type-checks
// every cell renderer below; routing these through an allowJs/checkJs:false JS file
// would let them cross into this typed file unchecked. The panel already imports
// FROM columns.tsx (createUserColumns), so importing `fmt` etc. the same way creates
// no panel<->columns cycle — columns.tsx never imports from the panel.
export const fmt = (n: number): string => Number(n).toLocaleString('en-US')
export const roleLabel = (role: string): string => (role === 'super_admin' ? 'Super admin' : 'Citizen')

/** One numeric limit cell: the effective value + a "default" pill when not overridden.
 * `value` defaults to 0 (matching the column's accessorFn) so a row missing
 * `effectiveLimits` renders "0", not the literal string "NaN" that `fmt(undefined)`
 * produces while sorting has already treated the same row as a zero. `null` is
 * folded the same way as `undefined` — LimitFields' fields are `number | null`
 * on the wire, "no value" either way. */
function LimitCell({ value, overridden }: { value: number | null | undefined; overridden: boolean }) {
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
function SuspensionBadge({ email, suspendedAt }: { email: string; suspendedAt: string | null }) {
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

// Lets a column def carry its own header/cell className (e.g. the Actions column's
// `pr-0`) instead of the panel re-deriving `id === 'actions' ? ... : ...` twice —
// once for TableHead, once for TableCell — with the two checks free to drift.
declare module '@tanstack/react-table' {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData, TValue> {
    className?: string
  }
}

export interface MergedUser {
  userId: string
  email: string
  displayName?: string | null
  role: string
  suspendedAt: string | null
  usageToday?: number
  limits?: LimitFields
  effectiveLimits?: LimitFields
}

interface CreateUserColumnsArgs {
  onEdit: (user: MergedUser) => void
  onDeactivate: (user: MergedUser) => void
  onReactivate: (user: MergedUser) => void
  onResetUsage: (user: MergedUser) => void
  busyId: string | null
}

/** A sortable column header — shares TableHead's own label styling (tableHeadLabelClass) so the two can't drift. */
function SortHeader({ label, column }: { label: string; column: Column<MergedUser, unknown> }) {
  const sorted = column.getIsSorted()
  return (
    <button
      type="button"
      data-testid={`sort-${column.id}`}
      onClick={() => column.toggleSorting(sorted === 'asc')}
      className={`flex items-center gap-1 hover:text-tertiary transition ${tableHeadLabelClass}`}
    >
      {label}
      <ArrowUpDown size={10} className={sorted ? 'text-primary' : 'text-neutral/40'} />
    </button>
  )
}

const equalsOrAll = (row: Row<MergedUser>, columnId: string, value: unknown) =>
  value === undefined || value === 'all' || row.getValue(columnId) === value

/**
 * Column defs for the admin roster table. A factory (not a static export) because
 * the Actions cell needs the row-action handlers + busyId in closure — every line of
 * the optimistic-update/error-branch logic stays in UsersLimitsPanel.jsx untouched;
 * only where this JSX lives moves.
 */
export function createUserColumns({
  onEdit,
  onDeactivate,
  onReactivate,
  onResetUsage,
  busyId,
}: CreateUserColumnsArgs): ColumnDef<MergedUser>[] {
  return [
    {
      id: 'user',
      accessorFn: (row) => row.displayName || row.email,
      header: ({ column }) => <SortHeader label="User" column={column} />,
      cell: ({ row }) => {
        const u = row.original
        return (
          <>
            <p className="font-semibold text-tertiary whitespace-nowrap">{u.displayName || u.email}</p>
            <p className="text-[11px] text-neutral">{u.email}</p>
          </>
        )
      },
    },
    {
      id: 'role',
      accessorFn: (row) => row.role,
      header: ({ column }) => <SortHeader label="Role" column={column} />,
      cell: ({ row }) => <span className="capitalize text-neutral whitespace-nowrap">{roleLabel(row.original.role)}</span>,
      filterFn: equalsOrAll,
    },
    {
      id: 'status',
      accessorFn: (row) => (row.suspendedAt ? 'suspended' : 'active'),
      header: ({ column }) => <SortHeader label="Status" column={column} />,
      cell: ({ row }) => <SuspensionBadge email={row.original.email} suspendedAt={row.original.suspendedAt} />,
      filterFn: equalsOrAll,
    },
    {
      id: 'usageToday',
      accessorFn: (row) => row.usageToday ?? 0,
      header: ({ column }) => <SortHeader label="Used today" column={column} />,
      cell: ({ getValue }) => (
        <span className="text-tertiary tabular-nums whitespace-nowrap">{fmt(getValue() as number)}</span>
      ),
    },
    {
      id: 'dailyTokenLimit',
      accessorFn: (row) => row.effectiveLimits?.dailyTokenLimit ?? 0,
      header: ({ column }) => <SortHeader label="Daily tokens" column={column} />,
      cell: ({ row }) => (
        <LimitCell
          value={row.original.effectiveLimits?.dailyTokenLimit}
          overridden={Number.isInteger(row.original.limits?.dailyTokenLimit)}
        />
      ),
    },
    {
      id: 'contextSoftLimit',
      accessorFn: (row) => row.effectiveLimits?.contextSoftLimit ?? 0,
      header: ({ column }) => <SortHeader label="Per-conv warn" column={column} />,
      cell: ({ row }) => (
        <LimitCell
          value={row.original.effectiveLimits?.contextSoftLimit}
          overridden={Number.isInteger(row.original.limits?.contextSoftLimit)}
        />
      ),
    },
    {
      id: 'contextHardLimit',
      accessorFn: (row) => row.effectiveLimits?.contextHardLimit ?? 0,
      header: ({ column }) => <SortHeader label="Per-conv max" column={column} />,
      cell: ({ row }) => (
        <LimitCell
          value={row.original.effectiveLimits?.contextHardLimit}
          overridden={Number.isInteger(row.original.limits?.contextHardLimit)}
        />
      ),
    },
    {
      id: 'actions',
      header: 'Actions',
      enableSorting: false,
      meta: { className: 'pr-0' },
      cell: ({ row }) => {
        const u = row.original
        const isSuper = u.role === 'super_admin'
        const suspended = u.suspendedAt != null
        const busy = busyId === u.userId
        return (
          <div className="flex items-center gap-1.5 flex-wrap">
            <button
              onClick={() => onEdit(u)}
              data-testid={`edit-${u.email}`}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-bial-border text-neutral hover:text-primary hover:bg-bial-bg transition text-xs font-medium"
            >
              <Pencil size={12} /> Edit
            </button>
            <button
              onClick={() => onResetUsage(u)}
              disabled={busy || !u.usageToday}
              title="Zero out today's usage so they don't have to wait for the midnight IST rollover"
              data-testid={`reset-usage-${u.email}`}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-bial-border text-blue-600 hover:bg-blue-50 transition text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {busy ? <Loader2 size={12} className="animate-spin" /> : <RotateCcw size={12} />} Reset usage
            </button>
            {/* Suspended wins over the super-admin guard: role is derived at read time
                from the env allowlist (ADR-0005), so a suspended user who later lands on
                that allowlist is reachable with no 403 bypass — checking isSuper first
                would strand them behind "Protected" with no Reactivate, forever, even
                though reactivate_user has no super-admin guard and would restore them. */}
            {isSuper && !suspended ? (
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
        )
      },
    },
  ]
}
