import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { RefObject } from 'react'
import type { ColumnDef, Row, SortingState, Table as TanStackTable } from '@tanstack/react-table'
import {
  AlertCircle, RefreshCw, ShieldCheck, ShieldOff, MoreHorizontal, PanelRightOpen, Power, Trash2,
} from 'lucide-react'
import { BusyGlyph } from '../ui/Waiting'
import {
  listApps, approveApp, rejectApp, patchApp, disableApp, enableApp, deleteApp,
} from '../../utils/appRegistryApi'
import type {
  RegistryApp, RegistryList, RegistryStatus, AppStatus, LiveVersion, SubmittedDeclaration,
} from '../../utils/appRegistryApi'
import { ApiError } from '../../utils/apiError'
import { dayMonth, dayMonthTime } from '../../utils/projectDates'
import { readDeclaration, shortSha } from './declaration'
import AppSheet from './AppSheet'
import { handle } from './columns'
import {
  countWords,
  MIN_DELETE_REASON_WORDS,
  MAX_DELETE_REASON_WORDS,
  MAX_DELETE_REASON_CHARS,
} from '../../utils/words'
import { cn } from '../../lib/utils'
import { Badge } from '../ui/badge'
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../ui/dropdown-menu'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../ui/select'
import { Textarea } from '../ui/textarea'
import AdminDataTable from './AdminDataTable'

/** What to call an app on screen. The internal id used to stand in for a missing name, but
 *  a UUID is not a name — it identifies the row for the platform, not the app for a person,
 *  and an administrator cannot do anything with it. An untitled app says so instead. */
const appLabel = (app: RegistryApp): string => app.name || '(untitled app)'

// `STATUS_TRANSITIONS[DISABLED]` on the server (`db/models/app_registry.py`), mirrored so
// the control appears exactly where the transition is legal. PENDING is absent on purpose:
// an app waiting for review is REJECTED, not switched off, and the server refuses it — an
// affordance whose only outcome is a refusal is a bug, not a safety net.
const CAN_DISABLE: readonly AppStatus[] = ['approved', 'draft', 'rejected']

// Advisory on-disk size of the app's own database. Null is a real value —
// "no number to show" (never provisioned, not yet ready, or the cluster was unreachable) —
// and renders as "—", never "0 B", which would read as an empty database.
const fmtBytes = (n: number | null): string => {
  if (n == null) return '—'
  const b = Number(n)
  if (!Number.isFinite(b)) return '—'
  if (b < 1024) return `${b} B`
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`
  return `${(b / 1024 / 1024).toFixed(1)} MB`
}

const ALL = 'all'
const SEARCHABLE = new Set(['name', 'owner'])
const INITIAL_SORT: SortingState = [{ id: 'updatedAt', desc: true }]

const QUIET = 'border-gray-500/20 bg-surface-muted text-neutral'
const STOPPED = 'border-slate-600/20 bg-slate-100 text-slate-600'
const FAILED = 'border-red-700/20 bg-red-50 text-red-700'

const REGISTRY_BADGE: Record<RegistryStatus, { label: string; className: string }> = {
  draft: { label: 'Draft', className: QUIET },
  not_published: { label: 'Not published', className: QUIET },
  waiting_for_review: { label: 'Waiting for review', className: 'border-amber-600/25 bg-amber-50 text-amber-700' },
  rejected: { label: 'Rejected', className: FAILED },
  publish_failed: { label: 'Publish failed', className: FAILED },
  publishing: { label: 'Publishing', className: 'border-primary/25 bg-[#F0F9FA] text-primary' },
  live: { label: 'Live', className: 'border-emerald-600/20 bg-emerald-50 text-emerald-700' },
  taken_offline: { label: 'Taken offline', className: STOPPED },
  disabled: { label: 'Disabled', className: STOPPED },
}

interface StatusFilter {
  key: string
  label: string
  statuses: readonly RegistryStatus[]
}

const STATUS_FILTERS: readonly StatusFilter[] = [
  { key: 'waiting_for_review', label: 'Waiting for review', statuses: ['waiting_for_review'] },
  { key: 'live', label: 'Live', statuses: ['live'] },
  { key: 'not_live', label: 'Not live', statuses: ['publishing', 'publish_failed', 'not_published', 'taken_offline'] },
  { key: 'draft', label: 'Draft', statuses: ['draft'] },
  { key: 'rejected', label: 'Rejected', statuses: ['rejected'] },
  { key: 'disabled', label: 'Disabled', statuses: ['disabled'] },
]

function matchesStatusFilter(row: Row<RegistryApp>, _columnId: string, value: unknown): boolean {
  const filter = STATUS_FILTERS.find((f) => f.key === value)
  return filter === undefined || filter.statuses.includes(row.original.registryStatus)
}

function matchesOwner(row: Row<RegistryApp>, _columnId: string, value: unknown): boolean {
  return value === undefined || row.original.ownerUsername === value
}

function RegistryBadge({ status }: { status: RegistryStatus }) {
  const { label, className } = REGISTRY_BADGE[status]
  return (
    <Badge variant="outline" className={cn('px-[9px]', className)}>
      {label}
    </Badge>
  )
}

function LiveVersionCell({ live }: { live: LiveVersion | null }) {
  if (live === null) return <span className="text-neutral">—</span>
  return (
    <div className="font-semibold text-tertiary">
      {live.number !== null && `v${live.number} · `}
      <code className="rounded bg-[#EEF2F6] px-1 py-px text-[11.5px]">{shortSha(live.commitSha)}</code>
      {live.since !== null && <div className="mt-0.5 text-[11px] font-normal text-neutral">since {dayMonth(live.since)}</div>}
    </div>
  )
}

/** The hard block that routed the app, or its score, from the row's own declaration. */
function ClassificationCell({ declaration }: { declaration: SubmittedDeclaration | null }) {
  const read = readDeclaration(declaration)
  if (read.version === 2 && read.found.length > 0) {
    return (
      <div className="flex flex-wrap gap-1">
        {read.found.map((entry) => (
          <Badge key={entry.key} variant="outline" className="border-red-700/20 bg-red-50 text-red-700">
            {entry.title}
          </Badge>
        ))}
      </div>
    )
  }
  if (read.version === 2 && read.score !== null) {
    return (
      <span className="text-[12.5px]">
        <span className="font-semibold tabular-nums">{read.score}</span>
        <span className="text-neutral">/100</span>
      </span>
    )
  }
  return <span className="text-neutral">—</span>
}

/** Each count is of the apps the owner filter and the search leave, whichever status is chosen. */
function StatusFilterPills({ table, selectedRef }: {
  table: TanStackTable<RegistryApp>
  /** The selected pill: where focus lands when the row that opened something has left the list. */
  selectedRef: RefObject<HTMLButtonElement>
}) {
  const column = table.getColumn('status')
  const value = column?.getFilterValue()
  const selected = typeof value === 'string' ? value : ALL
  const apps = column?.getFacetedRowModel().rows.map((row) => row.original) ?? []
  const pills = [
    { key: ALL, label: 'All', count: apps.length },
    ...STATUS_FILTERS.map((f) => ({
      key: f.key,
      label: f.label,
      count: apps.filter((app) => f.statuses.includes(app.registryStatus)).length,
    })),
  ]
  return (
    <div role="group" aria-label="Filter by status" className="flex items-center gap-0.5 rounded-[9px] bg-bial-bg p-[3px]">
      {pills.map(({ key, label, count }) => {
        const pressed = key === selected
        return (
          <button
            key={key}
            ref={pressed ? selectedRef : undefined}
            type="button"
            aria-pressed={pressed}
            data-testid={`filter-${key}`}
            onClick={() => column?.setFilterValue(key === ALL ? undefined : key)}
            className={cn(
              'flex h-[30px] items-center gap-1.5 rounded-[7px] px-2.5 text-[12.5px] transition',
              pressed ? 'bg-white font-bold text-primary shadow-[inset_0_0_0_1px_#E2E8F0]' : 'font-medium text-neutral hover:text-primary',
            )}
          >
            {label}
            <Badge
              variant="outline"
              data-testid={`filter-count-${key}`}
              className={cn(
                'border-transparent px-1.5 py-0 text-[10.5px] leading-4',
                key === 'waiting_for_review' && count > 0 ? 'bg-amber-700 text-white' : 'bg-bial-border text-slate-600',
              )}
            >
              {count}
            </Badge>
          </button>
        )
      })}
    </div>
  )
}

function OwnerFilter({ table, owners }: { table: TanStackTable<RegistryApp>; owners: string[] }) {
  const column = table.getColumn('owner')
  const value = column?.getFilterValue()
  return (
    <Select
      value={typeof value === 'string' ? value : ALL}
      onValueChange={(next: string) => column?.setFilterValue(next === ALL ? undefined : next)}
    >
      <SelectTrigger aria-label="Owner" data-testid="owner-filter" className="h-[34px] w-[160px] rounded-lg px-2.5 py-0 text-[13px] font-medium">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ALL}>All owners</SelectItem>
        {owners.map((email) => (
          <SelectItem key={email} value={email}>
            {handle(email)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

interface RowHandlers {
  busyIds: ReadonlySet<string>
  /** `opener` is where focus returns once whatever opened has closed. */
  onOpen: (app: RegistryApp, opener: HTMLElement | null) => void
  onToggleLogin: (app: RegistryApp) => void
  onDisable: (app: RegistryApp) => void
  onEnable: (app: RegistryApp) => void
  onDelete: (app: RegistryApp, opener: HTMLElement | null) => void
}

// The cells reach the panel through context so the columns can be a constant: the table renders
// each `cell` as a component, and a column list rebuilt per render would remount every row.
const RowHandlersContext = createContext<RowHandlers | null>(null)

function useRowHandlers(): RowHandlers {
  const handlers = useContext(RowHandlersContext)
  if (handlers === null) throw new Error('A registry row rendered outside the App Registry panel.')
  return handlers
}

function AppNameCell({ app }: { app: RegistryApp }) {
  const { onOpen } = useRowHandlers()
  return (
    <button
      type="button"
      data-row-opener=""
      onClick={(e) => onOpen(app, e.currentTarget)}
      className="text-left text-[13.5px] font-semibold text-tertiary decoration-1 underline-offset-2 transition hover:text-primary hover:underline focus-visible:underline focus-visible:outline-none"
    >
      {appLabel(app)}
    </button>
  )
}

function LoginCell({ app }: { app: RegistryApp }) {
  const { busyIds, onToggleLogin } = useRowHandlers()
  return (
    <button
      onClick={() => onToggleLogin(app)}
      disabled={busyIds.has(app.appId)}
      title="Toggle required login"
      className={`relative z-10 inline-flex items-center gap-1 text-xs font-medium px-2 py-1 rounded-lg border transition disabled:opacity-50 ${app.loginRequired ? 'border-primary/30 text-primary bg-primary/5' : 'border-bial-border text-neutral'}`}
    >
      {app.loginRequired ? <ShieldCheck size={12} /> : <ShieldOff size={12} />}
      {app.loginRequired ? 'Required' : 'Off'}
    </button>
  )
}

const menuItem = 'gap-2 rounded-sm px-2 py-1.5 text-sm text-primary-900 focus:bg-surface-muted'

function RowActions({ app }: { app: RegistryApp }) {
  const { busyIds, onOpen, onDisable, onEnable, onDelete } = useRowHandlers()
  const trigger = useRef<HTMLButtonElement>(null)
  const busy = busyIds.has(app.appId)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          ref={trigger}
          type="button"
          data-testid={`actions-${app.appId}`}
          aria-label={`Actions for ${appLabel(app)}`}
          className="flex h-[30px] w-[30px] items-center justify-center rounded-lg text-neutral transition hover:bg-surface-muted hover:text-primary-900 data-[state=open]:bg-surface-muted data-[state=open]:text-primary-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
        >
          <MoreHorizontal size={16} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-[160px] rounded-md border-bial-border bg-white p-1 shadow-lg">
        <DropdownMenuItem className={menuItem} onSelect={() => onOpen(app, trigger.current)}>
          <PanelRightOpen size={15} /> Open
        </DropdownMenuItem>
        {CAN_DISABLE.includes(app.status) && (
          <DropdownMenuItem className={menuItem} disabled={busy} onSelect={() => onDisable(app)}>
            <Power size={15} /> Disable
          </DropdownMenuItem>
        )}
        {app.status === 'disabled' && (
          <DropdownMenuItem className={menuItem} disabled={busy} onSelect={() => onEnable(app)}>
            <Power size={15} /> Enable
          </DropdownMenuItem>
        )}
        <DropdownMenuItem
          className={cn(menuItem, 'text-red-600 focus:text-red-700')}
          disabled={busy}
          onSelect={() => onDelete(app, trigger.current)}
        >
          <Trash2 size={15} /> Delete
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

const COLUMNS: ColumnDef<RegistryApp>[] = [
  {
    id: 'name',
    accessorFn: appLabel,
    header: 'App',
    cell: ({ row }) => <AppNameCell app={row.original} />,
  },
  {
    id: 'owner',
    accessorFn: (app) => app.ownerUsername ?? '',
    header: 'Owner',
    filterFn: matchesOwner,
    meta: { className: 'w-[160px]' },
    cell: ({ row: { original: app } }) =>
      app.ownerUsername === null ? (
        <span className="text-neutral">—</span>
      ) : (
        <span title={app.ownerUsername} className="whitespace-nowrap text-[12.5px] text-neutral">
          {handle(app.ownerUsername)}
        </span>
      ),
  },
  {
    id: 'login',
    header: 'Login',
    enableSorting: false,
    meta: { className: 'w-[100px]' },
    cell: ({ row }) => <LoginCell app={row.original} />,
  },
  {
    id: 'status',
    accessorFn: (app) => REGISTRY_BADGE[app.registryStatus].label,
    header: 'Status',
    filterFn: matchesStatusFilter,
    meta: { className: 'w-[150px]' },
    cell: ({ row }) => <RegistryBadge status={row.original.registryStatus} />,
  },
  {
    id: 'liveVersion',
    accessorFn: (app) => app.liveVersion?.since ?? '',
    header: 'Live version',
    meta: { className: 'w-[140px]' },
    cell: ({ row }) => <LiveVersionCell live={row.original.liveVersion} />,
  },
  {
    id: 'classification',
    header: 'Classification',
    enableSorting: false,
    meta: { className: 'w-[120px]' },
    cell: ({ row }) => <ClassificationCell declaration={row.original.declaration} />,
  },
  {
    id: 'updatedAt',
    accessorFn: (app) => app.updatedAt,
    header: 'Last activity',
    meta: { className: 'w-[130px] whitespace-nowrap' },
    cell: ({ row }) => (
      <span className="whitespace-nowrap text-[12.5px] tabular-nums text-neutral">{dayMonthTime(row.original.updatedAt)}</span>
    ),
  },
  {
    id: 'databaseBytes',
    accessorFn: (app) => app.databaseBytes ?? -1,
    header: 'DB',
    meta: { className: 'w-[70px]' },
    cell: ({ row: { original: app } }) => (
      <span data-testid={`db-bytes-${app.appId}`} className="whitespace-nowrap text-[12.5px] tabular-nums text-neutral">
        {fmtBytes(app.databaseBytes)}
      </span>
    ),
  },
  {
    id: 'actions',
    header: () => <span className="sr-only">Actions</span>,
    enableSorting: false,
    meta: { className: 'w-[36px] py-0' },
    cell: ({ row }) => <RowActions app={row.original} />,
  },
]
/**
 * Admin "App Registry" panel: every app on the shared admin table, filtered by the status the
 * backend derives, by owner and by search. Open, disable, enable, toggle login and delete are
 * all backed by the admin-gated /api/admin/apps endpoints.
 */
export interface AppRegistryPanelProps {
  // severity is optional (default 'ok' on the AdminPage side) so a plain confirmation
  // call reads exactly as it always has — only a failed `act()` below passes 'problem'.
  onToast: (msg: string, severity?: 'ok' | 'problem') => void
}

export default function AppRegistryPanel({ onToast }: AppRegistryPanelProps) {
  const [list, setList] = useState<RegistryList | null>(null)
  const [error, setError] = useState<string | null>(null)
  /** The app whose side panel is open, or null. */
  const [openApp, setOpenApp] = useState<RegistryApp | null>(null)
  /** The app awaiting a delete reason, or null. See `onDelete`. */
  const [deleting, setDeleting] = useState<RegistryApp | null>(null)
  const [deleteReason, setDeleteReason] = useState('')
  // Non-null once the developer withdraws the submission under review. Cleared
  // whenever a different item is opened, so one race can never haunt the next review.
  const [withdrawn, setWithdrawn] = useState<string | null>(null)
  // Why the panel's last Approve or Reject failed. Said inside the panel: the page's toast
  // renders beneath it.
  const [problem, setProblem] = useState<string | null>(null)
  // A SET of in-flight app ids, not one shared lock: acting on row A must never
  // re-enable row B's still-pending buttons (which a single busyId did, opening the
  // door to duplicate concurrent mutations + duplicate audit rows).
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set())
  // Staleness guard for overlapping loads: a stale response must not overwrite fresher state.
  const loadSeq = useRef(0)
  // Captured when something opens, rather than read back off `document.activeElement`: a click
  // focuses the control in a browser but not under `fireEvent`.
  const openerRef = useRef<HTMLElement | null>(null)
  const selectedFilterRef = useRef<HTMLButtonElement>(null)

  /**
   * Put focus back on the control that opened the side panel or the delete dialog once it closes,
   * or on the selected status filter when that row has left the list. Both are unmounted rather
   * than closed, and the dialog opens from a menu item that is gone when it closes.
   *
   * In an effect, not in the close handlers: approving closes the panel from an async
   * continuation, and whether the opener is still attached is a question about the DOM after
   * React commits. The suite cannot tell the two apart, because RTL's `act` flushes first.
   */
  useEffect(() => {
    if (openApp !== null || deleting !== null) return
    const opener = openerRef.current
    if (opener === null) return
    openerRef.current = null
    if (opener.isConnected) opener.focus()
    else selectedFilterRef.current?.focus()
  }, [openApp, deleting])

  const load = useCallback(async () => {
    const seq = ++loadSeq.current
    setError(null)
    try {
      const next = await listApps()
      if (loadSeq.current === seq) setList(next)
    } catch (e) {
      if (loadSeq.current === seq) setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => { load() }, [load])

  const owners = useMemo(
    () =>
      [...new Set((list?.apps ?? []).flatMap((app) => (app.ownerUsername === null ? [] : [app.ownerUsername])))].sort(
        (a, b) => handle(a).localeCompare(handle(b)),
      ),
    [list],
  )

  // Run a mutating action with a per-row busy lock, then reload. Returns the FAILURE, or null on
  // success — never a bare boolean, because the withdrawal race needs the error's `code`. A 409
  // means the app changed elsewhere, so the list reloads on that failure too.
  const run = async (appId: string, fn: () => Promise<unknown>, okMsg?: string): Promise<unknown> => {
    setBusyIds((s) => new Set(s).add(appId))
    try { await fn(); if (okMsg) onToast(okMsg) ; await load(); return null }
    catch (e) { if (e instanceof ApiError && e.status === 409) await load(); return e }
    finally { setBusyIds((s) => { const n = new Set(s); n.delete(appId); return n }) }
  }

  // A row action's failure is a toast. The toast is one channel for both a confirmation and a raw
  // failure, so the two must NOT render alike — an administrator left to tell them apart by
  // reading the words cannot know whether the action they just took worked, which is why only the
  // failure passes a 'problem' severity.
  const act = async (appId: string, fn: () => Promise<unknown>, okMsg?: string): Promise<unknown> => {
    const failure = await run(appId, fn, okMsg)
    if (failure !== null) onToast(failure instanceof Error ? failure.message : String(failure), 'problem')
    return failure
  }

  /** Close the panel on success; on a failure keep it open and say why inside it — on the 409
   *  the admin still needs the submission metadata. The withdrawal race replaces the actions. */
  const settleReview = (failure: unknown): void => {
    if (failure === null) { setOpenApp(null); setWithdrawn(null); return }
    const message = failure instanceof Error ? failure.message : String(failure)
    if (failure instanceof ApiError && failure.code === 'submission_withdrawn') setWithdrawn(message)
    else setProblem(message)
  }

  // Approve sends the on-display submission id (the reviewed-id guard's input); a stale review
  // 409s with copy the panel shows verbatim, and the panel closes only on success so a 409 leaves
  // the metadata visible to re-review. `submissionId` is nullable in the schema but always present
  // once an app is 'pending' — the `as string` below is an unchecked pass-through matching
  // pre-migration behavior, not a missed null check.
  const onApprove = (app: RegistryApp) => { setProblem(null); return run(app.appId, () => approveApp(app.appId, app.submissionId as string), `“${appLabel(app)}” approved`).then(settleReview) }
  const onReject = (app: RegistryApp, note: string) => { setProblem(null); return run(app.appId, () => rejectApp(app.appId, note), `“${appLabel(app)}” rejected`).then(settleReview) }
  const onToggleLogin = (app: RegistryApp) => act(app.appId, () => patchApp(app.appId, { loginRequired: !app.loginRequired }), `Login ${app.loginRequired ? 'disabled' : 'required'} for “${appLabel(app)}”`)
  const onDisable = (app: RegistryApp) => act(app.appId, () => disableApp(app.appId), `“${appLabel(app)}” disabled`)
  const onEnable = (app: RegistryApp) => act(app.appId, () => enableApp(app.appId), `“${appLabel(app)}” re-enabled`)
  const onOpen = (app: RegistryApp, opener: HTMLElement | null) => {
    openerRef.current = opener
    setWithdrawn(null)
    setProblem(null)
    setOpenApp(app)
  }
  // THE DELETE ASKS WHY, AND A `window.confirm` COULD NOT.
  //
  // The route now REQUIRES a word-bounded reason, so a confirm-and-send would 422 every time. The
  // reason rides the `app:delete` audit row, which is written before destruction and has no
  // foreign key to the app — so it outlives the thing it describes, which is the whole point.
  //
  // It uses the SAME word rule as the citizen's own project delete (`utils/words.ts`, mirrored
  // at `src/core/words.py`), because the harsher act — destroying somebody else's work — should
  // not ask for less than the gentler one.
  const onDelete = (app: RegistryApp, opener: HTMLElement | null) => {
    openerRef.current = opener
    // THE REASON IS PER-APP AND MUST NOT TRAVEL. `deleteReason` lives on the panel, so a
    // justification typed for one app and abandoned would open pre-filled on the next one —
    // and if it happened to be valid, one press away from destroying a different citizen's
    // work under words that were never about it. Cleared on OPEN rather than only on close,
    // because close is the path a mid-flight failure deliberately does not take.
    setDeleteReason('')
    setDeleting(app)
  }

  if (error) {
    return (
      <div className="text-center py-16">
        <AlertCircle size={20} className="text-red-500 mx-auto mb-3" />
        <p className="text-sm text-tertiary font-semibold">Couldn’t load apps</p>
        <p className="text-xs text-neutral mt-1">{error}</p>
        <button onClick={load} className="mt-4 inline-flex items-center gap-1.5 px-4 py-2 rounded-xl border border-bial-border text-sm font-medium text-tertiary hover:bg-bial-bg transition"><RefreshCw size={14} /> Retry</button>
      </div>
    )
  }
  if (list === null) {
    return <div className="flex items-center justify-center gap-2 py-16 text-neutral text-sm"><BusyGlyph size={16} /> Loading apps…</div>
  }

  return (
    <>
      <RowHandlersContext.Provider value={{ busyIds, onOpen, onToggleLogin, onDisable, onEnable, onDelete }}>
        <AdminDataTable<RegistryApp>
          columns={COLUMNS}
          rows={list.apps}
          getRowId={(app) => app.appId}
          searchable={SEARCHABLE}
          searchLabel="Search apps"
          searchPlaceholder="Search apps or owners…"
          emptyMessage="No apps yet."
          truncated={list.truncated}
          initialSorting={INITIAL_SORT}
          onRowClick={(app, row) => onOpen(app, row.querySelector<HTMLElement>('[data-row-opener]'))}
          selectedRowId={openApp?.appId ?? null}
          toolbarStart={(table) => <StatusFilterPills table={table} selectedRef={selectedFilterRef} />}
          toolbarEnd={(table) => <OwnerFilter table={table} owners={owners} />}
        />
      </RowHandlersContext.Provider>

      {openApp && (
        <AppSheet
          app={openApp}
          title={appLabel(openApp)}
          status={<RegistryBadge status={openApp.registryStatus} />}
          withdrawn={withdrawn}
          problem={problem}
          onClose={() => { setOpenApp(null); setWithdrawn(null) }}
          onApprove={() => onApprove(openApp)}
          onReject={(note) => onReject(openApp, note)}
        />
      )}
      {deleting && (
        <DeleteAppDialog
          app={deleting}
          reason={deleteReason}
          onReason={setDeleteReason}
          busy={busyIds.has(deleting.appId)}
          onClose={() => { setDeleting(null); setDeleteReason('') }}
          onConfirm={async () => {
            const target = deleting
            const outcome = await act(target.appId, () => deleteApp(target.appId, deleteReason), `“${appLabel(target)}” deleted`)
            // Close only on success — a 422 on the reason must leave the words on screen to fix,
            // not throw them away behind a dialog that has already gone.
            if (!(outcome instanceof Error)) { setDeleting(null); setDeleteReason('') }
          }}
        />
      )}
    </>
  )
}

/**
 * The admin delete's reason, on the vendored Radix `Dialog`: it gives the focus trap, Escape and
 * the overlay click, and routes the last two through `onOpenChange`, where `busy` holds the dialog
 * open mid-request. The panel restores focus, because the dialog opens from a menu item that is
 * gone by the time it closes.
 *
 * The reason is word-bounded by the same rule as the citizen's own delete (`utils/words.ts`,
 * mirrored at `src/core/words.py`) and rides the `app:delete` audit row, which outlives the app.
 * The client keeps the person inside the bounds; the server enforces them.
 */
function DeleteAppDialog({ app, reason, onReason, busy, onClose, onConfirm }: {
  app: RegistryApp
  reason: string
  onReason: (value: string) => void
  busy: boolean
  onClose: () => void
  onConfirm: () => void
}) {
  const words = countWords(reason)
  const valid = words >= MIN_DELETE_REASON_WORDS && words <= MAX_DELETE_REASON_WORDS

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        // Radix routes Escape, the overlay click and its own close through here, and `busy`
        // holds it open mid-request — the same guard the hand-rolled overlay carried, now
        // covering the two exits it never did.
        if (!next && !busy) onClose()
      }}
    >
      <DialogContent
        hideClose
        // The scrim this dialog already used, kept exactly — the vendored default is
        // `bg-black/80`, which is a different design.
        overlayClassName="bg-black/40"
        className="font-manrope w-full max-w-md rounded-2xl bg-white p-6 shadow-xl gap-0 border-0"
      >
        <DialogTitle className="text-base font-bold text-tertiary">Delete “{appLabel(app)}”?</DialogTitle>
        {/* Names the two things that do not come back. "Data and files" undersold it: the app's
            own PostgreSQL database is dropped outright — no export, no snapshot, no undo. */}
        <p className="mt-2 text-sm text-neutral">
          Its database is dropped and its files are deleted. This cannot be undone.
        </p>
        <label className="block mt-4">
          <span className="text-xs font-semibold text-tertiary">Why are you deleting this app?</span>
          {/* The SHARED `Textarea`, like the citizen dialog this one models itself on down to
              the word rule — two dialogs with the same job drifting apart on their input is how
              a design system stops being one. `maxLength` mirrors the server's own 2,000-char
              backstop (`clean_deletion_reason`): a paste guard, not the rule a person is told
              about, which is the word count below. */}
          <Textarea
            data-testid="admin-delete-reason"
            value={reason}
            onChange={(e) => onReason(e.target.value)}
            rows={3}
            maxLength={MAX_DELETE_REASON_CHARS}
            aria-describedby="admin-delete-reason-count"
            className="mt-1.5 resize-y"
          />
          <span id="admin-delete-reason-count" className="mt-1 block text-xs text-neutral">
            Between {MIN_DELETE_REASON_WORDS} and {MAX_DELETE_REASON_WORDS} words. Kept on the audit record.{' '}
            {words}/{MAX_DELETE_REASON_WORDS} words
          </span>
        </label>
        <div className="flex gap-3 mt-5">
          <button
            type="button"
            onClick={() => { if (!busy) onClose() }}
            aria-disabled={busy}
            className={`rounded-xl border border-bial-border px-4 py-2 text-sm font-semibold text-neutral transition hover:bg-bial-bg ${busy ? 'cursor-not-allowed opacity-50' : ''}`}
          >
            Cancel
          </button>
          <button
            type="button"
            data-testid="admin-delete-confirm"
            // `aria-disabled`, never `disabled`, and this is the control it matters most on:
            // it is the one holding focus at the instant it goes busy, because the citizen just
            // pressed it. A real `disabled` attribute throws focus to the document body from
            // under them, mid-request, which is the defect `ui/dialog.tsx`'s backstop exists to
            // clean up after — better not to cause it. The refusal is enforced in the handler,
            // the only place it can be once the attribute is gone.
            aria-disabled={!valid || busy}
            onClick={() => {
              if (!valid || busy) return
              onConfirm()
            }}
            className={`flex-1 flex items-center justify-center gap-2 bg-red-600 hover:bg-red-700 text-white font-semibold py-2.5 rounded-xl transition text-sm ${
              !valid || busy ? 'opacity-50 cursor-not-allowed' : ''
            }`}
          >
            {busy ? <BusyGlyph size={15} /> : null} Delete app
          </button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
