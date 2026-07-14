/**
 * The build activity feed: renders the C7 progress-envelope stream as a legible,
 * ordered list — the operator's window into the brain's build loop.
 *
 * Its input is the NARROWED 6-member `FeedEnvelope` subset (step | log | error |
 * escalation | quota_exceeded | ended). `preview_ready` is DELIBERATELY excluded:
 * the owning hook (U4) routes it to preview status only (the iframe reload), never to
 * a feed row (C7 §3.4). The `switch` + `assertNever` is therefore over SIX members —
 * an `assertNever` over the full 7-member union would (correctly) fail to compile
 * without a `preview_ready` arm.
 *
 * The rows are pushed into visible React state UP FRONT by the owning hook and passed
 * here as a prop — the feed never depends on a remount to render
 * ([[builder-no-code-reply-invisible-in-chat-2026-06-25]]). Envelope fields are
 * snake_case (C7) and consumed directly (no camel alias, KTD-5).
 */
import { AlertTriangle, Ban, CheckCircle2, CircleDashed, Clock, Loader2, XCircle } from 'lucide-react'
import { assertNever } from '../utils/assertNever'
import { formatDailyLimitMessage } from '../utils/buildSessionTypes'
import type { EndedEvent, FeedEnvelope } from '../utils/buildSessionTypes'

export interface ActivityFeedProps {
  envelopes: FeedEnvelope[]
}

/**
 * Dedup by `seq` (last-wins) and order by `seq`. This is the precise property C3 §4.2
 * relies on: a replayed / duplicate `seq` renders exactly ONE row, and rows show in
 * `seq` order regardless of arrival order. (The owning hook also upserts by `seq`;
 * doing it here too keeps the feed correct for any input.)
 */
function bySeq(envelopes: FeedEnvelope[]): FeedEnvelope[] {
  const latest = new Map<number, FeedEnvelope>()
  for (const env of envelopes) latest.set(env.seq, env)
  return [...latest.values()].sort((a, b) => a.seq - b.seq)
}

/** The terminal chip's label + tone, keyed by the `ended.reason` display string (C7 §3.7). */
const ENDED_LABELS: Record<string, string> = {
  completed: 'Build completed',
  stopped_by_user: 'Stopped by you',
  idle_teardown: 'Idle — session torn down',
  quota_exceeded: 'Daily limit reached',
  escalated: 'Build escalated',
  build_failed: 'Build failed',
}

function endedTone(ended: EndedEvent): string {
  if (ended.status === 'failed') return 'bg-danger/10 text-danger border-danger/20'
  if (ended.reason === 'quota_exceeded') return 'bg-warning/10 text-tertiary border-warning/30'
  return 'bg-green-500/10 text-green-700 border-green-500/20'
}

function StepIcon({ state }: { state: 'started' | 'ok' | 'failed' }) {
  if (state === 'ok') return <CheckCircle2 size={14} className="text-green-600 flex-shrink-0" />
  if (state === 'failed') return <XCircle size={14} className="text-danger flex-shrink-0" />
  return <Loader2 size={14} className="text-primary flex-shrink-0 animate-spin" />
}

/**
 * One row per envelope, dispatched by `type`. Total over the 6 feed members —
 * `default: assertNever` makes a new C7 member a compile error here.
 */
function FeedRow({ env }: { env: FeedEnvelope }) {
  switch (env.type) {
    case 'step':
      return (
        <li className="flex items-center gap-2 text-xs text-tertiary" data-kind="step" data-state={env.state}>
          <StepIcon state={env.state} />
          <span className={env.state === 'failed' ? 'text-danger' : ''}>{env.label || env.name}</span>
        </li>
      )
    case 'log':
      return (
        <li className="flex items-start gap-2 text-[11px] font-mono leading-relaxed" data-kind="log" data-stream={env.stream}>
          <span className="flex-shrink-0 select-none text-neutral/50">{env.source}</span>
          <span className={`whitespace-pre-wrap break-all ${env.stream === 'stderr' ? 'text-danger/90' : 'text-neutral'}`}>{env.text}</span>
        </li>
      )
    case 'error':
      return (
        <li className="rounded-lg border border-danger/20 bg-danger/5 px-2.5 py-2" data-kind="error" data-source={env.source}>
          <div className="flex items-center gap-1.5 text-xs font-semibold text-danger">
            <XCircle size={13} className="flex-shrink-0" />
            <span>{env.title}</span>
            <span className="ml-auto text-[10px] font-medium uppercase tracking-wide text-danger/60">{env.source}</span>
          </div>
          {env.cleaned_stack && (
            <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all text-[11px] leading-relaxed text-danger/80">{env.cleaned_stack}</pre>
          )}
        </li>
      )
    case 'escalation':
      return (
        <li className="rounded-lg border border-warning/30 bg-warning/10 px-2.5 py-2" data-kind="escalation" data-reason={env.reason}>
          <div className="flex items-center gap-1.5 text-xs font-semibold text-tertiary">
            <AlertTriangle size={13} className="flex-shrink-0 text-warning" />
            <span>{env.detail || env.reason}</span>
          </div>
          {env.last_error && <p className="mt-1 text-[11px] text-neutral">{env.last_error.title}</p>}
        </li>
      )
    case 'quota_exceeded':
      return (
        <li className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 px-2.5 py-2 text-xs text-tertiary" data-kind="quota_exceeded">
          <Clock size={13} className="mt-0.5 flex-shrink-0 text-warning" />
          <span>{formatDailyLimitMessage(env.limit, env.used)}</span>
        </li>
      )
    case 'ended':
      return (
        <li className="flex items-center" data-kind="ended" data-reason={env.reason} data-status={env.status}>
          <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold ${endedTone(env)}`}>
            {env.status === 'failed' ? <Ban size={12} /> : <CheckCircle2 size={12} />}
            {ENDED_LABELS[env.reason] ?? `Session ended (${env.reason || env.status})`}
          </span>
        </li>
      )
    default:
      return assertNever(env)
  }
}

export default function ActivityFeed({ envelopes }: ActivityFeedProps) {
  const rows = bySeq(envelopes)

  return (
    // A polite live region so a screen-reader operator hears new build activity (a failure,
    // a quota hit) without polling the DOM — and it does NOT steal focus from the composer.
    <ol role="log" aria-live="polite" aria-label="Build activity" className="space-y-1.5 overflow-y-auto scrollbar-thin p-3">
      {rows.length === 0 ? (
        <li className="flex items-center gap-2 py-6 text-xs text-neutral/70">
          <CircleDashed size={14} className="animate-pulse" />
          Waiting for build activity…
        </li>
      ) : (
        rows.map((env) => <FeedRow key={env.seq} env={env} />)
      )}
    </ol>
  )
}
