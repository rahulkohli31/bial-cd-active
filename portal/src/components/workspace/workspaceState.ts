/**
 * WHY THIS EXISTS: ONE WORKSPACE STATE, COMPUTED ONCE, RENDERED TWICE.
 *
 * It answers WHAT TO SAY: the sentence a person reads and the at-most-one thing they may press.
 * The app pane renders it; a Plan chat, which has no pane, renders the same value above its
 * composer — one author per workspace sentence, so "no pane" cannot come to mean "says nothing".
 *
 * IT DOES NOT ANSWER WHAT TO FRAME: there is no URL field to put one in, and that absence is the
 * enforcement. The address comes only from `utils/previewAddress.ts`, whose precedence — a live
 * turn's preview outranks the session URL — a `PreviewState` in hand here would silently drop.
 *
 * FOUR STATES A CITIZEN READS, PLUS ONE INTERNAL.
 *
 *   NEW       nothing to launch                             never-built
 *   BUILDING  something under way, nothing proven serving    starting
 *   RUNNING   proven serving                                 running
 *   SAVED     a saved copy, nothing serving it               not-running
 *
 * plus `could-not-read`, which is INTERNAL: it is what is left when a read decided nothing AND
 * nothing has ever been decided before. See {@link WorkspaceInputs.lastDecidedPreview}.
 *
 * THE ACTION UNION REACHES NOTHING DESTRUCTIVE UNASKED. Two members: start and retry. No restore,
 * rebuild, teardown or stop-somebody-else verb exists in the type, so a signal this client could
 * not interpret has nowhere destructive to land — not because a guard checks something first, but
 * because the union has no other verb. That closes the CLIENT half only. `POST /relaunch` is the
 * server's, so a client test asserting "this component made no restore call" passes in the very
 * state that loses work.
 *
 * THE COPY RULE: the pane says what IS, never what is not — "Your app is saved.", full stop, not
 * "saved but not running". `not running` survives only as an internal state name, never rendered,
 * and no sentence names a duration the platform has not measured.
 */
import type { PreviewLifeState, PreviewState, SurfacePresence } from '../../utils/buildSessionApi'
import { assertNever } from '../../utils/assertNever'

/**
 * THE BACKGROUND CADENCE and THE ANSWERS THAT END IT, moved here from `ConversationSurface.tsx`
 * so the two readers share one source rather than each keeping a private copy.
 *
 * The reason this is not left duplicated: the same file already records what happened the last
 * time a one-liner was copied instead of shared — two sites kept private `crypto.randomUUID()`
 * mints and both went on producing v4s long after the shared mint moved on. A cadence and a
 * terminal set that drift apart are worse than that, because the symptom is a poll that stops on
 * one surface and not on the other, with nothing red anywhere.
 */
export const PREVIEW_PROBE_MS = 45_000

/**
 * The answers that END the asking. All three are SETTLED FACTS about a workspace: nothing that
 * could change one of them happens without a reader hearing about it first. `unknown` is
 * deliberately absent — it is the one answer that decided nothing, so it must leave the timer
 * running rather than pin "we could not check" for the life of the tab.
 *
 * AND `restorable` BINDS THE SAME RULE. A settled `state` whose `restorable` is still `null` is
 * half an answer: the workspace is confirmed gone, but whether the work can be brought back was
 * not decided. Callers pair this set with a `restorable !== null` test; see `isTerminalReading`.
 */
export const SETTLED_GONE: ReadonlySet<PreviewLifeState> = new Set<PreviewLifeState>([
  'asleep',
  'slot_taken',
  'never_built',
])

/** Has the platform said everything it is going to say, so re-asking can only hear it again? */
export function isTerminalReading(preview: Pick<PreviewState, 'state' | 'restorable'>): boolean {
  return SETTLED_GONE.has(preview.state) && preview.restorable !== null
}

// ─── a read that decided something ────────────────────────────────────────────────────────────

/**
 * A READING THAT DECIDED SOMETHING — anything but `unknown`.
 *
 * "DECIDED" IS WEAKER THAN "SETTLED", AND THE TWO MUST NOT BE CONFUSED. {@link SETTLED_GONE} is
 * about a workspace that has finished changing, so re-asking it can only hear the same sentence
 * again. This is about the READ: the server answered with a state it was willing to stand behind.
 * `starting` and `alive` are decided and are the opposite of settled — their successors arrive
 * with no gesture from anybody.
 *
 * IT IS A TYPE RATHER THAN A CONVENTION because it is the input the map REMEMBERS across reads,
 * and a caller that fed an `unknown` into that slot would be storing "we could not check" as the
 * thing to fall back to when we cannot check — the exact circularity the fallback exists to break.
 * {@link asDecidedReading} is the only way to build one.
 */
export type DecidedPreview = PreviewState & { state: Exclude<PreviewLifeState, 'unknown'> }

/** The one narrowing, so no caller hand-rolls `state !== 'unknown'` and gets the polarity wrong. */
export function asDecidedReading(preview: PreviewState | null): DecidedPreview | null {
  return preview !== null && decidedSomething(preview) ? preview : null
}

const decidedSomething = (preview: PreviewState): preview is DecidedPreview =>
  preview.state !== 'unknown'

// ─── the cadence while a start is in flight ───────────────────────────────────────────────────

/**
 * THE ONE STATE WORTH ASKING ABOUT OFTEN, and the numbers that say how often and for how long.
 *
 * `starting` is the only reading whose successor arrives WITH NO GESTURE FROM ANYBODY — the
 * server holds the state, the container comes up, and the next read says `alive`. Every other
 * state changes because somebody did something, and the thing they did re-arms the poll on its
 * own. So a background cadence tuned for "has anything happened while nobody was looking" is the
 * wrong instrument for exactly one state, and using it there produced exactly the failure these
 * numbers exist to prevent: an app serving at t=2.7s, a pane still saying "Getting your app
 * ready." at t=45.5s, and nothing animating in between to suggest it was not simply hung.
 *
 * WHY 3 SECONDS. Chosen from the platform's own timings, because no start could be measured in
 * the session that wrote this (the Azure subscription was read-only) — and that is worth saying
 * plainly rather than dressing a guess as a measurement. Three anchors:
 *
 *  - `_ATTACHED_READY_BUDGET_SECONDS` is 15s server-side: a warm attach is expected to be serving
 *    inside it. An interval of 3s resolves such a start within a fifth of its own budget, so the
 *    lag the poll adds is small next to the event it is waiting for.
 *  - The one start measured directly had the flip at 2.7s. At 3s that start is caught on the
 *    first or second accelerated read; at 45s it was caught 42.8s late.
 *  - The read is cheap by contract — one cache read, at most two rows and two object-store HEADs,
 *    no container call — so 20 of them a minute — only while somebody is watching a start — is a
 *    real cost and a small one.
 *
 * WHAT WOULD HAVE SETTLED IT BETTER: the distribution of `starting`→`alive` on real starts, warm
 * attach and cold create+pull separately, with the interval set near the tenth percentile and the
 * window near the ninety-fifth. Anyone holding that data should change these two numbers and say
 * so here.
 *
 * WHY IT STOPS, AND WHY THE BOUND MOVED — see {@link STARTING_PROBE_LIMIT}.
 *
 * FALLING BACK IS NOT A VERDICT. The reading is left exactly as it was — still `starting`, still
 * "Getting your app ready." — and the background poll goes on correcting it if the app lands late.
 * Reading an elapsed budget as a statement about the container is the precise mistake that once
 * read a timeout as a death certificate and destroyed unsaved work.
 *
 * AND IT STOPS ON A CLOCK, NOT ON A TALLY OF ANSWERS WE LIKED. The bound is only a ceiling if
 * EVERY read spends from it — including the ones that came back with nothing.
 * `fetchPreviewState` throws on any non-2xx and on a dropped connection, and for as long as
 * only `nextProbeCadence` could advance the count, a workspace that reached `starting` and then hit
 * a 500, an expired session or a dead network was asked every three seconds FOR THE LIFE OF THE
 * TAB — twenty requests a minute, on both surfaces, with the bound that exists to prevent exactly
 * that never advancing a single step. {@link spendProbeCadence} is the other half, and both polls
 * call it from their `catch`.
 */
export const STARTING_PROBE_MS = 3_000

/**
 * 300 SECONDS OF ACCELERATED ASKING — the server's own outer bound on a start in flight.
 *
 * IT WAS 40 READS (120s), AND THAT NUMBER IS NOW WRONG BY CONSTRUCTION. 120s was
 * `_COLD_READY_BUDGET_SECONDS`, and that budget covers ONE LEG: the final `wait_ready` once the
 * container is already up. `manager.py` says so in as many words beside the number it records —
 * blob and app-DB provision, the bundle pull, the ACA create, the container's own startup and
 * `dev_start` all happen BEFORE the budget starts, so "the budget is not a ceiling on what gets
 * recorded". That mattered little while the wait on screen began at the final leg. It matters now:
 * BUILDING spans the WHOLE pre-serve interval, because `alive` is no longer allowed to mean
 * "scheduled". A build that first served past 120s would have fallen to the 45-second background
 * cadence at exactly the point it was most likely to land, leaving somebody sitting in front of a
 * finished app for up to 45 more seconds — the same defect this change exists to close, one door
 * down.
 *
 * 300s IS THE PLATFORM'S OWN NUMBER, NOT A LARGER GUESS. `STARTING_MARKER_TTL_SECONDS` is 300, and
 * its comment derives it the way this bound needs deriving: double the wait budget, plus margin
 * for the provisioning that runs before the wait even starts and for `_RESTORE_ATTEMPTS` paying
 * that setup twice. Past it the server itself stops claiming a start is in flight, so neither does
 * this timer.
 *
 * WHAT IT COSTS, STATED RATHER THAN BURIED: 100 cheap reads instead of 40, and only while somebody
 * is watching a start. It is also the ceiling a dark endpoint buys (see {@link spendProbeCadence})
 * — five minutes of 3-second polling against a broken server rather than two. That is the price of
 * the same ceiling covering the whole wait it is now a ceiling on.
 */
export const STARTING_PROBE_LIMIT = 100

/**
 * HOW OFTEN A HIDDEN SURFACE ASKS — and why it is not the same 45 seconds a visible one uses.
 *
 * A backgrounded tab cannot promise to come back on any cadence at all. Chrome throttles
 * background timers hard after a few minutes, Edge ships sleeping tabs on by default, and Safari
 * suspends them outright — so this number is a floor on how often we *try*, never a guarantee of
 * how often we succeed. The guarantee comes from the other two halves: the longer budget a hidden
 * renewal asks for, and the renewal that fires the moment the tab wakes.
 */
export const HIDDEN_PROBE_MS = 120_000

/**
 * Does THIS tick renew the container's lease, and on which budget?
 *
 * SHARED BY BOTH POLLS, deliberately, exactly like {@link nextProbeCadence}. There are two
 * surfaces that can frame a project — the project workspace and the chat route — and a surface
 * that frames an app WITHOUT renewing is a silent container-killer: the citizen is looking right
 * at their app while the platform counts it as abandoned. Putting this decision on one screen's
 * hook is precisely how the chat route would be missed, so it lives here where neither can drift
 * from the other and neither can forget it.
 *
 * `null` means this tick does not renew.
 *
 * NEVER ON AN ACCELERATED TICK. A container in `starting` is held by the start-in-flight marker
 * and the lock, not by a stay, so a renewal there writes a deadline onto a record that is not
 * being judged by it — the same reason the save read sits behind the same gate.
 */
export function presenceToRenew(
  accelerated: boolean,
  documentHidden: boolean,
): SurfacePresence | null {
  if (accelerated) return null
  return documentHidden ? 'hidden' : 'visible'
}

/**
 * The poll's cadence, and how much of the accelerated window it has spent.
 *
 * A pair rather than a bare number because the two are decided together and drift apart the moment
 * they are not: a delay with no count polls a hung start for the life of the tab, and a count with
 * no delay is a budget nothing spends.
 */
export interface ProbeCadence {
  /** Milliseconds until the next read. */
  readonly delayMs: number
  /** Accelerated reads scheduled so far in the current window. Zero means no window is open. */
  readonly fastReads: number
}

/** No window open, asking at the background cadence. Where every poll starts and returns to. */
export const BACKGROUND_CADENCE: ProbeCadence = { delayMs: PREVIEW_PROBE_MS, fastReads: 0 }

/**
 * THE CADENCE DECISION, MADE FROM THE ANSWER — never from a dependency list.
 *
 * Both polls read the workspace inside an effect whose deps are `[projectId, epoch]`, and both
 * blank their reading on every re-run so a stale verdict cannot be left under a frame that has
 * moved. Adding the preview state to either dep list would therefore re-run the effect on the very
 * transition this exists to catch, blanking the pane at the moment it should be holding still and
 * — on the chat surface — unframing an app that is running. So the reschedule happens HERE, inside
 * the read, on the `keepAsking`/`stopAsking` seam both effects already own.
 *
 * STRICTLY `starting`, and it reverts on anything else. A window that stayed open on `alive` would
 * put the whole product on a 3-second poll, which is the change nobody asked for.
 *
 * `unknown` NEITHER OPENS NOR CLOSES ONE, and that is worth stating precisely rather than as "an
 * unreadable read keeps the fast cadence", which is not what this does. It CONTINUES a window that
 * is already open, at 3 seconds — which is what a blip during a start needs, and is why the
 * unreadable arm is not a reason to slow down. It does NOT open one: a poll that has never seen
 * `starting` must not be accelerated by a broken server, so an `unknown` on a cold load is asked
 * again at the background cadence. And it still SPENDS from an open window, because the bound is
 * on reads made, not on answers liked: a server answering `unknown` forever must not buy an
 * unbounded fast poll.
 */
export function nextProbeCadence(answer: PreviewLifeState, held: ProbeCadence): ProbeCadence {
  if (answer !== 'starting' && answer !== 'unknown') return BACKGROUND_CADENCE
  if (answer === 'unknown' && held.fastReads === 0) return BACKGROUND_CADENCE
  return spendOpenWindow(held)
}

/**
 * SHOULD THIS READING ASK THE SERVER WHETHER THE APP HAS STOPPED?
 *
 * A dev server that dies after its last turn changes nothing in the registry, and `preview-state`
 * answers from the registry alone — so a stopped app reads as a wait that never ends. Either
 * `alive` with a frame that never vouches (the pane's slow card), or, once the reaper's probe has
 * retracted the serving proof, `starting` with nothing ever arriving. Neither has a control. The
 * workspace check can see the process, and when it finds the app stopped with its work provably
 * saved it puts the container away, so the next reading is the saved app with its start control.
 *
 * `alive` ONLY WITH A STALLED FRAME: a frame still loading, or one that has vouched, is an app the
 * citizen can see, and asking would spend a container call to hear "yes".
 *
 * `starting` ONLY ONCE THE ACCELERATED WINDOW IS SPENT (`held` is the cadence the earlier answers
 * decided). Inside it the wait is a start being watched, and the window's whole bargain is that
 * watching costs cheap reads and nothing else — a check there is a container call about a dev
 * server still booting, on the very read a Launch press triggers. Past {@link STARTING_PROBE_LIMIT}
 * the start has run longer than the bound any start is given, and nothing in the reading tells a
 * slow start from a dead one; the server's own guards (a live turn, a start in flight, work not
 * provably saved) are what keep a real one untouched.
 *
 * Each caller also skips accelerated ticks and running turns, for the reasons at its call site.
 */
export function mayHaveStopped(
  reading: PreviewLifeState,
  frameStalled: boolean,
  held: ProbeCadence,
): boolean {
  if (reading === 'alive') return frameStalled
  return reading === 'starting' && held.fastReads >= STARTING_PROBE_LIMIT
}

/**
 * A READ THAT NEVER PRODUCED AN ANSWER — a 500, a dropped connection, an expired session — and what
 * it costs the accelerated window.
 *
 * IT SPENDS, AND IT DECIDES NOTHING. THAT ASYMMETRY IS THE WHOLE RULE.
 *
 * SPENDS, because {@link STARTING_PROBE_LIMIT} is meant as a ceiling on how long anybody may be
 * polled at three seconds, and a budget only successful reads draw from is no ceiling at all: an
 * endpoint erroring from the first tick pinned both polls at 3s forever, which is the bug this
 * exists to close.
 *
 * DECIDES NOTHING, because a failed read is not evidence about the workspace. It cannot tell you
 * whether the container is still coming up, and ending the window on it — or worse, letting it
 * reclassify the reading — would be reading a failure to ask as an answer. That is the same
 * mistake that once read an elapsed readiness budget as a death certificate and destroyed unsaved
 * work. So three things it deliberately does NOT do: it does not open a window (a poll that has
 * never seen `starting` must not be accelerated by a broken server — `fastReads === 0` stays at
 * background), it does not close one early (the remaining fast reads are still owed to a start
 * that may yet land the moment the endpoint recovers), and it does not touch the reading, which
 * stays whatever the last real answer made it.
 *
 * The consequence, stated plainly: a start that goes dark is polled fast for the SAME 300 seconds a
 * start that keeps answering `starting` gets, and then both fall back to 45s with the pane still
 * saying a start is happening — because it still is, as far as anyone here knows.
 */
export function spendProbeCadence(held: ProbeCadence): ProbeCadence {
  if (held.fastReads === 0) return BACKGROUND_CADENCE
  return spendOpenWindow(held)
}

/**
 * One read off an OPEN window: fast until the bound, the background delay past it, and the count
 * never rewinds — so a window cannot be re-opened by spending from it. Whether a window is open at
 * all is the caller's question; this only draws from one.
 */
function spendOpenWindow(held: ProbeCadence): ProbeCadence {
  if (held.fastReads >= STARTING_PROBE_LIMIT) {
    return { delayMs: PREVIEW_PROBE_MS, fastReads: held.fastReads }
  }
  return { delayMs: STARTING_PROBE_MS, fastReads: held.fastReads + 1 }
}

// ─── what came back from a start attempt ──────────────────────────────────────────────────────

/**
 * How the most recent press of the start control ended — and only the endings that are this map's
 * business. A start that SUCCEEDED produces none of these: the read takes over and reports
 * `alive` on its own.
 *
 * NONE OF THE THREE IS A STATE. The READING decides which card is on screen, and an ending
 * contributes at most a `note` — the server's own words about a press the citizen made and is owed
 * an answer to. Two of the three have no such words and so change nothing a person sees; they are
 * kept because the producers still have to say how a press ended, and "it ended with nothing to
 * report" is a different fact from "no press has been made".
 */
export type StartOutcome =
  /** The server answered, and answered `ready: false` — the container is up and has not served a
   *  page yet. NOT a death: the wire's own contract records that an ABSENT `ready` reads `true`,
   *  which is exactly why liveness can never hang off this boolean. SAYS NOTHING ON SCREEN,
   *  because the state it describes is the one the citizen is already in: the registry's serving
   *  stamp is still empty, so the next read answers `starting` and the wait says so properly. */
  | { readonly kind: 'not-painted' }
  /** Nothing came back inside the budget. Says nothing about the container, and therefore nothing
   *  on screen either — a fact about a fetch is not a fact about a workspace, and the sentence it
   *  used to carry ("It may still be coming up.") was a guess the copy rule forbids. */
  | { readonly kind: 'timed-out' }
  /** The server named a reason. Carried verbatim — this map does not rewrite server prose. */
  | { readonly kind: 'failed'; readonly reason: string }

/**
 * HOW A START ATTEMPT ENDED, for the caller that has to decide what to do NEXT — distinct from
 * `StartOutcome`, which is what the pane SAYS about it.
 *
 * The rail's send is the caller that needs the difference: a project with nothing saved to bring
 * back is not a failed send — the first message is the very thing that provisions a workspace —
 * while a refusal must stop the address where the citizen is standing.
 */
export type StartResult =
  /** Nothing to re-say where the citizen is standing. The three ways of arriving here — the
   *  server answered, there was no saved build to restore, nobody could be asked — differ in the
   *  pane, which the sinks have already been told about, and not to this caller. */
  | { readonly kind: 'ok' }
  /** It did not start. `error` is what was thrown, for a caller that must re-say it where the
   *  citizen is standing rather than only in the pane. */
  | { readonly kind: 'failed'; readonly error: unknown }

// ─── what a person may press ──────────────────────────────────────────────────────────────────

/**
 * EXACTLY TWO VERBS EXIST, AND NEITHER REACHES ANYBODY ELSE'S APP. Adding a third is a deliberate
 * act at this declaration, visible in a diff, and every `switch` over it fails to compile until it
 * is handled. That is the whole mechanism behind "no unreadable signal can reach a destructive
 * verb from the client".
 */
export type WorkspaceAction =
  | { readonly kind: 'start'; readonly label: string }
  | { readonly kind: 'retry'; readonly label: string }

/** The person's word for the thing is their app. "Preview" is the developer's word. */
export const LAUNCH_LABEL = 'Launch Application'
const RETRY_LABEL = 'Try again'

const START: WorkspaceAction = { kind: 'start', label: LAUNCH_LABEL }
const RETRY: WorkspaceAction = { kind: 'retry', label: RETRY_LABEL }

// ─── the value both surfaces render ───────────────────────────────────────────────────────────

/**
 * INTERNAL NAMES, NEVER RENDERED. They exist so a test, a log line and a `switch` can talk about a
 * state without quoting its copy — and so the copy can be rewritten without a rename cascade.
 * `not-running` is the one to watch: it is a state name here and on the wire, and it is the exact
 * phrase the copy rule forbids on screen.
 *
 * FOUR ARE DRAWN AND ONE IS NOT. `could-not-read` is reachable only when a read decided nothing
 * AND nothing had ever been decided before it — see {@link WorkspaceInputs.lastDecidedPreview}. It
 * is kept in the union deliberately: the surfaces that special-case it — `AppPane`'s frame veto,
 * which leaves a standing frame alone, and the Plan chat's spoken set — are the reason a
 * coordination-store blip cannot pull a running app off somebody's screen.
 */
export type WorkspaceStateName =
  | 'never-built'
  | 'not-running'
  | 'starting'
  | 'running'
  | 'could-not-read'

export interface WorkspaceState {
  /** The internal name. Never rendered — see the type's own note. */
  readonly name: WorkspaceStateName
  /** The sentence a person reads. Always present: a state with nothing to say is not a state. */
  readonly headline: string
  /** The line under it, or `null` when the headline is the whole of it. */
  readonly detail: string | null
  /**
   * THE ONE THING A SURFACE MAY OFFER. `null` is a real answer — "nothing built" and "starting"
   * both offer none, and no arm offers two: a workspace has one remedy at a time.
   *
   * OPTIONAL MEMBERS BELOW ARE MANDATORY IN THE MAP, and the asymmetry is deliberate rather than a
   * softness. A dozen suites hand-build a `WorkspaceState` to stand a component up, and requiring
   * every one of them to restate a null they have no opinion about buys nothing: the totality that
   * matters is the MAP's, and `workspaceState.test.ts` pins its whole key set, so an arm that
   * forgets a field fails a test rather than passing a compile.
   */
  readonly action: WorkspaceAction | null
  /**
   * ONE EXTRA LINE — WHAT THE LAST PRESS ENDED AS.
   *
   * IT IS THE FIELD THE NEGATIVE-COPY SWEEP EXEMPTS, AND THAT IS WHY SERVER PROSE RIDES HERE
   * rather than in `detail`. The sweep forbids the pane describing this app by what it is not —
   * "saved", never "stopped", never "not running" — and it asserts over the headline, the detail
   * and both labels. A refusal sentence is somebody else's prose, carried verbatim because
   * rewriting it would put a second author on it and lose the only specific thing we know; put it
   * in `detail` and one refusal containing the words "not running" turns a green suite red on a
   * string this client does not control.
   *
   * `null` on every arm where no press has ended.
   */
  readonly note?: string | null
  /**
   * THE PLATFORM IS WORKING ON THIS RIGHT NOW — the wait's own flag.
   *
   * A wait has to say three things: what it is doing, that it IS doing it, and when it stops. The
   * first is the headline and the detail, which every state has. This is the second, and until it
   * existed the only state with a wait in it — `starting` — exposed nothing a reader could hear:
   * no `aria-busy` anywhere on the pane, and no action row to carry one, because `starting` offers
   * no action at all.
   *
   * IT IS A FACT ABOUT THE WORKSPACE, NOT A RENDERING DECISION, which is why it lives here beside
   * the sentence rather than being re-derived from `name === 'starting'` at each of the two
   * surfaces. A second surface deriving it is a second author for the same claim, and the moment a
   * second waiting state exists the two would disagree.
   *
   * TRUE ON EXACTLY ONE ARM. `could-not-read` is pointedly not busy — a read that failed is not
   * work in progress — and neither is an at-rest arm carrying a finished press's note.
   *
   * OPTIONAL IN THE TYPE, MANDATORY IN THE MAP, for the reason `action` states above: the suites
   * that hand-build a state must not have to restate a `false` they have no opinion about, and
   * `workspaceState.test.ts` pins the map's whole key set so an arm that forgets it goes red.
   * IT IS ALSO COMPARED BY {@link sameWorkspaceState} — a field this map can change and that
   * comparator cannot see is a pane that never re-renders, with nothing red anywhere.
   */
  readonly busy?: boolean
}

/**
 * TWO STATES THAT SAY THE SAME THING TO A READER. Every member is a primitive or a small union of
 * them, so this is exact — and it is what lets a poll that keeps returning the same answer stop
 * waking the surfaces rendering it.
 *
 * ONE COMPARISON PER FIELD, KEYED BY THE FIELD — so a new member of `WorkspaceState` that nobody
 * compares is a COMPILE error here, not a test failure somewhere else.
 *
 * This used to be an `&&` chain, and its own docblock admitted the hazard: an implementer who
 * added a field and forgot it "fails a test rather than passing a compile". That is exactly
 * backwards for the cell this guards — `sameReport` delegates to it and the report's subscriber
 * is the whole shell, so a field it does not compare is a field the pane never re-renders for.
 * The failure is silent, and it is one someone has to already suspect to go looking for.
 *
 * `Required<WorkspaceState>` is what does the work: mapping over it makes every key mandatory in
 * this record, including the ones that are optional in the state itself, so omitting an entry is
 * `TS2741` AT THE RECORD — where the person adding the field is standing.
 *
 * `?? null` / `?? false` on the optional members because an omitted field and an explicit null
 * are the same claim and must compare equal: otherwise a hand-built value and the map's own would
 * look like two different states to the cell.
 */
const STATE_FIELD_EQ: {
  [K in keyof Required<WorkspaceState>]: (a: WorkspaceState[K], b: WorkspaceState[K]) => boolean
} = {
  name: (a, b) => a === b,
  headline: (a, b) => a === b,
  detail: (a, b) => a === b,
  note: (a, b) => (a ?? null) === (b ?? null),
  busy: (a, b) => (a ?? false) === (b ?? false),
  action: (a, b) => sameAction(a, b),
}

/** The field names the comparator covers — the test's totality pin reads this rather than a
 *  second hand-kept list, so the two cannot drift apart. */
export const WORKSPACE_STATE_FIELDS = Object.keys(STATE_FIELD_EQ).sort()

/** One field, compared by its own entry. Generic in the KEY, which is what lets TypeScript
 *  correlate the three lookups — the comparator's parameter types and both operands are all
 *  `WorkspaceState[K]` for the same `K` — so no cast is needed to read them together. Written
 *  out rather than inlined for exactly that reason: inline, `key` widens to the union and the
 *  three types stop lining up. */
const fieldsAgree = <K extends keyof Required<WorkspaceState>>(
  key: K,
  a: WorkspaceState,
  b: WorkspaceState,
): boolean => STATE_FIELD_EQ[key](a[key], b[key])

export const sameWorkspaceState = (a: WorkspaceState, b: WorkspaceState): boolean =>
  a === b ||
  (Object.keys(STATE_FIELD_EQ) as Array<keyof Required<WorkspaceState>>).every((key) =>
    fieldsAgree(key, a, b),
  )

const sameAction = (a: WorkspaceAction | null, b: WorkspaceAction | null): boolean =>
  a === b || (a !== null && b !== null && a.kind === b.kind && a.label === b.label)

// ─── the inputs ───────────────────────────────────────────────────────────────────────────────

export interface WorkspaceInputs {
  /** The preview-state read as it arrived, `unknown` included. `null` before the first one lands. */
  readonly preview: PreviewState | null
  /**
   * THE LAST READ THAT DECIDED ANYTHING — what an unreadable read falls back to.
   *
   * WHY IT IS NOT DERIVABLE HERE. This map is a pure function of one reading, so "an unreadable
   * read never changes the pane" cannot be a rule it enforces on its own: it has no yesterday. The
   * callers have one — both polls already keep the previous reading and already refuse to let an
   * `unknown` overwrite it — so the memory is threaded in rather than invented, and there is still
   * exactly one place that decides what the memory MEANS.
   *
   * WHAT IT PREVENTS, CONCRETELY, because the alternative was to delete the unreadable arm
   * outright. Without it an `unknown` falls through to the at-rest arms, where `restorable` is
   * `null` (the object store was not consulted) and `projectHasSavedBuild` is still `null` on a
   * cold load — so a coordination-store blip printed "Describe what you want to build." over a
   * project whose app may be serving right now, with no action on the card at all. Rendering the
   * last decided reading instead means the pane simply does not move, which is the whole of the
   * rule.
   *
   * `null` ONLY WHEN NOTHING HAS EVER BEEN DECIDED, and only then does the map fall back to saying
   * so — see {@link resolveWorkspaceState}'s third step.
   */
  readonly lastDecidedPreview: DecidedPreview | null
  /**
   * The project row's own "is there anything to restore" — a cold-load answer that predates the
   * first read. Read with `??` against the read's fresher `restorable`, never `||`: `restorable`
   * is a TRI-STATE whose `null` means the object store could not be reached, which is not an
   * answer and must not retract a claim the project row already made.
   */
  readonly projectHasSavedBuild: boolean | null
  /** How the most recent start attempt ended, or `null` if none has been made or it succeeded. */
  readonly startOutcome: StartOutcome | null
  /**
   * A START IS IN FLIGHT RIGHT NOW, from this surface's own press.
   *
   * Without it the pane went on saying "Your app is saved." for up to a full poll cadence after
   * somebody pressed the button — true, but not an acknowledgement, and the only feedback was a
   * spinner inside the control. The server's own `starting` state is the honest answer and it
   * arrives on the next read; this is what covers the gap until it does, through the SAME arm, so
   * the sentence still has one author.
   */
  readonly startInFlight: boolean
}

// ─── the map ──────────────────────────────────────────────────────────────────────────────────

/**
 * A TOTAL FUNCTION over a closed input union. Five citizen arms, one internal, and every one of
 * them offers a verb from the four-member union or none.
 *
 * THE PRECEDENCE, and each step is a claim about which source is more current:
 *
 *  1. AN IN-FLIGHT PRESS outranks everything except a reading that already says `alive`. A press
 *     is newer than any of the readings below it — a stale `asleep`, an unreadable answer, a
 *     previous attempt's ending are all facts from before the button went down — and if the app is
 *     already serving then the start succeeded whatever it reported on the way, so saying "getting
 *     your app ready" over it would be the pane contradicting the frame beside it.
 *  2. WHICH READING IS BEING RENDERED AT ALL. The read that just landed, unless it decided nothing
 *     (`unknown`, or none has landed yet), in which case the last one that did. This is the whole
 *     of "an unreadable read never changes the pane": a standing frame stays framed and a standing
 *     card stays put, because the value the surfaces receive does not move.
 *  3. NOTHING HAS EVER BEEN DECIDED → "we could not check", with a retry. Not an empty pane, and
 *     not an invitation to build over an app that may be running: before the platform has said
 *     anything at all, the honest sentence is that we have not heard, and the retry is the only
 *     thing a person can usefully do with it.
 *  4. `alive` → RUNNING. It now means the platform watched the app answer a request rather than
 *     that a container was scheduled — which is why the arms that used to hedge against it are
 *     gone.
 *  5. `starting` → BUILDING. Something under way, nothing proven to be serving.
 *  6. `slot_taken` / `asleep` / `never_built` → SAVED or NEW, resolved against whether anything can
 *     be brought back. `slot_taken` shares that arm rather than having one of its own — see the
 *     case below for why the citizen's own other project holding the slot is not a negotiation.
 *
 * A START OUTCOME SELECTS NO ARM OF ITS OWN. It contributes a `note` — the server's words about a
 * press the citizen made — to whichever arm the READING chose.
 *
 * A SERVER STATE THIS CLIENT DOES NOT RECOGNISE never reaches here: `asPreviewLifeState` narrows it
 * to `unknown` at the wire, which resolves to the last decided reading — never to a confident
 * "gone". The `assertNever` at the bottom is what keeps that true when the union grows.
 */
export function resolveWorkspaceState(inputs: WorkspaceInputs): WorkspaceState {
  const { preview, lastDecidedPreview, projectHasSavedBuild, startOutcome, startInFlight } = inputs
  // WHAT THE LAST PRESS ENDED AS, in the server's own words — carried onto whichever arm the
  // reading selects rather than selecting one of its own. Computed once, here, so the two arms
  // that can carry it cannot come to disagree about what it says.
  const note = pressNote(startOutcome)
  // Step 2. `asDecidedReading` is the only narrowing in the file, so no arm below has to think
  // about `unknown` at all — and none of them can accidentally treat it as a verdict.
  const reading = asDecidedReading(preview) ?? lastDecidedPreview

  if (startInFlight && reading?.state !== 'alive') return gettingReady(note)

  if (reading === null) return couldNotRead()

  switch (reading.state) {
    case 'alive':
      return {
        name: 'running',
        headline: 'Your app is running.',
        // THE FRAME IS THE STATE. A sentence under a working app is noise, and a refusal left over
        // from a press that has since succeeded is worse than noise.
        detail: null,
        action: null,
        note: null,
        busy: false,
      }
    case 'starting':
      return gettingReady(note)
    // ANOTHER OF THIS CITIZEN'S PROJECTS HOLDS THE SLOT, AND THAT IS NOT A QUESTION FOR THEM.
    // Pressing start takes the workspace: the server starts the project that was asked for and
    // tears the outgoing one down behind it. So this reads exactly as a saved, stopped app does —
    // the same sentence and the same one control — and it names no other project, because a tab
    // whose container was taken by a switch made elsewhere has no cause to name.
    case 'slot_taken':
    case 'asleep':
    case 'never_built':
      return atRest(reading, projectHasSavedBuild, note)
    default:
      return assertNever(reading.state)
  }
}

/**
 * WHAT THE LAST PRESS ENDED AS, AS ONE LINE — or `null` when it left nothing worth saying.
 *
 * TWO OF THE THREE ENDINGS SAY NOTHING, AND THAT IS THE POINT rather than an omission.
 * `not-painted` is "the container is up and has not served a page yet", which is the DEFINITION of
 * the wait the citizen is already sitting in — the serving stamp is empty, so the next read
 * answers `starting` and the wait says it properly, with one author. `timed-out` is a fact about a
 * fetch that did not come back; it is not evidence about the container, and inventing a sentence
 * out of it is how a guess about a duration reached a screen in the first place.
 *
 * THE THIRD CARRIES SERVER PROSE VERBATIM. Rewriting it would put a second author on a sentence
 * that already has one and lose the only specific thing we know. See {@link WorkspaceState.note}
 * for why it rides in that field rather than in `detail`.
 */
function pressNote(outcome: StartOutcome | null): string | null {
  if (outcome === null) return null
  switch (outcome.kind) {
    case 'not-painted':
    case 'timed-out':
      return null
    case 'failed':
      return outcome.reason
    default:
      return assertNever(outcome)
  }
}

/**
 * AT REST, resolved against whether there is anything to bring back.
 *
 * `restorable ?? projectHasSavedBuild`, and the `??` is doing real work: `restorable`'s `null` is
 * "no claim" — the object store was unreachable, or the poll declined to spend a round trip — so
 * it falls through to the project row's older-but-real answer rather than retracting it.
 *
 * ONLY A DEFINITE `false` SUPPRESSES THE START CONTROL, and that is not a stylistic choice. The
 * server holds neither a recovery copy nor a saved bundle in that case, so `POST /relaunch`
 * answers 404 — offering "Launch Application" there is a button whose only outcome is an error.
 * What is left is the same affordance a project with nothing built has: ask for the app. So both
 * resolve to the SAME arm, which also keeps the pane from reporting an absence at somebody who
 * cannot act on it.
 *
 * BOTH ARMS CARRY THE NOTE, INCLUDING THE ONE WITH NO BUTTON, and the invitation arm is the one
 * that most needs it. The refusal a project with no saved copy actually gets is `no_saved_build`,
 * and that refusal produces exactly this reading — so an arm that dropped the note would answer a
 * press the citizen had just made with "Describe what you want to build." and no acknowledgement
 * that anything had happened at all.
 */
function atRest(
  preview: PreviewState,
  projectHasSavedBuild: boolean | null,
  note: string | null,
): WorkspaceState {
  const canRestore = preview.restorable ?? projectHasSavedBuild
  if (canRestore === true) {
    return {
      name: 'not-running',
      // VERBATIM AND CLIENT-APPROVED. Full stop after "saved". No negation follows it, and
      // the sentence beneath carries the rest without one.
      headline: 'Your app is saved.',
      detail: 'It stays running while you work, so you only do this once.',
      action: START,
      note,
      busy: false,
    }
  }
  return {
    name: 'never-built',
    // An invitation, not a report of an absence. "Nothing has been built here" is true and
    // useless; this is the sentence that tells a person what to do next.
    headline: 'Describe what you want to build.',
    detail: 'Your app will appear here as it takes shape.',
    action: null,
    note,
    busy: false,
  }
}

/**
 * THE NO-INVENTED-DURATIONS RULE, TAKEN LITERALLY: this says what is happening and names no number,
 * because nobody has measured one. The canvas's "about thirty seconds" and the register's "about
 * half a minute" are both dropped; a duration arrives from a measured constant or not at all.
 *
 * ONE SENTENCE FOR THE WHOLE PRE-SERVE INTERVAL, and it covers more of one than it used to. The
 * server's `starting`, this surface's own in-flight press, a relaunch that came back
 * `ready: false`, and a container that exists and has never answered a request are all the same
 * state — a start is happening, nothing is serving yet — and giving them one sentence is what
 * keeps them from drifting into four slightly different waits. Three of the four had cards of
 * their own until the platform could prove a serve.
 *
 * NO ESCAPE BUTTON, DELIBERATELY, AND IT IS NOT AN OVERSIGHT. The obvious kindness is a "Launch
 * Application" that appears after a long enough wait so the wait is never a dead end. It is not
 * offered, because of where that press would land: `relaunch_preview`'s cold arm tears the live
 * container down before restoring the last saved bundle, and the situation such a button exists
 * for — a start whose observer was lost — is exactly the situation that takes the cold arm. So the
 * button would be most dangerous at the precise moment it appeared. The escape is server-side
 * instead: a reconciler that un-sticks a stranded container with no gesture from the citizen,
 * which also reaches tabs that were loaded before it shipped and can destroy nothing.
 *
 * THE SECOND SENTENCE, AND THE CLAUSE IT SHIPS WITHOUT. The board draws this state as a still
 * glyph, a headline and a second sentence, and this arm used to carry only the first two — a
 * half-second-long headline standing alone over a wait that can run for minutes. The second
 * sentence says what the platform is actually doing, which is the difference between a wait a
 * person can sit through and a screen that looks hung.
 *
 * ITS DURATION CLAUSE IS STILL DROPPED, on the rule the docblock above states: the canvas pairs
 * this sentence with "about thirty seconds" and nothing in this tree has ever measured a cold
 * start. What replaces it is not a smaller guess but ELAPSED TIME, which `AppPane` counts from the
 * moment this state arrives — a fact rather than an estimate.
 *
 * AND NO PROGRESS BAR. A step-determinate one would advance on the workspace claim, the container
 * start and the first document served, but the wire carries a single opaque `starting`/`alive`
 * field, so a bar here could only be time-determinate, and a bar that sits at 80% for two minutes
 * is worse than the honest still card.
 */
function gettingReady(note: string | null): WorkspaceState {
  return {
    name: 'starting',
    headline: 'Getting your app ready.',
    detail: 'Setting up somewhere for it to run.',
    action: null,
    // WHY A WAIT MAY CARRY A REFUSAL. A press refused while a start really was in flight — the
    // server answering `BUILD_ALREADY_RUNNING` to somebody pressing Launch during a build — is a
    // question the citizen asked and is owed an answer to, and the honest answer does not change
    // the state they are in. It is a note rather than a second sentence for the same reason every
    // other piece of server prose is one: this map does not put words it did not write where the
    // negative-copy sweep asserts.
    note,
    // THE ONE ARM THAT IS BUSY. See `WorkspaceState.busy` — this is the state with a wait in it and
    // no action row, so before this field the pane had no way to say a wait was under way at all.
    busy: true,
  }
}

/**
 * THE ONE HONEST ANSWER TO A QUESTION NOBODY MANAGED TO ASK — and the only arm that is not drawn
 * for a state of the workspace.
 *
 * REACHED FROM ONE PLACE ONLY: a read that decided nothing, at a moment when nothing had ever been
 * decided. Once ANY reading has landed, an unreadable one renders THAT reading instead and this is
 * unreachable — see {@link WorkspaceInputs.lastDecidedPreview}. That narrowing is the whole reason
 * it survived the collapse while four other arms did not. "We could not check on your app." is an
 * engineer's sentence about the platform's own plumbing, and showing it to somebody whose app is
 * fine is the failure; showing it to somebody about whom we have genuinely never managed to learn
 * anything is simply the truth.
 *
 * Says nothing about the container, promises nothing about the work, and offers the only verb that
 * is safe against a signal we could not interpret.
 */
function couldNotRead(): WorkspaceState {
  return {
    name: 'could-not-read',
    headline: 'We could not check on your app.',
    detail: 'Nothing has changed while we were asking.',
    action: RETRY,
    note: null,
    busy: false,
  }
}
