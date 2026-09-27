import { useState } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, within } from '@testing-library/react'
import type { ColumnDef, RowSelectionState } from '@tanstack/react-table'

import AdminDataTable from '../AdminDataTable'
import type { AdminDataTableProps } from '../AdminDataTable'

interface Item {
  id: string
  name: string
  owner: string
  note: string
  size: number
}

const item = (n: number, over: Partial<Item> = {}): Item => ({
  id: `i${n}`,
  name: `Item ${String(n).padStart(2, '0')}`,
  owner: `owner${n}`,
  note: `note ${n}`,
  size: n,
  ...over,
})

const columns: ColumnDef<Item>[] = [
  { id: 'name', accessorFn: (r) => r.name, header: 'Name' },
  { id: 'owner', accessorFn: (r) => r.owner, header: 'Owner' },
  { id: 'note', accessorFn: (r) => r.note, header: 'Note' },
  { id: 'size', accessorFn: (r) => r.size, header: 'Size' },
  { id: 'actions', header: 'Actions', cell: () => <span>act</span> },
]

const SEARCHABLE = new Set(['name', 'owner'])

function renderTable(rows: Item[], extra: Partial<Omit<AdminDataTableProps<Item>, 'searchable' | 'serverSearch'>> = {}) {
  return render(
    <AdminDataTable<Item>
      columns={columns}
      rows={rows}
      getRowId={(r) => r.id}
      searchable={SEARCHABLE}
      searchLabel="Search items"
      searchPlaceholder="Search items or owners…"
      emptyMessage="No items yet."
      {...extra}
    />,
  )
}

const visibleIds = () => screen.getAllByTestId(/^row-/).map((el) => el.getAttribute('data-testid'))
const search = () => screen.getByRole('searchbox', { name: 'Search items' })

afterEach(cleanup)
beforeEach(() => {
  // jsdom lacks these; Radix Select calls them when it opens.
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn().mockReturnValue(false)
  Element.prototype.releasePointerCapture = vi.fn()
  Element.prototype.setPointerCapture = vi.fn()
})

describe('AdminDataTable — search', () => {
  it('matches any searchable column case-insensitively, and clearing the search restores every row', () => {
    renderTable([
      item(1, { name: 'Baggage Tracker' }),
      item(2, { owner: 'rahul.kohli' }),
      item(3),
    ])
    expect(visibleIds()).toHaveLength(3)

    fireEvent.change(search(), { target: { value: 'BAGGAGE' } })
    expect(visibleIds()).toEqual(['row-i1'])

    fireEvent.change(search(), { target: { value: 'Rahul' } })
    expect(visibleIds()).toEqual(['row-i2'])

    fireEvent.change(search(), { target: { value: '' } })
    expect(visibleIds()).toHaveLength(3)
  })

  it('never matches a column that is not declared searchable', () => {
    renderTable([item(1, { note: 'baggage' }), item(2)])
    fireEvent.change(search(), { target: { value: 'baggage' } })
    expect(screen.queryByTestId('row-i1')).toBeNull()
    expect(screen.getByText('Nothing matches this search or filter.')).toBeTruthy()
  })

  it('with a server search, typing goes to the panel and the table filters nothing itself', () => {
    const onChange = vi.fn()
    render(
      <AdminDataTable<Item>
        columns={columns}
        rows={[item(1), item(2)]}
        getRowId={(r) => r.id}
        serverSearch={{ value: 'zzz', onChange }}
        searchLabel="Search items"
        searchPlaceholder="Search items or owners…"
        emptyMessage="No items yet."
      />,
    )
    expect((search() as HTMLInputElement).value).toBe('zzz')
    expect(visibleIds()).toHaveLength(2)

    fireEvent.change(search(), { target: { value: 'zzzz' } })
    expect(onChange).toHaveBeenCalledWith('zzzz')
  })
})

describe('AdminDataTable — sorting', () => {
  it('a sortable header sorts ascending, then descending', () => {
    renderTable([item(2), item(3), item(1)])
    fireEvent.click(screen.getByTestId('sort-size'))
    expect(visibleIds()).toEqual(['row-i1', 'row-i2', 'row-i3'])
    expect(screen.getByTestId('sort-size').closest('th')?.getAttribute('aria-sort')).toBe('ascending')

    fireEvent.click(screen.getByTestId('sort-size'))
    expect(visibleIds()).toEqual(['row-i3', 'row-i2', 'row-i1'])
    expect(screen.getByTestId('sort-size').closest('th')?.getAttribute('aria-sort')).toBe('descending')
  })

  it('a header with nothing to sort by is a plain label that does nothing', () => {
    renderTable([item(2), item(1)])
    const header = screen.getByRole('columnheader', { name: 'Actions' })
    expect(within(header).queryByRole('button')).toBeNull()
    expect(header.getAttribute('aria-sort')).toBeNull()

    fireEvent.click(header)
    expect(visibleIds()).toEqual(['row-i2', 'row-i1'])
  })
})

describe('AdminDataTable — paging', () => {
  const thirty = Array.from({ length: 30 }, (_, i) => item(i + 1))

  it('shows ten rows a page, the "Showing" line, and numbered links that navigate', () => {
    renderTable(thirty)
    expect(visibleIds()).toHaveLength(10)
    expect(screen.getByText('Showing 1–10 of 30')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: '3' }))
    expect(screen.getByText('Showing 21–30 of 30')).toBeTruthy()
    expect(visibleIds()[0]).toBe('row-i21')
    expect(screen.getByRole('button', { name: '3' }).getAttribute('aria-current')).toBe('page')
    expect((screen.getByRole('button', { name: 'Go to next page' }) as HTMLButtonElement).disabled).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Go to previous page' }))
    expect(screen.getByText('Showing 11–20 of 30')).toBeTruthy()
  })

  it('changing rows per page returns to page 1 and updates the "Showing" line', async () => {
    renderTable(Array.from({ length: 60 }, (_, i) => item(i + 1)))
    // Page 4 on purpose: from here a plain page-size change would keep row 31 in view, on page 2.
    fireEvent.click(screen.getByRole('button', { name: '4' }))
    expect(screen.getByText('Showing 31–40 of 60')).toBeTruthy()

    fireEvent.click(screen.getByRole('combobox', { name: 'Rows per page' }))
    fireEvent.click(await screen.findByRole('option', { name: '25' }))
    expect(screen.getByText('Showing 1–25 of 60')).toBeTruthy()
    expect(visibleIds()).toHaveLength(25)

    fireEvent.click(screen.getByRole('combobox', { name: 'Rows per page' }))
    fireEvent.click(await screen.findByRole('option', { name: '10' }))
    expect(screen.getByText('Showing 1–10 of 60')).toBeTruthy()
  })

  it('a new search returns to page 1, and the count follows the matches', () => {
    renderTable(thirty)
    fireEvent.click(screen.getByRole('button', { name: '3' }))
    // Items 01, 10–19 and 21: twelve matches, two pages, so staying on page 3 would strand the view.
    fireEvent.change(search(), { target: { value: '1' } })
    expect(screen.getByText('Showing 1–10 of 12')).toBeTruthy()
  })

  it('a long list shows the first, the last and the neighbours of the current page', () => {
    renderTable(Array.from({ length: 200 }, (_, i) => item(i + 1)))
    const pager = screen.getByRole('navigation', { name: 'Table pagination' })
    const labels = () => within(pager).getAllByRole('button').map((b) => b.textContent)
    expect(labels()).toEqual(['Previous', '1', '2', '20', 'Next'])

    fireEvent.click(within(pager).getByRole('button', { name: '2' }))
    fireEvent.click(within(pager).getByRole('button', { name: '3' }))
    expect(labels()).toEqual(['Previous', '1', '2', '3', '4', '20', 'Next'])
  })
})

describe('AdminDataTable — states', () => {
  it('truncated shows the cap notice, so a partial list never looks complete', () => {
    const { rerender } = renderTable([item(1), item(2)])
    expect(screen.getAllByTestId(/^row-/)).toHaveLength(2)
    expect(screen.queryByRole('alert')).toBeNull()

    rerender(
      <AdminDataTable<Item>
        columns={columns}
        rows={[item(1), item(2)]}
        getRowId={(r) => r.id}
        searchable={SEARCHABLE}
        searchLabel="Search items"
        searchPlaceholder="Search items or owners…"
        emptyMessage="No items yet."
        truncated
      />,
    )
    expect(screen.getByRole('alert').textContent).toContain('Only the first 2 rows are loaded')
  })

  it('an empty roster renders the empty state, not a bare header', () => {
    renderTable([])
    expect(screen.getByText('No items yet.')).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
    expect(screen.queryByText(/^Showing/)).toBeNull()
  })

  it('a search with zero matches renders the no-match state, not a bare header', () => {
    renderTable([item(1), item(2)])
    fireEvent.change(search(), { target: { value: 'nothing like this' } })
    expect(screen.getByText('Nothing matches this search or filter.')).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('an empty roster that is still loading shows the loading line, not the empty state', () => {
    renderTable([], { loading: true })
    expect(screen.getByText('Loading…')).toBeTruthy()
    expect(screen.queryByText('No items yet.')).toBeNull()
  })
})

describe('AdminDataTable — toolbar and selection', () => {
  it('the toolbar slot receives the table and can narrow it by a column filter', () => {
    renderTable([item(1, { owner: 'meera' }), item(2, { owner: 'arjun' })], {
      toolbarEnd: (table) => (
        <button type="button" onClick={() => table.getColumn('owner')?.setFilterValue('meera')}>
          Only meera
        </button>
      ),
    })
    fireEvent.click(screen.getByRole('button', { name: 'Only meera' }))
    expect(visibleIds()).toEqual(['row-i1'])
  })

  it('the action closes the toolbar after the search, and the summary replaces the "Showing" line', () => {
    renderTable([item(1), item(2)], { action: <button type="button">Add item</button>, summary: '2 items in all' })

    const toolbar = search().parentElement
    expect(toolbar?.lastElementChild?.textContent).toBe('Add item')
    expect(screen.getByText('2 items in all')).toBeTruthy()
    expect(screen.queryByText(/^Showing/)).toBeNull()
  })

  it('row selection is TanStack state the panel owns, keyed by row id', () => {
    const seen: RowSelectionState[] = []
    const selectColumn: ColumnDef<Item> = {
      id: 'select',
      header: ({ table }) => (
        <input type="checkbox" aria-label="Select all" checked={table.getIsAllRowsSelected()} onChange={table.getToggleAllRowsSelectedHandler()} />
      ),
      cell: ({ row }) => (
        <input type="checkbox" aria-label={`Select ${row.original.name}`} checked={row.getIsSelected()} onChange={row.getToggleSelectedHandler()} />
      ),
    }
    function Harness() {
      const [rowSelection, setRowSelection] = useState<RowSelectionState>({})
      seen.push(rowSelection)
      return (
        <AdminDataTable<Item>
          columns={[selectColumn, ...columns]}
          rows={[item(1), item(2), item(3)]}
          getRowId={(r) => r.id}
          searchable={SEARCHABLE}
          searchLabel="Search items"
          searchPlaceholder="Search items or owners…"
          emptyMessage="No items yet."
          rowSelection={rowSelection}
          onRowSelectionChange={setRowSelection}
        />
      )
    }
    render(<Harness />)
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Item 02' }))
    expect(seen.at(-1)).toEqual({ i2: true })

    fireEvent.click(screen.getByRole('checkbox', { name: 'Select all' }))
    expect(seen.at(-1)).toEqual({ i1: true, i2: true, i3: true })
  })
})

describe('AdminDataTable — opening a row', () => {
  const withButton: ColumnDef<Item>[] = [
    ...columns.slice(0, 4),
    { id: 'actions', header: 'Actions', cell: ({ row }) => <button type="button">Act on {row.original.name}</button> },
  ]

  it('hands a click anywhere on a row to the handler, with the row and its element', () => {
    const onRowClick = vi.fn()
    renderTable([item(1), item(2)], { onRowClick })

    fireEvent.click(within(screen.getByTestId('row-i2')).getByText('owner2'))

    expect(onRowClick).toHaveBeenCalledTimes(1)
    expect(onRowClick.mock.calls[0][0]).toEqual(item(2))
    expect(onRowClick.mock.calls[0][1]).toBe(screen.getByTestId('row-i2'))
  })

  it('leaves a click on one of the row’s own controls to that control', () => {
    const onRowClick = vi.fn()
    const onAct = vi.fn()
    render(
      <AdminDataTable<Item>
        columns={[
          ...columns.slice(0, 4),
          { id: 'actions', header: 'Actions', cell: () => <button type="button" onClick={onAct}>Act</button> },
        ]}
        rows={[item(1)]}
        getRowId={(r) => r.id}
        searchable={SEARCHABLE}
        searchLabel="Search items"
        searchPlaceholder="Search items or owners…"
        emptyMessage="No items yet."
        onRowClick={onRowClick}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Act' }))

    expect(onAct).toHaveBeenCalledTimes(1)
    expect(onRowClick).not.toHaveBeenCalled()
  })

  it('draws the selected row highlighted, and only that row', () => {
    renderTable([item(1), item(2)], { onRowClick: vi.fn(), selectedRowId: 'i2' })

    expect(screen.getByTestId('row-i2').getAttribute('aria-current')).toBe('true')
    expect(screen.getByTestId('row-i2').className).toContain('bg-[#E6F2F2]')
    expect(screen.getByTestId('row-i1').getAttribute('aria-current')).toBeNull()
  })

  it('opens on the sort it is given, drawn as the sorted column', () => {
    renderTable([item(1), item(3), item(2)], { initialSorting: [{ id: 'size', desc: true }] })

    expect(visibleIds()).toEqual(['row-i3', 'row-i2', 'row-i1'])
    expect(screen.getByTestId('sort-size').closest('th')?.getAttribute('aria-sort')).toBe('descending')
  })

  it('keeps a table without these props exactly as it was: unsorted, and no row is clickable', () => {
    renderTable([item(2), item(1)], { columns: withButton })

    expect(visibleIds()).toEqual(['row-i2', 'row-i1'])
    const row = screen.getByTestId('row-i2')
    expect(row.className).not.toContain('cursor-pointer')
    expect(row.getAttribute('aria-current')).toBeNull()
    expect(screen.getByTestId('sort-size').closest('th')?.getAttribute('aria-sort')).toBe('none')
  })
})
