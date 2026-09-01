/**
 * PlanOptionsCard (U11) — the Build it / Keep refining confirmation, rendered live (a
 * `plan_options` frame) and on reload (the projection item) from the SAME item shape, so
 * the card can never disagree with the stored record.
 *
 * States: `pending` shows the two buttons (only the NEWEST card is actionable — older
 * pendings render expired); `refine` / `build` render the settled choice. There is no third
 * settled state and no re-arm arm: a card used to be BURNED by a press that failed, and
 * `build_failed` re-armed it with the failure named. Build-it starts a whole new chat now and
 * answers this offer only once that chat's turn is running, so a press that fails records
 * nothing — the card was never spent, and there is nothing to un-spend. A failure is said once,
 * in the error line below the buttons, by the caller that actually saw it.
 *
 * "Keep refining" resolves here; "Build it" is the caller's handoff (through `onBuildIt`).
 */

import { useState } from 'react'

import { Button } from '../ui/button'
import { resolvePlanOptions, type PlanOptionsItem } from '../../utils/turnStreamApi'

export interface PlanOptionsCardProps {
  conversationId: string
  item: PlanOptionsItem
  /** True when a newer card supersedes this one — renders as expired, never actionable. */
  expired?: boolean
  /** The atomic Build-it transition (U12). Absent → the button renders disabled. */
  onBuildIt?: (toolCallId: string) => Promise<void> | void
  /** Called after "Keep refining" resolves server-side (the caller refreshes state). */
  onRefined?: (toolCallId: string) => void
}

export function PlanOptionsCard({
  conversationId,
  item,
  expired = false,
  onBuildIt,
  onRefined,
}: PlanOptionsCardProps) {
  const [busy, setBusy] = useState<'build' | 'refine' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const actionable = !expired && item.state === 'pending'

  const handleRefine = async () => {
    setBusy('refine')
    setError(null)
    try {
      await resolvePlanOptions(conversationId, item.toolCallId)
      onRefined?.(item.toolCallId)
    } catch {
      setError('That choice could not be recorded. Try again.')
    } finally {
      setBusy(null)
    }
  }

  const handleBuild = async () => {
    if (!onBuildIt) return
    setBusy('build')
    setError(null)
    try {
      await onBuildIt(item.toolCallId)
    } catch {
      setError('The build could not be started. The options stay available — try again.')
    } finally {
      setBusy(null)
    }
  }

  if (item.state === 'refine') {
    return (
      <div className="plan-options-card" data-state="refine">
        <p className="text-sm text-muted-foreground">You kept refining this plan.</p>
      </div>
    )
  }
  if (item.state === 'build') {
    return (
      <div className="plan-options-card" data-state="build">
        <p className="text-sm text-muted-foreground">Build started from this plan.</p>
      </div>
    )
  }
  if (expired) {
    return (
      <div className="plan-options-card" data-state="expired">
        <p className="text-sm text-muted-foreground">A newer plan supersedes these options.</p>
      </div>
    )
  }

  return (
    <div className="plan-options-card" data-state={item.state}>
      <p className="text-sm">Ready to build this plan?</p>
      <div className="flex gap-2">
        <Button
          size="sm"
          onClick={() => void handleBuild()}
          disabled={!actionable || onBuildIt === undefined || busy !== null}
        >
          {busy === 'build' ? 'Starting…' : 'Build it'}
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => void handleRefine()}
          disabled={!actionable || busy !== null}
        >
          {busy === 'refine' ? 'Recording…' : 'Keep refining'}
        </Button>
      </div>
      {error !== null && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
