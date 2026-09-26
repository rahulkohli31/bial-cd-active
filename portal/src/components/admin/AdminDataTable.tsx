import { useEffect, useId, useState } from 'react'
import type { ReactNode } from 'react'
import {
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table'
import type {
  ColumnDef,
  ColumnFiltersState,
  Header,
  OnChangeFn,
  PaginationState,
  RowSelectionState,
  SortingState,
  Table as TanStackTable,
} from '@tanstack/react-table'
import { cn } from '../../lib/utils'
import { Alert, AlertDescription } from '../ui/alert'
import { Input } from '../ui/input'
import { Label } from '../ui/label'
import {
  Pagination,
  PaginationContent,
  PaginationEllipsis,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from '../ui/pagination'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../ui/table'
import { BusyGlyph } from '../ui/Waiting'
import { SortHeader, fmt } from './columns'

const PAGE_SIZES = [10, 25, 50]

const pagerButton = 'h-[30px] min-w-[30px] rounded-md border border-bial-border bg-white px-2 text-xs text-tertiary hover:bg-bial-bg'
const currentPage = 'border-0 !bg-primary font-bold text-white hover:text-white'
const stepButton = 'px-0 text-neutral [&>span]:sr-only'

export interface ServerSearch {
  value: string
  onChange: (value: string) => void
}

type SearchSource =
  // The table filters the loaded rows itself, over these column ids.
  | { searchable: ReadonlySet<string>; serverSearch?: undefined }
  // The panel owns the query and asks the server; the table filters nothing itself.
  | { serverSearch: ServerSearch; searchable?: undefined }

export type AdminDataTableProps<TRow> = SearchSource & {
  columns: ColumnDef<TRow>[]
  rows: TRow[]
  searchLabel: string
  searchPlaceholder: string
  /** Shown when there are no rows at all; a search or filter that matches nothing has its own line. */
  emptyMessage: string
  getRowId?: (row: TRow) => string
  /** The start of the toolbar: status filter tabs, a selection count. */
  toolbarStart?: (table: TanStackTable<TRow>) => ReactNode
  /** Just before the search box, which closes the toolbar: owner, role or status selects. */
  toolbarEnd?: (table: TanStackTable<TRow>) => ReactNode
  /** The server stopped at its cap, so these rows are not the whole list. */
  truncated?: boolean
  /** Rows are still arriving: an empty table shows the loading line, not the empty state. */
  loading?: boolean
  rowSelection?: RowSelectionState
  onRowSelectionChange?: OnChangeFn<RowSelectionState>
}

/** Every page when there are few; otherwise the first, the last and the current page's neighbours. `null` is a gap. */
function pageWindow(current: number, count: number): (number | null)[] {
  if (count <= 7) return Array.from({ length: count }, (_, i) => i)
  const shown = [0, current - 1, current, current + 1, count - 1]
    .filter((p, i, all) => p >= 0 && p < count && all.indexOf(p) === i)
    .sort((a, b) => a - b)
  return shown.flatMap((p, i) => (i > 0 && p - shown[i - 1] > 1 ? [null, p] : [p]))
}

function HeaderLabel<TRow>({ header }: { header: Header<TRow, unknown> }) {
  const { column } = header
  const label = column.columnDef.header
  if (header.isPlaceholder) return null
  if (column.getCanSort() && typeof label === 'string') return <SortHeader label={label} column={column} />
  return <>{flexRender(label, header.getContext())}</>
}

/**
 * The one admin table: search, sorting, filters, paging and the empty and truncated states over
 * rows a panel has already loaded. Loading them, and any server-side search, stays in the panel.
 */
export default function AdminDataTable<TRow>({
  columns,
  rows,
  searchable,
  serverSearch,
  searchLabel,
  searchPlaceholder,
  emptyMessage,
  getRowId,
  toolbarStart,
  toolbarEnd,
  truncated = false,
  loading = false,
  rowSelection,
  onRowSelectionChange,
}: AdminDataTableProps<TRow>) {
  const searchId = useId()
  const [sorting, setSorting] = useState<SortingState>([])
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([])
  const [globalFilter, setGlobalFilter] = useState('')
  const [pagination, setPagination] = useState<PaginationState>({ pageIndex: 0, pageSize: PAGE_SIZES[0] })

  const toFirstPage = () => setPagination((p) => (p.pageIndex === 0 ? p : { ...p, pageIndex: 0 }))

  const table = useReactTable({
    data: rows,
    columns,
    state: {
      sorting,
      columnFilters,
      globalFilter: globalFilter.trim(),
      pagination,
      ...(rowSelection ? { rowSelection } : {}),
    },
    getRowId,
    onSortingChange: (updater) => {
      setSorting(updater)
      toFirstPage()
    },
    onColumnFiltersChange: (updater) => {
      setColumnFilters(updater)
      toFirstPage()
    },
    onPaginationChange: setPagination,
    enableRowSelection: rowSelection !== undefined,
    onRowSelectionChange,
    globalFilterFn: 'includesString',
    getColumnCanGlobalFilter: (column) => searchable?.has(column.id) ?? false,
    // The rows' identity changes on every background page and optimistic row update; only a
    // new sort, filter or search should move the reader back to page 1.
    autoResetPageIndex: false,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
  })

  const pageCount = table.getPageCount()
  useEffect(() => {
    if (pageCount > 0 && pagination.pageIndex > pageCount - 1) {
      setPagination((p) => ({ ...p, pageIndex: pageCount - 1 }))
    }
  }, [pageCount, pagination.pageIndex])

  const onSearch = (value: string) => {
    if (serverSearch) serverSearch.onChange(value)
    else setGlobalFilter(value)
    toFirstPage()
  }

  const matched = table.getFilteredRowModel().rows.length
  const { pageIndex, pageSize } = table.getState().pagination
  const firstShown = pageIndex * pageSize + 1
  const lastShown = Math.min((pageIndex + 1) * pageSize, matched)

  return (
    <div className="flex flex-col gap-3.5">
      <div className="flex flex-wrap items-center gap-3">
        {toolbarStart?.(table)}
        <div className="flex-grow" />
        {toolbarEnd?.(table)}
        <Label htmlFor={searchId} className="sr-only">
          {searchLabel}
        </Label>
        <Input
          id={searchId}
          type="search"
          value={serverSearch ? serverSearch.value : globalFilter}
          onChange={(e) => onSearch(e.target.value)}
          placeholder={searchPlaceholder}
          className="h-[34px] w-[230px] rounded-lg bg-white px-3 text-[13px] text-tertiary shadow-none md:text-[13px]"
        />
      </div>

      {truncated && (
        <Alert className="rounded-xl border-bial-border bg-bial-bg px-3 py-2.5">
          <AlertDescription className="text-xs text-neutral">
            Only the first {fmt(rows.length)} rows are loaded. The server sends no more than that, so search, sorting
            and paging cover these rows only.
          </AlertDescription>
        </Alert>
      )}

      {rows.length === 0 && loading ? (
        <div className="flex items-center justify-center gap-2 py-16 text-sm text-neutral">
          <BusyGlyph size={16} /> Loading…
        </div>
      ) : rows.length === 0 || matched === 0 ? (
        <div className="rounded-xl border border-bial-border py-16 text-center text-sm text-neutral">
          {rows.length === 0 ? emptyMessage : 'Nothing matches this search or filter.'}
        </div>
      ) : (
        <>
          <div className="overflow-hidden rounded-xl border border-bial-border">
            <Table>
              <TableHeader>
                {table.getHeaderGroups().map((headerGroup) => (
                  <TableRow key={headerGroup.id} className="border-b border-bial-border bg-bial-bg/60">
                    {headerGroup.headers.map((header) => {
                      const sorted = header.column.getIsSorted()
                      const ariaSort = !header.column.getCanSort()
                        ? undefined
                        : sorted === 'asc'
                          ? 'ascending'
                          : sorted === 'desc'
                            ? 'descending'
                            : 'none'
                      return (
                        <TableHead
                          key={header.id}
                          aria-sort={ariaSort}
                          className={cn('px-4 py-2.5', header.column.columnDef.meta?.className)}
                        >
                          <HeaderLabel header={header} />
                        </TableHead>
                      )
                    })}
                  </TableRow>
                ))}
              </TableHeader>
              <TableBody>
                {table.getRowModel().rows.map((row) => (
                  <TableRow key={row.id} data-testid={`row-${row.id}`} className="hover:bg-bial-bg/50">
                    {row.getVisibleCells().map((cell) => (
                      <TableCell key={cell.id} className={cn('px-4 py-[11px]', cell.column.columnDef.meta?.className)}>
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          <div className="flex flex-wrap items-center gap-3 text-xs text-neutral">
            <p className="flex-grow">{`Showing ${fmt(firstShown)}–${fmt(lastShown)} of ${fmt(matched)}`}</p>
            <span>Rows per page</span>
            <Select
              value={String(pageSize)}
              onValueChange={(value: string) => table.setPagination({ pageIndex: 0, pageSize: Number(value) })}
            >
              <SelectTrigger aria-label="Rows per page" className="h-[30px] w-[68px] rounded-md px-2 py-0 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PAGE_SIZES.map((size) => (
                  <SelectItem key={size} value={String(size)}>
                    {size}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Pagination aria-label="Table pagination" className="mx-0 w-auto">
              <PaginationContent>
                <PaginationItem>
                  <PaginationPrevious
                    onClick={() => table.previousPage()}
                    disabled={!table.getCanPreviousPage()}
                    className={cn(pagerButton, stepButton)}
                  />
                </PaginationItem>
                {pageWindow(pageIndex, pageCount).map((page, i, all) =>
                  page === null ? (
                    <PaginationItem key={`gap-after-${all[i - 1]}`}>
                      <PaginationEllipsis className="h-[30px] w-6" />
                    </PaginationItem>
                  ) : (
                    <PaginationItem key={page}>
                      <PaginationLink
                        isActive={page === pageIndex}
                        onClick={() => table.setPageIndex(page)}
                        className={cn(pagerButton, page === pageIndex && currentPage)}
                      >
                        {page + 1}
                      </PaginationLink>
                    </PaginationItem>
                  ),
                )}
                <PaginationItem>
                  <PaginationNext
                    onClick={() => table.nextPage()}
                    disabled={!table.getCanNextPage()}
                    className={cn(pagerButton, stepButton)}
                  />
                </PaginationItem>
              </PaginationContent>
            </Pagination>
          </div>
        </>
      )}
    </div>
  )
}
