/**
 * AGENT ACTIVITY, DRAWN BY THE PARTS (R30–R35c).
 *
 * ══ THE STRUCTURAL ARGUMENT, WHICH IS THE WHOLE POINT ══
 *
 * The pinned card this replaces was rendered BY THE TURN, so it appeared whether or not anything
 * ran — ask "who can see the visitor log?" and you got a progress bar. This is rendered BY THE
 * PARTS, so a turn with no tool-call parts renders literally no element.
 *
 * Two requirements therefore stop being rules anybody has to remember and become consequences:
 *   R35 (a turn that ran no tools shows nothing) is free — no parts, no group, no code.
 *   R32 (a group seals when the agent next speaks) is free — the primitive coalesces ADJACENT
 *        parts, so a group ends the moment a different part type appears. THERE IS NO SEAL LOGIC.
 *        A reviewer looking for it will not find it, which is why this paragraph exists.
 *
 * ══ WHY THIS IS NOT THE REGISTRY'S `tool-group` ══
 *
 * The registry's `tool-group.aui.tsx` was fetched and read, and porting it would have shipped
 * nothing usable: the variant we would pass, `ghost`, is LITERALLY THE EMPTY STRING, and its
 * `outline` default is a `rounded-lg border py-3` card whose proportions are not the board's. Every
 * visible property below is authored here against `ActivityAnatomy`, which is a whole artboard
 * about exactly this component.
 *
 * What the port would genuinely have brought is kept: `useScrollLock`, so expanding does not throw
 * the reader somewhere else, is imported from the library directly.
 *
 * ══ IT HAS A CONTAINER NOW, AND THAT IS THE BOARD'S (plan 002, U8) ══
 *
 * An earlier pass read `BuildChat` as drawing the group with no border and no background, and said
 * so at length here. `ActivityAnatomy` is the artboard that actually specifies it, and it draws a
 * bordered chip: `border:1px solid #E2E8F0; background:#FCFDFD; border-radius:10px`, opening into
 * a bordered panel with a header rule. A group with no chrome at all was bare text sitting in the
 * transcript with nothing to say it was a receipt rather than a sentence.
 *
 * ══ ALWAYS COLLAPSED — INCLUDING WHILE IT RUNS (owner ruling, 2026-09-02) ══
 *
 * This AMENDS the board in place. `ActivityAnatomy` panel 2 draws a live group open, with a label
 * naming the current step inside it; the owner does not want the working detail on screen. So a
 * running group is ONE COLLAPSED ROW — icons accumulating in it as steps complete — with a single
 * quiet line beneath it naming what is happening right now. Nothing expands on its own.
 *
 * PRESSING IT IS A GLANCE, NOT A NEW RESTING STATE. Opening shows the rows; pressing again closes
 * them; and a group opened WHILE RUNNING returns to collapsed by itself WHEN THE TURN ENDS, because
 * the turn it belonged to is over and the peek was about watching it. A group that was ALREADY
 * sealed when the reader opened it stays open until they close it — there is no later event to hang
 * a self-close on, and snapping shut under someone reading a finished receipt would be hostile
 * rather than tidy. Both halves are deliberate; the second is the honest limit of the first.
 *
 * R34's FAIL-OPEN IS THE ONE THING THAT OPENS ITSELF, and it fires only once the group is
 * terminal: expanding mid-turn moves what the reader is reading. Nothing is hidden when something
 * went wrong.
 */
import {
  ChevronDown,
  ChevronRight,
  Circle,
  Eye,
  Flag,
  Hammer,
  Package,
  Pencil,
  ShieldCheck,
  type LucideIcon,
} from 'lucide-react'
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FC,
  type PropsWithChildren,
} from 'react'
import { useAuiState, useScrollLock } from '@assistant-ui/react'

import type { ThreadGroupPart } from '../assistant-ui/thread'
import type { ActivityArgs, ActivityState } from './runtime/convertMessage'
import { ToolActivityLine, type ToolActivityState } from './ToolActivityLine'

/**
 * R35b — what a row says when the server sent no friendly label.
 *
 * The canvas's wording, verbatim (`ActivityAnatomy`, board 3). Never the tool's own name: the
 * server's classifier fails closed precisely so an unrecognised command cannot reach a citizen as
 * argv, and this is the client half of that guarantee.
 */
export const UNRECOGNISED_STEP = 'Working on your app'

/**
 * Which messages ended on an interrupted turn (R35c).
 *
 * A group cannot know this by itself — it is a fact about the TURN, carried by Plan B's durable
 * turn-terminal row — so the surface supplies it. Without it a count from a build somebody stopped
 * reads exactly like a count from one that finished, which is the specific misreading R35c names.
 */
export const InterruptedMessagesContext = createContext<ReadonlySet<string>>(new Set())

/**
 * R66's SECOND ANNOUNCEMENT — what a group amounted to, the moment it sealed.
 *
 * It is reported from here rather than derived at the surface because this is the only place the
 * count is already right: a diagnostic joins the group as a failed row but never reaches the
 * surface's `turnSteps`, so a surface-side count would say "3 steps" about a group rendering four.
 * Two places computing the same sentence is how they come to disagree.
 *
 * The default is a no-op, so a group rendered outside a provider (every unit test of this file)
 * behaves exactly as it did.
 */
export const GroupSealedContext = createContext<(summary: string) => void>(() => {})

/**
 * Read a tool-call part's args, or `undefined` for anything that is not one.
 *
 * A hand-written guard rather than an inline `filter(p => p.type === 'tool-call')`: the content
 * array is a union across every part kind, and a predicate that narrows only to `NonNullable`
 * leaves `args` off the type — so the inline version compiles as `any` at best and does not
 * compile at all under this project's settings.
 */
function toolCallArgs(
  part: { type: string; args?: unknown } | undefined,
): Partial<ActivityArgs> | undefined {
  if (!part || part.type !== 'tool-call') return undefined
  return (part.args ?? {}) as Partial<ActivityArgs>
}

/**
 * The converter's state, mapped onto the shared row atom's vocabulary. Exported because the row
 * draws the same mapping, and two copies of a state map drift the first time a state is added.
 * An absent state is a step that has not reported yet, which is 'started' — same as 'running'.
 */
export function rowState(state: ActivityState | undefined): ToolActivityState {
  if (state === 'ok') return 'ok'
  if (state === 'failed') return 'failed'
  return 'started'
}

interface GroupFacts {
  count: number
  failures: number
  running: boolean
  /** The label of the step happening NOW, for the live trigger. */
  currentLabel: string
}

function pluralSteps(n: number): string {
  return n === 1 ? '1 step' : `${n} steps`
}

/**
 * WHAT THE ROW SAYS, running or sealed — and since plan 002's U8 it is a COUNT either way.
 *
 * It used to name the running step here, which is what `ActivityAnatomy` panel 2 draws. The owner's
 * ruling of 2026-09-02 moves that sentence OUT of the row and onto one quiet line beneath the
 * collapsed group, so the row is the receipt and the line is the commentary. The count is what
 * belongs on a receipt.
 *
 * No headline, no elapsed timer, no "step 3 of 9" — the screen reads as an app being built, not an
 * agent being watched. The suffixes are sealed-only: a count of problems while the run is still
 * going describes something that may yet be recovered from.
 */
export function groupLabel(facts: GroupFacts, interrupted: boolean): string {
  if (facts.running) return pluralSteps(facts.count)
  if (interrupted) return `${pluralSteps(facts.count)} · stopped before it finished`
  if (facts.failures === 1) return `${pluralSteps(facts.count)} · one problem`
  if (facts.failures > 1) return `${pluralSteps(facts.count)} · ${facts.failures} problems`
  return pluralSteps(facts.count)
}

/**
 * ONE ICON PER KIND OF STEP, rather than one tick for everything — `ActivityAnatomy`'s own rule.
 *
 * DERIVED FROM THE LABEL, WHICH IS AN HONEST LIMIT WORTH STATING. The wire carries `{label, state}`
 * and no kind, so there is no field to switch on: a real `kind` would have to be added by the
 * server's step classifier, which is where the platform's own vocabulary already lives, and that
 * is a backend change this unit does not carry. The labels ARE that vocabulary though — a small
 * closed set the classifier emits — so matching on their verb is reading the same decision one
 * layer later rather than inventing a second one.
 *
 * THE FALLBACK IS THE POINT OF THE SHAPE. An unrecognised label gets the neutral dot, never a
 * guessed icon: a wrong icon is a claim about what the agent did, and this component's whole
 * discipline is that it never makes one.
 */
export function stepIconFor(label: string): LucideIcon {
  // `Still ` FRONTS ANY OTHER LABEL. The projection wraps a long-running step's own words rather
  // than replacing them ("Still setting up the tools your app needs"), so matching the raw string
  // would send every slow step — the ones a citizen stares at longest — to the neutral dot.
  const words = label.toLowerCase().replace(/^still\s+/, '')

  // THE VOCABULARY IS THE SERVER'S, and these are its actual words rather than a guess at them.
  // The first cut of this map matched `reading` / `adding` / `putting` / `creating` / `installing`
  // / `finishing`, and the projection emits NONE of those — while the words it does emit, `looked`
  // / `inspected` / `read` / `updating` / `getting` / `edited`, had no branch at all. A quarter of
  // the tiles in a real transcript therefore drew the featureless fallback circle, which is the one
  // thing `ActivityAnatomy` never draws: every tile on it names a kind of call.
  //
  // Read alongside `backend/src/services/messages/projection.py`, which is where these words are
  // written. A label added there without a branch here is not an error — it is the fallback below,
  // doing its job.
  if (
    words.startsWith('looking') ||
    words.startsWith('looked') ||
    words.startsWith('inspected') ||
    words.startsWith('reading') ||
    words.startsWith('read ') ||
    words.startsWith('checking')
  ) {
    return Eye
  }
  if (words.startsWith('building') || words.startsWith('working')) return Hammer
  if (words.startsWith('updating') || words.startsWith('edited') || words.startsWith('inserted')) return Pencil
  if (words.startsWith('setting up') || words.startsWith('getting') || words.startsWith('installing')) return Package
  if (words.startsWith('making sure') || words.startsWith('verifying')) return ShieldCheck
  if (words.startsWith('wrapping up') || words.startsWith('tidying') || words.startsWith('organized')) return Flag
  // THE FALLBACK IS STILL THE POINT OF THE SHAPE — `Used {tool_name}` reaches it, and should: the
  // projection could not name that call either, so neither may this.
  return Circle
}

const ActivityGroup: FC<PropsWithChildren<{ group: ThreadGroupPart }>> = ({ group, children }) => {
  const messageId = useAuiState((s) => s.message.id)
  const parts = useAuiState((s) => s.message.content)
  /**
   * IS THIS THE MESSAGE THE TURN IS STILL WRITING?
   *
   * The library's own message status, which is `running` only while the thread is running AND this
   * is the last message — so it is a fact about the TURN, which is what the peek below needs and
   * what nothing inside a group can supply. It is read here rather than plumbed as a prop because
   * the surface already owns it: it hands `isRunning` to the runtime, and the runtime derives this.
   */
  const streaming = useAuiState((s) => s.message.status?.type === 'running')
  const interruptedIds = useContext(InterruptedMessagesContext)
  const interrupted = messageId ? interruptedIds.has(messageId) : false

  const facts = useMemo<GroupFacts>(() => {
    const args = group.indices
      .map((i) => toolCallArgs(parts[i]))
      .filter((a): a is Partial<ActivityArgs> => a !== undefined)
    const running = args.filter((a) => (a.state ?? 'running') === 'running')
    return {
      count: args.length,
      failures: args.filter((a) => a.state === 'failed').length,
      running: running.length > 0,
      // The step happening NOW is the newest one still running; falling back to the newest step
      // at all keeps the label truthful during the instant between one settling and the next
      // starting.
      currentLabel:
        running[running.length - 1]?.label ||
        args[args.length - 1]?.label ||
        UNRECOGNISED_STEP,
    }
  }, [group.indices, parts])

  // R34, as a controlled prop with the reader's own toggle winning thereafter. `null` means "the
  // reader has not decided", which is what lets a failure open the group once WITHOUT overriding a
  // reader who has already closed it.
  const [readerOpen, setReaderOpen] = useState<boolean | null>(null)
  const failOpen = !facts.running && facts.failures > 0
  const open = readerOpen ?? failOpen

  /**
   * A GLANCE INSIDE A RUNNING GROUP IS TEMPORARY (owner ruling, 2026-09-02).
   *
   * Opening one while it runs is about watching it, so when the turn ends the peek is over and the
   * group returns to its resting state — collapsed. Cleared to `null` rather than to `false`, so
   * a group that also FAILED still opens itself: the reader has stopped deciding, which is exactly
   * what `null` means here.
   *
   * IT ARMS PER GROUP AND FIRES PER TURN, and the two halves are different facts on purpose.
   * `facts.running` is "some step in HERE is pending", which is what makes the glance a glance at
   * something live — so it is the right thing to arm on. It is the WRONG thing to fire on:
   * between one tool call returning and the next one starting the model thinks again, seconds at
   * a time with adaptive reasoning on, and every step emitted so far reads as settled. Firing on
   * that gap snapped the group shut under a reader who had just pressed it open, over and over,
   * for the whole build. `streaming` is the turn-level fact, and it stays true across the gap.
   *
   * ONLY FOR A GROUP THE READER OPENED WHILE IT WAS RUNNING. One that was already sealed when they
   * opened it has no later event to hang a self-close on, and snapping shut under someone reading
   * a finished receipt would be hostile rather than tidy. That is the honest limit of this rule
   * and it is deliberate.
   */
  const openedWhileRunning = useRef(false)
  if (facts.running && readerOpen === true) openedWhileRunning.current = true
  useEffect(() => {
    if (streaming || !openedWhileRunning.current) return
    openedWhileRunning.current = false
    setReaderOpen(null)
  }, [streaming])

  // Expanding must not throw the reader somewhere else. The library's own lock is what the
  // registry's component uses and it is exported, so it comes across without the port.
  //
  // IT RETURNS AN ACTIVATOR AND DOES NOTHING UNTIL IT IS CALLED — mounting the hook is not arming
  // it. Dropping the return value left the sentence above describing a lock that never engaged.
  const contentRef = useRef<HTMLDivElement>(null)
  const lockScroll = useScrollLock(contentRef, 200)

  const label = groupLabel(facts, interrupted)
  const Chevron = open ? ChevronDown : ChevronRight

  // ONCE, ON THE TRANSITION — not on mount.
  //
  // R66 announces what just happened, so the group has to have RUN here to have anything to
  // report. Firing on "not running and has steps" instead announced every historical group in the
  // transcript the moment a finished chat was opened: five past builds meant five summaries into
  // the live region, none of them about anything the reader had just done.
  //
  // `watchedItRun` is what makes it a transition; `sealed` keeps it to one announcement after that.
  const announceSealed = useContext(GroupSealedContext)
  const watchedItRun = useRef(false)
  const sealed = useRef(false)
  useEffect(() => {
    if (facts.running) {
      watchedItRun.current = true
      return
    }
    if (!watchedItRun.current || facts.count === 0 || sealed.current) return
    sealed.current = true
    announceSealed(label)
  }, [facts.running, facts.count, label, announceSealed])

  // THE FAILURE TINT, from `ActivityAnatomy` panel 4 — its own container colours rather than the
  // status pills', because this sits quietly in a transcript and still has to be unmistakable.
  // It follows the same predicate as the fail-open: terminal, and with something to report.
  const problem = failOpen

  return (
    // `my-3` IS THE BOARD'S SPACING between a paragraph and the group that follows it. It was
    // `my-2`, which read as the group belonging to the next sentence rather than to the one above.
    <div data-testid="activity-group" data-state={open ? 'open' : 'closed'} className="my-3">
      <div
        data-testid="activity-group-container"
        data-problem={problem || undefined}
        // A BORDERED CHIP, which is what `ActivityAnatomy` actually draws — see the docblock for
        // the earlier reading that said otherwise. `w-fit` so the container hugs its contents
        // when collapsed and is not a full-width bar across the transcript.
        className={`w-fit max-w-full overflow-hidden rounded-[10px] border ${
          problem ? 'border-problem-edge' : 'border-bial-border'
        }`}
      >
        <button
          type="button"
          onClick={() => {
            // BEFORE the state change, so the lock is in place for the height change it causes.
            lockScroll()
            setReaderOpen(!open)
          }}
          aria-expanded={open}
          data-testid="activity-group-trigger"
          className={`flex w-full items-center gap-2.5 py-1.5 pe-3 ps-2 text-left transition ${
            problem ? 'bg-problem-ground' : 'bg-canvas-group'
          } ${open ? `border-b ${problem ? 'border-problem-edge' : 'border-bial-border'}` : ''}`}
        >
          {/* ONE TILE PER CONTAINED STEP, arrival order, oldest on the left, growing IN PLACE.
              Overlapped with a white ring so a long run stays compact — the board's treatment, and
              what keeps the row's HEIGHT constant as icons accumulate so the transcript never
              jumps. */}
          <span className="flex flex-shrink-0 items-center" data-testid="activity-glyphs">
            {group.indices.map((partIndex, i) => {
              const args = toolCallArgs(parts[partIndex])
              const state = rowState(args?.state)
              const live = state === 'started' || state === 'pending'
              const StepIcon = stepIconFor(args?.label ?? '')
              return (
                <span
                  key={partIndex}
                  className={`flex h-[22px] w-[22px] items-center justify-center rounded-[7px] border ring-2 ring-white ${
                    live ? 'border-canvas-tileedge bg-canvas-tilelive' : 'border-bial-border bg-canvas-tile'
                  }`}
                  style={i === 0 ? undefined : { marginLeft: '-7px' }}
                >
                  {/* A FAILED OR RUNNING STEP KEEPS ITS STATE GLYPH. The kind icon says what the
                      agent was doing; a cross says it did not work and a spinner says it still is,
                      and either outranks the kind — conveyed by SHAPE and not by colour alone
                      (WCAG 1.4.1), which is the row atom's own rule. */}
                  {state === 'failed' || live ? (
                    <GlyphOnly state={state} />
                  ) : (
                    <StepIcon size={12} aria-hidden="true" className="text-neutral" />
                  )}
                </span>
              )
            })}
          </span>
          <span
            className={`min-w-0 truncate text-xs font-semibold ${problem ? 'text-problem-ink' : 'text-neutral'}`}
          >
            {label}
          </span>
          {/* FLUSH AGAINST THE CARD'S RIGHT EDGE. Closed, the card is `w-fit` and the chevron lands
              there anyway; OPEN, the card takes the width of its widest step row and the chevron
              was left stranded beside a short label — "6 steps" and a chevron together in the
              middle of a 320px header, with a dead gap after them. `ActivityAnatomy` panel 3 draws
              it at the edge, which is also where a disclosure control is looked for. */}
          <Chevron
            size={13}
            aria-hidden="true"
            className={`ms-auto flex-shrink-0 ${problem ? 'text-problem-ink' : 'text-neutral/70'}`}
          />
        </button>

        {open && (
          <div
            ref={contentRef}
            data-testid="activity-group-rows"
            className="flex flex-col gap-2 bg-white px-3 py-2.5"
          >
            {children}
          </div>
        )}
      </div>

      {/* ONE QUIET LINE, BENEATH THE COLLAPSED ROW, naming what is happening right now (owner
          ruling, 2026-09-02). The board puts this sentence INSIDE an open group; the owner does
          not want the working detail on screen, so the group stays shut and the sentence moves
          here. Running only: a sealed group's steps are in the receipt, one press away. */}
      {facts.running && (
        <p data-testid="activity-group-now" className="mt-1.5 ps-1 text-[11px] text-neutral">
          {facts.currentLabel}
        </p>
      )}
    </div>
  )
}

/** The trigger's glyph: the row atom's state icon with no label beside it. */
const GlyphOnly: FC<{ state: ToolActivityState }> = ({ state }) => (
  <ToolActivityLine label="" state={state} className="w-auto gap-0" />
)

export default ActivityGroup
