/**
 * `Integrations` — the administrator's connector queue, as `AdminQueue` draws it: the filter
 * pills and people search on one row, `WAITING ON YOU` and `ALREADY DECIDED` as two bordered
 * tables, and the paragraph under them that says what a decision actually does.
 *
 * TWO FETCHES, ONE PER TABLE, AND THAT IS THE WIRE RATHER THAN A CHOICE. `state` is a REQUIRED
 * query parameter that selects the rows *and* their order — `waiting` oldest first, `decided`
 * newest first — so there is no single call that could answer for both halves of this screen.
 *
 * THE DEFAULT ORDER IS THE BOARD'S, AND IT IS STATED TWICE ON PURPOSE. The server returns each
 * table already ordered; each table here ALSO starts with that order as its initial sorting
 * state, because sortable headers are a departure this panel ships (see `columns.tsx`) and a
 * TanStack table with no sorting state renders whatever order its array arrived in — which is
 * the server's only until somebody clicks a header and clicks it back.
 *
 * WHERE THE DECISION LIVES. `Review` opens `ConnectorReviewDialog` and this panel owns three
 * things around it: which row is open, the reload after a write, and the toast. The dialog owns
 * the two API calls and the difference between a refusal worth retrying and a row that is
 * already gone — it hands back one sentence through `onSettled`, and BOTH outcomes land here the
 * same way, because a decision that landed and a row another administrator decided first leave
 * this queue equally stale.
 *
 * R18: no connector is named anywhere in this file. The pills are built from the connectors the
 * API actually reported, and every label on screen is `connectorDisplayName` off the wire.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
  flexRender,
} from '@tanstack/react-table'
import type { SortingState } from '@tanstack/react-table'
import { AlertCircle, Loader2, RefreshCw, Search } from 'lucide-react'
import { listConnectorRequests } from '../../utils/adminConnectorApi'
import { notifyConnectorsChanged } from '../../utils/connectorApi'
import type { ConnectorRequestRow } from '../../utils/adminConnectorApi'
import ConnectorReviewDialog from './ConnectorReviewDialog'
import { getStoredUser } from '../../utils/auth'
import {
  connectorRequestGlobalFilter,
  createConnectorRequestColumns,
  matchesPerson,
} from './columns'
import type { ConnectorQueueTable } from './columns'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '../ui/table'
import { ToggleGroup, ToggleGroupItem } from '../ui/toggle-group'

/** The `All connectors` pill's value. Not a connector key — the API is asked for no filter. */
const ALL = 'all'

/**
 * How long a keystroke waits before it becomes the server's `q`.
 *
 * THE TWO SEARCHES ARE NOT REDUNDANT. `q` bounds which 200 rows the cap returns, so it has to
 * reach the server; the client-side global filter narrows what is already on screen, with no
 * round trip, which is why the box feels live between keystrokes.
 */
const SEARCH_DEBOUNCE_MS = 300

/**
 * The board's order for each table, as an initial sorting state.
 *
 * `waiting` is oldest first so the person who has waited longest is on top — the review-queue
 * order the app registry already uses. `decided` is newest decision first, because the question
 * a decided table answers is "what just happened".
 */
const DEFAULT_SORTING: Readonly<Record<ConnectorQueueTable, SortingState>> = {
  waiting: [{ id: 'asked', desc: false }],
  decided: [{ id: 'when', desc: true }],
}

interface QueueTableProps {
  kind: ConnectorQueueTable
  rows: ConnectorRequestRow[]
  /** The live text in `Search people…`, narrowing rows already on screen. */
  globalFilter: string
  onReview: (request: ConnectorRequestRow) => void
  currentUserId: string | null
}

/**
 * One of the two tables — the vendored shadcn `Table` primitives driven by TanStack, exactly the
 * shape `UsersLimitsPanel` ships.
 *
 * NO `getPaginationRowModel`. The server caps at 200 and reports when it did; the board draws no
 * pager, so core + sorted + filtered is the whole of it, and pagination arrives the day a real
 * queue outgrows one screen.
 *
 * ITS OWN SORTING STATE, PER TABLE. Two instances of this component means clicking a header in
 * one leaves the other exactly where it was — which is the only behaviour that makes sense when
 * the two tables are about different questions.
 */
function QueueTable({ kind, rows, globalFilter, onReview, currentUserId }: QueueTableProps) {
  const [sorting, setSorting] = useState<SortingState>(() => DEFAULT_SORTING[kind])

  const columns = useMemo(
    () => createConnectorRequestColumns({ table: kind, onReview, currentUserId }),
    [kind, onReview, currentUserId],
  )

  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting, globalFilter },
    onSortingChange: setSorting,
    // The panel's search box owns this state; the table only reads it. Named explicitly rather
    // than left to the built-in, which walks every column's accessor and would let a word in
    // somebody's REMARKS answer a box labelled "search people".
    globalFilterFn: connectorRequestGlobalFilter,
    // Stable per-row identity, so a reload that returns the same requests does not remount every
    // row and drop focus from whatever the administrator was on.
    getRowId: (row) => row.id,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  })

  return (
    <div
      data-testid={`queue-table-${kind}`}
      className="border border-bial-border rounded-xl bg-white overflow-hidden"
    >
      <Table>
        <TableHeader>
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id} className="bg-canvas-group border-b border-bial-border">
              {headerGroup.headers.map((header) => {
                const sortDir = header.column.getIsSorted()
                const ariaSort = !header.column.getCanSort()
                  ? undefined
                  : sortDir === 'asc'
                    ? 'ascending'
                    : sortDir === 'desc'
                      ? 'descending'
                      : 'none'
                return (
                  <TableHead
                    key={header.id}
                    aria-sort={ariaSort}
                    className={`px-4 py-3 ${header.column.columnDef.meta?.className ?? ''}`}
                  >
                    {header.isPlaceholder
                      ? null
                      : flexRender(header.column.columnDef.header, header.getContext())}
                  </TableHead>
                )
              })}
            </tr>
          ))}
        </TableHeader>
        <TableBody className="divide-y divide-canvas-tile">
          {table.getRowModel().rows.map((row) => (
            <TableRow
              key={row.id}
              data-testid={`queue-row-${row.original.id}`}
              // The board tints ONE of its three waiting rows `#F8FCFC` and paints the other two
              // white, which is a per-row state rather than the table's ground: a hover, drawn on
              // a screenshot that cannot show one. Only the queue that can be acted on gets it.
              className={kind === 'waiting' ? 'hover:bg-canvas-queuehover' : 'hover:bg-bial-bg/50'}
            >
              {row.getVisibleCells().map((cell) => (
                <TableCell
                  key={cell.id}
                  className={`px-4 py-3 text-[12.5px] align-middle ${cell.column.columnDef.meta?.className ?? ''}`}
                >
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

export interface IntegrationsPanelProps {
  /**
   * The console's toast channel, as every other writing panel takes it.
   *
   * IT ARRIVED WITH THE FIRST WRITE, WHICH IS THE POINT. This panel shipped without one while it
   * only read; a prop wired to nothing is a promise a file cannot keep. Now there are two
   * writes, and two of the outcomes an administrator most needs to hear about — another
   * administrator decided first, the citizen withdrew — CLOSE the dialog rather than rendering
   * inside it, so without this channel they would vanish silently into a reloaded queue.
   */
  onToast: (message: string, severity?: 'ok' | 'problem') => void
}

/**
 * One connector as a filter pill. Built from the rows the API returned — never a written-down
 * list, which would be a component that had to know what a connector is called (R18).
 */
interface ConnectorOption {
  key: string
  displayName: string
}

/**
 * The pills, unioned across every load rather than recomputed from the current one.
 *
 * WHY A UNION AND NOT A RECOMPUTE: the selected pill is sent to the server as `connector`, so
 * the next response holds only that connector's rows — recomputing from it would delete every
 * other pill the moment one was pressed, leaving no way back. `q` narrows the same way. Merging
 * keeps a pill for anything this queue has shown, and pressing one that has since emptied lands
 * on the empty states, which are true.
 */
function mergeConnectors(
  known: readonly ConnectorOption[],
  rows: readonly ConnectorRequestRow[],
): ConnectorOption[] {
  const byKey = new Map(known.map((option) => [option.key, option]))
  for (const row of rows) byKey.set(row.connectorKey, {
    key: row.connectorKey,
    displayName: row.connectorDisplayName,
  })
  return [...byKey.values()].sort((a, b) => a.displayName.localeCompare(b.displayName))
}

export default function IntegrationsPanel({ onToast }: IntegrationsPanelProps) {
  const [waitingRows, setWaitingRows] = useState<ConnectorRequestRow[]>([])
  const [decidedRows, setDecidedRows] = useState<ConnectorRequestRow[]>([])
  const [connectors, setConnectors] = useState<ConnectorOption[]>([])
  const [truncated, setTruncated] = useState(false)
  const [connectorFilter, setConnectorFilter] = useState<string>(ALL)
  /** What is typed — narrows the rows on screen on the next render. */
  const [query, setQuery] = useState('')
  /** …and what has settled, which is what the server is asked for. */
  const [appliedQuery, setAppliedQuery] = useState('')
  const [error, setError] = useState<string | null>(null)
  /**
   * The row `Review` is open on, or `null`. THE WHOLE ROW rather than its id: the dialog renders
   * the person, their words and the connector's consent copy off it, and holding an id would
   * mean looking the row back up on every render of a dialog whose subject cannot change while
   * it is open.
   */
  const [review, setReview] = useState<ConnectorRequestRow | null>(null)
  /** False until a load has actually landed, so a re-filter never blanks the screen back to a spinner. */
  const [ready, setReady] = useState(false)
  // Staleness guard for overlapping loads — a pill press mid-debounce is two loads in flight,
  // and the slower one must not overwrite the fresher one's rows.
  const loadSeq = useRef(0)

  // The signed-in administrator, for `WHEN`. Read from the same cached `/auth/me` profile the
  // navbar and this page's own access gate read; `null` only if that cache is empty, in which
  // case no row claims to be the reader's.
  const currentUserId = getStoredUser()?.id ?? null

  useEffect(() => {
    if (query === appliedQuery) return
    const timer = setTimeout(() => setAppliedQuery(query), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [query, appliedQuery])

  const load = useCallback(async (): Promise<void> => {
    const seq = ++loadSeq.current
    setError(null)
    const filters = {
      connector: connectorFilter === ALL ? null : connectorFilter,
      q: appliedQuery.trim() === '' ? null : appliedQuery.trim(),
    }
    try {
      // In parallel: the two tables are independent reads and the screen is not usable until
      // both have landed, so serialising them would only make an administrator wait twice.
      const [waiting, decided] = await Promise.all([
        listConnectorRequests('waiting', filters),
        listConnectorRequests('decided', filters),
      ])
      if (loadSeq.current !== seq) return
      setWaitingRows(waiting.requests)
      setDecidedRows(decided.requests)
      setConnectors((known) => mergeConnectors(known, [...waiting.requests, ...decided.requests]))
      setTruncated(waiting.truncated || decided.truncated)
      setReady(true)
    } catch (caught) {
      if (loadSeq.current !== seq) return
      setError(caught instanceof Error ? caught.message : String(caught))
    }
  }, [connectorFilter, appliedQuery])

  useEffect(() => {
    void load()
  }, [load])

  /**
   * The `Review` seam. Defined here rather than on the column factory because this panel is what
   * owns the queue's data and the dialog's reload; the rows only know they have a control to
   * offer. `useCallback` because it is a dependency of the column factory's `useMemo` — an
   * unstable identity would rebuild every column on every render and drop the sort state.
   */
  const openReview = useCallback((request: ConnectorRequestRow): void => {
    setReview(request)
  }, [])

  /**
   * A decision landed, or the row was gone before it could. Both close the dialog, reload both
   * tables and say so — the queue is equally stale either way, and leaving the decided row in
   * `WAITING ON YOU` is how one administrator decides a request twice.
   */
  const settleReview = useCallback(
    (message: string, severity: 'ok' | 'problem'): void => {
      setReview(null)
      onToast(message, severity)
      void load()
      // THE TAB BADGE IS NOT OURS TO SET, BUT THE FACT IT COUNTS IS OURS TO ANNOUNCE. The waiting
      // count is fetched once by `AdminPage` when the console mounts, so without this an
      // administrator who decides three requests keeps reading `Integrations 3` beside two tables
      // that now show none waiting — the badge contradicting the queue directly under it. Same
      // signal the citizen dialog fires; `AdminPage` re-counts on it.
      notifyConnectorsChanged()
    },
    [onToast, load],
  )

  // The same predicate the tables filter with, so the sentence above a table and the rows under
  // it can never disagree about how many people are on screen.
  const needle = query.trim()
  const visibleWaiting = useMemo(
    () => waitingRows.filter((row) => matchesPerson(row, needle)),
    [waitingRows, needle],
  )
  const visibleDecided = useMemo(
    () => decidedRows.filter((row) => matchesPerson(row, needle)),
    [decidedRows, needle],
  )

  if (error !== null) {
    // NOT two empty tables. A queue that failed to load and a queue with nobody in it are
    // opposite facts, and rendering "caught up" over the first is how a request waits forever.
    return (
      <div className="text-center py-16" data-testid="integrations-error">
        <AlertCircle size={20} className="text-red-500 mx-auto mb-3" />
        <p className="text-sm text-tertiary font-semibold">Couldn’t load the connector queue</p>
        <p className="text-xs text-neutral mt-1">{error}</p>
        <button
          type="button"
          onClick={() => void load()}
          className="mt-4 inline-flex items-center gap-1.5 px-4 py-2 rounded-xl border border-bial-border text-sm font-medium text-tertiary hover:bg-bial-bg transition"
        >
          <RefreshCw size={14} /> Retry
        </button>
      </div>
    )
  }

  if (!ready) {
    return (
      <div className="flex items-center justify-center gap-2 py-16 text-neutral text-sm">
        <Loader2 size={16} className="animate-spin" /> Loading the queue…
      </div>
    )
  }

  return (
    <>
      <div className="flex flex-wrap items-center gap-2.5 mb-4">
        <ToggleGroup
          type="single"
          value={connectorFilter}
          onValueChange={(next) => {
            // Radix hands back `''` when the pressed item is pressed again. A queue is always
            // filtered by something — `All connectors` included — so an empty value is not a
            // state this control may reach.
            if (next !== '') setConnectorFilter(next)
          }}
          aria-label="Filter by connector"
          className="justify-start gap-2.5"
        >
          <ToggleGroupItem
            value={ALL}
            className="rounded-full border border-bial-border bg-white px-3.5 py-1.5 h-auto text-xs font-semibold data-[state=on]:bg-primary-50 data-[state=on]:border-primary-100 data-[state=on]:text-primary-dark data-[state=on]:font-bold data-[state=on]:shadow-none"
          >
            All connectors
          </ToggleGroupItem>
          {connectors.map((option) => (
            <ToggleGroupItem
              key={option.key}
              value={option.key}
              className="rounded-full border border-bial-border bg-white px-3.5 py-1.5 h-auto text-xs font-semibold data-[state=on]:bg-primary-50 data-[state=on]:border-primary-100 data-[state=on]:text-primary-dark data-[state=on]:font-bold data-[state=on]:shadow-none"
            >
              {option.displayName}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>

        <div className="relative ml-auto w-full max-w-[262px]">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-canvas-placeholder" />
          <input
            type="search"
            data-testid="queue-search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label="Search people"
            placeholder="Search people…"
            className="w-full pl-9 pr-3 py-2 text-sm border border-bial-border rounded-xl text-tertiary placeholder:text-canvas-placeholder focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition"
          />
        </div>
      </div>

      <div className="flex items-center gap-3 mb-3">
        <h3 className="text-[10.5px] font-bold uppercase tracking-[.7px] text-neutral">
          Waiting on you
        </h3>
        {visibleWaiting.length > 0 && (
          <span className="text-[11.5px] text-neutral">
            {visibleWaiting.length}{' '}
            {visibleWaiting.length === 1 ? 'person wants' : 'people want'} access to a connector
          </span>
        )}
      </div>

      {visibleWaiting.length === 0 ? (
        <EmptyQueue
          testId="waiting-empty"
          // Two different facts, two different sentences: a queue nobody is in, and a queue whose
          // rows this search is hiding. Only the first one is "caught up".
          message={
            waitingRows.length === 0
              ? 'Nobody is waiting on a decision'
              : `Nobody waiting matches “${needle}”`
          }
        />
      ) : (
        <QueueTable
          kind="waiting"
          rows={waitingRows}
          globalFilter={needle}
          onReview={openReview}
          currentUserId={currentUserId}
        />
      )}

      <h3 className="text-[10.5px] font-bold uppercase tracking-[.7px] text-neutral mt-6 mb-3">
        Already decided
      </h3>

      {visibleDecided.length === 0 ? (
        <EmptyQueue
          testId="decided-empty"
          message={
            decidedRows.length === 0 ? 'No decisions yet' : `No decisions match “${needle}”`
          }
        />
      ) : (
        <QueueTable
          kind="decided"
          rows={decidedRows}
          globalFilter={needle}
          onReview={openReview}
          currentUserId={currentUserId}
        />
      )}

      {truncated && (
        <p className="mt-3 text-[11px] text-neutral">
          {/* The number is COUNTED, not written down: the server's cap is the server's, and a
              hard-coded 200 here would be a second copy of it that ages badly. */}
          Showing the first {Math.max(waitingRows.length, decidedRows.length)} rows. Search for a
          person to see the rest.
        </p>
      )}

      {/*
        THE BOARD'S CLOSING PARAGRAPH, MINUS ITS FINAL CLAUSE. The board ends "…with your name and
        the remarks on both sides", which the decision this feature actually ships makes false:
        there is no approval remark at all, so "both sides" would promise a record that is never
        written. The withdrawal sentence before it stays, because it is true.
      */}
      <p className="mt-3 max-w-[840px] text-[11.5px] leading-[1.65] text-neutral">
        Access is given to a person, so one decision covers every project they own — including
        ones they have not made yet. Withdrawing it stops their chats and their published apps at
        the same moment. Every decision is written to the audit log with your name.
      </p>

      {/* CONDITIONALLY MOUNTED, as every dialog in this portal is — `dialog.tsx`'s focus
          backstop is written for exactly this shape, and a permanently-mounted dialog holding a
          stale row would be one render away from deciding somebody else's request. */}
      {review !== null && (
        <ConnectorReviewDialog
          request={review}
          onClose={() => setReview(null)}
          onSettled={settleReview}
        />
      )}
    </>
  )
}

/** One table's empty state — the steady state most days, so it is drawn rather than left blank. */
function EmptyQueue({ testId, message }: { testId: string; message: string }) {
  return (
    <div
      data-testid={testId}
      className="border border-bial-border rounded-xl bg-white text-center py-12 text-sm text-neutral"
    >
      {message}
    </div>
  )
}
