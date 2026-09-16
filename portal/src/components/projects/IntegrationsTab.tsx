import { useCallback, useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { listProjectConnectors, setProjectConnector } from '../../utils/connectorApi'
import type { ProjectConnectorEntry, WindowChoice } from '../../utils/connectorApi'
import { assertNever } from '../../utils/assertNever'
import { errorText } from '../../utils/apiError'
import { ConnectorGlyph, dayMonth } from '../connectors/connectorPresentation'
import ProjectConnectorRow from '../connectors/ProjectConnectorRow'

/**
 * SETTINGS › INTEGRATIONS — the BIAL data this one application may read.
 *
 * THE SAME SWITCH AS THE INTEGRATIONS PAGE, ONE WRITE BEHIND BOTH. This tab and the page's
 * disclosure are two views of `project_connectors`, and neither holds a cache: both re-read from
 * the server, so a switch flipped in one is what the other's next read returns. Nothing here
 * derives a switch position from anything else.
 *
 * THE ROW IS `ProjectConnectorRow`, MOUNTED — NOT FORKED, which is what keeps the optimistic
 * flip, the rollback, the in-flight lock and the write stamp in one implementation.
 *
 * THE TWO STATES WITH NO SWITCH TO OFFER SAY SO. A citizen an administrator has not approved, and
 * one still waiting, get a read-out where the switch would be: a control that exists in order to
 * be refused teaches somebody to distrust the screen. Asking is a person-level act and it lives
 * on the Integrations page, which is where the decline, its date and the administrator's words
 * are — this tab is about one application.
 *
 * A SKELETON, NEVER A BLANK GAP. The read fires when the tab is chosen, so the pre-load moment is
 * guaranteed rather than an edge case, and an empty box between two hairlines reads as "nothing
 * is connected" — a different and false statement.
 */

/** The board's own footer — it is the whole explanation of what the switch beside it does not do. */
const FOOTER =
  'The switch controls this application only. Access itself is granted once, by an administrator.'

/**
 * …AND WHAT TO SAY WHERE THERE IS NO SWITCH TO EXPLAIN. Every row can be a read-out — nothing
 * approved yet, or everything still waiting — and the sentence above then names a control that is
 * not on the panel, beside rows offering no way to change that. This says the one thing a citizen
 * in that state can act on, and where.
 */
const FOOTER_NO_SWITCH =
  'No data is connected to this application yet. Access is granted once, by an administrator — ask ' +
  'for it under Integrations, and it then covers every application you own.'

const LABEL = 'The BIAL data this application may read'

/**
 * Which of the four project states one row is in.
 *
 * `declined` LANDS IN `noAccess` WITH `neverAsked`, and that is the honest arm: what a declined
 * citizen has, on this application, is no access. The whole answer — the date, who decided and
 * their words — is on the Integrations page, which is the surface that owns person-level facts.
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
      // `effectivelyOn`, not `enabled`: "can this application see the data" is the resolver's
      // answer and this must not become a second home for that conjunction. The window is checked
      // beside it because the sentence COUNTS it.
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

/** The sentence under the connector's name, and the ink it is set in. */
function stateLine(entry: ProjectConnectorEntry, view: RowView): { text: string; tone: string } {
  switch (view.kind) {
    case 'on':
      return { text: `Reading ${view.days} days of ${entry.dataNoun}`, tone: 'text-neutral' }
    case 'off':
      return { text: `Switch it on when a chat needs ${entry.dataNoun}`, tone: 'text-neutral' }
    case 'noAccess':
      // THE CONNECTOR IS NAMED FROM THE WIRE. A literal here would be the one place in the
      // feature that has to change for a second connector.
      return { text: `You do not have access to ${entry.displayName} yet`, tone: 'text-neutral' }
    case 'waiting':
      return {
        text:
          view.askedAt === null
            ? 'You asked for access — waiting on an administrator'
            : `You asked for access on ${dayMonth(view.askedAt)} — waiting on an administrator`,
        // A wait is not a failure and must not be set in the grey of a settled state.
        tone: 'text-status-amber-fg',
      }
    default:
      return assertNever(view)
  }
}

export interface IntegrationsTabProps {
  projectId: string
}

export default function IntegrationsTab({ projectId }: IntegrationsTabProps): React.JSX.Element {
  const [entries, setEntries] = useState<ProjectConnectorEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  // A stale read must not overwrite a fresher one — `load` is also called by the retry.
  const loadSeq = useRef(0)
  // THE WRITES' OWN STAMP, per connector, and a different guard from the row's. The row discards
  // a stale answer for its own switch; this discards one for the sentence beside it.
  const writeSeq = useRef(new Map<string, number>())

  const load = useCallback(async (): Promise<void> => {
    const seq = ++loadSeq.current
    setError(null)
    try {
      const rows = await listProjectConnectors(projectId)
      if (loadSeq.current === seq) setEntries(rows)
    } catch (caught) {
      if (loadSeq.current === seq) setError(errorText(caught))
    }
  }, [projectId])

  useEffect(() => {
    void load()
  }, [load])

  /**
   * One row's write, and the settled answer kept where the sentence reads it.
   *
   * The row is handed back the whole server entry — `ProjectConnectorEntry` is a superset of the
   * state it asked for — so its switch and its chip settle from the same object this tab draws
   * the sentence from, and no `enabled && approved` exists anywhere in this file.
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

  return (
    <div data-testid="integrations-tab" className="flex flex-col gap-3">
      <p className="m-0 text-xs font-semibold text-tertiary">{LABEL}</p>

      {error !== null && (
        // A FAILED READ IS PROSE AND A RETRY, NOT AN `alert`. Nobody asked for this fetch — it
        // fires when the tab is chosen — so interrupting a reader with it is the wrong weight,
        // and the failure is already where the answer was going to be. The WRITE failure below
        // keeps `alert`, because that one follows a press and a silent rollback reads as a
        // missed click.
        <div data-testid="integrations-tab-error">
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
          data-testid="integrations-tab-write-failure"
          className="flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 px-3 py-2"
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
            data-testid="integrations-tab-loading"
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
            <span className="sr-only">Loading this application’s data…</span>
          </div>
        )
      ) : entries.length === 0 ? (
        <p className="m-0 rounded-[11px] border border-bial-border px-[13px] py-3 text-[11.5px] text-neutral">
          Nothing is connected to the platform yet.
        </p>
      ) : (
        <ul
          data-testid="integrations-tab-list"
          className="divide-y divide-bial-border overflow-hidden rounded-[11px] border border-bial-border bg-white"
        >
          {entries.map((entry) => {
            const view = viewOf(entry)
            const line = stateLine(entry, view)
            return (
              <ProjectConnectorRow
                key={entry.key}
                testId={`app-connector-${entry.key}`}
                connectorName={entry.displayName}
                leading={<ConnectorGlyph />}
                detail={
                  <div className="mt-0.5">
                    <span className={`text-[10.5px] leading-[1.45] ${line.tone}`}>{line.text}</span>
                  </div>
                }
                trailing={
                  // READ-OUTS, NOT CONTROLS. `<span>` rather than a disabled button on purpose:
                  // there is nothing here to press, and asking is a person-level act that lives
                  // on the Integrations page.
                  view.kind === 'noAccess' ? (
                    <span className="flex-shrink-0 text-[10.5px] font-bold text-canvas-placeholder">
                      No access
                    </span>
                  ) : view.kind === 'waiting' ? (
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

      <p className="m-0 text-[11px] leading-[1.6] text-neutral">
        {/* While the read is still out, `entries` is null and nothing is known — say the
            general thing rather than claim an absence that may be about to be disproved. */}
        {entries !== null && !entries.some((entry) => entry.state === 'approved')
          ? FOOTER_NO_SWITCH
          : FOOTER}
      </p>
    </div>
  )
}
