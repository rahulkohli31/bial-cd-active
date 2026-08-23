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
import type { ReactElement } from 'react'
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
import type { ToolActivityState } from './ToolActivityLine'

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
  /** U24 — the server's own at-limit sentence for this turn, when there is one.
   *
   *  IT COMES FROM THE SERVER RATHER THAN BEING WRITTEN HERE because two of its three facts are
   *  things only the server knows: whether a copy of the app was actually secured on the way out
   *  (the platform tries, and the try can fail), and which support address this deployment is
   *  configured with. A sentence assembled in the browser would have to guess at both, and the
   *  guess it would make — "your work is safe, contact your administrator" — is exactly the
   *  false-reassurance-plus-dead-end this unit exists to delete.
   *
   *  Absent (`null`/omitted) is the ordinary case for every surface that has not been wired to
   *  pass it; the row below falls back to the client-side quota copy rather than rendering
   *  nothing, so a missing prop degrades the message instead of losing it. */
  atLimitText?: string | null
}

/** An email address inside otherwise-plain prose. Deliberately CONSERVATIVE — it must not match
 *  a trailing full stop, or the `mailto:` would carry it into the mailbox name. */
const AN_EMAIL_ADDRESS = /[^\s<>@]+@[^\s<>@.]+(?:\.[^\s<>@.]+)+/g

/**
 * The at-limit sentence with its support address turned into a real `mailto:` link.
 *
 * THE ADDRESS ARRIVES AS TEXT, and that is a deliberate division of labour rather than an
 * oversight. The server owns the words — it renders the sentence into a plain-text banner slot
 * above the composer as well as into this row, and a `mailto:` URI spelled out mid-sentence is
 * precisely the register `services/turns/copy.py` exists to keep out. Making it clickable is a
 * rendering concern, so it happens where there is a DOM to click.
 *
 * Returns an array of React nodes, never a string of markup: the sentence is server copy today,
 * but a renderer that interprets its input as HTML is one configuration change away from being an
 * injection sink, and there is nothing here that needs the risk.
 */
export function withMailtoLinks(text: string): (string | ReactElement)[] {
  const out: (string | ReactElement)[] = []
  let cursor = 0
  // `matchAll` starts a fresh iteration each call — the regex is module-level and `g`-flagged, so
  // reusing `exec` across calls would carry `lastIndex` between renders and drop links at random.
  for (const match of text.matchAll(AN_EMAIL_ADDRESS)) {
    const at = match.index
    if (at > cursor) out.push(text.slice(cursor, at))
    out.push(
      <a
        key={`${at}-${match[0]}`}
        href={`mailto:${match[0]}`}
        className="font-semibold underline underline-offset-2"
      >
        {match[0]}
      </a>,
    )
    cursor = at + match[0].length
  }
  if (cursor < text.length) out.push(text.slice(cursor))
  return out
}

/** What the composer's SEND control does while the citizen is out of budget. */
export interface AtLimitSendState {
  /** Always true — the value exists so the call site reads as what it sets, not as a bare flag. */
  disabled: true
  /** The `title`, naming when sending starts working again. A disabled control with no
   *  explanation is the single most frustrating state a UI can be in: it looks broken, and the
   *  reader has no way to tell whether waiting would help. */
  title: string
}

/**
 * The SEND control's state while today's budget is spent — `null` when it is not.
 *
 * THE COMPOSER ITSELF STAYS ENABLED, and that is the whole reason this describes the send control
 * rather than the composer. A citizen who is refused mid-thought has usually just typed something
 * they want to keep; disabling the textarea takes their draft hostage until midnight, and (KTD-3)
 * `disabled` on a focused element blurs it to `document.body`, dropping keyboard focus out of the
 * page entirely. They can still select, copy, and paste their draft somewhere safe — they simply
 * cannot spend budget they do not have.
 *
 * Exported for the composer to apply, in the same way `hasBuildNarrative` is exported for the
 * bubble's chrome: one expression, two readers, so the row below and the control above the
 * composer can never disagree about whether the citizen is at their limit.
 */
export function atLimitSendState(envelopes: FeedEnvelope[]): AtLimitSendState | null {
  // NEWEST WINS, by seq rather than by array order. A reconnect replays the stream and a resumed
  // subscriber receives frames out of order; picking the last ARRIVED envelope would hand back a
  // stale reset time from a replayed frame.
  const quota = bySeq(envelopes).filter(
    (env): env is QuotaExceededEvent => env.type === 'quota_exceeded',
  )
  const newest = quota.length > 0 ? quota[quota.length - 1] : null
  if (!newest) return null
  const when = formatResetTime(newest.resets_at)
  return {
    disabled: true,
    title: when ? `You can send again after ${when}` : 'You can send again after midnight',
  }
}

/**
 * `resets_at` as a time a person can read, or `null` when it is not a usable instant.
 *
 * FALLS BACK RATHER THAN THROWING. The field is a wire value, and an unparseable one has already
 * reached this component in the existing tests — `new Date('x').toLocaleTimeString()` renders the
 * literal string "Invalid Date" into the citizen's banner, which is worse than saying nothing
 * specific at all. The caller's fallback ("after midnight") is true regardless of the wire value,
 * because the reset IS the next IST midnight.
 */
export function formatResetTime(isoUtc: string): string | null {
  const at = new Date(isoUtc)
  if (Number.isNaN(at.getTime())) return null
  return at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
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

/** The minimal shape `StepHistoryCollapsible` needs — satisfied structurally by both
 *  the live `StepEvent` (state: 'started'|'ok'|'failed') and BuilderPage's persisted
 *  `StepItem` (state: 'ok'|'failed'|'pending'), so one component renders both. */
export interface StepHistoryItem {
  /** Unique per rendered item — NOT the persisted row's `seq`, which one DB row can share
   *  across multiple reload-grouped `StepItem`s. */
  id: string
  seq: number
  label: string
  name?: string
  state: ToolActivityState
}

export interface StepHistoryCollapsibleProps {
  steps: StepHistoryItem[]
}

/**
 * The full step history behind a collapsed dropdown — the ONLY place it renders, ever
 * (never an ambient/live list). Fail-open: defaults EXPANDED when any step failed, so a
 * failure is never hidden behind an unopened trigger the way an all-success run is meant
 * to stay tucked away.
 *
 * Shared by BuildProgress's own post-build presentation and BuilderPage's reload path
 * (which groups consecutive persisted `step` messages through this same component) —
 * ONE renderer, so a finished build reads as one collapsed history whether you watched it
 * finish live or came back to a reloaded tab. Do not fork this back into two presentations.
 */
export function StepHistoryCollapsible({ steps }: StepHistoryCollapsibleProps) {
  const reduced = usePrefersReducedMotion()
  const failedCount = steps.filter((s) => s.state === 'failed').length
  const [stepsOpen, setStepsOpen] = useState(() => failedCount > 0)
  if (steps.length === 0) return null
  return (
    <Collapsible open={stepsOpen} onOpenChange={setStepsOpen} className="space-y-1">
      <CollapsibleTrigger className="flex w-full items-center gap-2 text-xs text-tertiary">
        {failedCount > 0 && <XCircle size={13} className="flex-shrink-0 text-danger" />}
        <span className="min-w-0 truncate font-medium">Build steps</span>
        <span className="flex-shrink-0 text-neutral/70">
          · {steps.length} step{steps.length === 1 ? '' : 's'}
          {failedCount > 0 && ` · ${failedCount} failed`}
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
      <CollapsibleContent>
        <ol aria-label="Build steps" className="space-y-1">
          {steps.map((s) => (
            <li key={s.id} data-kind="step" data-state={s.state}>
              <ToolActivityLine label={s.label || s.name || ''} state={s.state} />
            </li>
          ))}
        </ol>
      </CollapsibleContent>
    </Collapsible>
  )
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
  atLimitText,
}: BuildProgressProps) {
  const [confirmingForceEnd, setConfirmingForceEnd] = useState(false)
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

  // The LIVE view is ONE row, in one fixed spot: the most recent step replaces the
  // previous one in place rather than the transcript growing a line per step.
  // `role="status" aria-atomic="true"` — NOT `role="log"`, which describes an
  // APPENDING region where old entries persist; this node's one child is replaced
  // in place, and `status` (with its implicit `aria-live="polite"`) is the role for
  // a single live-updating value, `aria-atomic` making the whole line announce
  // coherently rather than as a partial diff. Under a burst of steps arriving
  // faster than a screen reader can speak them, only the newest one is ever
  // announced — that's an accepted trade, not a bug: the newest step is the only
  // authoritative one.
  //
  // Picked by WHAT'S ACTUALLY RUNNING, not by array/seq (= call) order — with one
  // deliberate exception: once anything AFTER a `started` step has resolved (ok/failed),
  // that step no longer counts as in-flight, even if it is, in fact, still a legitimately
  // slow step from a parallel batch. The envelope stream alone cannot tell that case apart
  // from a step permanently stuck at `started` (e.g. a snapshot/toolCallId key mismatch
  // upstream never resolving it) — and the orphan is the far worse failure, since it would
  // otherwise pin a stale step on the row forever and mask a genuinely newer `failed` step
  // behind it. A still-running parallel sibling degrades to the generic "Working…"
  // placeholder instead of losing the failure-masking guarantee.
  const lastResolvedIndex = visibleSteps.reduce(
    (idx, s, i) => (s.state === 'ok' || s.state === 'failed' ? i : idx),
    -1,
  )
  const inFlightStep =
    [...visibleSteps.slice(lastResolvedIndex + 1)].reverse().find((s) => s.state === 'started') ?? null
  const lastStep = visibleSteps.length > 0 ? visibleSteps[visibleSteps.length - 1] : null
  const currentStep = inFlightStep ?? lastStep
  // Once every visible step has resolved OK but the session is still active (the agent
  // continues into hidden work, or is simply between calls), a resolved tick is never
  // presented as current — this degrades to a neutral "Working…" instead, matching the
  // spinner already running in the headline above. A FAILED last step is the one
  // exception: it stays visible immediately rather than being masked behind a neutral
  // placeholder — the live row is the fastest signal something went wrong, and finding
  // 7's fail-open collapse below exists for exactly this "never hide a failure" reason.
  const showWorkingPlaceholder = inFlightStep === null && lastStep !== null && lastStep.state !== 'failed'
  const currentStepRow = currentStep ? (
    <div role="status" aria-atomic="true" aria-label="Build activity">
      <ToolActivityLine
        label={showWorkingPlaceholder ? 'Working…' : currentStep.label || currentStep.name}
        state={showWorkingPlaceholder ? 'started' : currentStep.state}
      />
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
      {/* min-h holds roughly two rows (the live arm's headline + step) so the terminal flip to
          the one-row collapsed trigger doesn't visibly shrink the bubble in the same frame
          BuildOutcome appends below it — a cosmetic jump the reviewer flagged, not a functional
          one, but cheap to steady. */}
      <div className={currentStepRow || visibleSteps.length > 0 ? 'min-h-[2.75rem]' : undefined}>
        {working ? (
          <div className="space-y-2">
            {line && (
              <div className="flex items-center gap-2 text-xs text-tertiary">
                {spinner}
                <span className="font-medium">{line}</span>
                {elapsed}
              </div>
            )}
            {currentStepRow}
          </div>
        ) : (
          <StepHistoryCollapsible
            steps={visibleSteps.map((s) => ({ ...s, id: String(s.seq) }))}
          />
        )}
      </div>

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
        // THE AT-LIMIT ROW (U24). The server's sentence when there is one, the client-side quota
        // copy when there is not — never nothing. The two differ in more than wording: the
        // server's says whether the citizen's work was actually secured, which is a fact no
        // amount of client-side phrasing can supply, and it names a real support address instead
        // of a role nobody can write to.
        //
        // `title` carries the reset time onto the row for the same reason the send control gets
        // it: the one question a person has here is "when does this stop being true".
        return (
          <div
            key={env.seq}
            className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 px-2.5 py-2 text-xs text-tertiary"
            data-kind="quota_exceeded"
            data-resets-at={env.resets_at}
            title={atLimitSendState([env])?.title}
          >
            <Clock size={13} className="mt-0.5 flex-shrink-0 text-warning" />
            <span>
              {atLimitText
                ? withMailtoLinks(atLimitText)
                : formatDailyLimitMessage(env.limit, env.used)}
            </span>
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
