import type { ColumnDef, Column, Row } from '@tanstack/react-table'
import { Pencil, UserX, UserCheck, ShieldCheck, ArrowUpDown, RotateCcw } from 'lucide-react'
import { BusyGlyph } from '../ui/Waiting'
import { tableHeadLabelClass } from '../ui/table'
// The connector glyph and the board's month list, imported rather than re-drawn: the queue's
// `CONNECTOR` cell is the same teal tile the citizen's own Integrations list draws, and `dayMonth`
// is the single place `2 Sep` is spelled (see its docblock for why `Intl` cannot produce it).
import { ConnectorGlyph, dayMonth } from '../connectors/ConnectorRow'
import type { LimitFields } from '../../utils/admin'
import type { ConnectorRequestRow, ConnectorRequestStatus } from '../../utils/adminConnectorApi'

// Formatting + small badge/pill helpers shared with UsersLimitsPanel (LimitField/
// EditModal import `fmt` from here too). Defined directly in this file — not a
// separate cells.jsx — because TanStack's ColumnDef<MergedUser> already type-checks
// every cell renderer below; routing these through an allowJs/checkJs:false JS file
// would let them cross into this typed file unchecked. The panel already imports
// FROM columns.tsx (createUserColumns), so importing `fmt` etc. the same way creates
// no panel<->columns cycle — columns.tsx never imports from the panel.
export const fmt = (n: number): string => Number(n).toLocaleString('en-US')
export const roleLabel = (role: string): string => (role === 'super_admin' ? 'Super admin' : 'Citizen')

/** One numeric limit cell: the effective value + a "default" pill when not overridden.
 * `value` defaults to 0 (matching the column's accessorFn) so a row missing
 * `effectiveLimits` renders "0", not the literal string "NaN" that `fmt(undefined)`
 * produces while sorting has already treated the same row as a zero. `null` is
 * folded the same way as `undefined` — LimitFields' fields are `number | null`
 * on the wire, "no value" either way. */
function LimitCell({ value, overridden }: { value: number | null | undefined; overridden: boolean }) {
  return (
    <div className="flex items-center gap-1.5 whitespace-nowrap">
      <span className="text-tertiary font-medium tabular-nums">{fmt(value ?? 0)}</span>
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

/** Active / Suspended pill driven purely by `suspendedAt` (null = active). */
function SuspensionBadge({ email, suspendedAt }: { email: string; suspendedAt: string | null }) {
  return suspendedAt ? (
    <span
      data-testid={`status-${email}`}
      className="inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-full bg-red-100 text-red-700"
    >
      Suspended
    </span>
  ) : (
    <span
      data-testid={`status-${email}`}
      className="inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-full bg-green-100 text-green-700"
    >
      Active
    </span>
  )
}

// Lets a column def carry its own header/cell className (e.g. the Actions column's
// `pr-0`) instead of the panel re-deriving `id === 'actions' ? ... : ...` twice —
// once for TableHead, once for TableCell — with the two checks free to drift.
declare module '@tanstack/react-table' {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData, TValue> {
    className?: string
  }
}

export interface MergedUser {
  userId: string
  email: string
  displayName?: string | null
  role: string
  suspendedAt: string | null
  usageToday?: number
  limits?: LimitFields
  effectiveLimits?: LimitFields
}

interface CreateUserColumnsArgs {
  onEdit: (user: MergedUser) => void
  onDeactivate: (user: MergedUser) => void
  onReactivate: (user: MergedUser) => void
  onResetUsage: (user: MergedUser) => void
  busyId: string | null
}

/** A sortable column header — shares TableHead's own label styling (tableHeadLabelClass) so the two can't drift.
 *
 *  GENERIC OVER THE ROW because this file now holds two factories: the roster's and the connector
 *  queue's. The header needs nothing off a row — only the column handle — so a second copy typed
 *  to the second row shape would be the same six lines with a different annotation, and the day
 *  one grew a `desc`-first default the other would keep the old one. */
function SortHeader<TRow>({ label, column }: { label: string; column: Column<TRow, unknown> }) {
  const sorted = column.getIsSorted()
  return (
    <button
      type="button"
      data-testid={`sort-${column.id}`}
      onClick={() => column.toggleSorting(sorted === 'asc')}
      className={`flex items-center gap-1 hover:text-tertiary transition ${tableHeadLabelClass}`}
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
  onResetUsage,
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
      meta: { className: 'pr-0' },
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
            <button
              onClick={() => onResetUsage(u)}
              disabled={busy || !u.usageToday}
              title="Zero out today's usage so they don't have to wait for the midnight IST rollover"
              data-testid={`reset-usage-${u.email}`}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-bial-border text-blue-600 hover:bg-blue-50 transition text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {busy ? <BusyGlyph size={12} /> : <RotateCcw size={12} />} Reset usage
            </button>
            {/* Suspended wins over the super-admin guard: role is derived at read time
                from the env allowlist, so a suspended user who later lands on
                that allowlist is reachable with no 403 bypass — checking isSuper first
                would strand them behind "Protected" with no Reactivate, forever, even
                though reactivate_user has no super-admin guard and would restore them. */}
            {isSuper && !suspended ? (
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
                {busy ? <BusyGlyph size={12} /> : <UserCheck size={12} />} Reactivate
              </button>
            ) : (
              <button
                onClick={() => onDeactivate(u)}
                disabled={busy}
                data-testid={`deactivate-${u.email}`}
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-bial-border text-red-600 hover:bg-red-50 transition text-xs font-medium disabled:opacity-50"
              >
                {busy ? <BusyGlyph size={12} /> : <UserX size={12} />} Deactivate
              </button>
            )}
          </div>
        )
      },
    },
  ]
}

// --- the administrator's connector queue ----------------------------------------
//
// A SECOND FACTORY BESIDE THE FIRST, not a second file. `createUserColumns` above and
// `createConnectorRequestColumns` below share `SortHeader`, `ColumnMeta` and the panels' whole
// TanStack + shadcn `Table` shape; splitting them would copy all three so that two tables of
// people on adjacent tabs could drift apart one header at a time.

/**
 * `AdminQueue` draws TWO tables with different columns, and this is which one is being built.
 * One factory rather than two because everything except the column list is shared — the person
 * cell, the sort headers, the row type — and the two lists are ten lines apart.
 */
export type ConnectorQueueTable = 'waiting' | 'decided'

interface CreateConnectorRequestColumnsArgs {
  table: ConnectorQueueTable
  /**
   * THE DECISION SEAM. The `Review` control calls this and nothing else; the dialog it opens is
   * the next unit's, and lives above this file in the panel that owns the queue's data.
   */
  onReview: (request: ConnectorRequestRow) => void
  /**
   * The signed-in administrator's id, or `null` when the profile is not cached.
   *
   * `WHEN` READS `you` ONLY TO THE ADMINISTRATOR WHO MADE THAT DECISION. The board writes
   * `2 Sep · you` on every decided row, which is true only for the administrator it was drawn
   * for; BIAL runs two super-admins, so every row here is read by somebody who may not have
   * decided it. `null` means we cannot claim any row is the reader's, so every row carries a
   * name — which is the safe direction to be wrong in.
   */
  currentUserId: string | null
}

/**
 * The avatar's two letters. Taken from the name the SERVER settled on, so a person with no
 * display name gets the first two characters of their work email rather than an empty circle.
 */
function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return '?'
  const first = parts[0]
  const last = parts.length > 1 ? parts[parts.length - 1] : ''
  return (last ? `${first[0]}${last[0]}` : first.slice(0, 2)).toUpperCase()
}

/**
 * `4 Sep, 09:12` — the `ASKED` column's form, which carries the time of day because a request
 * made twenty minutes ago and one made last Tuesday are a different kind of wait.
 *
 * IT COMPOSES `dayMonth` RATHER THAN RE-SPELLING THE MONTHS. `toLocaleDateString` cannot produce
 * the board's shape at all — en-GB and en-IN abbreviate September as `Sept` under current CLDR
 * and en-US puts the month first — so the list is spelled once, in `ConnectorRow.tsx`, and every
 * connector surface comes back to it. The clock half is local, as it is there: the reader is in
 * Bangalore and the server stamps UTC.
 *
 * WHY THIS IS NOT SIMPLY IMPORTED: `ConnectorRow`'s own `dayMonthTime` is module-private and
 * that file is out of this unit's scope, so the four lines are here and the month list — the
 * part that could actually disagree — is not.
 */
/**
 * `09:12` — the clock half on its own, local and zero-padded, or `null` for an instant that will
 * not parse.
 *
 * EXPORTED FOR THE ONE SENTENCE THAT SETS IT WITH A WORD RATHER THAN A COMMA: the decide
 * dialog's `Asked on 4 Sep at 09:12.` A third four-line copy of `padStart` would be the drift
 * the docblock above is already about, one level down — the column list is not the only thing
 * two surfaces can disagree on. `dayMonthTime` composes it, so both forms move together.
 */
export function clockTime(iso: string): string | null {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return null
  const hh = String(parsed.getHours()).padStart(2, '0')
  const mm = String(parsed.getMinutes()).padStart(2, '0')
  return `${hh}:${mm}`
}

function dayMonthTime(iso: string): string {
  const clock = clockTime(iso)
  return clock === null ? iso : `${dayMonth(iso)}, ${clock}`
}

/** The board's ` · ` separator, with absent parts dropped rather than rendered as a dangling gap. */
function dotted(parts: readonly (string | null)[]): string {
  return parts.filter((part): part is string => part !== null && part !== '').join(' · ')
}

/**
 * WHO IS ASKING / PERSON — the same two-line cell in both tables, so the email substitution
 * lands identically on either side of a decision.
 *
 * THE SECOND LINE IS THE WORK EMAIL, in place of the board's `department` (which exists nowhere
 * in this product and has no directory client behind it). An email is longer than the column was
 * drawn for, so it truncates with an ellipsis and carries the full value in a `title` AND in an
 * `aria-label`: a bare `title` is not reachable by keyboard or touch and is announced
 * inconsistently, which is not good enough beside an authorization decision. The ellipsis is CSS
 * only — the whole address is in the text node either way — so nothing is hidden from a reader,
 * only from the pixels.
 *
 * `whitespace-nowrap` ON BOTH LINES IS THE POINT, not styling: this cell is two lines and must
 * stay two, or a long address reflows every row in the table to three and the queue stops
 * scanning as a list.
 */
function PersonCell({ request }: { request: ConnectorRequestRow }) {
  return (
    <div className="flex items-center gap-[9px] min-w-0">
      <span
        aria-hidden
        className="w-[26px] h-[26px] flex-shrink-0 rounded-full bg-canvas-tile text-neutral text-[10.5px] font-extrabold inline-flex items-center justify-center"
      >
        {initials(request.displayName)}
      </span>
      <div className="min-w-0">
        <p className="font-bold text-tertiary truncate whitespace-nowrap">{request.displayName}</p>
        <span
          data-testid={`queue-email-${request.id}`}
          title={request.email}
          aria-label={request.email}
          className="block text-[10.5px] text-neutral truncate whitespace-nowrap"
        >
          {request.email}
        </span>
      </div>
    </div>
  )
}

/** The green `Approved` / red `Declined` pill, on the existing `status.green-*` / `status.red-*`
 *  triples — the same six values the app registry's own state pills are set in. */
function DecisionPill({ status }: { status: ConnectorRequestStatus }) {
  const approved = status === 'approved'
  return (
    <span
      data-testid={`decision-${status}`}
      className={`inline-flex items-center gap-1.5 text-[11px] font-bold px-2.5 py-1 rounded-full whitespace-nowrap ${
        approved ? 'text-status-green-fg bg-status-green-bg' : 'text-status-red-fg bg-status-red-bg'
      }`}
    >
      <span
        aria-hidden
        className={`w-1.5 h-1.5 rounded-full ${approved ? 'bg-status-green-dot' : 'bg-status-red-dot'}`}
      />
      {approved ? 'Approved' : 'Declined'}
    </span>
  )
}

/**
 * `Search people…`'s test — name OR work email, case-insensitively. An empty needle matches
 * everything, so a cleared box is not a filter.
 *
 * NAMED AND SHARED rather than left to TanStack's built-in global filter, which walks every
 * column's accessor value: that would let a search for `belt` hit somebody's REMARKS and put
 * them under a box labelled "search people". The server's `q` matches exactly these two fields,
 * so the client narrowing and the server narrowing agree about what a person is.
 *
 * EXPORTED AS A PREDICATE, NOT ONLY AS A FILTER FN, because the panel needs the same answer
 * outside the table: the sentence above `WAITING ON YOU` counts the people on screen, and the
 * empty state has to know whether a table is empty or merely hidden by this search. One
 * predicate, three readers, nothing to drift.
 */
export function matchesPerson(person: ConnectorRequestRow, needle: string): boolean {
  const wanted = needle.trim().toLowerCase()
  if (wanted === '') return true
  return (
    person.displayName.toLowerCase().includes(wanted) ||
    person.email.toLowerCase().includes(wanted)
  )
}

/** `matchesPerson`, in the shape TanStack's `globalFilterFn` is called in. */
export function connectorRequestGlobalFilter(
  row: Row<ConnectorRequestRow>,
  _columnId: string,
  value: unknown,
): boolean {
  return matchesPerson(row.original, String(value ?? ''))
}

/**
 * Column defs for one of the queue's two tables.
 *
 * SORTABLE HEADERS ARE A BOARD DEPARTURE, made by the owner rather than by an implementer:
 * `AdminQueue` draws no sort affordance, but Users & Limits is the product's other table of
 * people, it sits one tab away, and an administrator moving between the two should not lose it.
 * It is purely additive — the DEFAULT order stays the board's (waiting oldest first, decided
 * newest first, both set as the panel's initial sorting state), and nothing moves until a header
 * is clicked.
 *
 * `THEIR REMARKS` IS THE ONE COLUMN THAT DOES NOT SORT. Ordering a review queue by the first
 * letter of what people wrote is not a question anybody has; leaving the affordance there would
 * be a control that answers nothing.
 */
export function createConnectorRequestColumns({
  table,
  onReview,
  currentUserId,
}: CreateConnectorRequestColumnsArgs): ColumnDef<ConnectorRequestRow>[] {
  const person: ColumnDef<ConnectorRequestRow> = {
    id: 'person',
    accessorFn: (row) => row.displayName,
    header: ({ column }) => (
      <SortHeader label={table === 'waiting' ? 'Who is asking' : 'Person'} column={column} />
    ),
    cell: ({ row }) => <PersonCell request={row.original} />,
  }

  if (table === 'decided') {
    return [
      person,
      {
        id: 'connector',
        accessorFn: (row) => row.connectorDisplayName,
        header: ({ column }) => <SortHeader label="Connector" column={column} />,
        // No glyph on this side: the board gives the decided table the connector's name as plain
        // muted text, and keeps the teal tile for the rows still asking for something.
        cell: ({ row }) => (
          <span className="text-neutral font-semibold whitespace-nowrap">
            {row.original.connectorDisplayName}
          </span>
        ),
      },
      {
        id: 'decision',
        accessorFn: (row) => row.status,
        header: ({ column }) => <SortHeader label="Decision" column={column} />,
        cell: ({ row }) => <DecisionPill status={row.original.status} />,
      },
      {
        id: 'usingItIn',
        // `-1` FOR A ROW THAT HAS NO COUNT, so a decline sorts below an approved person with
        // zero switched on rather than beside them. `0` is a real answer and keeps its place.
        accessorFn: (row) => row.usingItIn ?? -1,
        header: ({ column }) => <SortHeader label="Using it in" column={column} />,
        cell: ({ row }) => {
          const count = row.original.usingItIn
          return (
            <span className="text-neutral whitespace-nowrap">
              {count === null ? '—' : `${count} ${count === 1 ? 'project' : 'projects'}`}
            </span>
          )
        },
      },
      {
        id: 'when',
        accessorFn: (row) => row.decidedAt ?? '',
        header: ({ column }) => <SortHeader label="When" column={column} />,
        cell: ({ row }) => {
          const decision = row.original
          // `you` only for the administrator who actually made this decision — never a
          // hard-coded word, which would put one super-admin's identity on the other's screen.
          const who =
            currentUserId !== null && decision.decidedById === currentUserId
              ? 'you'
              : decision.decidedByName
          return (
            <span className="text-neutral whitespace-nowrap">
              {dotted([decision.decidedAt === null ? null : dayMonth(decision.decidedAt), who])}
            </span>
          )
        },
      },
    ]
  }

  return [
    person,
    {
      id: 'connector',
      accessorFn: (row) => row.connectorDisplayName,
      header: ({ column }) => <SortHeader label="Connector" column={column} />,
      cell: ({ row }) => (
        <span className="inline-flex items-center gap-[7px] min-w-0">
          <ConnectorGlyph />
          <span className="font-semibold whitespace-nowrap">{row.original.connectorDisplayName}</span>
        </span>
      ),
    },
    {
      id: 'remarks',
      accessorFn: (row) => row.requesterRemarks,
      header: 'Their remarks',
      enableSorting: false,
      // IN FULL, AS PLAIN JSX TEXT — never truncated to a tooltip and never through the portal's
      // markdown renderer. One user writes this and another decides on it.
      cell: ({ row }) => (
        <span className="text-neutral leading-[1.55]">{row.original.requesterRemarks}</span>
      ),
    },
    {
      id: 'asked',
      accessorFn: (row) => row.askedAt,
      header: ({ column }) => <SortHeader label="Asked" column={column} />,
      meta: { className: 'pr-0' },
      // The date and the control share one cell, as the board draws them — right-aligned, the
      // date first. A column of its own would put a fifth header label on a table the board
      // gives four.
      cell: ({ row }) => (
        <div className="flex items-center gap-2.5 justify-end">
          <span className="text-neutral text-[11.5px] whitespace-nowrap">
            {dayMonthTime(row.original.askedAt)}
          </span>
          <button
            type="button"
            data-testid={`review-${row.original.id}`}
            onClick={() => onReview(row.original)}
            className="inline-flex items-center bg-primary text-white text-[11.5px] font-bold px-3.5 py-1.5 rounded-lg whitespace-nowrap hover:bg-primary-dark transition"
          >
            Review
          </button>
        </div>
      ),
    },
  ]
}
