import type { AriaAttributes } from 'react'
import type { Column } from '@tanstack/react-table'

/** A column's `aria-sort`: reported while it is sorted, and as `none` while it could be but is not. */
export function ariaSortOf<TRow>(column: Column<TRow, unknown>): AriaAttributes['aria-sort'] {
  const sorted = column.getIsSorted()
  if (sorted === 'asc') return 'ascending'
  if (sorted === 'desc') return 'descending'
  return column.getCanSort() ? 'none' : undefined
}
