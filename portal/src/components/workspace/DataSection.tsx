/**
 * DATA — the connector's WHOLE presence on the project screen, in the rail's third section.
 *
 * THE APP PANE IS NOT TOUCHED, and that is a ruling rather than an omission. `NoAccess` draws a
 * centred paragraph inside the pane telling a citizen their app has no flight data yet and where
 * to ask for some; none of it is built (owner, 2026-09-08). The row three inches from it already
 * says `You do not have access to … yet` and carries the control that fixes it, so the paragraph
 * would be the same message twice — and it claims to know the app NEEDS flight data, which the
 * platform cannot know. Nothing in `AppPane.tsx` or `workspaceState.ts` learns about connectors.
 * The composer does not either: there is no per-chat data control, in this pass or a later one.
 *
 * THE ROW IS `ProjectConnectorRow`, MOUNTED — NOT FORKED. The drill-down mounts the same
 * component per project; this mounts it per registry connector with `projectName={null}`, which
 * is what makes the rail's switch and the dialog's switch one implementation and one write. What
 * this file adds through the row's slots is the rail's own furniture: the teal tile (`leading`),
 * the project-state sentence (`detail`), and — for the two states with no switch to offer —
 * `Request →` or an inert `Waiting` (`trailing`).
 *
 * THE COUNT IS COMPUTED, NOT FETCHED. `entries.filter(effectivelyOn)` over the array already in
 * hand: counting it involves no clock and no second emitter, so it cannot disagree with the rows
 * under it. The wording is derived from that count too — `On` / `none on` with one connector in
 * the registry, and the boards' `N of M on` out of the same expression the day a second one
 * joins. The registry has ONE entry today, so the list has one row: the boards' greyed
 * `[ANOTHER BIAL SYSTEM]` placeholder is not built (owner ruling), and hard-coding it here would
 * turn this section's list test red on purpose.
 *
 * `Reading N days` IS DERIVED AND NEVER 30. `Main` draws a project set to 30 days; a project on
 * `Last 7 days` reads `Reading 7 days of flight data`. The number comes off the RESOLVED window
 * the server sent — the same field the chip beside it renders — so the sentence and the chip on
 * one row cannot contradict each other (R13).
 *
 * IT RE-READS WHEN THE DIALOG CLOSES, and the rail is what wires that up. `Manage integrations →`
 * opens the Integrations dialog OVER this section; toggling this very project inside the
 * drill-down reloads the drill-down and nothing else, so without the re-read the rail would keep
 * showing the old switch, the old chip and the old count. There is no query cache in this portal
 * to invalidate, so the mechanism is explicit: this component exposes `reload()` and the rail
 * passes it as the dialog's `onClose`.
 *
 * A SKELETON, NEVER A BLANK GAP. This read happens on every project navigation and is uncached by
 * design, so the pre-load moment is a guaranteed state rather than an edge case — and an empty
 * box between two hairlines reads as "nothing is connected", which is a different and false
 * statement. One skeleton row, because one row is what resolves: two would collapse to one and
 * shift the rail, which is the thing the skeleton exists to prevent.
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { ArrowRight, X } from 'lucide-react'
import { listProjectConnectors, onConnectorsChanged, setProjectConnector } from '../../utils/connectorApi'
import type { ProjectConnectorEntry, WindowChoice } from '../../utils/connectorApi'
import { ConnectorGlyph, dayMonth } from '../connectors/ConnectorRow'
import ProjectConnectorRow from '../connectors/ProjectConnectorRow'
import { assertNever } from '../../utils/assertNever'

/**
 * Which of the four PROJECT states one row is in — the `ConnectorStates` board's a–d, and the
 * only place the person's state is turned into a project's sentence.
 *
 * `declined` LANDS IN `noAccess` WITH `neverAsked`, and that is the honest arm. The board draws
 * four project states and a decline is not one of them: what a declined citizen has, on this
 * project, is no access. `Request →` sends them to Integrations, which is where the decline, its
 * date and the administrator's own words are — so the row points at the answer rather than
 * hiding it or pretending the ask can be repeated (`Ask again` is not built this pass).
 */
type RowView =
  | { kind: 'on'; days: number }
  | { kind: 'off' }
  | { kind: 'noAccess' }
  | { kind: 'waiting'; askedAt: string | null }

function viewOf(entry: ProjectConnectorEntry): RowView {
  switch (entry.state) {
    case 'pending':
      return { kind: 'waiting', askedAt: entry.askedAt }
    case 'approved':
      // `effectivelyOn`, not `enabled`: "can this project see the data" is the resolver's answer
      // and this must not become a second home for that conjunction (R13). The window is checked
      // beside it because the sentence COUNTS it — an on-ness with nothing to count is a
      // contract break the server cannot emit, and this is the narrowing that says so.
      return entry.effectivelyOn && entry.window !== null
        ? { kind: 'on', days: entry.window.days }
        : { kind: 'off' }
    case 'neverAsked':
    case 'declined':
      return { kind: 'noAccess' }
    default:
      return assertNever(entry.state)
  }
}

/** The board's sentence under the connector's name, and the ink it is set in. */
function stateLine(entry: ProjectConnectorEntry, view: RowView): { text: string; tone: string } {
  switch (view.kind) {
    case 'on':
      return { text: `Reading ${view.days} days of ${entry.dataNoun}`, tone: 'text-neutral' }
    case 'off':
      return { text: `Switch it on when a chat needs ${entry.dataNoun}`, tone: 'text-neutral' }
    case 'noAccess':
      // THE CONNECTOR IS NAMED FROM THE WIRE. The board writes the name into this sentence; a
      // literal here would be the one place in the feature that has to change for a second
      // connector (R18).
      return { text: `You do not have access to ${entry.displayName} yet`, tone: 'text-neutral' }
    case 'waiting':
      return {
        text:
          view.askedAt === null
            ? 'You asked for access — waiting on an administrator'
            : `You asked for access on ${dayMonth(view.askedAt)} — waiting on an administrator`,
        // The board's amber, the same one the person-level waiting row uses: a wait is not a
        // failure and must not be set in the grey of a settled state.
        tone: 'text-status-amber-fg',
      }
    default:
      return assertNever(view)
  }
}

/**
 * `On` · `none on` · `1 of 2 on` — ONE expression, so the boards' two forms are the same rule.
 *
 * `Main` draws `1 of 2 on` and `NoAccess` draws `none on` because both boards also draw a second,
 * placeholder connector that is not built. With the registry at one entry this returns `On` and
 * `none on`; the board's counted form comes back on its own the day a second connector joins,
 * with nothing here to edit. Writing either string down instead would freeze it at today's
 * registry size.
 */
export function onCountLabel(on: number, total: number): string {
  if (on === 0) return 'none on'
  if (total === 1) return 'On'
  return `${on} of ${total} on`
}

export interface DataSectionProps {
  projectId: string
  /**
   * The rail's own small-caps label, drawn by this component so the count can share its row —
   * the same arrangement `AppStatusPanel` uses, and for the same reason: the boards put the
   * label and its right-hand read-out on ONE line. A node, not a string, because the treatment
   * belongs to the rail and a second definition here would drift out of step with it.
   */
  label: ReactNode
  /**
   * Opens the Integrations dialog. The RAIL owns that mount, not this section, because the same
   * close has to drive `reload()` — and a dialog owned here could not be closed by the rail's
   * own handler. Both `Manage integrations →` and state c's `Request →` call it: `Request →`
   * opens the dialog, never the ask panel directly, so the citizen sees what they are asking
   * about and consents before they ask.
   */
  onOpenIntegrations: () => void
}

function message(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught)
}

function DataSection({
  projectId,
  label,
  onOpenIntegrations,
}: DataSectionProps): React.JSX.Element {
  const [entries, setEntries] = useState<ProjectConnectorEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  // A stale read must not overwrite a fresher one — the ref-token variant of `let live`, because
  // `load` is also called imperatively by the rail and by the retry. `AppRegistryPanel` is where
  // this shape comes from.
  const loadSeq = useRef(0)
  // THE WRITES' OWN STAMP, per connector, and a different guard from the row's. The row discards
  // a stale answer for its own switch; this discards one for the SENTENCE and the COUNT beside
  // it. Both are needed: a slow `Apply` and a fast toggle genuinely overlap, and without this the
  // late answer would set a count the switch above it has already moved past.
  const writeSeq = useRef(new Map<string, number>())

  const load = useCallback(async (): Promise<void> => {
    const seq = ++loadSeq.current
    setError(null)
    try {
      const rows = await listProjectConnectors(projectId)
      if (loadSeq.current === seq) setEntries(rows)
    } catch (caught) {
      if (loadSeq.current === seq) setError(message(caught))
    }
  }, [projectId])

  useEffect(() => {
    void load()
  }, [load])

  /**
   * RE-READ WHEN ANY CONNECTOR WRITE HAPPENS ANYWHERE, not only when the door this rail owns
   * closes. `IntegrationsDialog` opens from the profile menu too — it is on this very screen —
   * and its drill-down can switch THIS project's connector. The rail's own `onClose` covers only
   * its own door, so the avatar-menu route left this section asserting a window and a switch
   * position the server had already changed. Subscribing puts the re-read where the fact is,
   * which also covers whatever mounts that dialog next.
   */
  useEffect(() => onConnectorsChanged(() => void load()), [load])

  /**
   * One row's write, and the settled answer kept BOTH places it is needed.
   *
   * The row is handed back the whole server entry — `ProjectConnectorEntry` is a superset of the
   * `ProjectConnectorState` it asked for — so it settles its switch and its chip from the same
   * object this section counts. That is why there is no `onSettled` here and no
   * `enabled && approved` anywhere in this file: `effectivelyOn` arrives already answered.
   */
  const write = useCallback(
    async (
      key: string,
      update: { enabled: boolean; window?: WindowChoice },
    ): Promise<ProjectConnectorEntry> => {
      const seq = (writeSeq.current.get(key) ?? 0) + 1
      writeSeq.current.set(key, seq)
      const settled = await setProjectConnector(projectId, key, update)
      if (writeSeq.current.get(key) === seq) {
        setEntries((rows) => rows?.map((row) => (row.key === key ? settled : row)) ?? rows)
      }
      return settled
    },
    [projectId],
  )

  const onCount = entries === null ? null : entries.filter((entry) => entry.effectivelyOn).length

  return (
    <>
      <div className="mb-[9px] flex items-center gap-2.5">
        {label}
        {onCount !== null && entries !== null && (
          // NOT RENDERED WHILE LOADING. `none on` under a skeleton would be a statement the
          // section has not read yet, and it would flip to `On` a moment later.
          <span data-testid="data-on-count" className="ml-auto text-[10.5px] font-bold text-canvas-label">
            {onCountLabel(onCount, entries.length)}
          </span>
        )}
      </div>

      {error !== null && (
        // A FAILED READ IS PROSE AND A RETRY, NOT AN `alert`, and that is the RAIL's convention
        // rather than the dialog's: `AppStatusPanel` reports its own failed read exactly this way
        // one section down. Nobody asked for this fetch — it fires on arrival — so interrupting a
        // reader with it is the wrong weight, and the failure is already in the place the answer
        // was going to be, with the remedy beside it. The WRITE failure below keeps `alert`,
        // because that one follows a press and a silent rollback reads as a missed click.
        <div data-testid="data-section-error" className="mb-2">
          <p className="m-0 text-[11.5px] leading-relaxed text-neutral">{error}</p>
          <button
            type="button"
            onClick={() => void load()}
            className="mt-1 text-[11px] font-semibold text-primary underline-offset-2 hover:underline"
          >
            Try again
          </button>
        </div>
      )}

      {failure !== null && (
        <div
          role="alert"
          data-testid="data-write-failure"
          className="mb-2 flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 px-3 py-2"
        >
          <p className="m-0 flex-1 text-[11px] text-red-600">{failure}</p>
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

      {entries === null ? (
        error === null && (
          <div
            data-testid="data-section-loading"
            aria-busy="true"
            className="overflow-hidden rounded-[11px] border border-bial-border bg-white"
          >
            <div className="flex items-center gap-3 px-[13px] py-[11px]">
              <span className="h-[26px] w-[26px] flex-shrink-0 animate-pulse rounded-lg bg-canvas-tile" />
              <div className="min-w-0 flex-1">
                <span className="block h-3 w-20 animate-pulse rounded bg-canvas-tile" />
                <span className="mt-1.5 block h-2.5 w-36 animate-pulse rounded bg-canvas-tile" />
              </div>
              <span className="h-[18px] w-8 flex-shrink-0 animate-pulse rounded-full bg-canvas-tile" />
            </div>
            <span className="sr-only">Loading this project’s data…</span>
          </div>
        )
      ) : entries.length === 0 ? (
        <p className="m-0 rounded-[11px] border border-bial-border px-[13px] py-3 text-[11.5px] text-neutral">
          Nothing is connected to the platform yet.
        </p>
      ) : (
        <ul
          data-testid="data-section-list"
          className="divide-y divide-bial-border overflow-hidden rounded-[11px] border border-bial-border bg-white"
        >
          {entries.map((entry) => {
            const view = viewOf(entry)
            const line = stateLine(entry, view)
            return (
              <ProjectConnectorRow
                key={entry.key}
                testId={`data-connector-${entry.key}`}
                connectorName={entry.displayName}
                // `null`: the project IS this screen, so the row draws the connector's name and
                // both controls say `in this project` rather than repeating it.
                projectName={null}
                leading={<ConnectorGlyph />}
                detail={
                  <div className="mt-0.5">
                    <span className={`text-[10.5px] leading-[1.45] ${line.tone}`}>{line.text}</span>
                  </div>
                }
                trailing={
                  view.kind === 'noAccess' ? (
                    <button
                      type="button"
                      onClick={onOpenIntegrations}
                      className="flex-shrink-0 inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-lg border border-bial-border bg-white px-3 py-1.5 text-[11.5px] font-semibold text-neutral transition hover:text-primary-900"
                    >
                      Request
                      <ArrowRight size={12} aria-hidden />
                    </button>
                  ) : view.kind === 'waiting' ? (
                    // A READ-OUT, NOT A CONTROL. It is `<span>` rather than a disabled button on
                    // purpose: there is nothing here to press, and cancelling the request is the
                    // person-level act that lives in Integrations.
                    <span className="flex-shrink-0 text-[10.5px] font-bold text-status-amber-fg">
                      Waiting
                    </span>
                  ) : undefined
                }
                enabled={entry.enabled}
                window={entry.window}
                onSet={(update) => write(entry.key, update)}
                onError={setFailure}
              />
            )
          })}
        </ul>
      )}

      <button
        type="button"
        onClick={onOpenIntegrations}
        className="mt-[9px] inline-flex items-center gap-1.5 text-[11px] font-semibold text-primary transition hover:text-primary-dark"
      >
        Manage integrations
        <ArrowRight size={12} aria-hidden />
      </button>
    </>
  )
}

export default DataSection
