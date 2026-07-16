import { Pencil, Loader2, UserCheck, UserX, ShieldCheck, UserPlus } from 'lucide-react'
import { Badge } from '../../ui/badge'

const fmt = (n) => Number(n).toLocaleString('en-US')
const roleLabel = (role) => (role === 'super_admin' ? 'Super admin' : 'Citizen')

/** One numeric limit cell: the effective value + a "default"/"custom" pill. */
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

/** 3-state status pill, replacing the old suspension-only Active/Suspended badge. */
export function StatusBadge({ email, status }) {
  const props = { 'data-testid': `status-${email}` }
  if (status === 'disabled') return <Badge variant="danger" {...props}>Suspended</Badge>
  if (status === 'pending') return <Badge variant="neutral" {...props}>Pending</Badge>
  return <Badge variant="success" {...props}>Active</Badge>
}

/**
 * Column defs for the users roster. Actions stay plain inline buttons (Edit +
 * conditional Approve + Suspend/Reactivate/Protected) — this app has no
 * existing dropdown-menu affordance and one wasn't worth introducing just for
 * 2-4 buttons (see the admin-approval-gate plan's hand-roll decision).
 */
export function getUserColumns({ onEdit, onApprove, onDeactivate, onReactivate, busyId }) {
  return [
    {
      id: 'user',
      accessorFn: (u) => u.displayName || u.email,
      header: 'User',
      cell: ({ row }) => (
        <>
          <p className="font-semibold text-tertiary whitespace-nowrap">
            {row.original.displayName || row.original.email}
          </p>
          <p className="text-[11px] text-neutral">{row.original.email}</p>
        </>
      ),
    },
    {
      id: 'role',
      accessorFn: (u) => roleLabel(u.role),
      header: 'Role',
      cell: ({ row }) => (
        <span className="capitalize text-neutral whitespace-nowrap">{roleLabel(row.original.role)}</span>
      ),
    },
    {
      id: 'status',
      accessorKey: 'status',
      header: 'Status',
      cell: ({ row }) => <StatusBadge email={row.original.email} status={row.original.status} />,
    },
    {
      id: 'usageToday',
      accessorFn: (u) => u.usageToday ?? 0,
      header: 'Used today',
      cell: ({ row }) => (
        <span className="text-tertiary tabular-nums whitespace-nowrap">{fmt(row.original.usageToday ?? 0)}</span>
      ),
    },
    {
      id: 'dailyTokenLimit',
      accessorFn: (u) => u.effectiveLimits?.dailyTokenLimit ?? 0,
      header: 'Daily tokens',
      cell: ({ row }) => (
        <LimitCell
          value={row.original.effectiveLimits?.dailyTokenLimit}
          overridden={Number.isInteger(row.original.limits?.dailyTokenLimit)}
        />
      ),
    },
    {
      id: 'contextSoftLimit',
      accessorFn: (u) => u.effectiveLimits?.contextSoftLimit ?? 0,
      header: 'Per-conv warn',
      cell: ({ row }) => (
        <LimitCell
          value={row.original.effectiveLimits?.contextSoftLimit}
          overridden={Number.isInteger(row.original.limits?.contextSoftLimit)}
        />
      ),
    },
    {
      id: 'contextHardLimit',
      accessorFn: (u) => u.effectiveLimits?.contextHardLimit ?? 0,
      header: 'Per-conv max',
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
            {u.status === 'pending' && (
              <button
                onClick={() => onApprove(u)}
                disabled={busy}
                data-testid={`approve-${u.email}`}
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-bial-border text-primary hover:bg-primary/10 transition text-xs font-medium disabled:opacity-50"
              >
                {busy ? <Loader2 size={12} className="animate-spin" /> : <UserPlus size={12} />} Approve
              </button>
            )}
            {isSuper ? (
              <span
                data-testid={`noguard-${u.email}`}
                title="Super-admins can’t be suspended"
                className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium text-neutral/60"
              >
                <ShieldCheck size={12} /> Protected
              </span>
            ) : u.status === 'disabled' ? (
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
