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
 * The plan's file list names a hand-port of `tool-group.aui.tsx`. It was fetched and read, and
 * porting it would have shipped nothing: its `defaultVariants` is `outline` (`rounded-lg border
 * py-3` — the bordered card this work exists to delete), and the variant we would pass, `ghost`, is
 * LITERALLY THE EMPTY STRING. Every visible property below — the overlapping state glyphs, the
 * count, the chevron, the row list — is authored here against the design canvas's `BuildChat` and
 * `ActivityAnatomy` boards, which draw the group with no border, no background and no card. So the
 * v4→v3 rewrite would have been applied to a component whose entire styling we discard: the same
 * "expensive way to ship nothing" the plan gives as the reason not to port `tool-fallback`.
 *
 * What the port would genuinely have brought is kept: `useScrollLock`, so expanding does not throw
 * the reader somewhere else, is imported from the library directly.
 *
 * ══ SEALED MEANS COLLAPSED ══
 *
 * Settled with the client on 2026-09-01: a sealed group collapses to a count, INCLUDING the last
 * group of a turn. Vercel's AI Elements auto-open completed tools and we deliberately do not — the
 * reading line stays prose, and the receipt is one press away. R34's fail-open is the only
 * exception, and it fires only once the group is terminal: expanding mid-turn moves what the reader
 * is reading.
 */
import { ChevronDown, ChevronRight } from 'lucide-react'
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
 * The sealed and live labels, in the canvas's register.
 *
 * A running group names what is happening now; a sealed one is a count. No headline, no elapsed
 * timer, no "step 3 of 9" — the screen reads as an app being built, not an agent being watched.
 */
export function groupLabel(facts: GroupFacts, interrupted: boolean): string {
  if (facts.running) return facts.currentLabel
  if (interrupted) return `${pluralSteps(facts.count)} · stopped before it finished`
  if (facts.failures === 1) return `${pluralSteps(facts.count)} · one problem`
  if (facts.failures > 1) return `${pluralSteps(facts.count)} · ${facts.failures} problems`
  return pluralSteps(facts.count)
}

const ActivityGroup: FC<PropsWithChildren<{ group: ThreadGroupPart }>> = ({ group, children }) => {
  const messageId = useAuiState((s) => s.message.id)
  const parts = useAuiState((s) => s.message.content)
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

  return (
    <div data-testid="activity-group" data-state={open ? 'open' : 'closed'} className="my-2">
      <button
        type="button"
        onClick={() => {
          // BEFORE the state change, so the lock is in place for the height change it causes.
          lockScroll()
          setReaderOpen(!open)
        }}
        aria-expanded={open}
        data-testid="activity-group-trigger"
        // NO border and NO background — see the docblock. This is a line of text with glyphs, not
        // a card, and a `border` class appearing here is a review comment.
        className="inline-flex max-w-full items-center gap-2 rounded-md py-0.5 text-left transition hover:opacity-80"
      >
        {/* R31's second clause: one state glyph per contained part, arrival order, oldest on the
            left, growing IN PLACE. Overlapped with a ring so a long run stays compact — the
            canvas's treatment. */}
        <span className="flex flex-shrink-0 items-center" data-testid="activity-glyphs">
          {group.indices.map((partIndex, i) => {
            const args = toolCallArgs(parts[partIndex])
            return (
              <span
                key={partIndex}
                className="flex h-[18px] w-[18px] items-center justify-center rounded-md bg-surface-muted ring-2 ring-white"
                style={i === 0 ? undefined : { marginLeft: '-6px' }}
              >
                <GlyphOnly state={rowState(args?.state)} />
              </span>
            )
          })}
        </span>
        <span className="min-w-0 truncate text-xs font-semibold text-neutral">{label}</span>
        <Chevron size={13} aria-hidden="true" className="flex-shrink-0 text-neutral/70" />
      </button>

      {open && (
        <div ref={contentRef} data-testid="activity-group-rows" className="mt-2 flex flex-col gap-2 ps-0.5">
          {children}
        </div>
      )}
    </div>
  )
}

/** The trigger's glyph: the row atom's state icon with no label beside it. */
const GlyphOnly: FC<{ state: ToolActivityState }> = ({ state }) => (
  <ToolActivityLine label="" state={state} className="w-auto gap-0" />
)

export default ActivityGroup
