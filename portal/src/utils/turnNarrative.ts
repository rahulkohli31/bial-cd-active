/**
 * Turn frames → the C7 envelope shape, plus what the surface asks ABOUT a turn (U5, Plan D U17).
 *
 * A build is a Write turn now, so its narrative arrives as `step` / `diagnostic` / `quota` turn
 * frames instead of C7 progress envelopes. ADAPTING rather than rewriting is deliberate: the two
 * transports must never tell different stories about what a build looks like, and one mapping is
 * how they agree by construction rather than by discipline.
 *
 * THE ENVELOPES NOW HAVE ONE READER, NOT TWO. `BuildProgress` — the pinned card that used to draw
 * them — is gone, and the transcript draws activity from the message parts themselves (Plan D's
 * `ActivityGroup`). What still needs the envelope shape is the legacy build-session feed and the
 * two questions the surface asks of a turn: what phase it is in (`turnPhase`, for the app pane)
 * and whether today's budget is spent (`atLimitSendState`, for the composer). Both moved here when
 * their old home was deleted, and both belong here for the same reason: envelopes are this
 * module's vocabulary, and neither the pane nor the composer should have to learn it.
 *
 * The mapping is small because the two vocabularies already describe the same thing. The one
 * place they genuinely differ is `diagnostic` → `error`: on the turn stream a diagnostic is
 * explicitly NOT a failure (a repair run follows), so its envelope carries `recovering: true`
 * and renders as a retry — collapsing it into the plain red `error` shape told the user their
 * build died four times on its way to succeeding.
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
  /** Structurally a `DiagnosticFrame`. The citizen-facing pair is part of the shape ON
   *  PURPOSE: this mapping is the seam a new field disappears at — everything not named
   *  below is dropped silently, with a green typecheck, because the target `ErrorEvent`
   *  fields are optional. Listing them here makes omitting them a compile error. */
  diagnostics: {
    source: string
    userMessage: string
    userAction: string
  }[]
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
      // Fail to `server` rather than drop: an unrecognized source still carries a sentence
      // the user needs to see, and a swallowed diagnostic is a silent build failure.
      source: (ERROR_SOURCES.has(diagnostic.source) ? diagnostic.source : 'server') as ErrorSource,
      // EMPTY, and deliberately. The target `ErrorEvent` is the LEGACY C7 feed's shape, which
      // still has these two fields because that transport still carries them; the turn stream
      // does not send them any more, so there is nothing to map. They are written explicitly
      // rather than omitted because the field list above is what makes a dropped field a
      // compile error, and that property is worth more than two blank strings cost.
      title: '',
      cleaned_stack: '',
      // THE HALF THE USER ACTUALLY READS.
      user_message: diagnostic.userMessage,
      user_action: diagnostic.userAction,
      // A diagnostic is a recovery in progress, never a terminal failure (the wire says so:
      // "the turn is not failing — a repair run follows").
      recovering: true,
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
 * The phase this turn is in, in the status vocabulary the app pane reads.
 *
 * `null` — nothing to say about the app, so the pane keeps whatever it already had. The ordering
 * below is the honest one: an unavailable workspace is terminal for this turn no matter what else
 * arrived, and a live preview outranks "still provisioning" because the user can SEE it.
 *
 * ══ `narrativeStatus`'s `isBuild` IS GONE, AND THE FRAMES ANSWER INSTEAD ══
 *
 * This used to be TOLD, by its caller, whether the turn was a build — because the surface knew the
 * chat's kind and the frames did not. There is ONE surface now and it consults no kind anywhere
 * (R72), so a parameter whose only honest source is "what sort of chat is this?" has no caller
 * left. It also arrived as the literal `true` at the one site that passed it, which made the
 * read-turn arm below unreachable in the shipped product.
 *
 * The frames already carry the distinction. A turn that WORKED ON THE APP emits steps, or a
 * preview, or a diagnostic about one; a turn that only answered a question attaches the same live
 * container (U5b) and emits nothing else. So the question is read off the narrative, and the two
 * arms are the same two arms as before — reached by evidence rather than by declaration.
 */
export function turnPhase(
  narrative: TurnNarrative,
  {
    running,
    terminal,
  }: {
    running: boolean
    terminal: 'completed' | 'failed' | 'stopped' | null
  }
): BuildSessionStatus | null {
  if (narrative.workspace === null) return null
  if (narrative.workspace.state === 'unavailable') return 'failed'
  // DID THIS TURN TOUCH THE APP? Generous on purpose — every one of these is a frame only a turn
  // doing app work emits, and under-reading it would leave the pane uncovered over a real build,
  // which is the louder wrong of the two. A question about a heading produces none of them.
  const touchedTheApp =
    Object.keys(narrative.steps).length > 0 ||
    narrative.diagnostics.length > 0 ||
    narrative.preview.url !== null ||
    narrative.preview.state !== null
  if (!touchedTheApp) {
    // A read turn has exactly one thing worth narrating: the 30-60s wait for its container,
    // while it is still happening. Everything after it belongs to the answer.
    return running && narrative.workspace.state === 'preparing' ? 'provisioning' : null
  }
  if (terminal === 'failed' || terminal === 'stopped') return 'failed'
  if (terminal === 'completed') return 'ended'
  if (!running) return null
  if (narrative.preview.state === 'ready') return 'ready'
  return narrative.workspace.state === 'preparing' ? 'provisioning' : 'building'
}

/** What the composer's SEND control does while the citizen is out of budget. */
export interface AtLimitSendState {
  /** Always true — the value exists so the call site reads as what it sets, not as a bare flag. */
  disabled: true
  /** The `title`, naming when sending starts working again. A control that will not act and does
   *  not say why is the single most frustrating state a UI can be in: it looks broken, and the
   *  reader has no way to tell whether waiting would help. */
  title: string
}

/**
 * The SEND control's state while today's budget is spent — `null` when it is not.
 *
 * RE-HOMED FROM `BuildProgress.tsx`, which U17 deleted. It came HERE rather than to the composer
 * because it reads FEED ENVELOPES, which is this module's vocabulary and nothing a composer should
 * have to know about: the surface asks the question and hands the composer a finished sentence.
 *
 * THE COMPOSER ITSELF STAYS ENABLED, and that is the whole reason this describes the send control
 * rather than the composer. A citizen who is refused mid-thought has usually just typed something
 * they want to keep; disabling the textarea takes their draft hostage until midnight, and (KTD-3)
 * `disabled` on a focused element blurs it to `document.body`, dropping keyboard focus out of the
 * page entirely. They can still select, copy and paste their draft somewhere safe — they simply
 * cannot spend budget they do not have.
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
 * reached a renderer in the existing tests — `new Date('x').toLocaleTimeString()` renders the
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
