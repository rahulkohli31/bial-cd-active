/**
 * One connector, one project: a switch and the days it reads. BUILT ONCE AND MOUNTED TWICE.
 *
 * THE TWO MOUNTS, and the reason the props are shaped the way they are:
 *
 *   - The drill-down (`ConnectorProjectsPanel`, U8) mounts one per project the citizen owns.
 *     `projectName` is that project's name, which is what the row draws on the left and what both
 *     controls name themselves after.
 *   - The rail's DATA section (`DataSection`, U10) mounts one per registry connector, on the
 *     project screen. `projectName` is `null` there — the project IS the screen — so the row
 *     draws the connector's name instead and the controls say `in this project`. The rail also
 *     passes `leading` (the connector's teal tile) and `detail` (its state sentence), and, for
 *     the two states that have no switch to offer, `trailing` (`Request →` / an inert `Waiting`).
 *
 * U10 MUST NOT NEED TO FORK THIS. The switch and the chip being the same components in both
 * places is what makes origin R5's "the two must read from one source and cannot disagree"
 * structural rather than an assertion — the rail and the drill-down cannot show different
 * switch positions for one project, because there is one component and one write.
 *
 * IT OWNS ITS WRITE, NOT ITS TRUTH. The row holds no fetch and no list; the mount site passes
 * `onSet` and gets `onSettled` back. What the row DOES own is the mechanics of one write — the
 * optimistic flip, the rollback, the in-flight lock and the sequence stamp — because those are
 * what both mounts would otherwise reimplement, and the second implementation is where they
 * would drift.
 *
 * THE LOCK AND THE STAMP ARE TWO DIFFERENT GUARDS, and only one of them can be dropped without
 * anything going red, which is why both are here and both have a test:
 *
 *   - THE LOCK is the switch's own. Between its press and its answer the switch is
 *     `aria-disabled` and its change handler returns early, so a double press sends one write.
 *     `aria-disabled` rather than the native `disabled`, per `dialog.tsx`'s focus backstop:
 *     disabling a focused control throws focus to `<body>` mid-request.
 *   - THE STAMP is the ROW's. The switch and the popover's `Apply` are two controls writing the
 *     same row, and the switch's lock says nothing about a write `Apply` started — so a slow
 *     `Apply` and a fast toggle genuinely overlap. Every write takes a number; a response whose
 *     number is no longer the newest is discarded, along with its rollback and its error. Without
 *     it the late answer wins and the switch adopts whichever resolved last, which is the same
 *     `loadSeq` failure `AppRegistryPanel` guards on its reads, carried onto the mutation.
 *
 * NO WINDOW IS EVER GUESSED. A toggle flips `enabled` optimistically because a switch that lags
 * its own press feels broken — but the chip re-renders only from what the server RESOLVED, never
 * from the local pick, because the resolver is the only thing that knows about the clamp (R13).
 */
import { useCallback, useRef, useState } from 'react'
import type { ConnectorWindow, WindowChoice } from '../../utils/connectorApi'
import { Popover } from '../ui/popover'
import { Switch } from '../ui/switch'
import WindowChip, { formatWindowLabel } from './WindowChip'
import WindowPopover from './WindowPopover'

/**
 * The two facts this row renders. Both wire objects that a write can answer with —
 * `ProjectConnectorEntry` and `ConnectorProjectEntry` — carry them, so either satisfies this.
 */
export interface ProjectConnectorState {
  enabled: boolean
  window: ConnectorWindow | null
}

export interface ProjectConnectorRowProps {
  /** The connector's display name, off the wire. Both controls name it. */
  connectorName: string
  /**
   * The project this row is about, when the row is one of several projects. `null` in the rail,
   * where the row is the connector and the project is the whole screen — it selects the bold
   * label AND how the two controls name themselves.
   */
  projectName: string | null
  /** Drawn before the label. The rail passes the connector's tile; the drill-down passes none. */
  leading?: React.ReactNode
  /** A sentence under the label — the rail's project-state line. The drill-down has none. */
  detail?: React.ReactNode
  /**
   * Replaces the chip and the switch entirely. The rail's states c and d have no switch to
   * offer (`Request →`, and an inert `Waiting`); a row with no access must not draw a control
   * that would be refused.
   */
  trailing?: React.ReactNode
  enabled: boolean
  /** `null` for a project this connector was never switched on in — an em dash, not an empty chip. */
  window: ConnectorWindow | null
  /**
   * Writes the row's new state and answers with what the SERVER resolved. Rejects on failure;
   * the row rolls back and reports through `onError` for a toggle, and re-throws to the popover
   * for an `Apply` so the refusal stays where the citizen is looking.
   */
  onSet: (update: { enabled: boolean; window?: WindowChoice }) => Promise<ProjectConnectorState>
  /**
   * The settled server answer, for a mount that keeps a count or a sentence beside the row —
   * the rail's `1 of 2 on` and its `Reading N days of flight data`. The drill-down needs none.
   */
  onSettled?: (settled: ProjectConnectorState) => void
  /** A failed TOGGLE, in the mount site's own words channel. A silent rollback reads as a missed click. */
  onError: (message: string) => void
  testId?: string
}

/** A window signature that changes exactly when something the row renders changes. */
function signature(state: ProjectConnectorState): string {
  const w = state.window
  return `${state.enabled}|${w === null ? '' : `${w.kind}:${w.start}:${w.end}:${w.days}`}`
}

function message(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught)
}

export default function ProjectConnectorRow({
  connectorName,
  projectName,
  leading,
  detail,
  trailing,
  enabled,
  window,
  onSet,
  onSettled,
  onError,
  testId,
}: ProjectConnectorRowProps): React.JSX.Element {
  // THE ROW'S OWN NEWER ANSWER, or `null` for "what the props say". A write settles here rather
  // than waiting for the mount site to re-read: the drill-down deliberately does not reload the
  // whole list on one switch press, so project A's answer must not touch project B's row.
  const [override, setOverride] = useState<ProjectConnectorState | null>(null)
  const [switchBusy, setSwitchBusy] = useState(false)
  const [popoverOpen, setPopoverOpen] = useState(false)
  const writeSeq = useRef(0)

  const fromProps: ProjectConnectorState = { enabled, window }
  // Adjusting state during render, the documented React pattern: when the mount site hands down
  // a genuinely different row, its value is newer than ours and the override goes.
  const propsSignature = signature(fromProps)
  const lastSeen = useRef(propsSignature)
  if (lastSeen.current !== propsSignature) {
    lastSeen.current = propsSignature
    setOverride(null)
  }
  const shown = override ?? fromProps

  const write = useCallback(
    async (update: { enabled: boolean; window?: WindowChoice }): Promise<void> => {
      const seq = ++writeSeq.current
      try {
        const settled = await onSet(update)
        // A newer write owns this row now. Its answer is the one on screen, and this one's —
        // including its failure — belongs to a state the citizen has already moved past.
        if (writeSeq.current !== seq) return
        const next = { enabled: settled.enabled, window: settled.window }
        setOverride(next)
        onSettled?.(next)
      } catch (caught) {
        if (writeSeq.current !== seq) return
        throw caught
      }
    },
    [onSet, onSettled],
  )

  const toggle = useCallback(
    (next: boolean): void => {
      // The lock, doing as well as saying: `aria-disabled` alone would still deliver the click.
      if (switchBusy) return
      setSwitchBusy(true)
      // Optimistic, and only on `enabled` — the window it shows beside the switch stays whatever
      // the server last resolved.
      setOverride({ enabled: next, window: shown.window })
      void write({ enabled: next })
        .catch((caught: unknown) => {
          // Back to the props, AND said out loud. Restoring the switch in silence looks
          // identical to the press never landing.
          setOverride(null)
          onError(message(caught))
        })
        .finally(() => setSwitchBusy(false))
    },
    [onError, shown.window, switchBusy, write],
  )

  const apply = useCallback(
    async (choice: WindowChoice): Promise<void> => {
      // The switch position is carried through unchanged: this write is about the days.
      await write({ enabled: shown.enabled, window: choice })
      setPopoverOpen(false)
    },
    [shown.enabled, write],
  )

  const where = projectName === null ? 'this project' : projectName
  const label = projectName ?? connectorName

  return (
    <li
      data-testid={testId}
      className="flex items-center gap-3 bg-white px-[13px] py-[11px]"
    >
      {leading}
      <div className="min-w-0 flex-1">
        <div className="truncate text-[12.5px] font-semibold text-primary-900">{label}</div>
        {detail}
      </div>

      {trailing ?? (
        <>
          {shown.enabled && shown.window !== null ? (
            <Popover open={popoverOpen} onOpenChange={setPopoverOpen}>
              {/* The announced name carries the subject AND the value the eye sees — `1 – 30
                  Sep` alone says nothing about which of five projects it belongs to. */}
              <WindowChip
                window={shown.window}
                accessibleName={`Days ${connectorName} reads in ${where}: ${formatWindowLabel(shown.window)}`}
              />
              {popoverOpen && (
                <WindowPopover
                  connectorName={connectorName}
                  window={shown.window}
                  onApply={apply}
                  onCancel={() => setPopoverOpen(false)}
                />
              )}
            </Popover>
          ) : (
            <span aria-hidden className="flex-shrink-0 text-[10.5px] text-canvas-placeholder">
              —
            </span>
          )}

          <Switch
            checked={shown.enabled}
            aria-disabled={switchBusy}
            aria-label={`Read ${connectorName} in ${where}`}
            onCheckedChange={toggle}
          />
        </>
      )}
    </li>
  )
}
