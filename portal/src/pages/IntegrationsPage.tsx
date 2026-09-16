import { useCallback, useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { Check, ChevronDown, Database, X } from 'lucide-react'
import {
  cancelConnectorRequest,
  listConnectors,
  notifyConnectorsChanged,
  requestConnectorAccess,
  setProjectConnector,
} from '../utils/connectorApi'
import type { ConnectorEntry, ConnectorOnProject } from '../utils/connectorApi'
import { assertNever } from '../utils/assertNever'
import { errorText } from '../utils/apiError'
import ConfirmDialog from '../components/ui/ConfirmDialog'
import AskAccessPanel from '../components/connectors/AskAccessPanel'
import {
  ConnectorGlyph,
  dayMonth,
  dayMonthTime,
  dotted,
} from '../components/connectors/connectorPresentation'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '../components/ui/collapsible'
import { Switch } from '../components/ui/switch'
import { DURATION, LAYOUT_EASE } from '../lib/motion'

/**
 * INTEGRATIONS — one card per connector, and behind a disclosure the applications that have it
 * switched on right now.
 *
 * ONE CARD, NOT A ROW PER STATE. The connector's name is written once and the CARD changes: the
 * access position on the right is `You have access`, the ask, or the wait. Four states stacked as
 * repeated rows would read as four connectors.
 *
 * IT ANSWERS WHO MAY READ THE DATA, AND NOTHING ELSE. No record counts, no last-read dates, no
 * ranges — the wire object behind the disclosure carries a name and an id and deliberately
 * nothing more. A usage figure here would be a second, unowned answer to a question the platform
 * does not ask on this screen.
 *
 * THE LIST FILTERS ON THE APPLICATION'S OWN SWITCH, never on whether the person is still
 * approved. So a withdrawn grant leaves those applications listed and the access pill above them
 * is the one place that says the person-level access is gone — rows quietly disappearing would
 * leave the page short for a reason it never gives. The count beside the disclosure prefers
 * `onProjectCount` and falls back to the list's length, because the server sends the number only
 * in the approved state — and because the list itself is capped, so its length is the count only
 * while the cap has not bitten.
 *
 * IT OWNS THE API, THE LOCK AND THE RELOAD. Every state change re-reads from the server rather
 * than patching a card from a write's answer: the person's state is a derivation over their
 * remaining rows, and a client that assembled `pending` itself after a POST would be a second
 * copy of that rule. `loadSeq` discards a stale response — there is no query library in this
 * portal.
 *
 * THE LIST IS THE REGISTRY, AND THE REGISTRY HAS ONE ENTRY. The boards draw a second, greyed
 * `[ANOTHER BIAL SYSTEM]` placeholder card; it is not built and there is no entry behind it.
 */

const SUBTITLE =
  'Data BIAL already holds. An administrator gives you access once — every application you own can then use it.'

/** The board's own line under the disclosure — the whole explanation of what its switches do. */
const DISCLOSURE_FOOTER =
  'Only applications with this data switched on are listed. Turning one off stops that application reading the data; it does not change your own access.'

/** Authored: no board draws it, and a grant runs forward, so an approval with nothing switched on
 *  is a guaranteed state rather than an edge case. It points at where the switch is. */
const NOTHING_ON = 'No application has this switched on yet — the switch is in an application’s own settings.'

const DISCLOSURE_TITLE = 'Applications using this data'

/** The sentence under the connector's name, and the ink it is set in — one line, state-selected. */
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
        className: 'text-status-amber-fg',
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

interface OnApplicationRowProps {
  connectorName: string
  project: ConnectorOnProject
  /** Switches this application off. Rejects on failure, and the switch goes back where it was. */
  onTurnOff: () => Promise<void>
}

/**
 * One application behind the disclosure: its name, the word `On`, and the switch that is the
 * list's only control.
 *
 * THE FLIP IS OPTIMISTIC AND THE ROLLBACK IS LOUD. A switch that lags its own press feels broken;
 * a switch that silently returns looks exactly like a press that missed, so the failure is said
 * out loud by the card above and the control goes back to where the server still has it.
 */
function OnApplicationRow({
  connectorName,
  project,
  onTurnOff,
}: OnApplicationRowProps): React.JSX.Element {
  const [on, setOn] = useState(true)
  const [busy, setBusy] = useState(false)
  const [asking, setAsking] = useState(false)

  const turnOff = async (): Promise<void> => {
    setAsking(false)
    setBusy(true)
    setOn(false)
    try {
      await onTurnOff()
    } catch {
      setOn(true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <li
      data-testid={`connector-app-${project.projectId}`}
      className="flex items-center gap-3.5 border-b border-bial-border py-3 last:border-b-0"
    >
      <span className="min-w-0 flex-1 truncate text-[13.5px] font-semibold text-primary-900">
        {project.name}
      </span>
      <span
        className={`text-[11px] font-bold uppercase tracking-[.3px] ${
          on ? 'text-status-green-fg' : 'text-canvas-placeholder'
        }`}
      >
        {on ? 'On' : 'Off'}
      </span>
      <Switch
        checked={on}
        aria-disabled={busy}
        aria-label={`Read ${connectorName} in ${project.name}`}
        onCheckedChange={(next) => {
          // The lock, doing as well as saying: `aria-disabled` alone still delivers the click.
          if (busy) return
          // THIS LIST ONLY EVER TURNS THINGS OFF — it is the applications a connector is ON for,
          // so a row leaves it the moment the switch is flipped and there is no "on" to write
          // from here. The handler used to run the write whatever value it was handed.
          if (next) return
          setAsking(true)
        }}
      />
      {asking && (
        <ConfirmDialog
          testId="connector-turn-off"
          title={`Stop “${project.name}” reading ${connectorName}?`}
          body={
            'It stops reading straight away. If the application is live, whoever is using it ' +
            'sees that data go. Your own access is unchanged, and you can switch it back on ' +
            'from that application’s own Settings.'
          }
          icon={<Database size={17} className="text-status-amber-fg" />}
          iconClassName="bg-status-amber-bg"
          confirmLabel="Switch it off"
          tone="danger"
          onClose={() => setAsking(false)}
          onConfirm={turnOff}
        />
      )}
    </li>
  )
}

interface ConnectorCardProps {
  entry: ConnectorEntry
  /** Something this card asked for is in flight. */
  busy: boolean
  onAsk: () => void
  /** Waiting only. Withdraws this person's own request — see the page's `cancelRequest`. */
  onCancel: () => void
  onTurnOff: (projectId: string) => Promise<void>
}

function ConnectorCard({
  entry,
  busy,
  onAsk,
  onCancel,
  onTurnOff,
}: ConnectorCardProps): React.JSX.Element {
  const [open, setOpen] = useState(false)
  const status = statusLine(entry)
  const count = entry.onProjectCount ?? entry.onProjects.length
  // NOT `state === 'approved'`. A grant can be withdrawn while applications keep their switch up,
  // and those applications are exactly the ones somebody needs to reach. Without access AND with
  // nothing switched on there is no list to disclose, so there is no disclosure either.
  const hasDisclosure = entry.state === 'approved' || count > 0

  return (
    <section
      data-testid={`connector-card-${entry.key}`}
      className="max-w-[860px] overflow-hidden rounded-2xl border border-bial-border bg-white"
    >
      <div className="px-6 py-5">
      <div className="flex items-start gap-4">
        <ConnectorGlyph size="card" />
        <div className="min-w-0 flex-1">
          <h2 className="m-0 text-base font-bold leading-[1.3] text-primary-900">
            {entry.displayName}
          </h2>
          {status.text !== '' && (
            <p className={`mt-[5px] max-w-[470px] text-[13px] leading-[1.55] ${status.className}`}>
              {status.text}
            </p>
          )}
        </div>

        <div className="flex flex-shrink-0 flex-col items-end gap-2">
          {entry.state === 'neverAsked' && (
            <>
              <button
                type="button"
                data-testid={`connector-ask-${entry.key}`}
                aria-disabled={busy}
                onClick={() => {
                  if (busy) return
                  onAsk()
                }}
                className="inline-flex h-9 items-center justify-center whitespace-nowrap rounded-lg bg-primary px-4 text-[13.5px] font-semibold text-white transition hover:bg-primary-600 aria-disabled:opacity-60"
              >
                Ask for access
              </button>
              <span className="text-[11.5px] text-neutral">You do not have access yet</span>
            </>
          )}

          {entry.state === 'pending' && (
            <>
              <span
                data-testid={`connector-waiting-${entry.key}`}
                className="inline-flex items-center whitespace-nowrap rounded-full bg-status-amber-bg px-2.5 py-1 text-[11px] font-bold uppercase tracking-[.3px] text-status-amber-fg"
              >
                Waiting for approval
              </span>
              {/* QUIET, AND NAMED AFTER WHAT IT ACTS ON. Withdrawing takes back the REQUEST; it
                  is not a decision about access, and a bare `Cancel` beside an access pill reads
                  as one. A text control rather than a second filled button, because the card's
                  subject is the wait and this is the smaller of the two things to do about it. */}
              <button
                type="button"
                data-testid={`connector-cancel-${entry.key}`}
                aria-disabled={busy}
                onClick={() => {
                  if (busy) return
                  onCancel()
                }}
                className="text-[11.5px] font-semibold text-neutral underline-offset-2 transition hover:text-primary-900 hover:underline aria-disabled:opacity-60"
              >
                Cancel this request
              </button>
            </>
          )}

          {entry.state === 'approved' && (
            <span
              data-testid={`connector-granted-${entry.key}`}
              className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full bg-status-green-bg px-2.5 py-1 text-[11px] font-bold uppercase tracking-[.3px] text-status-green-fg"
            >
              <Check size={12} strokeWidth={3} aria-hidden />
              You have access
            </span>
          )}
        </div>
      </div>

      {entry.state === 'declined' && entry.decisionRemarks !== null && (
        <div className="mt-3 rounded-lg border border-[#F4C7C7] bg-[#FEF7F7] px-[9px] py-[7px]">
          {/* IN FULL, AND AS TEXT. Not truncated to a tooltip and not routed through a markdown
              renderer — an administrator wrote this about this person, and it is the whole of
              what they were told. */}
          <p className="m-0 text-[11px] leading-[1.55] text-[#B4483F]">“{entry.decisionRemarks}”</p>
        </div>
      )}
      </div>

      {hasDisclosure && (
        <Collapsible open={open} onOpenChange={setOpen} className="border-t border-bial-border">
          <CollapsibleTrigger
            data-testid={`connector-disclosure-${entry.key}`}
            // FOCUS MUST NOT LOOK LIKE OPEN. The trigger's ground says whether the list is
            // showing; focus says where the keyboard is. A ring rather than a third ground is
            // what keeps the two readable at once.
            className="flex w-full items-center gap-2.5 bg-surface-muted px-6 py-3.5 text-left outline-none transition-colors data-[state=open]:bg-white focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary"
          >
            <ChevronDown
              size={16}
              strokeWidth={2.4}
              aria-hidden
              className={`flex-shrink-0 text-neutral transition-transform ${open ? '' : '-rotate-90'}`}
            />
            <span className="text-[13px] font-bold text-primary-900">{DISCLOSURE_TITLE}</span>
            <span className="rounded-full bg-primary/10 px-[7px] py-px text-[11px] font-bold tabular-nums text-primary">
              {count}
            </span>
          </CollapsibleTrigger>

          {/* `forceMount` UNDER `AnimatePresence`, not on its own: Radix would otherwise unmount
              the list the instant it closes and the closing height would never be drawn. Which
              means WE decide whether it is mounted, so a closed disclosure is genuinely absent
              from the page rather than present at zero height. */}
          <AnimatePresence initial={false}>
            {open && (
              <CollapsibleContent asChild forceMount>
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: DURATION.layout, ease: LAYOUT_EASE }}
                  className="overflow-hidden"
                >
                  <div className="px-6 pb-[18px] pt-1">
                    {count === 0 ? (
                      <p
                        data-testid={`connector-none-on-${entry.key}`}
                        className="m-0 py-3 text-[12.5px] text-neutral"
                      >
                        {NOTHING_ON}
                      </p>
                    ) : (
                      <ul className="m-0 list-none p-0">
                        {entry.onProjects.map((project) => (
                          <OnApplicationRow
                            key={project.projectId}
                            connectorName={entry.displayName}
                            project={project}
                            onTurnOff={() => onTurnOff(project.projectId)}
                          />
                        ))}
                      </ul>
                    )}
                    {entry.onProjectCount !== null &&
                      entry.onProjectCount > entry.onProjects.length && (
                        // A SHORTER LIST THAN THE NUMBER ABOVE IT, SAID OUT LOUD. The server caps
                        // what it sends; a reader counting rows against the heading and coming up
                        // short would conclude the page had lost some of their applications.
                        <p
                          data-testid={`connector-more-on-${entry.key}`}
                          className="mt-3 text-[11.5px] leading-[1.6] text-neutral"
                        >
                          Showing the first {entry.onProjects.length}. The rest are switched on
                          too — each one’s own Settings has its switch.
                        </p>
                      )}
                    <p className="mt-3 text-[11.5px] leading-[1.6] text-neutral">
                      {DISCLOSURE_FOOTER}
                    </p>
                  </div>
                </motion.div>
              </CollapsibleContent>
            )}
          </AnimatePresence>
        </Collapsible>
      )}
    </section>
  )
}

export default function IntegrationsPage(): React.JSX.Element {
  const [entries, setEntries] = useState<ConnectorEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  // WHICH connector is mid-write, not a shared boolean: the moment there is a second registry
  // entry, one card's ask must not grey out the other card's button.
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [asking, setAsking] = useState<ConnectorEntry | null>(null)
  // A stale response must not overwrite fresher state — the ref-token variant of `let live`,
  // because `load` is also called imperatively after every write.
  const loadSeq = useRef(0)

  const load = useCallback(async (): Promise<void> => {
    const seq = ++loadSeq.current
    setError(null)
    try {
      const rows = await listConnectors()
      if (loadSeq.current === seq) setEntries(rows)
    } catch (caught) {
      if (loadSeq.current === seq) setError(errorText(caught))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  /**
   * REJECTS ON FAILURE, deliberately. The ask panel renders the server's own sentence — the
   * duplicate-request 409 explains that an administrator is already looking at it, which a
   * generic failure message would throw away.
   */
  const submitAsk = useCallback(
    async (entry: ConnectorEntry, remarks: string): Promise<void> => {
      setBusyKey(entry.key)
      try {
        await requestConnectorAccess(entry.key, remarks)
        setAsking(null)
        notifyConnectorsChanged()
        await load()
      } finally {
        setBusyKey(null)
      }
    },
    [load],
  )

  /**
   * Withdraws this person's own waiting request, then RE-READS — including after a refusal.
   *
   * THE REFUSAL IS THE INTERESTING PATH. `409 nothing_pending` means an administrator answered
   * between the press and the write, so the press did not fail so much as arrive late: the
   * server's own sentence says what happened, and the re-read is what puts the card on the state
   * that actually holds now instead of the one the press was hoping for.
   */
  const cancelRequest = useCallback(
    async (entry: ConnectorEntry): Promise<void> => {
      if (busyKey !== null) return
      setFailure(null)
      setBusyKey(entry.key)
      try {
        await cancelConnectorRequest(entry.key)
        notifyConnectorsChanged()
      } catch (caught) {
        setFailure(errorText(caught))
      } finally {
        setBusyKey(null)
      }
      await load()
    },
    [busyKey, load],
  )

  /**
   * Switches one application off, then RE-READS rather than dropping the row locally.
   *
   * The card's count and its list are two renderings of one server answer, and settling them
   * here from the write's own result would be a second place that decides what "on" means.
   * Re-throwing is what puts the switch back, since the row is the only thing that knows where
   * it was.
   */
  const turnOff = useCallback(
    async (connectorKey: string, projectId: string): Promise<void> => {
      setFailure(null)
      try {
        await setProjectConnector(projectId, connectorKey, { enabled: false })
      } catch (caught) {
        setFailure(errorText(caught))
        throw caught
      }
      notifyConnectorsChanged()
      await load()
    },
    [load],
  )

  if (asking !== null) {
    return (
      <main data-testid="integrations-page" className="font-manrope px-10 py-8">
        <div className="max-w-[640px] overflow-hidden rounded-2xl border border-bial-border bg-white">
          <AskAccessPanel
            entry={asking}
            busy={busyKey === asking.key}
            // Leaving mid-request would unmount the only place its refusal can show.
            onBack={() => {
              if (busyKey === null) setAsking(null)
            }}
            onSubmit={(remarks) => submitAsk(asking, remarks)}
          />
        </div>
      </main>
    )
  }

  return (
    <main data-testid="integrations-page" className="font-manrope px-10 py-8">
      <h1 className="m-0 text-2xl font-extrabold tracking-[-0.3px] text-primary-900">
        Integrations
      </h1>
      <p className="mt-1.5 max-w-[660px] text-sm leading-[1.55] text-neutral">{SUBTITLE}</p>

      {error !== null && (
        <div
          role="alert"
          className="mt-6 max-w-[860px] rounded-xl border border-red-200 bg-red-50 px-3 py-2.5"
        >
          <p className="text-xs text-red-600">{error}</p>
          <button
            type="button"
            onClick={() => void load()}
            className="mt-1.5 text-xs font-semibold text-primary underline-offset-2 hover:underline"
          >
            Try again
          </button>
        </div>
      )}

      {failure !== null && (
        <div
          role="alert"
          data-testid="integrations-write-failure"
          className="mt-6 flex max-w-[860px] items-start gap-2 rounded-xl border border-red-200 bg-red-50 px-3 py-2.5"
        >
          <p className="m-0 flex-1 text-xs text-red-600">{failure}</p>
          <button
            type="button"
            onClick={() => setFailure(null)}
            aria-label="Dismiss"
            className="flex-shrink-0 text-red-600/70 transition hover:text-red-600"
          >
            <X size={13} />
          </button>
        </div>
      )}

      <div className="mt-6 flex flex-col gap-4">
        {entries === null
          ? error === null && (
              // A skeleton, never a blank gap: an empty box under the subtitle reads as "nothing
              // is connected" rather than as "still loading".
              <div
                data-testid="connector-cards-loading"
                className="max-w-[860px] rounded-2xl border border-bial-border bg-white p-6"
              >
                <div className="flex items-start gap-4">
                  <span className="h-11 w-11 flex-shrink-0 animate-pulse rounded-xl bg-canvas-tile" />
                  <div className="min-w-0 flex-1">
                    <span className="block h-4 w-40 animate-pulse rounded bg-canvas-tile" />
                    <span className="mt-2 block h-3 w-64 animate-pulse rounded bg-canvas-tile" />
                  </div>
                </div>
                <span className="sr-only">Loading your integrations…</span>
              </div>
            )
          : entries.length === 0
            ? (
                <p className="max-w-[860px] rounded-2xl border border-bial-border bg-white px-6 py-5 text-[12.5px] text-neutral">
                  Nothing is connected to the platform yet.
                </p>
              )
            : entries.map((entry) => (
                <ConnectorCard
                  key={entry.key}
                  entry={entry}
                  busy={busyKey === entry.key}
                  onAsk={() => setAsking(entry)}
                  onCancel={() => void cancelRequest(entry)}
                  onTurnOff={(projectId) => turnOff(entry.key, projectId)}
                />
              ))}
      </div>
    </main>
  )
}
