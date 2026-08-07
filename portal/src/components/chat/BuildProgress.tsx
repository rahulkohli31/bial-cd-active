/**
 * The chat-native build narrative (U15): ONE assistant bubble that carries the whole
 * live build — friendly steps, a working indicator with elapsed-time reassurance, the
 * Stop / Force-end controls, and a Details expander holding the raw (server-redacted)
 * output. It replaces the cockpit's ActivityFeed pane and SessionControls row: the
 * right pane frames only the app, and the build can never look "dead" — this bubble is
 * visibly alive for exactly as long as the session is.
 *
 * Input is the same C7 envelope stream the feed consumed (pushed into visible React
 * state up front by `useBuildSession` — never a remount). `preview_ready` stays routed
 * to the preview pane by the hook; here its arrival shows as the `ready` headline (the
 * visible chat transition). `ended` is deliberately NOT rendered: the persisted
 * BuildOutcome message is the permanent record, and rendering it here too would print
 * the terminal twice.
 *
 * On reload the U6 projection re-derives the same friendly items from the persisted
 * step rows — live and restored transcripts tell one story.
 */
import {
  AlertTriangle,
  Ban,
  ChevronDown,
  Clock,
  Loader2,
  RotateCw,
  Square,
  XCircle,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { cn } from '@/lib/utils'
import type {
  BuildSessionStatus,
  ErrorEvent,
  EscalationEvent,
  FeedEnvelope,
  LogEvent,
  QuotaExceededEvent,
  StepEvent,
} from '../../utils/buildSessionTypes'
import { formatDailyLimitMessage, isActiveBuildStatus } from '../../utils/buildSessionTypes'
import { assertNever } from '../../utils/assertNever'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '../ui/collapsible'
import { ToolActivityLine, usePrefersReducedMotion } from './ToolActivityLine'

type AlertEnvelope = ErrorEvent | EscalationEvent | QuotaExceededEvent

export interface BuildProgressProps {
  envelopes: FeedEnvelope[]
  status: BuildSessionStatus | null
  startedAt: number | null
  stopping: boolean
  onStop: () => void
  /** OPTIONAL, and its absence is meaningful (U5). Force-end tears down a build SESSION's
   *  sandbox out of band; a build turn has no session, so `stopTurn` is its whole interrupt
   *  vocabulary. Omit the handler and the button does not render — rather than rendering a
   *  kill switch that confirms "this kills in-progress work" and then silently does nothing,
   *  which is how an operator comes to believe a runaway build was stopped. */
  onForceEnd?: () => void
}

/** Dedup by `seq` (last-wins) and order by `seq` — C3 §4.2's replay property, kept. */
function bySeq(envelopes: FeedEnvelope[]): FeedEnvelope[] {
  const latest = new Map<number, FeedEnvelope>()
  for (const env of envelopes) latest.set(env.seq, env)
  return [...latest.values()].sort((a, b) => a.seq - b.seq)
}

function formatElapsed(startedAt: number | null): string {
  if (startedAt === null) return ''
  const secs = Math.max(0, Math.round((Date.now() - startedAt) / 1000))
  const m = Math.floor(secs / 60)
  const s = secs % 60
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}

/** The headline is the build's voice in chat — including the `ready` transition. */
function headline(status: BuildSessionStatus | null): string | null {
  if (status === null) return null
  switch (status) {
    case 'provisioning':
      return 'Setting up your sandbox…'
    case 'building':
      return 'Building your app…'
    case 'ready':
      // `ready` means the PREVIEW is framed, NOT that the build is done — the agent routinely
      // keeps working for several more minutes after the dev server starts serving. The old
      // wording ("Your app is ready") announced a finish that had not happened, and paired with
      // a stopped spinner it let a wedged command look exactly like a completed build.
      return 'Your preview is live on the right — still working on your app…'
    case 'ended':
    case 'failed':
      // The terminal is the outcome bubble's to narrate, not this one's.
      return null
    default:
      return assertNever(status)
  }
}

/**
 * Whether there is any build narrative to show at all.
 *
 * Exported because the bubble's CHROME lives in BuilderPage — the avatar, the rounded
 * background — and it has to make the same call this component does. Rendering the wrapper
 * around a component that returns `null` is an empty grey bubble: harmless-looking, and exactly
 * what a Write turn that fails before its first tool call leaves behind. One expression, two
 * readers, so the two can never disagree about whether there is a story.
 */
export function hasBuildNarrative(
  status: BuildSessionStatus | null,
  envelopes: FeedEnvelope[],
): boolean {
  if (headline(status) !== null) return true
  // A recovering diagnostic is a LIVE story: at a terminal the outcome message narrates the
  // ending, and a leftover "trying another way" would be a second (now false) presentation —
  // so a diagnostic-only narrative counts while the build runs, and stops counting at the
  // terminal, where the render below would show nothing and the chrome would be an empty
  // grey bubble.
  const terminal = status === 'ended' || status === 'failed'
  return envelopes.some(
    (env) =>
      (env.type === 'step' && !env.hidden) ||
      (env.type === 'error' && !(env.recovering && terminal)) ||
      env.type === 'escalation' ||
      env.type === 'quota_exceeded',
  )
}

export default function BuildProgress({
  envelopes,
  status,
  startedAt,
  stopping,
  onStop,
  onForceEnd,
}: BuildProgressProps) {
  const [confirmingForceEnd, setConfirmingForceEnd] = useState(false)
  // Closed by default: the full step history is a look-back an operator opts into AFTER
  // the build ends, never an ambient list shown while it runs (see the live/complete split
  // below).
  const [stepsOpen, setStepsOpen] = useState(false)
  const reduced = usePrefersReducedMotion()
  const active = isActiveBuildStatus(status)
  // THE INDICATOR RUNS UNTIL THE SESSION IS TERMINAL — not until it reaches `ready`.
  //
  // The spinner and the elapsed clock were tied to the *building* state, which ends the moment
  // the preview frames. From roughly a minute in, the chat therefore sat with nothing moving
  // while the agent worked on for another six minutes, and a wedged command was
  // indistinguishable from a finished build.
  //
  // Gated on NOT-TERMINAL rather than on `!== 'ready'`, which is the trap: decoupling this from
  // `building` without naming a stop condition leaves a spinner and a running clock on screen
  // forever after every successful build — one wrong state traded for another. `ended` and
  // `failed` stop it; nothing else does.
  const working = status !== null && status !== 'ended' && status !== 'failed'

  // The elapsed-time reassurance ticks while the build runs — a re-render every few
  // seconds, not a timer per row. Cheap, and it is exactly what makes a long install
  // step read as "still going" rather than "hung".
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!working || startedAt === null) return undefined
    const timer = setInterval(() => setTick((t) => t + 1), 5000)
    return () => clearInterval(timer)
  }, [working, startedAt])

  const rows = bySeq(envelopes)
  const steps = rows.filter((env): env is StepEvent => env.type === 'step')
  // F3/U3: read-only + housekeeping steps are dropped from the VISIBLE feed (the raw command still
  // reaches the model). Everything below the fold renders the FRIENDLY label only — no raw shell.
  const visibleSteps = steps.filter((env) => !env.hidden)
  const logs = rows.filter((env): env is LogEvent => env.type === 'log')
  const alerts = rows.filter(
    (env): env is AlertEnvelope =>
      env.type === 'error' || env.type === 'escalation' || env.type === 'quota_exceeded',
  )
  const line = headline(status)
  if (!hasBuildNarrative(status, envelopes)) return null

  // The FULL history — every visible step, oldest first. Only ever shown after the build
  // ends, behind the collapsed dropdown below; never the live view (see `currentStep`).
  const stepList =
    visibleSteps.length > 0 ? (
      <ol aria-label="Build steps" className="space-y-1">
        {visibleSteps.map((env) => (
          <li key={env.seq} data-kind="step" data-state={env.state}>
            <ToolActivityLine label={env.label || env.name} state={env.state} />
          </li>
        ))}
      </ol>
    ) : null

  // The LIVE view is ONE row, in one fixed spot: the most recent step replaces the
  // previous one in place rather than the transcript growing a line per step. `role="log"
  // aria-live="polite"` still lives here so a screen reader hears each replacement.
  const currentStep = visibleSteps.length > 0 ? visibleSteps[visibleSteps.length - 1] : null
  const currentStepRow = currentStep ? (
    <div role="log" aria-live="polite" aria-label="Build activity">
      <ToolActivityLine label={currentStep.label || currentStep.name} state={currentStep.state} />
    </div>
  ) : null

  const spinner = working ? (
    <Loader2 size={13} className={cn('flex-shrink-0 text-primary', !reduced && 'animate-spin')} />
  ) : null
  const elapsed =
    working && startedAt !== null ? (
      <span className="flex-shrink-0 text-neutral/70">running {formatElapsed(startedAt)}</span>
    ) : null

  return (
    <div data-testid="build-progress" className="space-y-2">
      {working ? (
        <>
          {line && (
            <div className="flex items-center gap-2 text-xs text-tertiary">
              {spinner}
              <span className="font-medium">{line}</span>
              {elapsed}
            </div>
          )}
          {currentStepRow}
        </>
      ) : (
        stepList && (
          // The ONLY place the full step history renders — collapsed by default, an
          // operator opts in after the fact rather than watching it accumulate live.
          <Collapsible open={stepsOpen} onOpenChange={setStepsOpen} className="space-y-1">
            <CollapsibleTrigger className="flex w-full items-center gap-2 text-xs text-tertiary">
              <span className="min-w-0 truncate font-medium">Build steps</span>
              <span className="flex-shrink-0 text-neutral/70">
                · {visibleSteps.length} step{visibleSteps.length === 1 ? '' : 's'}
              </span>
              <ChevronDown
                size={13}
                aria-hidden="true"
                className={cn(
                  'ml-auto flex-shrink-0 text-neutral/50',
                  !reduced && 'transition-transform',
                  stepsOpen && 'rotate-180',
                )}
              />
            </CollapsibleTrigger>
            <CollapsibleContent>{stepList}</CollapsibleContent>
          </Collapsible>
        )
      )}

      {alerts.map((env) => {
        if (env.type === 'error') {
          if (env.recovering) {
            // A self-heal in progress, not a failure. The detail renders ONCE — the
            // word-boundary-sliced title, no <pre> — so the old double render (prose +
            // monospace, both clipped mid-word) cannot come back through the stack copy.
            // At a terminal this envelope renders nothing: the outcome message owns the
            // ending, and exactly one failure presentation may be visible there.
            if (!working) return null
            return (
              <div
                key={env.seq}
                className="rounded-lg border border-bial-border bg-surface-muted px-2.5 py-2"
                data-kind="retry"
                data-source={env.source}
              >
                <div className="flex items-center gap-1.5 text-xs font-medium text-tertiary">
                  <RotateCw size={13} className="flex-shrink-0" />
                  <span>That didn’t work — trying another way</span>
                </div>
                {env.title && (
                  <p className="mt-1 text-[11px] leading-relaxed text-neutral">{env.title}</p>
                )}
              </div>
            )
          }
          return (
            <div
              key={env.seq}
              className="rounded-lg border border-danger/20 bg-danger/5 px-2.5 py-2"
              data-kind="error"
              data-source={env.source}
            >
              <div className="flex items-center gap-1.5 text-xs font-semibold text-danger">
                <XCircle size={13} className="flex-shrink-0" />
                <span>{env.title}</span>
              </div>
              {env.cleaned_stack && (
                <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-all text-[11px] leading-relaxed text-danger/80">
                  {env.cleaned_stack}
                </pre>
              )}
            </div>
          )
        }
        if (env.type === 'escalation') {
          return (
            <div
              key={env.seq}
              className="rounded-lg border border-warning/30 bg-warning/10 px-2.5 py-2"
              data-kind="escalation"
              data-reason={env.reason}
            >
              <div className="flex items-center gap-1.5 text-xs font-semibold text-tertiary">
                <AlertTriangle size={13} className="flex-shrink-0 text-warning" />
                <span>{env.detail || env.reason}</span>
              </div>
              {env.last_error && (
                <p className="mt-1 text-[11px] text-neutral">{env.last_error.title}</p>
              )}
            </div>
          )
        }
        return (
          <div
            key={env.seq}
            className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 px-2.5 py-2 text-xs text-tertiary"
            data-kind="quota_exceeded"
          >
            <Clock size={13} className="mt-0.5 flex-shrink-0 text-warning" />
            <span>{formatDailyLimitMessage(env.limit, env.used)}</span>
          </div>
        )
      })}

      {logs.length > 0 && (
        // The raw output stays available, never ambient: server-redacted log lines live
        // behind this expander only — the chat shows zero raw shell lines otherwise.
        <details className="text-[11px]">
          <summary className="cursor-pointer select-none text-neutral/70 hover:text-tertiary">
            Details
          </summary>
          <div className="mt-1 max-h-48 space-y-0.5 overflow-auto rounded-lg bg-tertiary/5 p-2 font-mono">
            {logs.map((env) => (
              <div key={env.seq} className="flex items-start gap-2" data-kind="log" data-stream={env.stream}>
                <span className="flex-shrink-0 select-none text-neutral/50">{env.source}</span>
                <span
                  className={`whitespace-pre-wrap break-all ${env.stream === 'stderr' ? 'text-danger/90' : 'text-neutral'}`}
                >
                  {env.text}
                </span>
              </div>
            ))}
          </div>
        </details>
      )}

      {active && !confirmingForceEnd && (
        <div className="flex items-center gap-2 pt-0.5">
          <button
            type="button"
            onClick={onStop}
            disabled={stopping}
            className="inline-flex items-center gap-1.5 rounded-lg border border-bial-border bg-white px-2.5 py-1 text-[11px] font-semibold text-tertiary transition hover:border-primary hover:text-primary disabled:opacity-50"
          >
            {stopping ? <Loader2 size={11} className="animate-spin" /> : <Square size={11} />}
            {stopping ? 'Stopping…' : 'Stop'}
          </button>
          {onForceEnd && (
            <button
              type="button"
              onClick={() => setConfirmingForceEnd(true)}
              className="inline-flex items-center gap-1.5 rounded-lg border border-danger/30 bg-white px-2.5 py-1 text-[11px] font-semibold text-danger transition hover:bg-danger/5"
            >
              <Ban size={11} /> Force-end
            </button>
          )}
        </div>
      )}

      {active && confirmingForceEnd && onForceEnd && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-danger/30 bg-danger/5 px-2.5 py-1.5">
          <span className="text-[11px] text-danger">
            Force-end this build? It kills in-progress work
            {startedAt !== null ? ` (running ${formatElapsed(startedAt)})` : ''}.
          </span>
          <button
            type="button"
            onClick={() => {
              setConfirmingForceEnd(false)
              onForceEnd()
            }}
            className="rounded-md bg-danger px-2 py-1 text-[11px] font-semibold text-white transition hover:bg-danger/90"
          >
            Force-end
          </button>
          <button
            type="button"
            onClick={() => setConfirmingForceEnd(false)}
            className="rounded-md border border-bial-border px-2 py-1 text-[11px] font-semibold text-neutral transition hover:text-tertiary"
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  )
}
