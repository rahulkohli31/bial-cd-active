/**
 * Small pill badge — variant-based, plain Tailwind (no shadcn/Radix; see
 * table.jsx's header comment). Colors mirror the pre-existing SuspensionBadge.
 */
const VARIANTS = {
  success: 'bg-green-100 text-green-700',
  danger: 'bg-red-100 text-red-700',
  neutral: 'bg-gray-100 text-neutral',
}

export function Badge({ variant = 'neutral', className = '', ...props }) {
  return (
    <span
      className={`inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-full ${VARIANTS[variant]} ${className}`}
      {...props}
    />
  )
}
