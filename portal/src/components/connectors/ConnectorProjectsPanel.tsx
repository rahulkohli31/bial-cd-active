/**
 * `DialogProjects` — every project the citizen owns, each with its own switch and its own days.
 *
 * A BODY OF THE INTEGRATIONS DIALOG, not a dialog of its own, for the reason `AskAccessPanel`
 * gives beside it: `IntegrationsDialog` owns one `<Dialog open>` mount and swaps its body, so a
 * second dialog would unmount one Radix dialog and mount another on every forward and back click
 * — a double backdrop fade, and `dialog.tsx`'s focus backstop firing twice, on what the boards
 * draw as one continuous panel.
 *
 * IT OWNS THE READ; EACH ROW OWNS ITS OWN WRITE. This panel fetches the list once, and a switch
 * press does NOT reload it — `ProjectConnectorRow` settles from the write's own answer, so
 * toggling project A leaves project B's switch and chip untouched instead of re-rendering five
 * rows out of a list that only one of them changed. `loadSeq` is still here for the read, which
 * the retry can start a second time.
 *
 * ZERO PROJECTS IS A GUARANTEED STATE, NOT AN EDGE CASE. A grant runs forward, so an
 * administrator can approve somebody before they have made anything. No board draws it; the line
 * is authored, and it points at what to do rather than reporting an absence.
 *
 * THE FAILURE CHANNEL IS AN ALERT INSIDE THE PANEL, not the page-level toast the admin console
 * uses. There is no toast host above this dialog to reach — it opens from the profile menu over
 * whatever the citizen was doing — and a fixed banner behind a modal scrim is not somewhere a
 * message can be read. It is a live region either way, which is the part that matters: a switch
 * that rolls back in silence looks exactly like a press that missed.
 *
 * SEARCH FILTERS WHAT IS ALREADY HERE. The server has no project search; this is a browser filter
 * over the fetched list, which is why the truncation notice below says the overflow is NOT on
 * this list rather than inviting a search that could not reach it.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronLeft, Search, X } from 'lucide-react'
import {
  listConnectorProjects,
  setProjectConnector,
} from '../../utils/connectorApi'
import type {
  ConnectorEntry,
  ConnectorProjectEntry,
  WindowChoice,
} from '../../utils/connectorApi'
import { DialogTitle } from '../ui/dialog'
import { Input } from '../ui/input'
import { ConnectorGlyph, dayMonth } from './ConnectorRow'
import ProjectConnectorRow from './ProjectConnectorRow'
import type { ProjectConnectorState } from './ProjectConnectorRow'

/** The one id `DialogContent` describes itself with, whichever body is rendering it. */
const SUBTITLE_ID = 'integrations-dialog-subtitle'

/** The board's footer, verbatim — it is the whole explanation of what the chips beside it do. */
const FOOTER =
  'The calendar sets how far back each project reads while you build it — a published app reads whatever dates the person looking at it picks.'

/** Authored: no board draws the state, because a grant runs forward and this one is guaranteed. */
const NO_PROJECTS = 'You do not have a project yet — the switch will be here when you make one.'

const NO_MATCH = 'No project matches that search.'

/**
 * The board's header sentence, with every absent part dropped rather than rendered as a gap.
 *
 * `approvedByName` IS ALLOWED TO BE ABSENT and it does not mean "look up the email" — the server
 * already substitutes one when a display name is unset, so `null` means the administrator who
 * decided has since been deleted. `Approved for you 2 Sep.` is the true sentence then.
 */
function approvalLine(entry: ConnectorEntry): string {
  const when = entry.approvedAt === null ? '' : ` ${dayMonth(entry.approvedAt)}`
  const who = entry.approvedByName === null ? '' : ` by ${entry.approvedByName}`
  return `Approved for you${when}${who}. Switch it on where you need it.`
}

export interface ConnectorProjectsPanelProps {
  /** The approved connector the citizen drilled into. Every string about it comes off here. */
  entry: ConnectorEntry
  /** The back chevron. Returns to the connector list — the header's X is what closes. */
  onBack: () => void
  onClose: () => void
}

export default function ConnectorProjectsPanel({
  entry,
  onBack,
  onClose,
}: ConnectorProjectsPanelProps): React.JSX.Element {
  const [projects, setProjects] = useState<ConnectorProjectEntry[] | null>(null)
  const [truncated, setTruncated] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const loadSeq = useRef(0)

  const load = useCallback(async (): Promise<void> => {
    const seq = ++loadSeq.current
    setError(null)
    try {
      const answer = await listConnectorProjects(entry.key)
      if (loadSeq.current !== seq) return
      setProjects(answer.projects)
      setTruncated(answer.truncated)
    } catch (caught) {
      if (loadSeq.current !== seq) return
      setError(caught instanceof Error ? caught.message : String(caught))
    }
  }, [entry.key])

  useEffect(() => {
    void load()
  }, [load])

  const write = useCallback(
    (projectId: string, update: { enabled: boolean; window?: WindowChoice }) =>
      setProjectConnector(projectId, entry.key, update),
    [entry.key],
  )

  /**
   * KEEP THE LIST IN STEP WITH WHAT THE SERVER SETTLED, exactly as the rail's DATA section does.
   *
   * This does NOT re-read the list — one switch press must not reorder or reload the other rows.
   * It patches the single row the write answered for, and it is load-bearing twice over, because
   * `ProjectConnectorRow` treats its props as "the last settled answer":
   *
   * 1. ITS FAILURE ROLLBACK GOES BACK TO THE PROPS. Without this patch the props are whatever
   *    `load()` fetched, so a successful toggle followed by a FAILED one rolled the switch back
   *    to the state the dialog was opened with — leaving the citizen looking at a switch in the
   *    position they asked for while the server held the opposite, with a red banner above it.
   * 2. THE SEARCH UNMOUNTS ROWS. `shown` is a filtered array, so a row that stops matching is
   *    destroyed and its local override dies with it; when it matches again it remounts from
   *    these props. Without the patch, typing a query and clearing it resurrected the
   *    pre-toggle switch position for every row that had been toggled.
   *
   * Both were invisible to the suite because its failure test toggles only once, from a state
   * where the props and the server happen to agree.
   */
  const recordSettled = useCallback((projectId: string, settled: ProjectConnectorState): void => {
    setProjects((rows) =>
      rows === null
        ? rows
        : rows.map((row) =>
            row.projectId === projectId
              ? { ...row, enabled: settled.enabled, window: settled.window }
              : row,
          ),
    )
  }, [])

  const needle = query.trim().toLowerCase()
  const shown =
    projects === null
      ? null
      : needle === ''
        ? projects
        : projects.filter((project) => project.name.toLowerCase().includes(needle))

  return (
    <div data-testid="connector-projects-panel">
      <div className="flex items-start gap-2.5 px-6 pt-[22px]">
        <button
          type="button"
          onClick={onBack}
          aria-label="Back to integrations"
          className="flex-shrink-0 pt-0.5 text-neutral transition hover:text-primary-900"
        >
          <ChevronLeft size={16} />
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2.5">
            <ConnectorGlyph size="title" />
            <DialogTitle className="text-base font-extrabold tracking-[-0.2px] text-primary-900">
              {entry.displayName}
            </DialogTitle>
          </div>
          <p id={SUBTITLE_ID} className="mt-[5px] text-xs leading-[1.6] text-neutral">
            {approvalLine(entry)}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="flex-shrink-0 p-0.5 text-neutral transition hover:text-primary-900"
        >
          <X size={17} />
        </button>
      </div>

      {error !== null && (
        <div className="px-6 pt-4">
          <div role="alert" className="rounded-xl border border-red-200 bg-red-50 px-3 py-2.5">
            <p className="text-xs text-red-600">{error}</p>
            <button
              type="button"
              onClick={() => void load()}
              className="mt-1.5 text-xs font-semibold text-primary underline-offset-2 hover:underline"
            >
              Try again
            </button>
          </div>
        </div>
      )}

      {failure !== null && (
        <div className="px-6 pt-4">
          <div
            role="alert"
            data-testid="connector-projects-toast"
            className="flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 px-3 py-2.5"
          >
            <p className="flex-1 text-xs text-red-600">{failure}</p>
            <button
              type="button"
              onClick={() => setFailure(null)}
              aria-label="Dismiss"
              className="flex-shrink-0 text-red-600/70 transition hover:text-red-600"
            >
              <X size={13} />
            </button>
          </div>
        </div>
      )}

      {projects !== null && projects.length > 0 && (
        <>
          {truncated && (
            <div className="px-6 pt-4">
              <p className="m-0 text-[11px] leading-[1.55] text-neutral">
                {/* The number is COUNTED, not written down: the server's cap is the server's, and
                    a hard-coded 200 here would be a second copy of it that ages badly. */}
                Showing your {projects.length} most recent projects. Any older ones are not on this
                list.
              </p>
            </div>
          )}
          <div className="relative px-6 pt-4">
            <Search
              size={14}
              aria-hidden
              className="pointer-events-none absolute left-[37px] top-1/2 mt-2 -translate-y-1/2 text-canvas-placeholder"
            />
            <Input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search projects…"
              aria-label={`Search your projects for ${entry.displayName}`}
              className="h-auto rounded-[10px] border-bial-border py-[9px] pl-[34px] pr-3 text-[12.5px] shadow-none placeholder:text-canvas-placeholder md:text-[12.5px]"
            />
          </div>
        </>
      )}

      <div className="px-6 pb-5 pt-4">
        {shown === null ? (
          error === null && (
            // A skeleton, never a blank gap — the same reasoning as the connector list's.
            <div
              data-testid="connector-projects-loading"
              className="divide-y divide-bial-border overflow-hidden rounded-xl border border-bial-border bg-white"
            >
              {[0, 1, 2].map((row) => (
                <div key={row} className="flex items-center gap-3 px-[13px] py-[11px]">
                  <span className="h-3 w-40 flex-1 animate-pulse rounded bg-canvas-tile" />
                  <span className="h-[18px] w-8 flex-shrink-0 animate-pulse rounded-full bg-canvas-tile" />
                </div>
              ))}
              <span className="sr-only">Loading your projects…</span>
            </div>
          )
        ) : projects !== null && projects.length === 0 ? (
          <p
            data-testid="connector-projects-empty"
            className="m-0 rounded-xl border border-bial-border px-[13px] py-3 text-[11.5px] text-neutral"
          >
            {NO_PROJECTS}
          </p>
        ) : shown.length === 0 ? (
          <p
            data-testid="connector-projects-no-match"
            className="m-0 rounded-xl border border-bial-border px-[13px] py-3 text-[11.5px] text-neutral"
          >
            {NO_MATCH}
          </p>
        ) : (
          <ul className="divide-y divide-bial-border overflow-hidden rounded-xl border border-bial-border bg-white">
            {shown.map((project) => (
              <ProjectConnectorRow
                key={project.projectId}
                testId={`project-connector-${project.projectId}`}
                connectorName={entry.displayName}
                projectName={project.name}
                enabled={project.enabled}
                window={project.window}
                onSet={(update) => write(project.projectId, update)}
                onSettled={(settled) => recordSettled(project.projectId, settled)}
                onError={setFailure}
              />
            ))}
          </ul>
        )}
      </div>

      <div className="px-6 pb-[18px]">
        <p className="m-0 text-[11px] leading-[1.6] text-neutral">{FOOTER}</p>
      </div>
    </div>
  )
}
