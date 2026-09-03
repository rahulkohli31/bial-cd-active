/**
 * THE CHAT'S COMPOSER (R40, R41, R42, R43, R45, R55, R57–R60, R64, R72; plan 002, U5).
 *
 * ═══ WHAT IS HERE AND WHAT IS NOT ═══
 *
 * The BOX — the border, the input, the attachment control, the send control, the chips, the
 * dropzone and the clear-only-on-acceptance rule — is `ComposerBox`. What is added HERE is the
 * character cap, the counter, the gate note, the draft that follows a conversation, and the
 * chat-only chrome: the offer strip, the stop control, the context warning.
 *
 * ══ THE RAIL MOUNTS THIS COMPONENT TOO, AND THAT IS THE CORRECTION ══
 *
 * This docblock used to say the two composers "differ in seven respects, so a single component
 * with a placeholder prop would have been a fiction", and the rail therefore mounted `ComposerBox`
 * directly. The fiction was the other way round. Sharing only the inner box meant the rail had no
 * cap, no counter and no draft — so a citizen could paste 45,000 characters there with Send still
 * lit (the server refuses at 64,000), and lose a half-written description by stepping to another
 * screen and back. That screen carries the LONGEST message anyone writes.
 *
 * The seven differences are all ABSENT PROPS. `offer`, `stop`, `contextWarning`, `footerNote` and
 * `gate` are optional and render nothing when omitted; `isRunning` is false where no turn can run;
 * `placeholder` and `frameClassName` are the two that genuinely differ. A component whose
 * chat-only chrome is opt-in serves both surfaces without pretending they are the same screen —
 * and, unlike two components, cannot drift apart again.
 *
 * ══ NOTHING HERE IS EVER `disabled` (R45, R64) ══
 *
 * Not the textarea, not attach, not Send — in any state, including while a turn runs, while an
 * offer is pending, and when today's budget is spent. `disabled` on the currently-focused element
 * blurs it to `document.body`, which is the mechanism behind "it blurs mid-sentence and focus never
 * comes back". This codebase records that twice already and it is not a style preference. The
 * boards say the same thing in words: the composer keeps accepting typing while the agent answers.
 *
 * ══ THE DRAFT IS CONSUMED, NOT OWNED (R-7) ══
 *
 * `utils/composerDraft.ts` is the one store, and this is its ONE WRITER. `sessionStorage`, per
 * conversation, last-writer-wins across tabs, cleared only on a SUCCESSFUL send, throw-wrapped
 * because `sessionStorage` genuinely throws in Safari private mode rather than degrading.
 *
 * IT IS WRITTEN THROUGH THE RUNTIME NOW, not through local state, because the library owns the
 * composer's text. Hydration sets it; every keystroke mirrors it out. One writer either way.
 *
 * ══ ISSUE #154's FOUR DEFECTS ARE PROPERTIES, NOT PATCHES (R57–R60) ══
 *
 * R58 — THE DRAFT OVERWRITE. Nothing is cleared optimistically, so a failed send leaves the text
 *   and the files exactly where they were and there is no restore path to race with. That rule
 *   lives in `ComposerBox`, against the library's own `composer.send()`, which empties the text
 *   before it awaits anything.
 * R59 — DROPPED IN-FLIGHT READS. Because nothing clears until the server confirms, a read that
 *   lands late lands on the send it belongs to.
 * R57 — THE CAP BYPASS. The adapter validates against what is ALREADY staged, read live off the
 *   runtime — see `stagedAttachments.tsx` for why that is a ref rather than a closure.
 * R60 — CROSS-CHAT LEAKAGE is guarded by the send path stamping the conversation at press time
 *   and the surface comparing it on completion.
 */
import { useCallback, useEffect, useMemo, useRef, type FC, type ReactNode } from 'react'
import { useAui, useAuiState } from '@assistant-ui/react'

import { capState, MAX_COMPOSER_CHARS } from '../../utils/composerCap'
import { clearDraft, readDraft, writeDraft } from '../../utils/composerDraft'
import ComposerBox, { type ComposerSubmission } from './ComposerBox'
import OfferStrip, { OFFER_GATE_NOTE, type OfferStripProps } from './OfferStrip'
import StopTurnControl, { type StopTurnControlProps } from './StopTurnControl'

export type { ComposerSubmission } from './ComposerBox'

export interface ComposerProps {
  conversationId: string | null
  /** Placeholder — the only copy that differs between the two kinds, and it is a hint, not a mode. */
  placeholder?: string
  /**
   * Perform the send. Resolves on server confirmation; REJECTS on failure, and the composer keeps
   * everything it was holding.
   */
  onSubmit: (submission: ComposerSubmission) => Promise<void>
  /** True while a turn is in flight — Send waits, typing does not. */
  isRunning: boolean
  /** Any other reason Send must wait, with the sentence that explains it. */
  gate?: { blocked: boolean; reason: string } | undefined
  /** R55 — the relocated stop, given its permanent home on this chrome. */
  stop?: Omit<StopTurnControlProps, 'onStopFailed'> | undefined
  /** R29 — the pending plan offer, rendered on the composer rather than in the transcript. */
  offer?: Omit<OfferStripProps, 'onFailed'> | undefined
  /**
   * The "this chat is getting long" line, or null/absent when it is not.
   *
   * A SENTENCE, NOT A NUMBER AND NOT A STATE. The surface owns the transcript and therefore
   * owns the estimate (`utils/contextLimits.ts`); the composer's job is to show the line where
   * a citizen will read it.
   *
   * IT IS NOT A GATE. Send stays available past the soft threshold; the hard boundary is the
   * server's refusal, which arrives as an ordinary turn error.
   */
  contextWarning?: string | null | undefined
  /**
   * A standing caption under the box — the plan chat's line about its app, and nothing transient.
   *
   * A SLOT RATHER THAN A FLAG, because the sentence and the states it may speak for belong to the
   * surface that knows the chat's kind; the composer only knows where the board draws it.
   */
  footerNote?: ReactNode
  /** Urgent sentences go to the assertive slot the surface owns. */
  onUrgent: (message: string) => void
  /**
   * The frame around the box. Defaults to the chat surface's, which sits the composer on the
   * transcript's own ground with its own gutter.
   *
   * It is a prop because the RAIL mounts this same composer inside a section that already owns
   * its padding and ground — and the whole point of the rail using `Composer` rather than
   * `ComposerBox` is that the cap, the counter and the draft come with it. Letting the frame
   * differ is what makes one composer serve both surfaces instead of two composers drifting
   * apart, which is what happened last time.
   */
  frameClassName?: string
}

const Composer: FC<ComposerProps> = ({
  conversationId,
  placeholder = 'Ask for another change…',
  onSubmit,
  isRunning,
  gate,
  stop,
  offer,
  contextWarning,
  footerNote,
  onUrgent,
  frameClassName = 'flex flex-col gap-1.5 bg-bial-surface px-3 py-2.5',
}) => {
  const aui = useAui()
  const text = useAuiState((s) => s.composer.text)

  // Hydrate the draft for whichever conversation is mounted, and re-hydrate on a switch. The store
  // is keyed per conversation, so this is what makes a draft follow its own chat rather than
  // leaking into a sibling.
  //
  // THE STAGED FILES GO WITH IT, and they are dropped rather than restored because — unlike the
  // draft — they live only in memory as decoded bytes, so there is nothing to hydrate them back
  // from. Leaving them was the worse of the two: this composer is NOT remounted per chat (flat
  // routing keeps one instance), so a file picked in one chat stayed staged in the next, counted
  // against that chat's budgets, and would have been sent into a conversation the citizen never
  // attached it to.
  /** The conversation the mirror below is currently mirroring. See that effect for why. */
  const mirroring = useRef<string | null>(null)

  useEffect(() => {
    aui.composer.setText(readDraft(conversationId))
    aui.composer.clearAttachments()
  }, [aui, conversationId])

  // THE DRAFT MIRROR. The library owns the text, so this watches it rather than intercepting a
  // change event — which also means a paste, a drag-in and the library's own writes are all
  // covered by one rule instead of three.
  //
  // NO TRUNCATION, EVER, AT ANY LENGTH. The text is stored exactly as typed or pasted; R42's whole
  // point is that a citizen whose paste was silently cut believes it all went in.
  useEffect(() => {
    const id = conversationId ?? null
    // THE FIRST COMMIT FOR A CONVERSATION WRITES NOTHING, and that one skip is the whole guard.
    //
    // Hydration and this run on the SAME commit, and on it `text` is still the OUTGOING box:
    // `setText` is synchronous, but `text` comes from `useAuiState` and only catches up on the
    // next render. Mirroring that value would write the previous chat's box — or, on a fresh
    // mount, an empty one — over this conversation's key, and `writeDraft(id, '')` REMOVES the
    // key rather than storing a blank. So every draft died on the next mount, which is a reload:
    // the first of the three losses `composerDraft.ts` exists to prevent. Nothing reported it,
    // because an empty box where a draft should be looks exactly like never having had one.
    //
    // Skipping by IDENTITY rather than by timing is what makes this deterministic — no microtask
    // to lose a race with, and the very next render (the one carrying the hydrated text) mirrors
    // normally.
    if (mirroring.current !== id) {
      mirroring.current = id
      return
    }
    writeDraft(conversationId, text)
  }, [conversationId, text])

  const cap = useMemo(() => capState(text), [text])
  const offerPending = Boolean(offer && !offer.spent && offer.toolCallId)

  /**
   * Why Send is unavailable — one sentence, always stated, with a distinct reason per cause.
   * Plain language: a citizen reading this asked for an app and is watching it get made.
   */
  const unavailableReason = useMemo<string | null>(() => {
    // THE ORDER IS BY IMMEDIACY, and the offer is LAST on purpose. All four block Send, but the
    // first three describe something happening right now — the citizen's own text is too long, a
    // reply is arriving, their app is being built — while a pending offer describes a question
    // still waiting. The offer also stays pending for the whole of the round trip its own Build
    // press starts, so putting it first told a citizen to "choose one of the two above" while the
    // build they had just chosen was starting.
    if (cap.over) return cap.message
    if (isRunning) return 'Replying — keep typing if you like; send unlocks when it is done.'
    if (gate?.blocked) return gate.reason
    if (offerPending) return OFFER_GATE_NOTE
    return null
  }, [offerPending, cap.over, cap.message, isRunning, gate])

  const handleSubmit = useCallback(
    async (submission: ComposerSubmission) => {
      await onSubmit(submission)
      // The draft's own clear. The BOX clears the runtime's text and attachments on the same
      // acceptance; this clears the copy in `sessionStorage`, which the box knows nothing about.
      clearDraft(submission.conversationId)
    },
    [onSubmit],
  )

  return (
    // NO RULE ABOVE THE BOX. Every board that draws a composer — BuildChat, PlanChat, PlainAnswer,
    // NewBuildChat, NewPlanChat and the rest — runs the transcript's white straight down into the
    // composer's own bordered box, with nothing between them. The full-width hairline read as a
    // second edge stacked on the box's, and on a plan chat it cut the one centred column in two.
    <div className={frameClassName}>
      {/* R55 — stop's permanent home, ABOVE the box rather than inside it: it acts on the turn,
          not on the message being composed, and a control inside the box would read as part of
          sending one. */}
      {stop && (
        <div className="flex justify-end">
          <StopTurnControl {...stop} onStopFailed={onUrgent} />
        </div>
      )}

      <ComposerBox
        conversationId={conversationId}
        placeholder={placeholder}
        onSubmit={handleSubmit}
        unavailableReason={unavailableReason}
        onUrgent={onUrgent}
        header={
          offer ? (
            /* R29 — INSIDE the box, fixed to its top, which is what the board draws: "this teal
               strip is not text the agent typed — it is a control the interface draws". */
            <OfferStrip {...offer} onFailed={onUrgent} />
          ) : undefined
        }
        footer={
          <>
            {unavailableReason && (
              <p data-testid="composer-gate-note" role="status" className="text-xs text-neutral">
                {unavailableReason}
              </p>
            )}

            {/* The context guardrail's SOFT half: advisory, non-blocking, and deliberately not
                `disabled` anything. Send still works past this line; what stops a turn is the
                server, and it says so itself. `role="status"` so it is announced once when it
                appears rather than interrupting. */}
            {contextWarning && (
              <p
                role="status"
                data-testid="composer-context-warning"
                className="text-xs leading-relaxed text-neutral"
              >
                {contextWarning}
              </p>
            )}

            {/* THE STANDING NOTE, LAST AND BELOW THE BOX (plan 002, U6). The plan chat's line
                about the app is the board's use of this slot: a caption under the composer, not a
                banner above it. It comes after the transient notes because those answer the press
                the citizen just made, and this one has been true the whole time. */}
            {footerNote}

            {/* R43 — silent until it is useful, and then exact. See `composerCap.ts` for why the
                number is code points rather than String.length. */}
            {cap.showCounter && (
              <p
                data-testid="composer-counter"
                className={`self-end text-xs tabular-nums ${cap.over ? 'font-semibold text-danger' : 'text-neutral'}`}
              >
                {cap.count.toLocaleString()} / {MAX_COMPOSER_CHARS.toLocaleString()}
              </p>
            )}
          </>
        }
      />
    </div>
  )
}

export default Composer
