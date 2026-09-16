/**
 * One connector, one project: a switch and the days it reads.
 *
 * `projectName` SELECTS WHO THE ROW IS ABOUT. Settings › Integrations mounts one per registry
 * connector and passes `null` — the application IS the surface — so the row draws the connector's
 * name and both controls say `in this project`. A mount that lists several applications passes
 * each one's name instead, and the label and the control names follow it. The tab also passes
 * `leading` (the connector's teal tile), `detail` (its state sentence), and, for the two states
 * with no switch to offer, `trailing` (an inert read-out).
 *
 * IT OWNS ITS WRITE, NOT ITS TRUTH. The row holds no fetch and no list; the mount site passes
 * `onSet` and gets `onSettled` back. What the row DOES own is the mechanics of one write — the
 * optimistic flip, the rollback, the in-flight lock and the sequence stamp — because a second
 * mount reimplementing them is where the two would drift.
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
 * from the local pick, because the resolver is the only thing that knows about the clamp.
 */
import { useCallback, useRef, useState } from 'react'
import type { ConnectorWindow, WindowChoice } from '../../utils/connectorApi'
import { errorText } from '../../utils/apiError'
import { Popover } from '../ui/popover'
import { Switch } from '../ui/switch'
import WindowChip, { formatWindowLabel } from './WindowChip'
import WindowPopover from './WindowPopover'

/**
 * The two facts this row renders — a narrowing rather than a wire type, so any object a write can
 * answer with satisfies it. `ProjectConnectorEntry` is the one that does today.
 */
export interface ProjectConnectorState {
  enabled: boolean
  window: ConnectorWindow | null
}

export interface ProjectConnectorRowProps {
  /** The connector's display name, off the wire. Both controls name it. */
  connectorName: string
  /**
   * The application this row is about, when the row is one of several applications. `null` in
   * Settings › Integrations, where the row is the connector and the application is the whole
   * surface — it selects the bold label AND how the two controls name themselves.
   */
  projectName: string | null
  /** Drawn before the label. The settings tab passes the connector's teal tile. */
  leading?: React.ReactNode
  /** A sentence under the label — the settings tab's project-state line. */
  detail?: React.ReactNode
  /**
   * Replaces the chip and the switch entirely. The two states with no access have no switch to
   * offer; a row with no access must not draw a control that would be refused.
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
   * The settled server answer, for a mount that keeps a sentence or a count beside the row — the
   * settings tab's `Reading N days of flight data`. A mount that re-reads instead needs none.
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
  // than waiting for the mount site to re-read, so one row's answer never touches another's.
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
          onError(errorText(caught))
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

  const where = projectName === null ? 'this application' : projectName
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
