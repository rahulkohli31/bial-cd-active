/**
 * Turn frames → the envelope shape `BuildProgress` already renders (U5).
 *
 * A build is a Write turn now, so its narrative arrives as `step` / `diagnostic` / `quota`
 * turn frames instead of C7 progress envelopes. ADAPTING rather than rewriting is deliberate:
 * `BuildProgress` is the one place that knows how a build should read to a citizen — the
 * dedupe, the hidden-step policy, the elapsed-time reassurance, the Details expander — and a
 * second renderer fed by the new frames would be a second answer to all of that, free to
 * drift. One component, one story, two sources that agree by construction.
 *
 * The mapping is small because the two vocabularies already describe the same thing. The one
 * place they genuinely differ is `diagnostic` → `error`: on the turn stream a diagnostic is
 * explicitly NOT a failure (a repair run follows), while the envelope feed's `error` was
 * always an in-narrative alert with a repair behind it too. Same meaning, older name.
 */
import type { StepItem } from './turnStreamApi'
import type {
  BuildSessionStatus,
  ErrorEvent,
  ErrorSource,
  FeedEnvelope,
  QuotaExceededEvent,
  StepEvent,
} from './buildSessionTypes'

export interface TurnNarrative {
  /** Live steps, keyed by tool-call id so the `finished` frame REPLACES its `started` one. */
  steps: Record<string, StepItem>
  diagnostics: { source: string; title: string; cleanedStack: string }[]
  quota: { limit: number; used: number; resetsAt: string } | null
  workspace: { state: 'preparing' | 'ready' | 'unavailable'; message: string | null } | null
  preview: { url: string | null; state: 'ready' | 'reconnecting' | null }
}

const ERROR_SOURCES = new Set(['tsc', 'next_build', 'server', 'client'])

/**
 * The synthetic `seq` space. Envelopes are deduped and ordered BY SEQ, and turn frames carry
 * their own seq numbering that these items do not preserve — so order is imposed here, by
 * emission order, with diagnostics and the quota notice after the steps they followed.
 */
export function narrativeEnvelopes(narrative: TurnNarrative): FeedEnvelope[] {
  const out: FeedEnvelope[] = []
  let seq = 1

  for (const item of Object.values(narrative.steps)) {
    const step: StepEvent = {
      type: 'step',
      seq: seq++,
      name: item.tool,
      label: item.label,
      // `pending` is the in-flight state in the turn vocabulary; `started` is its name here.
      state: item.state === 'pending' ? 'started' : item.state,
      hidden: item.hidden,
    }
    out.push(step)
  }

  for (const diagnostic of narrative.diagnostics) {
    const error: ErrorEvent = {
      type: 'error',
      seq: seq++,
      // Fail to `server` rather than drop: an unrecognized source still has a real title and
      // stack the user needs to see, and a swallowed diagnostic is a silent build failure.
      source: (ERROR_SOURCES.has(diagnostic.source) ? diagnostic.source : 'server') as ErrorSource,
      title: diagnostic.title,
      cleaned_stack: diagnostic.cleanedStack,
    }
    out.push(error)
  }

  if (narrative.quota) {
    const quota: QuotaExceededEvent = {
      type: 'quota_exceeded',
      seq: seq++,
      limit: narrative.quota.limit,
      used: narrative.quota.used,
      resets_at: narrative.quota.resetsAt,
    }
    out.push(quota)
  }

  return out
}

/**
 * The phase a Write turn is in, in the status vocabulary `BuildProgress` reads.
 *
 * `null` — not a build turn at all, so the bubble stays out of the way. The ordering below is
 * the honest one: an unavailable workspace is terminal for this turn no matter what else
 * arrived, and a live preview outranks "still provisioning" because the user can SEE it.
 */
export function narrativeStatus(
  narrative: TurnNarrative,
  { running, terminal }: { running: boolean; terminal: 'completed' | 'failed' | 'stopped' | null }
): BuildSessionStatus | null {
  if (narrative.workspace === null) return null
  if (narrative.workspace.state === 'unavailable') return 'failed'
  if (terminal === 'failed' || terminal === 'stopped') return 'failed'
  if (terminal === 'completed') return 'ended'
  if (!running) return null
  if (narrative.preview.state === 'ready') return 'ready'
  return narrative.workspace.state === 'preparing' ? 'provisioning' : 'building'
}
