/**
 * The chat-native build narrative (U15): ONE assistant bubble that carries the whole
 * live build — friendly steps, a working indicator with elapsed-time reassurance, and the
 * Stop / Force-end controls. It replaces the cockpit's ActivityFeed pane and SessionControls
 * row: the right pane frames only the app, and the build can never look "dead" — this bubble
 * is visibly alive for exactly as long as the session is.
 *
 * NOTHING IN THIS BUBBLE IS ADDRESSED TO A DEVELOPER (U16). The raw-output expander is gone —
 * it was the last place a citizen could open and find shell lines — and an error status renders
 * the platform's product sentence plus a next action, never the compiler's own title and never
 * a <pre> stack. The developer detail still exists and still travels; it goes to the agent,
 * which is the party that can act on it.
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
import { useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import type {
  BuildSessionStatus,
  ErrorEvent,
  EscalationEvent,
  FeedEnvelope,
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

/**
 * THE COMMITTED FALLBACK (U16), for a failure with no product-language equivalent.
 *
 * Named constants because every copy decision in this codebase is one — `LivePreview.tsx` sets
 * the convention with `FRAMING_TEXT` / `SLOW_TEXT` / `GONE_TITLE` — and it lives HERE, in the
 * feed's own module, rather than being imported across from the preview pane: the two surfaces
 * answer different questions and should be free to move apart.
 *
 * THE ACTION HALF IS NOT DECORATION. Deleting a stack trace and leaving a bare apology trades a
 * dead end the reader cannot act on for a quieter one; a rendered error status without a next
 * step is still a rendered error status the reader cannot act on. Both halves render, always —
 * for the legacy C7 feed, which carries neither field, and for any error class the server has
 * no sentence for. The server's own last-resort copy is word-for-word identical, so which side
 * supplied it is invisible to the reader.
 */
export const ERROR_FALLBACK_MESSAGE = 'We hit a problem finishing that change.'
export const ERROR_FALLBACK_ACTION =
  'Try describing what you want again, or ask for something simpler.'

/** The citizen-facing pair for one error status — the server's when it sent one, the committed
 *  fallback when it did not. Never returns an empty half. */
function userFacingError(env: ErrorEvent): { message: string; action: string } {
  return {
    message: env.user_message || ERROR_FALLBACK_MESSAGE,
    action: env.user_action || ERROR_FALLBACK_ACTION,
  }
}

/**
 * THE HARNESS'S OWN TURN-START ROW (U17), told apart from the agent's real steps by a reserved
 * tool name it emits under (`ACK_TOOL` in `services/turns/engine.py` — keep the two in step).
 *
 * It needs a name at all because it is the one step envelope that must never be treated as
 * work: it is REPLACED by the first real step rather than listed beside it, it never reaches
 * the finished build's step history, and on its own it is not a build narrative — an
 * acknowledgement is not a reason to draw an empty bubble around a chat turn that has nothing
 * else to say.
 */
export const ACK_STEP_NAME = '__ack__'

/**
 * THE ANNOUNCEMENT CADENCE (U17) — how often the live region below is allowed to speak.
 *
 * A CORRECTNESS DETAIL, NOT POLISH. The region is `aria-atomic`, so any change to it
 * re-announces the WHOLE line; a long-operation status line that is "refreshed until it
 * completes" would otherwise be a screen reader repeating the same sentence every few seconds
 * for the length of an npm install. Two rules, and the second is what makes the first enough:
 * announce only when the text actually CHANGES (an identical refresh mutates no DOM, so it is
 * silent by construction), and at most once per this interval — a burst of steps arriving
 * faster than anyone can listen coalesces to its newest line rather than queueing six.
 *
 * Trailing-edge, never dropping: a change held back by the window is announced when the window
 * elapses, so the last thing that happened is always what gets said.
 */
export const ANNOUNCE_MIN_INTERVAL_MS = 10_000

/** The neutral live-row copy for "the agent is busy with something you were not shown". */
const WORKING_PLACEHOLDER = 'Working…'

/**
 * The text the live region is currently allowed to be announcing — `text`, rate-limited to one
 * change per `ANNOUNCE_MIN_INTERVAL_MS`.
 *
 * The visible row is deliberately NOT throttled with it: a failed step has to appear the
 * instant it arrives (the live row is the fastest signal something went wrong), and only the
 * SPEAKING of it is worth pacing.
 */
function useAnnouncement(text: string): string {
  const [announced, setAnnounced] = useState(text)
  // 0, not `Date.now()`: content present when a live region is inserted is not announced, so
  // the first real change after mount has no earlier announcement to be spaced away from and
  // must not be delayed behind a window that never spoke.
  const lastAnnouncedAt = useRef(0)
  const pending = useRef(text)
  useEffect(() => {
    pending.current = text
    if (text === announced) return undefined
    const wait = ANNOUNCE_MIN_INTERVAL_MS - (Date.now() - lastAnnouncedAt.current)
    if (wait <= 0) {
      lastAnnouncedAt.current = Date.now()
      setAnnounced(text)
      return undefined
    }
    // Re-armed on every further change while the window runs, and always against the SAME
    // absolute deadline (`wait` is recomputed from the last announcement, not from now), so a
    // stream of changes cannot push the announcement out indefinitely.
    const timer = setTimeout(() => {
      lastAnnouncedAt.current = Date.now()
      setAnnounced(pending.current)
    }, wait)
    return () => clearTimeout(timer)
  }, [text, announced])
  return announced
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
  // U17 — the acknowledgement is explicitly NOT a narrative. It rides inside a bubble that
  // exists for some other reason (a headline, a step, an alert); counting it here would draw
  // the build bubble around every Ask turn in the product and leave an empty grey wrapper
  // behind each answer, since the row itself never survives into the step history.
  return envelopes.some(
    (env) =>
      (env.type === 'step' && !env.hidden && env.name !== ACK_STEP_NAME) ||
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
  // `log` envelopes are deliberately NOT read here any more (U16). The Details expander that
  // rendered them was the last surface in the chat where a citizen could open a disclosure and
  // find raw, redacted-but-still-raw shell output — a developer surface behind one click. The
  // lines are still produced and still relayed; nothing in this bubble renders them.
  const alerts = rows.filter(
    (env): env is AlertEnvelope =>
      env.type === 'error' || env.type === 'escalation' || env.type === 'quota_exceeded',
  )
  const line = headline(status)
  // U17 — the harness's acknowledgement is not one of the agent's steps. It NEVER reaches the
  // finished build's history (a transcript that accumulated one "Getting started on that…" per
  // turn is the thing it must not become) and it holds the live row only until there is real
  // work to show, which is what "replaced by the first real step" means concretely.
  const realSteps = visibleSteps.filter((env) => env.name !== ACK_STEP_NAME)
  const liveSteps = realSteps.length > 0 ? realSteps : visibleSteps

  // The LIVE view is ONE row, in one fixed spot: the most recent step replaces the
  // previous one in place rather than the transcript growing a line per step.
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
  const lastResolvedIndex = liveSteps.reduce(
    (idx, s, i) => (s.state === 'ok' || s.state === 'failed' ? i : idx),
    -1,
  )
  const inFlightStep =
    [...liveSteps.slice(lastResolvedIndex + 1)].reverse().find((s) => s.state === 'started') ?? null
  const lastStep = liveSteps.length > 0 ? liveSteps[liveSteps.length - 1] : null
  const currentStep = inFlightStep ?? lastStep
  // Once every visible step has resolved OK but the session is still active (the agent
  // continues into hidden work, or is simply between calls), a resolved tick is never
  // presented as current — this degrades to a neutral "Working…" instead, matching the
  // spinner already running in the headline above. A FAILED last step is the one
  // exception: it stays visible immediately rather than being masked behind a neutral
  // placeholder — the live row is the fastest signal something went wrong, and finding
  // 7's fail-open collapse below exists for exactly this "never hide a failure" reason.
  const showWorkingPlaceholder =
    inFlightStep === null && lastStep !== null && lastStep.state !== 'failed'
  const activityLabel = currentStep
    ? showWorkingPlaceholder
      ? WORKING_PLACEHOLDER
      : currentStep.label || currentStep.name
    : ''
  // NOTHING TO ANNOUNCE ONCE THE SESSION IS TERMINAL, which is what silences the region there:
  // the outcome message owns the ending, and a live region still saying "Working…" under a
  // finished build is the same lie the spinner used to tell.
  //
  // Called BEFORE the early return below — a hook that runs on some renders and not others is
  // a hook-order crash, not a subtle bug.
  const announcement = useAnnouncement(working ? activityLabel : '')
  if (!hasBuildNarrative(status, envelopes)) return null

  const currentStepRow = currentStep ? (
    <div data-testid="build-activity">
      <ToolActivityLine
        label={activityLabel}
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
      <div className={currentStepRow || liveSteps.length > 0 ? 'min-h-[2.75rem]' : undefined}>
        {working ? (
          <div className="space-y-2">
            {/* WHAT THE BUILD SAYS OUT LOUD — one line, spoken politely, never stealing focus.
                `role="status" aria-atomic="true"` — NOT `role="log"`, which describes an
                APPENDING region where old entries persist; this node's one child is replaced in
                place, and `status` (with its implicit `aria-live="polite"`) is the role for a
                single live-updating value, `aria-atomic` making the whole line announce
                coherently rather than as a partial diff. Under a burst of steps arriving faster
                than a screen reader can speak them, only the newest one is ever announced —
                that's an accepted trade, not a bug: the newest step is the only authoritative
                one.

                U17 EXTENDS THAT, it does not undo it. The atomic region is now a dedicated
                MIRROR of the visible row rather than the visible row itself, because the two
                want different clocks: the row has to update at the speed of the build (a
                failure must appear the moment it lands), while the region — which re-announces
                its whole contents on every change — has to update at the speed of a person
                listening. A long-operation status line "refreshed until it completes" inside
                the visible node would be the same sentence read aloud every few seconds.
                `useAnnouncement` is the pacing; `aria-atomic` is still why the line is spoken
                whole. It lives on the live arm for the same reason the row does: at a terminal
                the outcome message owns the ending, and a region still saying "Working…" under
                a finished build is the spinner's old lie in another modality. */}
            <div
              role="status"
              aria-atomic="true"
              aria-label="Build activity"
              className="sr-only"
              data-testid="build-activity-announcement"
            >
              {announcement}
            </div>
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
          <StepHistoryCollapsible steps={realSteps.map((s) => ({ ...s, id: String(s.seq) }))} />
        )}
      </div>

      {alerts.map((env) => {
        if (env.type === 'error') {
          // ONE PAIR, BOTH ARMS. Whether a failure reads as a retry or as the terminal red
          // block changes the framing around it, never what the reader is told about their app
          // or what they can do next.
          const { message, action } = userFacingError(env)
          if (env.recovering) {
            // A self-heal in progress, not a failure. At a terminal this envelope renders
            // nothing: the outcome message owns the ending, and exactly one failure
            // presentation may be visible there.
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
                <p className="mt-1 text-[11px] leading-relaxed text-neutral" data-part="message">
                  {message}
                </p>
                <p className="mt-0.5 text-[11px] leading-relaxed text-neutral/80" data-part="action">
                  {action}
                </p>
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
                <span data-part="message">{message}</span>
              </div>
              {/* No <pre>, and no `title`. `cleaned_stack` is the de-noised compiler log and
                  `title` its first meaningful line — both are built for the repair run, both
                  still travel on the envelope, and neither is a thing to hand a citizen. What
                  replaces them is the one line that tells the reader what to do next. */}
              <p className="mt-1 text-[11px] leading-relaxed text-danger/80" data-part="action">
                {action}
              </p>
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
                <span data-part="message">{env.detail || env.reason}</span>
              </div>
              {/* `last_error.title` used to render here. It is the SAME compiler-authored line
                  the error arm above stopped showing, so leaving it on this row would have kept
                  the developer surface alive one branch over. An escalation is an error status
                  like any other, so it carries an action clause too. */}
              <p className="mt-1 text-[11px] leading-relaxed text-neutral" data-part="action">
                {ERROR_FALLBACK_ACTION}
              </p>
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
