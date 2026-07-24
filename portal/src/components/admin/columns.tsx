import type { ColumnDef, Column, Row } from '@tanstack/react-table'
import { Pencil, Loader2, UserX, UserCheck, ShieldCheck, ArrowUpDown } from 'lucide-react'
import { fmt, roleLabel, LimitCell, SuspensionBadge } from './cells'

export interface MergedUser {
  userId: string
  email: string
  displayName?: string | null
  role: string
  suspendedAt: string | null
  usageToday?: number
  limits?: Record<string, number | null | undefined>
  effectiveLimits?: Record<string, number>
}

interface CreateUserColumnsArgs {
  onEdit: (user: MergedUser) => void
  onDeactivate: (user: MergedUser) => void
  onReactivate: (user: MergedUser) => void
  busyId: string | null
}

/** A sortable column header — mirrors the th's own text styling exactly (not the generic Button). */
function SortHeader({ label, column }: { label: string; column: Column<MergedUser, unknown> }) {
  const sorted = column.getIsSorted()
  return (
    <button
      type="button"
      onClick={() => column.toggleSorting(sorted === 'asc')}
      className="flex items-center gap-1 text-[10px] font-bold uppercase tracking-wider text-neutral hover:text-tertiary transition"
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
            {isSuper ? (
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
