/**
 * THE publishing surface. One chip beside the project name, one server-computed field,
 * one sentence and at most one action (R37, R38, R39).
 *
 * IT REPLACES THREE CONTROLS THAT COULD DISAGREE — the Publish card, the Review & approval
 * card and the builder's toolbar button — and, more to the point, it replaces the thing
 * that made them disagree: each of them re-decided, in the browser, something the server
 * had already decided. That mirror has produced the same class of bug four times in this
 * one feature (`docs/solutions/ui-bugs/publish-dialog-scored-unmerged-answers-2026-08-21.md`),
 * most recently promising "this can publish automatically" beside a Publish button moments
 * before the server routed the app to an administrator. Nine labels were nine assertions
 * about server behaviour. They now have one source.
 *
 * SO THE ONE RULE HERE IS: `presentationFor` switches on `publishState` and on NOTHING
 * ELSE. No status, no `unpublishedAt`, no failure code, no approval lineage, no pin. The
 * other fields on the response are still read — but only to fill in a version row the
 * state has already asked for, never to decide which state it is.
 *
 * PUBLISHING BEHAVIOUR IS UNCHANGED. The seven-rule ladder, the classification
 * questionnaire and the two successes are exactly as they were; what changed is that the
 * interface stopped guessing which of them a press will produce. The button states the
 * CEILING of what it will attempt, and the server's answer states what happened —
 * publishing directly where the button said "Send update for review" reads as the better
 * outcome, not as a contradiction.
 *
 * ── THE CONTRACT PLAN F INHERITS ────────────────────────────────────────────────────
 * Stated here so the workspace header can re-parent this component without reading a line
 * of its internals:
 *   · It takes a project id and nothing else. It reads no router state, no rail mode and
 *     no chat.
 *   · It renders INLINE at its intrinsic size — no absolute positioning, no fixed width,
 *     no sticky behaviour — so a container may lay it out however it likes.
 *   · Its popover is PORTALLED to the document body, so a header with clipped overflow or
 *     its own stacking context cannot hide it. This is load-bearing today: the builder
 *     mount sits under four nested `overflow-hidden` ancestors.
 *   · It owns its own read and its own refresh lifetime, so a SECOND mount would be correct
 *     rather than merely tolerated. There are two mount SITES today — this project page and
 *     the builder's pane toolbar — but they are sibling routes under one Outlet, so only
 *     ever one of them is live.
 *   · Plan F's only obligation is to place it beside the project name and drop the mount
 *     that dies with the builder page. Nothing here changes when F collapses the screens.
 * ────────────────────────────────────────────────────────────────────────────────────
 *
 * WHERE THE COPY COMES FROM. Nine sentences are the design canvas's own, from its
 * "The status chip, nine states" board. Four states have no artboard and their copy is
 * carried across from the tree or written here, each marked at its arm. Three deliberate
 * departures from the canvas are marked the same way — the canvas is authoritative for
 * register and wording, never for a claim the endpoints cannot honestly serve.
 */
import { useCallback, useEffect, useId, useState } from 'react'
import { ChevronDown, ExternalLink } from 'lucide-react'

import { Popover, PopoverContent, PopoverTrigger } from './ui/popover'
import DataClassificationModal from './DataClassificationModal'
import { usePublishState } from '../hooks/usePublishState'
import { shortSha } from '../utils/shortSha'
import {
  ACTION_LABEL,
  formatStamp,
  lookFor,
  presentationFor,
  SECONDARY_ACTIONS,
  versionRowData,
} from '../utils/publishPresentation'
import type { DeployOutcome, PublishState } from '../utils/deployApi'

/* THE PRESENTATION LAYER MOVED TO `utils/publishPresentation.ts` (plan 002, U4). What lived
   here — the action labels, the state-to-words map with all of its copy reasoning, the version
   rows and the date format — is now shared with the rail's APP STATUS panel, which the boards
   make the fuller of the two surfaces. Neither renders the other; both read the same decision,
   so the panel and this chip cannot say different things about one app. The colour map and the
   provenance rows are new there and belong to the same decision. */

/**
 * The answer to a press, and there is exactly ONE treatment for it because there is only
 * ever one kind of thing here: a success. Both of the ladder's outcomes resolve — `202
 * started` and `200 routed_for_review` — and being sent for review is a success, not a
 * failure of the thing the citizen just asked for. Every REFUSAL throws instead, and the
 * questionnaire renders it beside its own button with the answers still on screen, which
 * is where a citizen who has to change something is already looking.
 *
 * So this region is never red and never carries an alert role. That is not a styling
 * choice to be tidied later — it is the property three retired tests pinned.
 */
const STARTED_ANSWER = 'Publishing now — this takes a few minutes.'

export interface PublishStatusChipProps {
  projectId: string
}

export default function PublishStatusChip({
  projectId,
}: PublishStatusChipProps): React.ReactElement {
  const {
    deployment,
    approval,
    loadError,
    unsaved,
    saving,
    onConfirm,
    saveAndPublish,
    dismissUnsaved,
    refresh,
    withdraw,
    withdrawing,
    withdrawError,
  } = usePublishState(projectId)

  const [open, setOpen] = useState(false)
  const [showModal, setShowModal] = useState(false)
  const [confirmingWithdraw, setConfirmingWithdraw] = useState(false)
  const [answer, setAnswer] = useState<string | null>(null)
  const headingId = useId()

  // An answer, or the unsaved-work question, must not land behind a closed popover: the
  // citizen pressed something and is owed what happened.
  useEffect(() => {
    if (answer !== null || unsaved !== null) setOpen(true)
  }, [answer, unsaved])

  // A fresh press starts from a clean slate, and closing the popover retires an answer
  // that has already been read.
  const onOpenChange = useCallback(
    (next: boolean): void => {
      setOpen(next)
      if (!next) {
        setAnswer(null)
        setConfirmingWithdraw(false)
      }
    },
    [],
  )

  const state: PublishState | null = deployment?.publishState ?? null
  const presentation = state === null ? null : presentationFor(state)
  // The pill's own colour pair, from the same one field. `lookFor` is exhaustive over the
  // union, so a state the server adds is a compile error rather than an unpainted chip.
  const look = state === null ? null : lookFor(state)
  const version =
    presentation === null ? null : versionRowData(presentation.version, deployment, approval)
  const busy = saving || withdrawing
  const busyReason = withdrawing ? 'Taking it back…' : saving ? 'Saving and publishing…' : undefined

  const speak = useCallback((outcome: DeployOutcome | null): void => {
    // `null` is the unsaved-work question, which speaks for itself below.
    if (outcome === null) return
    setAnswer(
      outcome.outcome === 'routed_for_review'
        ? // The server's OWN sentence. Both publish surfaces said the same words before
          // there was one of them, and a success rendered in anyone else's words is a
          // success the citizen has to translate.
          outcome.message
        : STARTED_ANSWER,
    )
  }, [])

  const pressAction = useCallback((): void => {
    if (busy) return
    setAnswer(null)
    if (presentation?.action === 'take_it_back') {
      setConfirmingWithdraw(true)
      return
    }
    // Every other action is the same request through the same questionnaire.
    setShowModal(true)
    setOpen(false)
  }, [busy, presentation])

  const doWithdraw = useCallback(async (): Promise<void> => {
    if (withdrawing) return
    await withdraw()
    setConfirmingWithdraw(false)
  }, [withdraw, withdrawing])

  const doSaveAndPublish = useCallback(async (): Promise<void> => {
    if (saving) return
    speak(await saveAndPublish())
  }, [saving, saveAndPublish, speak])

  /**
   * ONE permanently-mounted, initially-empty polite live region, rendered in every arm
   * below. A region injected together with its text is frequently not announced at all —
   * the portal already states this convention at `LivePreview.tsx` — which is why it is
   * mounted before it has anything to say and never unmounted.
   *
   * IT SPEAKS THE STATE, NOT ONLY THE ANSWERS. Both retired controls derived their live
   * region straight from the loaded state, so a state that arrived while the citizen was
   * looking at something else — a version approved overnight, a publish that routed from
   * another tab, an administrator switching the app off — announced itself. Filling this
   * only from `speak()` would have made it silent for any mount that did not itself press
   * something, which is most of them. The answer wins while there is one, because it is
   * the more specific thing to say about a press that just happened.
   *
   * It sits OUTSIDE the popover on purpose: an announcement is owed whether or not the
   * popover happens to be open.
   */
  let announcement = answer ?? ''
  if (answer === null && loadError !== null) announcement = 'Publish status: unavailable'
  else if (answer === null && presentation !== null) {
    announcement = `Publish status: ${presentation.label}`
  }

  const liveRegion = (
    <span data-testid="publish-announce" role="status" aria-live="polite" className="sr-only">
      {announcement}
    </span>
  )

  // ── The read itself failed ────────────────────────────────────────────────────────
  // One honest presentation for all of it: the narrower threw on a value it did not
  // recognise, the network failed, or the server 500'd. Never a blank space where the
  // publish affordance was — this is the only publishing surface the citizen has, and a
  // chip that renders nothing is indistinguishable from a broken page.
  //
  // A SERVER-SIDE STORAGE FAILURE IS NOT ONE OF THESE and must not be treated as one: the
  // server degrades that to `live_drift_unknown` and answers 200, so it arrives here as an
  // ordinary state with its own words and its own action.
  if (loadError !== null) {
    return (
      <>
        {liveRegion}
        <Popover open={open} onOpenChange={onOpenChange}>
          <PopoverTrigger asChild>
            <button
              type="button"
              data-testid="publish-chip"
              aria-label="Publish status: unavailable"
              className="inline-flex items-center gap-1 rounded-md border border-bial-border bg-surface-muted px-2 py-0.5 text-xs font-semibold text-neutral transition hover:bg-white"
            >
              Status unavailable
              <ChevronDown size={12} aria-hidden />
            </button>
          </PopoverTrigger>
          <PopoverContent data-testid="publish-popover" aria-labelledby={headingId}>
            <p id={headingId} className="text-xs leading-relaxed text-neutral">
              {loadError}
            </p>
            <button
              type="button"
              data-testid="publish-recheck"
              onClick={() => void refresh()}
              className="mt-3 w-full rounded-lg bg-primary px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-primary-600"
            >
              Check again
            </button>
          </PopoverContent>
        </Popover>
      </>
    )
  }

  // ── The first read has not answered yet ───────────────────────────────────────────
  // Holds the space beside the project name so nothing jumps, and claims no state. Not a
  // button: there is nothing yet to explain, and the live region has nothing to say.
  //
  // ONE CONDITION, NOT TWO. The words and the colour pair come out of the same `state === null`
  // test a line apart, so they are null together and never apart; the second operand is here to
  // say that to the compiler, not to guard a divergence that can happen. It was a separate
  // `if (look === null) return <>{liveRegion}</>` below this line — a branch that could never
  // run, reading as though the two could come apart.
  if (presentation === null || look === null) {
    return (
      <>
        {liveRegion}
        <span
          data-testid="publish-chip-pending"
          className="inline-flex items-center rounded-md border border-transparent bg-surface-muted px-2 py-0.5 text-xs font-semibold text-neutral"
        >
          Checking…
        </span>
      </>
    )
  }

  return (
    <>
      {liveRegion}

      <Popover open={open} onOpenChange={onOpenChange}>
        <PopoverTrigger asChild>
          <button
            type="button"
            data-testid="publish-chip"
            data-publish-state={state}
            // The state is IN the accessible name, so a screen reader user learns it
            // without opening anything — R39's "visible without opening the chip" is not
            // a sighted-only guarantee.
            aria-label={`Publish status: ${presentation.label}`}
            // A 999px PILL WITH ITS OWN COLOUR PAIR AND A LEADING DOT, per the board that is
            // devoted to exactly this. It was one neutral grey `rounded-md` chip for all
            // thirteen states: the word changed and nothing else did, so "Draft" looked
            // identical to "Changes requested" and to "Didn't start". The colour is the
            // signal a citizen reads before they read anything.
            className={`inline-flex items-center gap-[7px] rounded-full border border-[rgba(15,23,42,.07)] px-[11px] py-[5px] text-[11.5px] font-bold whitespace-nowrap transition hover:brightness-[.97] ${look.pill}`}
          >
            <span className={`h-1.5 w-1.5 flex-shrink-0 rounded-full ${look.dot}`} aria-hidden />
            {presentation.label}
            <ChevronDown size={11} className="opacity-55" aria-hidden />
          </button>
        </PopoverTrigger>

        <PopoverContent data-testid="publish-popover" aria-labelledby={headingId} className="w-80">
          <p id={headingId} className="text-xs leading-relaxed text-neutral">
            {presentation.sentence}
          </p>

          {version && (
            <div data-testid="publish-version" className="mt-3 border-t border-bial-border pt-2.5">
              <p className="text-[10px] font-bold uppercase tracking-wide text-neutral/70">
                {version.heading}
              </p>
              <p className="mt-0.5 text-xs font-semibold text-tertiary">
                {version.stamp ? formatStamp(version.stamp) : '—'}
                {version.sha && (
                  <code
                    data-testid="publish-version-sha"
                    className="ml-1.5 rounded bg-surface-muted px-1 py-0.5 text-[10px] font-normal text-neutral"
                  >
                    {shortSha(version.sha)}
                  </code>
                )}
              </p>
              {version.url && (
                <a
                  data-testid="publish-url"
                  href={version.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-1 inline-flex items-center gap-1 break-all text-xs font-semibold text-primary hover:underline"
                >
                  <ExternalLink size={12} className="flex-shrink-0" aria-hidden />
                  {version.url}
                </a>
              )}
              {version.note && (
                <p
                  data-testid="publish-rejection-note"
                  className="mt-2 border-l-2 border-bial-border pl-2 text-xs italic leading-relaxed text-neutral"
                >
                  {version.note}
                </p>
              )}
            </div>
          )}

          {/* The unsaved-work QUESTION, not a failure: the one refusal that has a second
              answer. It re-sends the answers already declared rather than reopening the
              questionnaire. */}
          {unsaved !== null && (
            <div data-testid="publish-unsaved" className="mt-3 border-t border-bial-border pt-2.5">
              <p className="text-xs leading-relaxed text-neutral">{unsaved}</p>
              <div className="mt-2 flex gap-2">
                <button
                  type="button"
                  data-testid="publish-save-and-publish"
                  onClick={() => void doSaveAndPublish()}
                  // R64/D1: marked unavailable, never hard-disabled. Disabling a control
                  // that has focus blurs it to `document.body` (KTD-2), which is how a
                  // keyboard user loses their place mid-flight. `doSaveAndPublish` is the
                  // enforcement; this is affordance only.
                  aria-disabled={saving}
                  title={saving ? 'Saving and publishing…' : undefined}
                  className={`flex-1 rounded-lg bg-primary px-3 py-1.5 text-xs font-semibold text-white transition ${
                    saving ? 'cursor-default opacity-40' : 'hover:bg-primary-600'
                  }`}
                >
                  Save and publish
                </button>
                <button
                  type="button"
                  data-testid="publish-unsaved-cancel"
                  onClick={dismissUnsaved}
                  className="rounded-lg border border-bial-border px-3 py-1.5 text-xs font-semibold text-neutral transition hover:bg-surface-muted"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {answer !== null && (
            <p
              data-testid="publish-answer"
              className="mt-3 border-t border-bial-border pt-2.5 text-xs leading-relaxed text-neutral"
            >
              {answer}
            </p>
          )}

          {withdrawError !== null && (
            <p
              data-testid="publish-withdraw-error"
              role="alert"
              className="mt-3 text-xs leading-relaxed text-danger"
            >
              {withdrawError}
            </p>
          )}

          {/* AT MOST ONE ACTION. A state with nothing to do renders NO button — not a
              disabled one. R64's "mark unavailable rather than switch off" governs a
              control that is temporarily away and will come back, which is a different
              thing from a state where nothing can be done. */}
          {presentation.action !== null && unsaved === null && !confirmingWithdraw && (
            <button
              type="button"
              data-testid="publish-action"
              onClick={pressAction}
              aria-disabled={busy}
              title={busyReason}
              // SECONDARY WHERE THE BOARD DRAWS IT SECONDARY — `StatusCardStates` fills every
              // action but state 3's with the primary teal. The set is shared with the rail panel
              // so the two surfaces cannot disagree about which action that is.
              className={`mt-3 w-full rounded-lg px-3 py-1.5 text-xs font-semibold transition ${
                SECONDARY_ACTIONS.has(presentation.action)
                  ? 'border border-bial-border bg-white text-tertiary'
                  : 'bg-primary text-white'
              } ${busy ? 'cursor-default opacity-40' : SECONDARY_ACTIONS.has(presentation.action) ? 'hover:border-primary hover:text-primary' : 'hover:bg-primary-600'}`}
            >
              {ACTION_LABEL[presentation.action]}
            </button>
          )}

          {/* Taking a submission back is destructive from the citizen's side — the version
              leaves the queue and an administrator stops looking at it — so it asks once. */}
          {confirmingWithdraw && (
            <div data-testid="publish-withdraw-confirm" className="mt-3">
              <p className="text-xs leading-relaxed text-neutral">
                Take this version back? An administrator will stop reviewing it, and you
                can send it again whenever you are ready.
              </p>
              <div className="mt-2 flex gap-2">
                <button
                  type="button"
                  data-testid="publish-withdraw-yes"
                  onClick={() => void doWithdraw()}
                  aria-disabled={withdrawing}
                  title={withdrawing ? 'Taking it back…' : undefined}
                  className={`flex-1 rounded-lg bg-primary px-3 py-1.5 text-xs font-semibold text-white transition ${
                    withdrawing ? 'cursor-default opacity-40' : 'hover:bg-primary-600'
                  }`}
                >
                  Take it back
                </button>
                <button
                  type="button"
                  data-testid="publish-withdraw-no"
                  onClick={() => setConfirmingWithdraw(false)}
                  className="rounded-lg border border-bial-border px-3 py-1.5 text-xs font-semibold text-neutral transition hover:bg-surface-muted"
                >
                  Keep it there
                </button>
              </div>
            </div>
          )}
        </PopoverContent>
      </Popover>

      {showModal && (
        <DataClassificationModal
          projectId={projectId}
          // A citizen who presses after a rejection reads WHY before anything else
          // happens — the note belongs in the flow they are actually in, not only on a
          // panel beside it that they may never open.
          rejectionNote={approval?.status === 'rejected' ? approval.rejectionNote : null}
          onConfirm={async (answers) => {
            // Refusals THROW and the modal renders them itself, beside the button, with
            // the answers still on screen. Only the two successes and the unsaved-work
            // question reach this line.
            speak(await onConfirm(answers))
            setShowModal(false)
          }}
          onCancel={() => setShowModal(false)}
        />
      )}
    </>
  )
}
