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
  onForceEnd: () => void
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

export default function BuildProgress({
  envelopes,
  status,
  startedAt,
  stopping,
  onStop,
  onForceEnd,
}: BuildProgressProps) {
  const [confirmingForceEnd, setConfirmingForceEnd] = useState(false)
  const [stepsOpen, setStepsOpen] = useState(true)
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
  if (!line && visibleSteps.length === 0 && alerts.length === 0) return null

  // A polite live region so a screen-reader operator hears new build activity without polling the
  // DOM — and it does NOT steal focus from the composer. ONE renderer (`ToolActivityLine`) drives
  // both the live rows here and the reload rows in BuilderPage.
  const stepList =
    visibleSteps.length > 0 ? (
      <ol role="log" aria-live="polite" aria-label="Build activity" className="space-y-1">
        {visibleSteps.map((env) => (
          <li key={env.seq} data-kind="step" data-state={env.state}>
            <ToolActivityLine label={env.label || env.name} state={env.state} />
          </li>
        ))}
      </ol>
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
      {line && stepList ? (
        // Mode-B: the headline is the collapse trigger; the friendly steps are its content.
        <Collapsible open={stepsOpen} onOpenChange={setStepsOpen} className="space-y-1">
          <CollapsibleTrigger className="flex w-full items-center gap-2 text-xs text-tertiary">
            {spinner}
            <span className="min-w-0 truncate font-medium">{line}</span>
            {elapsed}
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
      ) : (
        <>
          {line && (
            <div className="flex items-center gap-2 text-xs text-tertiary">
              {spinner}
              <span className="font-medium">{line}</span>
              {elapsed}
            </div>
          )}
          {stepList}
        </>
      )}

      {alerts.map((env) => {
        if (env.type === 'error') {
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
          <button
            type="button"
            onClick={() => setConfirmingForceEnd(true)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-danger/30 bg-white px-2.5 py-1 text-[11px] font-semibold text-danger transition hover:bg-danger/5"
          >
            <Ban size={11} /> Force-end
          </button>
        </div>
      )}

      {active && confirmingForceEnd && (
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
