import { useCallback, useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { listProjectConnectors, setProjectConnector } from '../../utils/connectorApi'
import type { ProjectConnectorEntry, WindowChoice } from '../../utils/connectorApi'
import { errorText } from '../../utils/apiError'
import { ConnectorGlyph } from '../connectors/connectorPresentation'
import ProjectConnectorRow from '../connectors/ProjectConnectorRow'

/**
 * SETTINGS › INTEGRATIONS — the BIAL data this one application may read, and the switch that
 * decides it. The switch is the whole of access: nobody else is asked.
 *
 * THE ROW IS `ProjectConnectorRow`, MOUNTED — NOT FORKED, which is what keeps the optimistic
 * flip, the rollback, the in-flight lock and the write stamp in one implementation.
 *
 * A SKELETON, NEVER A BLANK GAP. The read fires when the tab is chosen, so the pre-load moment is
 * guaranteed rather than an edge case, and an empty box between two hairlines reads as "nothing
 * is connected" — a different and false statement.
 */

/** The board's own footer — what the switch beside it does and does not reach. */
const FOOTER = 'The switch controls this application only.'

const LABEL = 'The BIAL data this application may read'

/**
 * The sentence under the connector's name. `effectivelyOn`, not `enabled`: "can this application
 * see the data" is the resolver's answer. The window is checked beside it because the sentence
 * COUNTS it.
 */
function stateLine(entry: ProjectConnectorEntry): string {
  return entry.effectivelyOn && entry.window !== null
    ? `Reading ${entry.window.days} days of ${entry.dataNoun}`
    : `Switch it on when a chat needs ${entry.dataNoun}`
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
   * the sentence from.
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
          {entries.map((entry) => (
            <ProjectConnectorRow
              key={entry.key}
              testId={`app-connector-${entry.key}`}
              connectorName={entry.displayName}
              leading={<ConnectorGlyph />}
              detail={
                <div className="mt-0.5">
                  <span className="text-[10.5px] leading-[1.45] text-neutral">{stateLine(entry)}</span>
                </div>
              }
              enabled={entry.enabled}
              window={entry.window}
              onSet={(update) => write(entry.key, update)}
              onError={setFailure}
            />
          ))}
        </ul>
      )}

      <p className="m-0 text-[11px] leading-[1.6] text-neutral">{FOOTER}</p>
    </div>
  )
}
