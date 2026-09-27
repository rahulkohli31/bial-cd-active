/**
 * THE publishing surface. One chip beside the project name, one server-computed field, one
 * sentence and at most one action.
 *
 * WHY THIS EXISTS. It replaces three controls that could disagree — the Publish card, the
 * Review & approval card and the builder toolbar button — because each re-decided in the
 * browser something the server had already decided. That mirror produced the same class of
 * bug four times, most recently promising "this can publish automatically" beside a Publish
 * button moments before the server routed the app to an administrator. Nine labels were nine
 * assertions about server behaviour; they now have one source (`presentationFor`, switching
 * on `publishState` — see its call site below).
 *
 * THE CONTRACT A FUTURE WORKSPACE HEADER INHERITS, so it can re-parent this component
 * without reading its internals: takes a project id only (no router/rail/chat state);
 * renders INLINE at intrinsic size (no absolute/fixed/sticky); its popover is PORTALLED to
 * `document.body` — load-bearing today, the builder mount sits under four nested
 * `overflow-hidden` ancestors; it owns its own read/refresh lifetime, so a second mount is
 * correct, not merely tolerated (today's two mounts are sibling routes under one Outlet, so
 * only ever one is live). The header's only job is to place it and drop the builder mount.
 *
 * Nine copy sentences are the design canvas's own ("The status chip, nine states"); four
 * states with no artboard, and three deliberate departures from it, are marked at their arm —
 * the canvas governs register and wording, never a claim the endpoints cannot honestly serve.
 */
import { useCallback, useEffect, useId, useState } from 'react'
import { ChevronDown, ExternalLink } from 'lucide-react'

import { Popover, PopoverContent, PopoverTrigger } from './ui/popover'
import PublishDialog from './PublishDialog'
import { usePublishState } from '../hooks/usePublishState'
import { shortSha } from '../utils/shortSha'
import {
  ACTION_LABEL,
  answerFor,
  busyLabel,
  formatStamp,
  lookFor,
  presentationFor,
  SECONDARY_ACTIONS,
  versionRowData,
} from '../utils/publishPresentation'
import type { DeployOutcome, PublishState } from '../utils/deployApi'

/* THE PRESENTATION LAYER MOVED TO `utils/publishPresentation.ts`. What lived
   here — the action labels, the state-to-words map with all of its copy reasoning, the version
   rows and the date format — is now shared with the rail's APP STATUS panel, which the boards
   make the fuller of the two surfaces. Neither renders the other; both read the same decision,
   so the panel and this chip cannot say different things about one app. The colour map and the
   provenance rows are new there and belong to the same decision. */

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
    publish,
    publishPhase,
    publishError,
    onConfirm,
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

  // An answer, or a press that failed, must not land behind a closed popover: the citizen
  // pressed something and is owed what happened.
  useEffect(() => {
    if (answer !== null || publishError !== null) setOpen(true)
  }, [answer, publishError])

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
  // THE ONE RULE: `presentationFor` switches on `publishState` — no status, no
  // `unpublishedAt`, no failure code, no approval lineage, no pin. `approvedRetryCommit` is
  // the server's own word for what the button publishes. Other response fields are still
  // read, but only to fill a version row the state already asked for.
  const presentation =
    state === null ? null : presentationFor(state, deployment?.approvedRetryCommit ?? null)
  // The pill's own colour pair, from the same one field. `lookFor` is exhaustive over the
  // union, so a state the server adds is a compile error rather than an unpainted chip.
  const look = state === null ? null : lookFor(state)
  const version =
    presentation === null ? null : versionRowData(presentation.version, deployment, approval)
  const busy = publishPhase !== null || withdrawing
  const busyReason = busyLabel(withdrawing, publishPhase) ?? undefined

  /**
   * The answer to a press: exactly ONE treatment, because there is only one kind of thing
   * here — a success (both ladder outcomes, `202 started` and `200 routed_for_review`,
   * resolve; review-routing is a success, not a failure of what the citizen asked for). Every
   * REFUSAL throws instead and the publish dialog renders it beside its own button. So this
   * region is never red and never carries an alert role.
   */
  const speak = useCallback((outcome: DeployOutcome): void => {
    setAnswer(answerFor(outcome))
  }, [])

  const pressAction = useCallback(async (): Promise<void> => {
    if (busy) return
    setAnswer(null)
    if (presentation?.action === 'take_it_back') {
      setConfirmingWithdraw(true)
      return
    }
    // Every other action is `publish`: the approved copy is sent and answered here; anything
    // else is saved and reviewed in the publish dialog.
    const next = await publish()
    if (next === null) return
    if (next === 'review') {
      setShowModal(true)
      setOpen(false)
      return
    }
    speak(next)
  }, [busy, presentation, publish, speak])

  const doWithdraw = useCallback(async (): Promise<void> => {
    if (withdrawing) return
    await withdraw()
    setConfirmingWithdraw(false)
  }, [withdraw, withdrawing])

  /**
   * ONE permanently-mounted, initially-empty polite live region (see `LivePreview.tsx` for
   * why: text injected together with the mount is often not announced). Speaks the STATE,
   * not only answers — a state arriving while the citizen looked elsewhere (approved
   * overnight, routed from another tab, switched off by an admin) still announces itself;
   * `speak()` alone would leave any mount that never itself pressed something silent. The
   * answer wins while there is one (more specific). Sits OUTSIDE the popover: owed either way.
   */
  let announcement = answer ?? ''
  if (answer === null && loadError !== null) announcement = 'Publish status: unavailable'
  else if (answer === null && presentation !== null) {
    announcement = `Publish status: ${presentation.label}`
  }
  // THE WAIT ITSELF SPEAKS, and it takes precedence while it is running. `busyReason`
  // existed and was rendered ONLY as a `title` attribute — which is neither visible text nor an
  // exposed busy state, and is unreachable to a keyboard or a touch screen. So the one thing
  // this component said out loud was the publish OUTCOME: a citizen whose press saved their
  // work first heard nothing at all until it finished.
  //
  // ENTERING AND LEAVING BOTH ANNOUNCE, which is what the region already gives for free: this
  // string replaces the outcome while busy, and the outcome replaces it when the wait ends.
  if (busyReason !== undefined) announcement = busyReason

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
              // The same 44px touch floor every other pressable occupant of the workspace toolbar
              // carries below the stacking threshold — this chip is a press, not a label, and in
              // this branch it is the only way to reach "Check again".
              className="inline-flex items-center gap-1 rounded-md border border-bial-border bg-surface-muted px-2 py-0.5 text-xs font-semibold text-neutral transition hover:bg-white narrow:min-h-[44px]"
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
            // without opening anything — "visible without opening the chip" is not
            // a sighted-only guarantee.
            aria-label={`Publish status: ${presentation.label}`}
            // A 999px PILL WITH ITS OWN COLOUR PAIR AND A LEADING DOT, per the board that is
            // devoted to exactly this. It was one neutral grey `rounded-md` chip for all
            // thirteen states: the word changed and nothing else did, so "Draft" looked
            // identical to "Changes requested" and to "Didn't start". The colour is the
            // signal a citizen reads before they read anything.
            // ~26px tall, and wider than 44px on every one of the thirteen state words — so the
            // touch floor below the stacking threshold is a HEIGHT only. `min-h` rather than
            // padding, so the 999px pill, its dot and its chevron keep the exact proportions the
            // board draws at every width above it.
            className={`inline-flex items-center gap-[7px] rounded-full border border-[rgba(15,23,42,.07)] px-[11px] py-[5px] text-[11.5px] font-bold whitespace-nowrap transition hover:brightness-[.97] narrow:min-h-[44px] ${look.pill}`}
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

          {answer !== null && (
            <p
              data-testid="publish-answer"
              className="mt-3 border-t border-bial-border pt-2.5 text-xs leading-relaxed text-neutral"
            >
              {answer}
            </p>
          )}

          {publishError !== null && (
            <p
              data-testid="publish-error"
              role="alert"
              className="mt-3 text-xs leading-relaxed text-danger"
            >
              {publishError}
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
              disabled one. "Mark unavailable rather than switch off" governs a
              control that is temporarily away and will come back, which is a different
              thing from a state where nothing can be done. */}
          {presentation.action !== null && !confirmingWithdraw && (
            <button
              type="button"
              data-testid="publish-action"
              onClick={() => void pressAction()}
              aria-disabled={busy}
              // `aria-busy` is the PROPERTY that says a control is working — `aria-disabled`
              // only says it will not respond, which is also true of a state with nothing to
              // do. The label below is the visible half of the same fact.
              aria-busy={busy}
              // SECONDARY WHERE THE BOARD DRAWS IT SECONDARY — `StatusCardStates` fills every
              // action but state 3's with the primary teal. The set is shared with the rail panel
              // so the two surfaces cannot disagree about which action that is.
              className={`mt-3 w-full rounded-lg px-3 py-1.5 text-xs font-semibold transition ${
                SECONDARY_ACTIONS.has(presentation.action)
                  ? 'border border-bial-border bg-white text-tertiary'
                  : 'bg-primary text-white'
              } ${busy ? 'cursor-default opacity-40' : SECONDARY_ACTIONS.has(presentation.action) ? 'hover:border-primary hover:text-primary' : 'hover:bg-primary-600'}`}
            >
              {/* THE WAIT IS READABLE, not a tooltip: a control that says its own label while it
                  works looks pressable, reads pressable, and is doing the thing. */}
              {busyReason ?? ACTION_LABEL[presentation.action]}
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
        <PublishDialog
          projectId={projectId}
          deployment={deployment}
          // A citizen who presses after a rejection reads WHY before anything else
          // happens — the note belongs in the flow they are actually in, not only on a
          // panel beside it that they may never open.
          rejectionNote={approval?.status === 'rejected' ? approval.rejectionNote : null}
          onConfirm={async (commitSha, send) => {
            // Refusals THROW and the dialog renders them itself, beside the button, with
            // the answers still on screen. Only the two successes reach this line.
            speak(await onConfirm(commitSha, send))
            setShowModal(false)
          }}
          onCancel={() => setShowModal(false)}
        />
      )}
    </>
  )
}
