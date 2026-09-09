/**
 * One connector, as the person asking sees it — the four built rows of `ConnectorStates`' THE
 * PERSON half, drawn exactly once.
 *
 * WHAT THE ROW DOES NOT DECIDE. It renders the state the server sent and calls back; it never
 * infers a state, never reads a second field to work out which sentence it is in, and holds no
 * request of its own. The dialog above it owns the API, the busy lock and the reload — so the
 * only way this row can be wrong is if the server is.
 *
 * THE DECLINED ROW CARRIES NO CONTROL. Not an omission: `Ask again` is drawn on the board and was
 * ruled out for this pass (owner, 2026-09-08), and `declined` is terminal server-side too. The
 * citizen is still owed the whole answer — the date, who decided, and the administrator's remark
 * in full, in the bordered quote the board draws — so all three are here and the button is not.
 *
 * BOTH REMARKS ARE PLAIN JSX TEXT. One user writes them and another reads them, and this portal
 * ships a markdown renderer that must never be pointed at them. No raw-HTML injection prop is
 * used here, and none may be added.
 */
import { ChevronRight, Database } from 'lucide-react'
import type { ConnectorEntry } from '../../utils/connectorApi'
import { assertNever } from '../../utils/assertNever'

/**
 * The connector's teal tile. Two sizes because the board draws two: 26px in a list row, 24px
 * beside the ask panel's title. Not a `size` number — Tailwind cannot build a class from a
 * runtime value, and the two the boards use are the two that exist.
 */
export function ConnectorGlyph({ size = 'row' }: { size?: 'row' | 'title' }): React.JSX.Element {
  const tile = size === 'row' ? 'w-[26px] h-[26px]' : 'w-6 h-6'
  return (
    <span
      aria-hidden
      className={`${tile} flex-shrink-0 rounded-lg bg-primary-50 border border-primary-100 inline-flex items-center justify-center`}
    >
      <Database size={size === 'row' ? 13 : 12} strokeWidth={1.8} className="text-primary-dark" />
    </span>
  )
}

/**
 * SPELLED OUT RATHER THAN LEFT TO `Intl`, and this is the reason.
 *
 * The board draws `2 Sep` and `5 Sep, 08:30`. `toLocaleDateString(undefined, …)` produces neither
 * reliably: en-GB and en-IN — the locales BIAL's own browsers are set to — abbreviate September as
 * `Sept` under current CLDR, and en-US puts the month first (`Sep 2`). So the one form the board
 * specifies is not any runtime's default, and a suite that pinned it would be pinning the machine
 * it ran on. The portal's copy is English throughout; the day and the clock stay LOCAL (the reader
 * is in Bangalore and the server stamps UTC), only the shape is fixed.
 *
 * EXPORTED for `WindowChip.tsx`, which sets `1 – 30 Sep` on the date chip. It reuses this LIST
 * rather than `dayMonth` below, and the difference matters: the functions here take an ISO
 * INSTANT and read it in local time, while a window's bounds are calendar DAYS (`2026-09-01`) that
 * `new Date()` would parse as UTC midnight — the day before, anywhere west of Greenwich. The chip
 * splits its own strings on the hyphen and comes back here only for the month's three letters.
 */
export const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/**
 * `2 Sep` — the board's form for a decision's date, which carries no year and no time.
 *
 * EXPORTED for `ConnectorProjectsPanel.tsx`, whose header sets the same date in the same shape
 * (`Approved for you 2 Sep by Rahul Menon.`). One formatter, so the row and the panel behind it
 * cannot render one approval two ways.
 */
export function dayMonth(iso: string): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return iso
  return `${parsed.getDate()} ${MONTHS[parsed.getMonth()]}`
}

/**
 * `5 Sep, 08:30` — the waiting row's form, which DOES carry the time of day. The board writes it
 * that way because a request made twenty minutes ago and one made last Tuesday are a different
 * kind of wait, and a date alone flattens them. Twenty-four hour and zero-padded, as drawn.
 */
function dayMonthTime(iso: string): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return iso
  const hh = String(parsed.getHours()).padStart(2, '0')
  const mm = String(parsed.getMinutes()).padStart(2, '0')
  return `${dayMonth(iso)}, ${hh}:${mm}`
}

/**
 * The board's ` · ` separator, with absent parts dropped rather than rendered as a gap.
 *
 * A NAME IS ALLOWED TO BE ABSENT, and this is the whole handling of it. The server already
 * substitutes the decider's email when their display name is unset, so `null` means there is no
 * decider to name — the administrator was deleted after deciding. `Approved for you 2 Sep` is the
 * true sentence in that case; `Approved for you 2 Sep · ` is a dangling separator, and looking up
 * an email here would invent a second answer to a question the server already answered.
 */
function dotted(parts: readonly (string | null)[]): string {
  return parts.filter((part): part is string => part !== null && part !== '').join(' · ')
}

/** The sentence under the connector's name, and the ink it is set in. */
function statusLine(entry: ConnectorEntry): { text: string; className: string } {
  switch (entry.state) {
    case 'neverAsked':
      return { text: entry.subtitle, className: 'text-neutral' }
    case 'pending':
      return {
        text: dotted([
          entry.askedAt === null ? 'Asked' : `Asked ${dayMonthTime(entry.askedAt)}`,
          'waiting on an administrator',
        ]),
        className: 'text-amber-700',
      }
    case 'approved':
      return {
        text: dotted([
          entry.approvedAt === null
            ? 'Approved for you'
            : `Approved for you ${dayMonth(entry.approvedAt)}`,
          entry.approvedByName,
        ]),
        className: 'text-neutral',
      }
    case 'declined':
      return {
        text: dotted([
          entry.decidedAt === null ? 'Declined' : `Declined ${dayMonth(entry.decidedAt)}`,
          entry.decidedByName,
        ]),
        // The board's muted brick, not the product's `danger` red: this is a decision that was
        // taken, not an error that occurred, and the two must not read alike.
        className: 'text-[#B4483F]',
      }
    default:
      return assertNever(entry.state)
  }
}

export interface ConnectorRowProps {
  entry: ConnectorEntry
  /**
   * Something this row asked for is in flight. `aria-disabled`, never the native `disabled`:
   * disabling a focused control throws focus to `<body>` mid-request, which is the exact strand
   * `dialog.tsx`'s focus backstop exists to catch. The handlers below return early, so the
   * attribute says so AND the row does so.
   */
  busy: boolean
  /** Never-asked only. Opens the ask panel — the row does not ask. */
  onRequestAccess: () => void
  /** Pending only. Withdraws the waiting request. */
  onCancelRequest: () => void
  /**
   * Approved only. Drills into every project the citizen owns (U8). Owned by the dialog, which is
   * what swaps its own body — the row does not know it is inside one.
   */
  onOpenProjects: () => void
}

export default function ConnectorRow({
  entry,
  busy,
  onRequestAccess,
  onCancelRequest,
  onOpenProjects,
}: ConnectorRowProps): React.JSX.Element {
  const status = statusLine(entry)
  // Contract-guaranteed on an approved row (the server counts before it answers), so this is the
  // unreachable arm rather than a default anybody should read as meaningful.
  const projectCount = entry.onProjectCount ?? 0

  return (
    <li data-testid={`connector-row-${entry.key}`} className="px-[13px] py-3">
      <div className="flex items-center gap-2.5">
        <ConnectorGlyph />
        <div className="min-w-0 flex-1">
          <div className="text-[12.5px] font-bold leading-[1.25] text-primary-900">
            {entry.displayName}
          </div>
          {status.text !== '' && (
            <div className="mt-0.5">
              <span className={`text-[10.5px] leading-[1.45] ${status.className}`}>
                {status.text}
              </span>
            </div>
          )}
        </div>

        {entry.state === 'neverAsked' && (
          <button
            type="button"
            aria-disabled={busy}
            onClick={() => {
              if (busy) return
              onRequestAccess()
            }}
            className="flex-shrink-0 inline-flex items-center justify-center whitespace-nowrap rounded-lg bg-primary px-3 py-1.5 text-[11.5px] font-bold text-white transition hover:bg-primary-600 aria-disabled:opacity-60"
          >
            Request access
          </button>
        )}

        {entry.state === 'pending' && (
          <button
            type="button"
            aria-disabled={busy}
            onClick={() => {
              if (busy) return
              onCancelRequest()
            }}
            className="flex-shrink-0 inline-flex items-center justify-center whitespace-nowrap rounded-lg border border-bial-border bg-white px-3 py-1.5 text-[11.5px] font-semibold text-neutral transition hover:text-primary-900 aria-disabled:opacity-60"
          >
            Cancel
          </button>
        )}

        {entry.state === 'approved' && (
          <button
            type="button"
            data-testid={`connector-projects-${entry.key}`}
            onClick={onOpenProjects}
            className="flex-shrink-0 inline-flex items-center gap-2 whitespace-nowrap text-[11px] font-bold text-primary-dark"
          >
            On in {projectCount} {projectCount === 1 ? 'project' : 'projects'}
            <ChevronRight size={15} className="text-canvas-placeholder" aria-hidden />
          </button>
        )}
      </div>

      {entry.state === 'declined' && entry.decisionRemarks !== null && (
        <div className="mt-[7px] rounded-lg border border-[#F4C7C7] bg-[#FEF7F7] px-[9px] py-[7px]">
          {/* IN FULL, AND AS TEXT. Not truncated to a tooltip and not routed through a markdown
              renderer — an administrator wrote this about this person, and it is the whole of
              what they were told. */}
          <p className="m-0 text-[11px] leading-[1.55] text-[#B4483F]">“{entry.decisionRemarks}”</p>
        </div>
      )}
    </li>
  )
}
