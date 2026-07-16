import { Search } from 'lucide-react'

const STATUS_OPTIONS = [
  { value: 'all', label: 'All statuses' },
  { value: 'pending', label: 'Pending' },
  { value: 'approved', label: 'Approved' },
  { value: 'disabled', label: 'Disabled' },
]

/** Search input (moved verbatim from UsersLimitsPanel) + a native status-filter
 * select — no shadcn/Radix Select needed for one dropdown. */
export function UsersTableToolbar({ q, onQueryChange, statusFilter, onStatusFilterChange }) {
  return (
    <div className="flex items-center gap-3 mb-4">
      <div className="relative max-w-xs flex-1">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral" />
        <input
          type="search"
          data-testid="users-search"
          value={q}
          onChange={(e) => onQueryChange(e.target.value)}
          placeholder="Search name or email…"
          className="w-full pl-9 pr-3 py-2 text-sm border border-bial-border rounded-xl text-tertiary placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition"
        />
      </div>
      <select
        value={statusFilter}
        onChange={(e) => onStatusFilterChange(e.target.value)}
        data-testid="users-status-filter"
        className="py-2 px-3 text-sm border border-bial-border rounded-xl text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition"
      >
        {STATUS_OPTIONS.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </div>
  )
}
