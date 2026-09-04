/**
 * THE PLAN OFFER, AS A STRIP ON THE COMPOSER (R29, R29a, R45, R64, R51a).
 *
 * The plan agent calls an offer tool when it judges the plan finished. The browser renders the
 * PENDING call as a strip on the composer with two buttons; pressing either supplies the tool
 * result.
 *
 * ══ THE COMPOSER IS MARKED UNAVAILABLE, NOT DISABLED (D1, R45, R64) ══
 *
 * The canvas draws the message box greyed out and the origin document overrides it — a departure
 * recorded in the plan and named here at the point it happens. The box stays mounted, focusable
 * and typeable; only SEND carries `aria-disabled`, with the reason. A locked box behind a single
 * "Build this plan" is a dead end for anyone who wanted to change something, which is exactly why
 * there are two buttons.
 *
 * The reason is honest and worth stating in the copy: a tool call must be answered by a tool
 * result before the conversation can continue, so a typed message would leave the call open and
 * the next request would be rejected. "Keep planning" answers it too, and hands the box straight
 * back.
 *
 * ══ THE LABELS ══
 *
 * `Build this plan` and `Keep planning`. Client call, 2026-08-31 (R-15). NOT "Build it", NOT
 * "Keep refining" (which the client found confusing), and NOT the canvas's older
 * "Not yet — keep talking". Each names the mode the press puts you in, which is why they read as a
 * pair. The internal resolution values stay `build` and `refine` — wire values nobody reads, and
 * renaming them would be churn with a migration attached.
 *
 * The SAME two words appear in the model-facing copy, or the agent will tell a citizen to press a
 * button that does not exist: Plan B owns the tool docstring, Plan C the Plan segment and the two
 * reminders. This unit owns only what is drawn.
 *
 * ══ D2 — A SPENT STRIP STAYS, AND STAYS PRESSABLE ══
 *
 * The first press answers the tool call; that is unavoidable. The strip then renders in its spent
 * treatment but REMAINS A LIVE CONTROL. Pressing it again is an ordinary request that creates
 * another Build chat. Nothing is stored to record that a build happened. "Only one offer is live"
 * is about which one blocks the composer, not about which one is pressable.
 *
 * ══ D4 — IDEMPOTENCY WITHOUT STORAGE, AND ITS HONEST BOUNDARY ══
 *
 * The press names the chat it is creating: a UUIDv7 minted ONCE PER PRESS-SESSION, held in a ref,
 * sent as the new conversation's id. A double press and a retry carry the same id and collide on
 * the primary key, so the server returns the chat that already exists.
 *
 * A RELOAD IS OUT OF REACH, and the plan says so once, here: a ref dies with the page, and the
 * only thing that would survive it is a local record — which D2 forbids. So after a reload a fresh
 * press-session mints a new id and creates a second Build chat. That is R28's reload clause going
 * undelivered; it is asserted by a test rather than discovered in production, and closing it needs
 * storage, which is a decision nobody has taken.
 *
 * ══ D3 — THE BROWSER NEVER POSTS THE PLAN TEXT BACK ══
 *
 * The server reads it from the offering tool call's own message. The press sends the conversation
 * id, the tool call id and the minted new-chat id, and nothing else. A browser-supplied body would
 * let a stale second tab write stale requirements into a permanent first message.
 */
import { useCallback, useRef, useState, type FC } from 'react'
import { Loader2, Wand2 } from 'lucide-react'

import { uuidv7 } from '../../utils/conversationApi'
import { usePrefersReducedMotion } from './ToolActivityLine'

/** What the press sends. No plan text — see D3. */
export interface BuildHandoff {
  conversationId: string
  toolCallId: string
  /** Client-minted, once per press-session. */
  newChatId: string
}

export interface OfferStripProps {
  /** The pending call's id. A strip without one is not rendered — the card IS its tool-call id. */
  toolCallId: string | null
  conversationId: string | null
  /** True once this offer has been answered — it stays on screen and stays pressable (D2). */
  spent: boolean
  /** Answer the call with `build` and hand off. Resolves when the server has confirmed. */
  onBuild: (handoff: BuildHandoff) => Promise<void>
  /** Answer the call with `refine`, which hands the composer straight back. */
  onKeepPlanning: (toolCallId: string) => Promise<void>
  /** A failed handoff. The reader stays where they are, told what happened (R29). */
  onFailed: (message: string) => void
}

export const BUILD_LABEL = 'Build this plan'
export const KEEP_PLANNING_LABEL = 'Keep planning'
/** The canvas's wording for why sending waits. It sits INSIDE the box, where the placeholder would
 *  be — which is where `PlanReady` and `PlanRevised` both draw it. */
export const OFFER_GATE_NOTE = 'Choose one of the two above to carry on…'
/**
 * THE LINE UNDER THE BOX WHILE THE OFFER WAITS, verbatim from `PlanReady` and `PlanRevised`.
 *
 * The boards draw TWO sentences, not one. The gate note above sits in the box and says the box is
 * not where the answer goes; this one sits under it, centred and in the strip's own teal, and says
 * that neither answer is the wrong answer. Only the first of the two had been written, as a small
 * grey note below the box, which put the "you cannot type here" half where the reassurance goes
 * and left the reassurance out — and reassurance is the half someone just handed a decision
 * actually needs.
 */
export const OFFER_LOCKED_NOTE =
  'The box is locked until you pick one. Either answer is fine — one opens a Build chat, the other hands the conversation back to you.'
/**
 * WHAT THE STRIP SAYS BEFORE ANYBODY PRESSES ANYTHING, verbatim from `PlanReady`.
 *
 * The board's annotation is the requirement, and it is about register rather than decoration:
 * "this teal strip is not text the agent typed — it is a control the interface draws". Two bare
 * buttons at the right of the box read as chrome; nothing distinguished them from Send, and
 * nothing said what pressing one would DO. A citizen pressed "Build this plan" with no statement
 * anywhere on the screen that it opens a second chat and leaves this one alone — which is the one
 * thing they would want to know before pressing it.
 */
export const OFFER_HEADLINE = 'This looks ready to build.'
export const OFFER_EXPLANATION =
  'Opens a new Build chat with the message above as its first instruction. This chat stays exactly as it is.'

const OfferStrip: FC<OfferStripProps> = ({
  toolCallId,
  conversationId,
  spent,
  onBuild,
  onKeepPlanning,
  onFailed,
}) => {
  const [busy, setBusy] = useState<'build' | 'refine' | null>(null)
  const reducedMotion = usePrefersReducedMotion()

  // D4. Minted once per PRESS-SESSION and held in a ref, so a double press and a retry carry the
  // same id. `ProjectBuilder` mints through the same shared `uuidv7` but does it INLINE inside
  // `navigate()` with no ref — that site mints on every press by design, which is the opposite of
  // what this needs, so the lifetime here is new work rather than a pattern lifted from it.
  const mintedRef = useRef<string | null>(null)

  const handleBuild = useCallback(async () => {
    if (busy || !toolCallId || !conversationId) return
    mintedRef.current ??= uuidv7()
    setBusy('build')
    try {
      // NO optimistic navigation. It moves only on a confirmed response — deriving "it worked"
      // from anything less is how a spent strip with no build behind it happens.
      await onBuild({ conversationId, toolCallId, newChatId: mintedRef.current })
    } catch {
      onFailed('Could not start the build. Nothing has changed — try again.')
    } finally {
      setBusy(null)
    }
  }, [busy, toolCallId, conversationId, onBuild, onFailed])

  const handleKeepPlanning = useCallback(async () => {
    if (busy || !toolCallId) return
    setBusy('refine')
    try {
      await onKeepPlanning(toolCallId)
    } catch {
      onFailed('Could not answer that. Try again.')
    } finally {
      setBusy(null)
    }
  }, [busy, toolCallId, onKeepPlanning, onFailed])

  // The card IS its tool-call id. Without one there is nothing to answer, so the strip is not
  // rendered at all and the composer is left unblocked — never a dead button.
  if (!toolCallId) return null

  const spin = reducedMotion ? undefined : 'animate-spin'

  return (
    // A BAND ACROSS THE TOP OF THE BOX, edge to edge — the negative margins undo the composer's own
    // padding so the strip meets its border, which is what "fixed to the top of the message box"
    // means on the board. `ComposerBox` turns the box's own border teal while this is mounted, so
    // the two read as one card.
    <div
      data-testid="offer-strip"
      data-spent={spent ? 'true' : 'false'}
      className={`-mx-3 -mt-[11px] flex flex-wrap items-center gap-3 rounded-t-[13px] border-b border-canvas-offerrule bg-canvas-offer px-3.5 py-3 ${
        spent ? 'opacity-70' : ''
      }`}
    >
      <Wand2 size={18} aria-hidden className="flex-shrink-0 text-primary" />

      <div className="min-w-0 flex-1 basis-48">
        <p className="text-[12.5px] font-bold text-canvas-offerink">{OFFER_HEADLINE}</p>
        <p className="mt-0.5 text-[11px] leading-[1.55] text-neutral">{OFFER_EXPLANATION}</p>
      </div>

      <button
        type="button"
        onClick={handleKeepPlanning}
        aria-disabled={busy !== null}
        data-testid="offer-keep-planning"
        className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-[10px] border border-slate-300 bg-white px-3.5 py-2.5 text-[12.5px] font-semibold text-slate-600 transition hover:border-primary hover:text-primary"
      >
        {busy === 'refine' && <Loader2 size={13} className={spin} />}
        {KEEP_PLANNING_LABEL}
      </button>
      <button
        type="button"
        onClick={handleBuild}
        aria-disabled={busy !== null}
        data-testid="offer-build"
        className="inline-flex items-center gap-[7px] whitespace-nowrap rounded-[10px] bg-primary px-[15px] py-2.5 text-[12.5px] font-bold text-white transition hover:bg-primary-600"
      >
        {busy === 'build' ? <Loader2 size={13} className={spin} /> : <Wand2 size={13} aria-hidden />}
        {BUILD_LABEL}
      </button>
    </div>
  )
}

export default OfferStrip
