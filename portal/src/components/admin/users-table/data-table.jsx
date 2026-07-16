import { useState } from 'react'
import { ChevronUp, ChevronDown, ChevronsUpDown } from 'lucide-react'
import { flexRender, getCoreRowModel, getSortedRowModel, useReactTable } from '@tanstack/react-table'
import { Table, TableBody, TableCell, TableHeader, TableRow } from '../../ui/table'

/**
 * TanStack Table wrapper. Sorting is CLIENT-SIDE over whatever rows the keyset
 * pagination has loaded so far (an accepted limitation, not a true global sort
 * — see the admin-approval-gate plan). No built-in pagination: "Load more"
 * stays server-driven in UsersLimitsPanel.
 */
export function UsersDataTable({ columns, data }) {
  const [sorting, setSorting] = useState([])

  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getRowId: (row) => row.userId,
  })

  return (
    <Table>
      <TableHeader>
        {table.getHeaderGroups().map((headerGroup) => (
          <tr key={headerGroup.id} className="border-b border-bial-border">
            {headerGroup.headers.map((header) => {
              const sortable = header.column.getCanSort()
              const sortDir = header.column.getIsSorted()
              return (
                <th
                  key={header.id}
                  className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral last:pr-0"
                >
                  {sortable ? (
                    <button
                      type="button"
                      onClick={header.column.getToggleSortingHandler()}
                      data-testid={`sort-${header.column.id}`}
                      className="flex items-center gap-1 hover:text-tertiary transition"
                    >
                      {flexRender(header.column.columnDef.header, header.getContext())}
                      {sortDir === 'asc' ? (
                        <ChevronUp size={12} />
                      ) : sortDir === 'desc' ? (
                        <ChevronDown size={12} />
                      ) : (
                        <ChevronsUpDown size={12} className="text-neutral/40" />
                      )}
                    </button>
                  ) : (
                    flexRender(header.column.columnDef.header, header.getContext())
                  )}
                </th>
              )
            })}
          </tr>
        ))}
      </TableHeader>
      <TableBody>
        {table.getRowModel().rows.map((row) => (
          <TableRow key={row.id} data-testid={`row-${row.original.email}`}>
            {row.getVisibleCells().map((cell) => (
              <TableCell key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
