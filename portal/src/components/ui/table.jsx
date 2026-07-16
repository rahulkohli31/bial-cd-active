/**
 * Plain-Tailwind table primitives — no shadcn/Radix (this branch predates PR
 * #35's shadcn toolchain; see the admin-approval-gate plan for why). Classes
 * mirror UsersLimitsPanel's previous inline `<table>` exactly, so swapping it
 * for <UsersDataTable> is a visual no-op.
 */
export function Table({ className = '', ...props }) {
  return (
    <div className="overflow-x-auto">
      <table className={`w-full text-sm ${className}`} {...props} />
    </div>
  )
}

export function TableHeader(props) {
  return <thead {...props} />
}

export function TableBody({ className = '', ...props }) {
  return <tbody className={`divide-y divide-bial-border ${className}`} {...props} />
}

export function TableRow({ className = '', ...props }) {
  return <tr className={`hover:bg-bial-bg/50 transition ${className}`} {...props} />
}

export function TableHead({ className = '', ...props }) {
  return (
    <th
      className={`pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral last:pr-0 ${className}`}
      {...props}
    />
  )
}

export function TableCell({ className = '', ...props }) {
  return <td className={`py-3 pr-6 last:pr-0 ${className}`} {...props} />
}
